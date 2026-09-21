# 01 — Embeddings and representation

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (planned — the retrieval→generation
> pipeline as a data system), [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
> (the single most on-target existing doc in the repo for this whole folder — read it once, early),
> [`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md)
> (graph construction, `M`/`efConstruction`/`efSearch` — you need to know what an index *does*
> with a vector before you decide what vector to give it), [`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md)
> (noise floors, bootstrap confidence intervals — every recall number in this chapter's labs
> needs one).
>
> **Feeds into:** [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md)
> (chunk boundaries interact with embedding context limits and late chunking),
> [`03-indexing-and-vector-stores.md`](03-indexing-and-vector-stores.md) (quantized vectors are
> what actually gets indexed), [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md)
> (rescoring is the second stage of the quantization trick in §7), [`08-evaluation-methodology.md`](08-evaluation-methodology.md)
> (recall@k, the metric this whole chapter keeps deferring to), [`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md)
> (the cost formulas in §13 get their own full treatment there).
>
> **THESIS:** the embedding model *defines what "similar" means* for your system. It is not a
> hyperparameter you tune after the fact — it is a schema decision, and like every schema
> decision its migration cost is proportional to corpus size. Pick wrong on day one and you pay
> for it on day four hundred, in a full re-embed of everything you've ingested since. Every
> downstream property of your retrieval system — index type, storage cost, achievable recall,
> what queries even *can* succeed — is downstream of this one choice. Treat it with the gravity
> of a database schema, not the gravity of a config flag.

---

## Contents

1. [Thesis, restated as an engineering claim](#1-thesis-restated-as-an-engineering-claim)
2. [What an embedding actually is](#2-what-an-embedding-actually-is)
3. [Symmetric vs asymmetric embedding — the most-skipped detail](#3-symmetric-vs-asymmetric-embedding--the-most-skipped-detail)
4. [The 2026 model landscape](#4-the-2026-model-landscape)
5. [Why leaderboards mislead, and what to do instead](#5-why-leaderboards-mislead-and-what-to-do-instead)
6. [Dimensionality and Matryoshka Representation Learning](#6-dimensionality-and-matryoshka-representation-learning)
7. [Quantization](#7-quantization)
8. [Context length, truncation, and what the model actually sees](#8-context-length-truncation-and-what-the-model-actually-sees)
9. [The representation limit: a chunk that means nothing alone](#9-the-representation-limit-a-chunk-that-means-nothing-alone)
10. [Domain adaptation: when to fine-tune and when not to](#10-domain-adaptation-when-to-fine-tune-and-when-not-to)
11. [Multilingual and multimodal](#11-multilingual-and-multimodal)
12. [Drift, versioning, and migration](#12-drift-versioning-and-migration)
13. [Cost model for the representation layer](#13-cost-model-for-the-representation-layer)
14. [Anti-patterns](#14-anti-patterns)
15. [Mental models — the compressed set](#15-mental-models--the-compressed-set)
16. [Lab exercises](#16-lab-exercises)
17. [Interview questions and system design prompts](#17-interview-questions-and-system-design-prompts)

---

## 1. Thesis, restated as an engineering claim

Every retrieval system is built on top of a similarity judgment, and the embedding model is the
thing that makes that judgment. Change the model and you change what counts as "close." Two
chunks that were neighbors under `text-embedding-3-small` are not necessarily neighbors under
`voyage-4-lite` — the geometry is different, full stop, because the two models were trained on
different data with different contrastive objectives and different negative-sampling strategies.
There is no shared coordinate system between them. A vector from one model plotted next to a
vector from another is not "slightly off," it is meaningless — like comparing a UTM coordinate to
a lat/long without a conversion.

This has a consequence people routinely underestimate: **you cannot incrementally migrate an
embedding model.** You can incrementally migrate almost anything else in a data system — add a
column, backfill it in the background, cut over reads once it's populated. You cannot do that
with embeddings, because there is no meaningful "in-between" state where half your index is old
vectors and half is new — a similarity search across that mixed index is comparing apples to a
number that happens to also be a fruit. The practical unit of migration is the *entire corpus*.
Re-embedding is O(corpus size) in tokens and dollars, and it has to complete (or run in shadow,
see §12) before you can trust the new index at all.

So the embedding model decision behaves like a schema decision in every way that matters:

- It is expensive to change (§12, §13 give the cost formula).
- It constrains everything built on top of it (index type in `03`, retrieval strategy in `04`).
- Getting it wrong doesn't throw an error. It just quietly caps your recall ceiling, and you find
  out from users, not from a stack trace.

Everything else in this chapter — dimensionality, quantization, fine-tuning, versioning — is in
service of that one framing. Read the rest of this chapter as "how to make a schema decision you
can defend," not "how to pick a vector database plugin."

---

## 2. What an embedding actually is

An embedding is a point in `ℝ^d` produced by a function `f: text → ℝ^d` that was trained so that
semantically related inputs land close together and unrelated inputs land far apart, under some
distance metric. That's the whole definition. Everything interesting is in the word "trained."

### 2.1 The training objective is the geometry

Embedding models are overwhelmingly trained with **contrastive learning**: for an anchor (a query,
say), the loss pulls a known positive (a relevant document) toward it in vector space and pushes
known negatives away. The canonical loss is InfoNCE-style — something like:

```
L = -log( exp(sim(a, p) / τ) / Σᵢ exp(sim(a, nᵢ) / τ) )
```

where `a` is the anchor embedding, `p` the positive, `nᵢ` the negatives in the batch (or mined
separately — see §10), `sim` a similarity function (usually cosine or dot product), and `τ` a
temperature that controls how sharply the loss penalizes near-miss negatives.

The single most important thing to internalize from this formula: **the geometry that comes out
is entirely a function of what counted as a positive pair during training.** If a model was
trained on (query, relevant-passage) pairs scraped from search-engine click logs, its notion of
"similar" is tuned for short-question-to-long-passage retrieval. If a model was trained on
(sentence, paraphrase) pairs, its notion of "similar" is tuned for near-duplicate detection. If a
model was trained on (code, docstring) pairs, its notion of "similar" is tuned for code search.
These are not the same space, even when the same architecture and the same 1536 dimensions
produce all three. **"Semantic similarity" is not one thing** — it's a family of things, and which
member of the family you get depends on what pairs the contrastive objective saw. This is why a
model that tops a paraphrase-mining benchmark can be mediocre at retrieval, and vice versa — they
were pulled toward different notions of "close."

Practically: when you evaluate a model, evaluate it on data that resembles *your* task's positive
pairs, not a generic "is this a good embedding model" vibe check. §5 formalizes this.

### 2.2 Similarity metrics, and the identity that makes normalization load-bearing

Three metrics show up everywhere:

| Metric | Formula | Range | Notes |
|---|---|---|---|
| Cosine similarity | `(a·b) / (‖a‖‖b‖)` | `[-1, 1]` | angle only, magnitude-invariant |
| Dot product | `a·b` | `(-∞, ∞)` | angle *and* magnitude |
| Euclidean (L2) distance | `‖a - b‖ = √(Σ(aᵢ-bᵢ)²)` | `[0, ∞)` | a distance, not a similarity — smaller is closer |

These look like three different design choices. On **L2-normalized vectors** (every vector scaled
to unit length, `‖v‖ = 1`), they collapse into one ranking. Here's the identity:

```
‖a - b‖² = ‖a‖² + ‖b‖² - 2(a·b)
```

If `‖a‖ = ‖b‖ = 1`, this becomes:

```
‖a - b‖² = 2 - 2(a·b)
```

Euclidean distance squared is a strictly decreasing function of the dot product. So ranking
candidates by *ascending* Euclidean distance gives you the identical order as ranking by
*descending* dot product. And because both vectors have unit norm, `cosine(a,b) = a·b`, so cosine
similarity ranking is the same order too. **On L2-normalized vectors: cosine ranking == dot-product
ranking == inverse-euclidean ranking.** Three metrics, one answer.

Prove it in numpy:

```python
import numpy as np

rng = np.random.default_rng(0)
vecs = rng.normal(size=(200, 384))          # unnormalized "raw" embeddings
query = rng.normal(size=384)

def normalize(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)

vecs_n = normalize(vecs)
query_n = normalize(query)

cos_rank = np.argsort(-(vecs_n @ query_n))                      # descending cosine
dot_rank = np.argsort(-(vecs_n @ query_n))                      # == cosine on normalized vecs
euc_rank = np.argsort(np.linalg.norm(vecs_n - query_n, axis=1))  # ascending distance

assert np.array_equal(cos_rank, dot_rank)
assert np.array_equal(cos_rank, euc_rank)
print("identical ranking, as expected on normalized vectors")
```

Now break it on purpose — scale a handful of vectors up, without renormalizing, and dot-product
ranking diverges from cosine ranking because dot product now rewards magnitude:

```python
vecs_scaled = vecs_n.copy()
vecs_scaled[:20] *= 5.0                       # 20 vectors get 5x magnitude, direction unchanged

cos_rank2 = np.argsort(-(normalize(vecs_scaled) @ query_n))
dot_rank2 = np.argsort(-(vecs_scaled @ query_n))

print("agree after breaking normalization:", np.array_equal(cos_rank2, dot_rank2))  # False
```

The scaled-up vectors now rank artificially high under dot product even though their *direction*
relative to the query didn't change at all. This is why normalization is not cosmetic — it is the
thing that makes "dot product" and "cosine similarity" mean the same operation. **Normalization is
load-bearing, and it silently breaks in three places you will actually hit:**

1. **Dimensionality truncation** (§6) — slicing a normalized vector to its first `k` dimensions
   produces an *un*-normalized vector. If your index computes dot product (many do, because it's
   cheaper than cosine — no division at query time), your ranking is now silently wrong.
2. **Quantization** (§7) — binary and int8 quantization operate on the float values; if you
   quantize before normalizing, or quantize a truncated-but-unrenormalized vector, the thresholds
   and calibration ranges are computed against the wrong distribution.
3. **Mixing vectors from different pipelines** — one ingestion path normalizes, another doesn't
   (a bug, a library default, a forgotten step after a refactor). Nothing throws. Recall just gets
   worse, and it looks like "the model isn't very good" rather than "half the index is
   un-normalized."

### 2.3 Anisotropy and hubness — known, honest, no invented numbers

Two structural phenomena are worth knowing by name, without claiming numbers this fact sheet
doesn't have:

- **Anisotropy.** Embeddings from many contextual models don't fill the sphere uniformly — they
  cluster in a narrow cone of the space rather than spreading across all directions. This has been
  reported repeatedly in the embedding literature as a property of contextual (transformer-based)
  representations. It matters practically because a narrow cone compresses the *range* of cosine
  similarities you observe: everything looks moderately similar to everything else, which flattens
  your ability to distinguish "very relevant" from "somewhat relevant" by threshold alone.
- **Hubness.** In high-dimensional spaces, some points become "hubs" — they show up disproportionately
  often as a nearest neighbor to many different queries, regardless of whether they're actually
  relevant. This is a known consequence of the geometry of high-dimensional distance concentration,
  not a bug in any particular model. It's also not free to search around: the same dimensionality
  that produces hubness is what an ANN graph has to build edges through, which is why the dimension
  you pick here has a downstream cost in graph construction and search quality that
  [`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md)
  quantifies in terms of `M` and `efConstruction`, not just in the storage bytes §6.3 counts.

Neither phenomenon has a clean universal number attached to it that belongs in a fact sheet — they
vary by model, training data, and dimensionality. Treat them as *reasons to distrust a raw
similarity score as an absolute measure of relevance* and to prefer rank-based and set-based
metrics (recall@k, MRR, nDCG — see `08`) over "cosine > 0.8 means relevant" thresholds. If you
find yourself hardcoding a similarity threshold as a relevance cutoff, that threshold is a number
you invented, not one anisotropy or hubness gave you permission to invent.

---

## 3. Symmetric vs asymmetric embedding — the most-skipped detail

A query and a document are not the same kind of text. "What was ACME's Q2 revenue growth?" is
nine words framed as a question. The chunk that answers it is a declarative sentence sitting
inside a filing. If you embed both with the exact same forward pass and no signal about which
role each one is playing, you're asking one representation to do two jobs — and every embedding
model that supports **asymmetric embedding** does so because a single "generic" embedding
underperforms a role-aware one for retrieval specifically.

This is the most-skipped detail in RAG pipelines because it fails silently: the API call succeeds,
a vector comes back, retrieval still returns *something*. Nothing breaks. It just retrieves worse,
and unless you have a golden set (§16 lab 2 builds one), nothing will tell you.

### 3.1 How each vendor exposes it

| Vendor / model | Mechanism | Required? |
|---|---|---|
| Cohere (embed-v3+) | `input_type` param: `search_document`, `search_query`, `classification`, `clustering`, `image` | **Required** for v3+ — the API will not silently default it away |
| Voyage AI | `input_type` param: `None`, `"query"`, `"document"` — Voyage *prepends a prompt internally* when set | Optional but strongly recommended |
| EmbeddingGemma | Literal string templates you build yourself: query → `task: {task description} | query: {content}` (default task description "search result"); document → `title: {title or "none"} | text: {content}` | Manual — you own the template |
| Qwen3-Embedding | Instruction-aware — you prepend a natural-language instruction; vendor states this yields roughly **1–5% improvement**, and *omitting* the instruction on the query side costs roughly **1–5%** (Qwen3-Embedding vendor docs) | Optional, vendor-recommended |
| Gemini `gemini-embedding-001` | `task_type` param: e.g. `SEMANTIC_SIMILARITY`, `RETRIEVAL_QUERY`, `RETRIEVAL_DOCUMENT` | Optional |
| Gemini `gemini-embedding-2` | **No `task_type` field at all.** You put the task instruction directly in the prompt text | N/A — different mechanism entirely |
| OpenAI `text-embedding-3-*` | No asymmetric mechanism exposed | N/A |

Four distinct mechanisms for the same underlying idea (a typed field, a typed field that also
does prompt injection under the hood, a manual string template, a free-text instruction), plus one
vendor (OpenAI) that doesn't expose the concept at all — and one model generation from the *same
vendor* (Gemini) that removed the typed field between `gemini-embedding-001` and
`gemini-embedding-2` and replaced it with "put it in the prompt yourself." If you're upgrading
across that boundary, this is a code change, not a version bump.

### 3.2 The wrong call and the right call, side by side

Cohere, because `input_type` is a required parameter and therefore the clearest illustration:

```python
# WRONG — embedding a user's query with the document setting.
# The API call succeeds. Nothing errors. Retrieval quality just degrades.
response = co.embed(
    texts=[user_query],
    model="embed-v4.0",
    input_type="search_document",   # bug: this is a query, not a document
)

# RIGHT — query and document sides use different input_type values,
# even though it's the same model, same call shape, same everything else.
query_vec = co.embed(
    texts=[user_query],
    model="embed-v4.0",
    input_type="search_query",
).embeddings

doc_vecs = co.embed(
    texts=chunk_texts,
    model="embed-v4.0",
    input_type="search_document",
).embeddings
```

Voyage, showing the "prepends a prompt internally" behavior explicitly:

```python
# WRONG — no input_type, so Voyage treats query and documents identically
# at ingestion and query time, forfeiting the asymmetric objective the
# model was actually trained with.
doc_vecs = vo.embed(chunk_texts, model="voyage-4").embeddings
query_vec = vo.embed([user_query], model="voyage-4").embeddings

# RIGHT — Voyage silently prepends a role-specific prompt for you when
# input_type is set; you don't write the prompt, you just declare the role.
doc_vecs = vo.embed(chunk_texts, model="voyage-4", input_type="document").embeddings
query_vec = vo.embed([user_query], model="voyage-4", input_type="query").embeddings
```

EmbeddingGemma, where there is no API flag at all — the "flag" is a string you build by hand,
which means it's exactly as easy to get wrong as any other string-formatting bug:

```python
# WRONG — same raw text on both sides, no template applied.
query_text = "what caused the Q2 revenue increase?"
doc_text = "The company's revenue grew by 3% over the previous quarter."

# RIGHT — hand-built asymmetric templates. Get the literal syntax wrong
# (missing "|", wrong field order) and you've silently trained-away
# nothing, because the model just sees it as unstructured text.
query_prompt = f"task: search result | query: {query_text}"
doc_prompt = f"title: none | text: {doc_text}"
```

### 3.3 The punchline

Embedding a query with the document-side setting (or template, or missing instruction) is a
**silent quality bug**. It doesn't raise, doesn't log a warning, doesn't show up in a smoke test
that just checks "did I get a 200 back and a vector of the right length." The only thing that
catches it is a golden set with known-relevant query→document pairs and a recall@k measurement —
which is exactly what lab 2 in §16 asks you to build, because it is the cheapest real experiment
in this entire chapter: no new infrastructure, just two embedding passes and a metric you already
need for `08`.

---

## 4. The 2026 model landscape

The table below is built from vendor documentation checked 2026-08-08 (fact sheet §1). Prices are
included only where the fact sheet has a verified number; everything else is left blank rather
than guessed.

| Model | Context (tokens) | Dimensions | Modalities | Quantized output types | Fine-tunable | Open weights | Price / 1M tok |
|---|---|---|---|---|---|---|---|
| `voyage-4-large` | 32,000 | 256 / 512 / 1024 (default) / 2048 | text | float, int8, uint8, binary, ubinary | enterprise service | no | — |
| `voyage-4` | 32,000 | 256 / 512 / 1024 (default) / 2048 | text | float, int8, uint8, binary, ubinary | enterprise service | no | — |
| `voyage-4-lite` | 32,000 | 256 / 512 / 1024 (default) / 2048 | text | float, int8, uint8, binary, ubinary | enterprise service | no | — |
| `voyage-4-nano` | 32,000 | 256 / 512 / 1024 (default) / 2048 | text | float, int8, uint8, binary, ubinary | fully (open weights) | **yes**, on Hugging Face | — |
| `embed-v4.0` (Cohere) | **128,000** | 256 / 512 / 1024 / 1536 (default) | text, images, mixed text+image (PDF) | float, int8, uint8, binary, ubinary, base64 | enterprise service | no | — |
| `gemini-embedding-001` | 8,192 (shared across modalities where applicable) | 3072 default, manual-normalize on truncation | text (+ `task_type` for retrieval roles) | — | no | no | unverified, see §13 |
| `gemini-embedding-2` | **8,192, shared across text/image/audio/video/PDF**, 258 visual tokens/PDF page | 3072 default; recommended truncations 768/1536/3072, **auto-renormalizes** | text, image, audio, video, PDF — one shared space | — | no | no | unverified, see §13 |
| `text-embedding-3-small` (OpenAI) | 8,192 | 1536 default, `dimensions` param truncates | text | — | **no** | no | **$0.02** |
| `text-embedding-3-large` (OpenAI) | 8,192 | 3072 default, `dimensions` param truncates | text | — | **no** | no | **$0.13** |
| `text-embedding-ada-002` (OpenAI, legacy) | 8,192 | 1536 | text | — | **no** | no | **$0.10** |
| `Qwen3-Embedding-8B` | 32,000 | up to 4096, user-definable 32–4096 (MRL) | text, instruction-aware | — | fully (sentence-transformers) | **yes** | — |
| `Qwen3-Embedding-{0.6B,4B}` | 32,000 | matching smaller ranges | text, instruction-aware | — | fully | **yes** | — |
| EmbeddingGemma (300M) | **2,048** | 768 default, MRL 512/256/128 | text | QAT variants (int4/int8 mixed precision) | fully (sentence-transformers) | **yes** | — |

(Voyage previous-gen models — `voyage-3-large`, `voyage-3.5`, `voyage-3.5-lite`, `voyage-3`,
`voyage-3-lite`, `voyage-2`, and the domain models `voyage-code-3`, `voyage-finance-2`,
`voyage-law-2` — are still served; `voyage-01`, `voyage-lite-01`, `voyage-02`, and
`voyage-lite-02-instruct` are deprecated. See §12 for why that deprecation lineage matters more
than it looks like it should.)

### 4.1 Outliers worth knowing by name

**Cohere's 128k context** is an outlier by a wide margin — every other model on this table tops
out at 32k or less, and several (Gemini, EmbeddingGemma) are an order of magnitude smaller. If
your corpus has long documents you'd rather embed whole (a precondition for late chunking, §9),
Cohere's context ceiling is the widest door in.

**Gemini's 8,192-token budget being *shared across modalities*** is a design detail that bites
people who don't read the fine print: it is not 8,192 tokens of text *plus* however many images
you want. Text, image, audio, video, and PDF content all draw from the same 8,192-token pool per
input. A PDF costs **258 visual tokens per page** on top of its text tokens — feed it a 25-page PDF
and you've spent 6,450 tokens on visual encoding alone, before a single word of body text. This
budget exhausts fast, and — per §8 — it exhausts *silently*.

**EmbeddingGemma's footprint** is the opposite outlier: **300M parameters**, a **2,048-token**
context ceiling (an order of magnitude below its open-weight peer Qwen3-Embedding's 32k), and a
768-dimension default with MRL truncation down to 128. This is the model you reach for when you
need something that runs on a laptop GPU or fits an edge-inference budget, not something you reach
for to embed long documents whole.

**`voyage-4-nano` being open-weight** is notable because it's the *nano* tier, not a separate
research release — Voyage ships an open-weight model inside its production naming scheme, on
Hugging Face, fully fine-tunable with sentence-transformers like any other open model, while its
larger siblings (`voyage-4`, `voyage-4-large`) remain closed and fine-tunable only as an enterprise
service.

**The Voyage 4-series shared embedding space** is the single most architecturally interesting fact
in this table. Voyage states explicitly that "all embeddings created with the 4 series are
compatible with each other" — `voyage-4-nano`, `voyage-4-lite`, `voyage-4`, and `voyage-4-large`
all place text into the *same* coordinate system, just with different quality/cost tradeoffs per
tier. That breaks the "changing models means re-embedding everything" rule from §1, but only
*within* the 4-series: you can embed your (large, slow-changing) document corpus once with the
cheap `voyage-4-lite` tier and embed queries at request time with the expensive, higher-quality
`voyage-4-large` tier — asymmetric cost tiering, no reindex required, because both tiers write into
the same space. This does not extend across generations (4-series to 3-series is a real
migration) or across vendors. It is a genuine exception to the rule in §1, and it is the only one
on this fact sheet.

---

## 5. Why leaderboards mislead, and what to do instead

### 5.1 The numbers, dated and labeled as secondary

Here is what the fact sheet has for the state of the leaderboards, mid-2026, and every figure below
is labeled the way it should be — blog-grade, dated, not a substitute for your own measurement:

- Tencent KaLM-Embedding-Gemma3-12B reported #1 on MMTEB at **72.32** (secondary source, as of
  July 2026).
- Microsoft Harrier-OSS-v1 (27B) reported **74.3** on MTEB v2 (secondary source, mid-2026).
- Qwen3-Embedding-8B **70.58** — this one has a firmer citation: the vendor states it ranks No. 1
  on the MTEB multilingual leaderboard **as of June 5, 2025**.
- Cohere embed-v4 reported **65.2** (secondary, mid-2026).
- OpenAI `text-embedding-3-large` **64.6%** — this figure appears in OpenAI's own published table,
  so it carries a firmer provenance than the others in this list, but it's still a single
  aggregate leaderboard number.
- BGE-M3 reported **63.0** (secondary, mid-2026).
- Gemini Embedding reported "highest overall on MTEB(Multilingual)" as of **2025-03-10**, per its
  own paper (arXiv 2503.07891) — a vendor's paper reporting a vendor's own result.

Every one of those numbers is true in the narrow sense that someone ran that model against that
benchmark on that date and got that score. None of them tell you how the model will perform on
your corpus. Here's why, mechanically.

### 5.2 The four structural reasons MTEB is a weak proxy for your task

**1. Contamination is structural, not accidental.** MTEB's retrieval tasks are backed by BEIR.
BEIR was designed as a zero-shot benchmark — models weren't supposed to have seen its data during
training. That assumption is dead: models are now routinely trained on data that overlaps BEIR,
whether deliberately (BEIR is public and useful) or incidentally (it's built from Wikipedia,
StackExchange, and other web-scale sources that show up in every large training corpus anyway).
A model's BEIR/MTEB retrieval score is now partly a measurement of memorization, not
generalization, and there's no clean way to separate the two components from the outside.

**2. The leaderboard has saturated.** There are 400+ models on it with marginal deltas between
adjacent ranks. When 400 models are within a few points of each other, either the benchmark has
stopped discriminating meaningfully between them, or a meaningful fraction of them are overfit to
it (or both). Either way, "#7 vs #23" is not a decision-relevant gap.

**3. Domain shift.** MTEB retrieval is dominated by general web corpora — MS MARCO, TREC,
NFCorpus. These are search-engine-flavored, English-heavy, short-query-long-passage tasks. If your
corpus is legal contracts, scientific PDFs, or internal SaaS documentation, none of MTEB's
retrieval tasks resemble your actual distribution of queries or documents. The fact sheet's own
framing for this is exactly right: MTEB is "a directional signal at best" outside its home
domain.

**4. MTEB versions aren't comparable to each other.** MTEB v1 and v2 scores are not on the same
scale — and there are now separate boards for English v2, Multilingual (MMTEB), and Code. A number
copied from a blog post without its board and version attached is not even internally consistent,
let alone comparable to your corpus.

There's a useful heuristic here, worth stating even though it's secondary-source and not a
measurement: a model that scores well on MTEB but *cliffs* hard on your private domain eval is
showing you contamination or overfitting; a model that degrades *gracefully* from MTEB to your
domain is showing you actual generalization. You only see this by running the private eval — which
is the entire point of this section. (PTEB, arXiv 2510.06730, proposes stochastic paraphrasing at
eval time specifically to make contamination harder; worth knowing it exists, not required
reading to act on this section's advice.)

### 5.3 The replacement procedure

None of the above means leaderboards are useless — they're a fine way to build a *shortlist*. They
are not a way to make a final decision. Here's the procedure that replaces "pick the top row":

```
1. Shortlist 3–5 candidates from the landscape table in §4, filtered by hard constraints
   you already know: context length ≥ your longest document, price you can afford at your
   ingest volume, open-weight if you need on-prem, multimodal if your corpus has images/PDFs.

2. Build a 50-query golden set on YOUR corpus:
   - Sample real (or realistically synthetic) queries.
   - For each, hand-label the set of chunks that would satisfy it — this is the expensive
     part, and there's no shortcut; it's also the same artifact `08-evaluation-methodology.md`
     needs, so this work isn't a detour, it's the foundation.

3. For each shortlisted model: embed the corpus, embed the 50 queries (with the CORRECT
   input_type / template per §3 — this step is where §3's bug most commonly leaks in),
   measure recall@k (k = whatever your retrieval stage actually returns, e.g. top-20).

4. Compare recall@k across candidates, with a confidence interval (bootstrap over the 50
   queries — see `../python-mastery/31-measurement-methodology.md`). 50 queries is a small
   sample; without an interval you cannot tell a real 3-point gap from noise.

5. Pick the model with the best recall@k on YOUR data, subject to your cost and latency
   constraints — not the model with the best MTEB row.
```

Step 2 is the only genuinely slow part, and even it is bounded: 50 hand-labeled queries is an
afternoon for someone who knows the corpus, not a research project. Every other step is API calls
and arithmetic you already have the code for once `08`'s harness exists. **This evaluation costs
less than one afternoon and outperforms every leaderboard, because it measures the only thing that
was ever going to matter: your corpus, your queries, your definition of relevant.**

---

## 6. Dimensionality and Matryoshka Representation Learning

### 6.1 What MRL actually trains

Matryoshka Representation Learning (arXiv 2205.13147, submitted May 2022, last revised Feb 2024)
trains an embedding so that **prefixes of it are independently useful representations**. Not "the
first 256 dimensions happen to carry a lot of the signal" — the training loss explicitly supervises
multiple prefix lengths simultaneously, at `O(log d)` points across the dimension range, so that
truncating to any of those supported lengths gives you a representation that was *trained to be
complete at that length*, not a representation that was trained at full length and got lucky when
sliced.

Two properties matter operationally:

- **No additional forward pass.** You embed once, at full dimension, and every supported prefix
  length is available for free by slicing the array. There's no "re-embed at 256 dims" call — you
  already have the 256-dim prefix sitting inside the 2048-dim vector you already computed.
- **The nesting is a trained property, not a mathematical fact about the space.** This is the
  detail that trips people into thinking MRL truncation and generic dimensionality reduction (PCA,
  SVD) are the same kind of operation. They are not.

OpenAI states this directly in their own documentation: reducing embedding dimensionality via SVD
or PCA, **"even by 10%, generally results in worse downstream performance."** PCA/SVD find the
directions of maximum variance in a fixed, already-trained embedding — a *post hoc* fit — with no
guarantee that those directions preserve the semantic distinctions the model actually learned to
encode. MRL, by contrast, bakes the "the first `k` dims must stand alone" constraint into training
itself, before the weights are even final. The output looks similar (both give you a shorter
vector), but one throws away information optimized for a different objective, and the other was
optimized for exactly the operation you're about to perform on it.

MRL is now widely adopted: OpenAI's `text-embedding-3-*` family, Gemini `embedding-001` and
`embedding-2`, Voyage's 3/3.5/4 series, Cohere `embed-v4`, Qwen3-Embedding, EmbeddingGemma, and
(outside the vendors covered above) `nomic-embed-text-v1.5`, `mxbai-embed-large-v1`, and `jina-v3`.
If you're picking among 2026-era models, assume MRL support is the norm, not the exception, and
check for it explicitly only when it isn't advertised.

### 6.2 The renormalization trap

Truncating a unit-norm vector to its first `k` dimensions does not produce a unit-norm vector — the
sum of squares over a subset of dimensions is smaller than the sum over all of them, so
`‖v[:k]‖ < 1` in general. If your downstream index computes cosine similarity by literally dividing
by the norm at query time, this self-corrects. If it computes dot product (common, because it's
cheaper), you now have exactly the bug from §2.2: the ranking is contaminated by residual magnitude
differences that have nothing to do with semantic similarity. **You must re-normalize after
truncating.**

This is not a hypothetical gotcha — it's different behavior on two models from the same vendor,
released one generation apart, and the fact sheet is explicit about it:

- `gemini-embedding-001`: **you must manually L2-normalize** truncated dimensions yourself. Skip
  this step and you have a silent quality regression, not an error.
- `gemini-embedding-2`: **introduces automatic renormalization** for non-default (truncated)
  dimensions. The same truncation call that was a bug on the previous model is correct, unmodified,
  on this one.

That pair is the cleanest illustration in this whole chapter of why you read the docs for the
*specific model version* you're calling, not the docs for "Gemini embeddings" as a category.

The numpy, done correctly:

```python
import numpy as np

def truncate_and_renormalize(embedding: np.ndarray, k: int) -> np.ndarray:
    """MRL truncation. Assumes `embedding` is already L2-normalized at full
    dimension (true for every model in §4). Do this manually for any model
    that does not state it auto-renormalizes truncated output (e.g.
    gemini-embedding-001, OpenAI's `dimensions` param per their own docs,
    EmbeddingGemma per its docs). Safe to call even where the vendor already
    renormalizes — renormalizing an already-unit vector is a no-op.
    """
    truncated = embedding[..., :k]
    norm = np.linalg.norm(truncated, axis=-1, keepdims=True)
    return truncated / norm

full = embed("some chunk of text")          # e.g. 2048-dim, unit norm
short = truncate_and_renormalize(full, 256)

assert np.isclose(np.linalg.norm(short), 1.0)   # would fail without the renormalize step
```

OpenAI's docs make the same point in prose, and it generalizes beyond OpenAI: *"In general, using
the `dimensions` API parameter when creating the embedding is the suggested approach"* — because
the provider-side truncation implements MRL correctly, including any renormalization the model
needs. Manual post-hoc truncation (slicing a vector you already got back at full size) is exactly
where the renormalization step gets forgotten, because it's a step *you* now own that used to be
invisible.

### 6.3 Storage formula and a worked example

Raw (unquantized) storage per vector:

```
bytes_per_vector = d × bytes_per_dim
total_bytes      = N_vectors × d × bytes_per_dim
```

`bytes_per_dim` is 4 for float32 (the typical wire format), before any index overhead. HNSW graph
structure adds its own per-vector overhead on top of raw vector storage — the graph edges
themselves, at the `M` neighbors-per-node parameter — covered in
[`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md);
this chapter's formula covers the vector payload only, not the index structure built around it.

Worked symbolic example — the question MRL exists to let you answer for your own corpus: what does
2048 → 256 buy?

```
N_vectors = 10,000,000 (illustrative — plug in your corpus size)
float32, 4 bytes/dim

at d = 2048:  10,000,000 × 2048 × 4 bytes = 81.92 GB
at d = 256:   10,000,000 ×  256 × 4 bytes = 10.24 GB

reduction factor = 2048 / 256 = 8x
```

An 8x reduction in raw vector storage, for whatever recall cost your golden set measures at that
truncation — which is precisely lab 3 in §16, because the "knee" (the truncation point where
recall starts dropping faster than storage savings justify) is corpus-specific and Google's own
documentation only promises the shape, not the number: their doc states performance "is not
strictly tied to the size of the embedding dimension, with lower dimensions achieving scores
comparable to their higher dimension counterparts" — a direction, not a threshold you can copy into
your own system without measuring it.

---

## 7. Quantization

This section draws on the richest verified material on the fact sheet: the Hugging Face blog
*"Binary and Scalar Embedding Quantization"* (March 2024), MTEB retrieval subset, 15 benchmarks,
top-k=100, rescore_multiplier=4 (400 candidates rescored per query). Every number below carries
that provenance unless stated otherwise.

### 7.1 Binary quantization

The idea: take a normalized float32 embedding and threshold each dimension at 0 — positive becomes
1, non-positive becomes 0. One bit per dimension instead of 32. That's a **32x** reduction in
memory and disk footprint for the vector payload.

```python
import numpy as np

def binary_quantize(embeddings: np.ndarray) -> np.ndarray:
    """embeddings: (n, d) float32, L2-normalized. Returns bit-packed uint8,
    shape (n, ceil(d/8)) — NOT (n, d). See §7.2 for why the shape shrinks."""
    bits = (embeddings > 0).astype(np.uint8)      # (n, d) of 0/1
    packed = np.packbits(bits, axis=-1)            # (n, ceil(d/8))
    return packed
```

Search over binary-quantized vectors uses **Hamming distance** — count of differing bits between
two bit-strings — computed as XOR followed by a population count (popcount):

```python
def hamming_distance(a_packed: np.ndarray, b_packed: np.ndarray) -> int:
    """a_packed, b_packed: 1-D uint8 arrays, same length (bytes)."""
    xor = np.bitwise_xor(a_packed, b_packed)
    return int(np.unpackbits(xor).sum())            # popcount over all bits
```

XOR-then-popcount is why binary quantization is also fast, not just small: it's a handful of CPU
instructions per comparison instead of 384 or 1024 floating-point multiply-adds. This is where the
**up to 32x faster retrieval** figure comes from, alongside the 32x memory reduction — two separate
32x's, one for space, one for time, both from the same bit-packing move.

Retention, from the HF experiment: **~92.5%** of retrieval performance (relative to float32) with
binary quantization and no rescoring; **up to ~96%** with the rescore step (§7.3). Both figures are
from that specific 15-benchmark MTEB retrieval subset at k=100 with a 4x rescore multiplier — treat
them as "this is the shape and rough magnitude to expect," and measure your own corpus (lab 4 in
§16) before trusting them as your production number.

### 7.2 The bit-packing gotcha

This is the detail that confuses everyone wiring up a vector database for the first time, so it
gets its own subsection. A model with **1024 float dimensions** produces, under binary
quantization, **1024 bits** — which is **128 bytes**, not 1024 bytes and not a 1024-length array.

```python
d = 1024
bits = d                      # one bit per dimension
bytes_ = bits // 8             # np.packbits packs 8 bits per byte
assert bytes_ == 128

packed = binary_quantize(np.random.default_rng(0).normal(size=(1, d)))
assert packed.shape == (1, 128)      # NOT (1, 1024)
```

If your vector database schema declares a column with dimensionality 1024 for the binary field —
because that's the model's dimensionality — every insert will fail a length check, or worse, silently
get zero-padded or truncated by whatever ORM layer sits in front of it. The dimensionality of the
*binary-packed array* is `d / 8`, full stop; this is exactly why Voyage's and Cohere's own docs
state the returned `binary`/`ubinary` output length as `output_dimension / 8` — they're telling you
the packed length up front because this is the single most common integration bug in binary
quantization setups. This is a column-encoding problem before it's a vector-search problem — the
general theory of how fixed-width values get packed and typed at the column level is
[`../databases/02-data-storage-formats-and-encoding.md`](../databases/02-data-storage-formats-and-encoding.md),
and it's worth reading before you write the schema, not after the length-check exception teaches
you the same lesson the hard way.

### 7.3 The rescore/rerank trick

Binary quantization alone gets you ~92.5% retention. The **rescore/rerank** two-stage trick,
tracing to Yamada et al. 2021 (Binary Passage Retriever), gets you up to ~96%:

```
1. Retrieve rescore_multiplier × top_k candidates using fast Hamming search
   over the binary index (e.g. multiplier=4, top_k=100 → retrieve 400).
2. Rescore those 400 candidates using the FLOAT32 query vector against the
   candidates' int8 (or float32) document vectors, via dot product.
3. Return the top_k from the rescored set.
```

```python
def rescore(query_f32: np.ndarray, candidate_ids: list[int],
            doc_vecs_f32_or_int8: np.ndarray, top_k: int) -> list[int]:
    """query_f32: exact float32 query vector (never quantized — the query
    side stays full-precision, since there's only one query per search and
    the cost of keeping it float32 is negligible).
    doc_vecs_f32_or_int8: the higher-precision representation of just the
    candidate_ids surfaced by the cheap binary pass — you load only 400 of
    these, not the whole corpus, which is what keeps this step cheap."""
    scores = doc_vecs_f32_or_int8[candidate_ids] @ query_f32
    order = np.argsort(-scores)[:top_k]
    return [candidate_ids[i] for i in order]
```

The asymmetry is the entire trick: the *query* stays full-precision (there's one of it per search,
so precision is free), while the *corpus* stays cheap (binary in RAM for the first pass, int8 on
disk for the rescore pass) because there are millions of it. You get float32-adjacent recall
(~96%) while paying binary-index costs for the expensive part (the full-corpus scan) and only
paying int8/float32 costs for a few hundred candidates per query.

### 7.4 int8 scalar quantization

A gentler alternative to binary: map the float32 range into 256 discrete levels (an int8 range,
-128 to 127 for signed or 0–255 for unsigned), using a **calibration range** derived from a sample
of the corpus. This gives **4x** size reduction instead of binary's 32x, but preserves more
information per dimension since you keep 8 bits instead of 1.

The operational trap here is explicit in the HF write-up and worth its own paragraph: **"the
calibration dataset greatly influences performance since it defines the quantization buckets."**
If you calibrate int8 quantization on a sample that doesn't represent your actual corpus'
distribution — say, calibrating on a small clean subset before a large messy corpus lands, or
calibrating once and never recalibrating as the corpus' distribution drifts (§12) — the bucket
boundaries are wrong for the data you'll actually store, and you lose accuracy that has nothing to
do with the quantization *method* being weak and everything to do with the calibration step being
sloppy. This is a data-pipeline problem wearing a math costume: recalibrate when your corpus'
distribution meaningfully shifts, the same discipline you'd apply to any other statistic fit on a
sample and then applied to production traffic.

### 7.5 The worked production example

From the HF blog, a real 41-million-text index, quantized:

```
binary index (in memory):    5.2 GB
int8 index (on disk):       47.5 GB, 0 bytes memory
pipeline: binary Hamming search → top 40 → load int8 from disk for those
          40 → rescore against float32 query → return top 10

total: 5.2 GB RAM + 52 GB disk

vs. plain float32 retrieval on the same 41M texts:
total: 200 GB RAM + 200 GB disk
```

The binary-in-RAM / int8-on-disk split isn't a quantization-specific trick — it's the standard
storage-engine argument for tiering hot data by access cost, covered generally in
[`../databases/01-storage-engine-fundamentals.md`](../databases/01-storage-engine-fundamentals.md);
quantization just gives you a second, cheaper representation to put in the cheap tier. And the
two-stage shape itself — a fast, low-precision scan over everything followed by a precise pass over
a small candidate set — is the classical access-method tradeoff (index scan to narrow, then fetch
to verify) worked through in
[`../databases/03-access-methods-and-table-scans.md`](../databases/03-access-methods-and-table-scans.md);
binary-then-rescore is that pattern applied to vector similarity instead of row lookups.

Cost basis used in that blog post: roughly **$3.8 per GB/month** (AWS x2gd instances, memory-optimized).
Applying that basis to just the RAM delta — 200 GB vs 5.2 GB — is roughly `(200 - 5.2) × $3.8 ≈
$740/month` saved on memory alone for this one 41M-text index, before counting the disk savings.
That arithmetic is illustrative math on the blog's own basis, not a new verified figure — the
$3.8/GB/month rate and the 5.2/47.5/200/200 GB figures are the HF blog's; the multiplication is
mine, shown so you can redo it with your own corpus size and your own cloud pricing.

### 7.6 Stacking MRL and quantization

MRL truncation and quantization attack the same problem (storage, and by extension, cost and
speed) from independent axes — one shrinks dimensionality, the other shrinks bits-per-dimension —
and their reductions **multiply**. Truncating 2048 → 256 is an 8x reduction (§6.3); binary
quantization on top of that is another 32x. Stacked: `8 × 32 = 256x` smaller than unquantized,
full-dimension float32, for whatever combined recall cost your golden set shows (measure it — this
is not a number the fact sheet has for a stacked configuration, only for each technique
independently). Vespa has documented this stacked approach in production.

### 7.7 Who returns quantized types natively

You don't have to implement §7.1–§7.4 yourself if your vendor returns quantized output directly:

- **Voyage**: `output_dtype` ∈ `{float, int8, uint8, binary, ubinary}` — request the quantized
  representation at embedding time, no post-processing.
- **Cohere**: `embedding_types` ∈ `{float, int8, uint8, binary, ubinary, base64}` — same idea, and
  note the binary/ubinary outputs are bit-packed at `length / 8` per §7.2, straight out of the API.

Both vendors doing the packing for you removes the single most error-prone step (getting the
`np.packbits` axis and bit order right) — but the two-stage rescore pipeline in §7.3 is still yours
to build; the vendor gives you the representations, not the retrieval architecture around them.

---

## 8. Context length, truncation, and what the model actually sees

This is the silent-failure section. Every vendor on this fact sheet handles over-length input
differently, and the defaults are not uniformly safe.

| Vendor | Default behavior on over-length input | How to force a loud failure instead |
|---|---|---|
| Gemini (both `-001` and `-2`) | **Silently truncates** inputs exceeding the 8,192-token limit — the doc states this outright | No documented flag on the fact sheet to force an error; you must count tokens yourself before the call |
| Voyage AI | `truncation=True` by default — silently truncates | Set `truncation=False` to get an error on over-length input instead of a silently truncated embedding |
| Cohere | `truncate` defaults to `END` — silently drops the tail | Set `truncate=NONE` to force an error on over-length input |
| OpenAI | Max input 8,192 tokens (`text-embedding-3-*`), `cl100k_base` encoding | Not on the fact sheet — count tokens client-side with `tiktoken` before sending |

### 8.1 The operational argument

The default across essentially every vendor here is "truncate quietly and keep going." That default
is *reasonable* for a chat completion, where an over-length prompt losing its tail is often an
acceptable degradation. It is the **wrong default for an ingestion pipeline**, for a specific
reason: a chunk that gets silently truncated at embedding time produces a vector for the *first
half* of the document, and that vector is then treated identically — stored, indexed, retrieved
with the same confidence — as every vector that embedded its source completely. There is no marker
anywhere downstream saying "this one is partial." A query whose answer lives in the second half of
that document will never retrieve it, and the failure looks exactly like "the model missed it,"
when the actual cause is an untuned flag three layers up the pipeline.

The position this chapter argues for: **in an ingestion pipeline you want it to fail loudly, not
silently embed the first half of a document.** Set `truncation=False` on Voyage. Set `truncate=NONE`
on Cohere. Where a vendor gives you no such flag (Gemini, OpenAI, per this fact sheet), count
tokens client-side before the call and reject or split anything over the limit yourself — the
absence of a vendor-side flag doesn't remove the obligation, it just means you own the check
instead of delegating it. Chunking your documents to a size that respects the model's context
limit (`02-chunking-and-document-processing.md`'s job) is the actual fix; the loud-failure flag is
the safety net that tells you when chunking policy and model choice have drifted out of sync — e.g.
someone raises the max chunk size in the chunker without checking it still fits under the new
model's context ceiling.

### 8.2 Token counting

OpenAI's third-generation embedding models use the `cl100k_base` encoding — the same tokenizer
family used by their chat models of that era, countable client-side with `tiktoken` before you ever
make the API call, which is exactly what a truncation audit (lab 6 in §16) needs.

### 8.3 The model's own knowledge cutoff — what it does and doesn't matter for

OpenAI states its embedding models lack knowledge of events after **September 2021**. This is
easy to over-interpret. An embedding model's "knowledge cutoff" describes what concepts, entities,
and phrasing patterns it learned to represent during pretraining — it does **not** mean the model
can't produce a usable vector for a document about a 2026 event. The model isn't answering
questions or recalling facts; it's mapping text to geometry based on patterns of language and
usage it learned. A document about a company or event that postdates the model's training will
still get embedded — it just might land in a slightly less well-calibrated part of the space if the
vocabulary or entity is genuinely novel and underrepresented in training data (a brand-new company
name, a coined term). This matters for edge cases at the margin, not as a blocking constraint on
using the model for current content — but it's worth knowing the distinction so "the model doesn't
know about it" doesn't get misapplied as a reason to avoid embedding recent documents, when the
actual risk is much narrower and much rarer than that framing suggests.

---

## 9. The representation limit: a chunk that means nothing alone

### 9.1 The core failure

Take this sentence: *"The company's revenue grew by 3% over the previous quarter."*

Embed it in isolation and you get a vector that encodes: revenue, growth, percentage, quarter,
comparison. It does not encode *which* company, *which* quarter, or *which* year — because that
information was never in the text you handed the model. A query like "how did ACME do in Q2 2023"
will not reliably retrieve this chunk, not because the embedding model is bad, but because the
chunk **genuinely does not contain the information the query is asking about.** This is a
representation problem, not a model quality problem, and no amount of model shopping (§5) fixes
it. It is the single most consequential lesson in this chapter and the reason chunking
(`02-chunking-and-document-processing.md`) and embedding are not separable concerns.

Three real fixes exist, with genuinely different cost/determinism/storage tradeoffs. None of them
is strictly dominant — the decision table in §9.5 is the point of this section.

### 9.2 Fix 1 — Contextual retrieval (Anthropic, Sep 2024)

**Mechanism:** an LLM writes a 50–100 token blurb that situates the chunk within its source
document, and that blurb is **prepended to the chunk before embedding** (and, in Anthropic's full
recipe, before BM25 indexing too). The prompt Anthropic used:

> "Please give a short succinct context to situate this chunk within the overall document for the
> purposes of improving search retrieval of the chunk. Answer only with the succinct context and
> nothing else."

The transformation, from Anthropic's own example:

```
BEFORE (raw chunk):
"The company's revenue grew by 3% over the previous quarter."

AFTER (contextualized chunk, what actually gets embedded):
"This chunk is from an SEC filing on ACME corp's performance in Q2 2023; the
previous quarter's revenue was $314 million. The company's revenue grew by
3% over the previous quarter."
```

The embedded vector for the "after" version now has "ACME," "Q2 2023," and "$314 million" to work
with — a query about ACME's Q2 2023 performance has something to actually match against.

**Verified numbers, with full provenance** (Anthropic, published 2024-09-19; baseline top-20-chunk
retrieval failure rate **5.7%**):

| Configuration | Failure rate | Reduction vs baseline |
|---|---|---|
| Baseline (no contextual retrieval) | 5.7% | — |
| + Contextual Embeddings | 3.7% | **35%** |
| + Contextual Embeddings + Contextual BM25 | 2.9% | **49%** |
| + Contextual Embeddings + Contextual BM25 + Reranking | 1.9% | **67%** |

**Cost: $1.02 per million document tokens**, one-time, under Anthropic's stated assumptions:
800-token chunks, 8k-token source documents, 50-token context-generation instruction, 100 tokens
of generated context per chunk. That figure is affordable specifically *because* of prompt
caching — the source document is cached once and reused across every chunk-context-generation call
for that document, rather than re-sent in full for each chunk. Anthropic's separate claim about
prompt caching on the same page: latency reduced more than 2x, cost reduced up to 90% for the
cached portion of the prompt.

**State this plainly, because it matters for how much weight to put on the numbers**: these are
Anthropic's numbers, on Anthropic's own eval set, published to promote Anthropic's own prompt
caching feature. The *method* — situate each chunk with document-level context before embedding —
generalizes to any corpus and any embedding model. The *specific percentages* (5.7% → 1.9%, $1.02/M
tokens) are Anthropic's, measured on Anthropic's eval set, and should be treated as "here's the
shape and rough magnitude other people have found," not as a number your corpus will reproduce.
Lab 7 in §16 has you measure your own.

### 9.3 Fix 2 — Late chunking (arXiv 2409.04701)

**Mechanism:** invert the usual order. Instead of chunk-then-embed, **embed the whole document
first** through a long-context embedding model to get token-level embeddings for the entire
document, *then* apply your chunk boundaries and mean-pool the token embeddings within each chunk.
Each resulting chunk-level vector was computed with the whole document's context already baked
into every token's representation, before pooling — so "the company" inside a chunk carries
information from the sentence three paragraphs earlier that named the company, even though that
sentence never appears in the chunk's own text.

**Tradeoffs versus contextual retrieval:** late chunking needs **no LLM calls** — it's cheap and
fully deterministic, the same document always produces the same chunk vectors. But it **requires a
long-context embedding model** capable of processing the whole document in one forward pass (this
is where Cohere's 128k context from §4 becomes directly relevant, versus EmbeddingGemma's 2,048
being disqualifying for anything but very short source documents). Contextual retrieval needs an
LLM call per document (costly, and LLM output is not perfectly deterministic run to run) but works
with *any* embedding model, including a short-context one, because the LLM does the long-range
reasoning and the embedding model only ever sees a short, pre-contextualized chunk.

Both attack the identical failure mode from §9.1 — a chunk that's unresolvable in isolation — from
opposite ends: contextual retrieval adds words; late chunking preserves context inside the
*vector* without adding any words at all.

(Related work worth knowing exists, not required for the decision table: contextual document
embeddings, arXiv 2505.24782; visual late chunking, arXiv 2604.10167.)

### 9.4 Fix 3 — Late interaction (ColBERT-style)

**Mechanism:** don't pool to one vector per chunk at all. Keep a vector **per token**, and at query
time compute similarity as **MaxSim** — for each query token, find its best-matching document
token, then sum those best-matches across all query tokens. This preserves fine-grained,
token-level matching that a single pooled vector necessarily discards.

This is the most precise of the three fixes — nothing about "the company" being underspecified
matters if the retrieval mechanism can match at the token level against tokens elsewhere in the
same chunk or document that *do* name the company (assuming the chunk boundary was drawn to include
that context, or the document itself is short enough to be the "chunk"). It is also the most
expensive: storing a vector per token instead of a vector per chunk is an order-of-magnitude
storage increase, and MaxSim at query time is more compute than a single dot product per candidate.

For visual documents specifically, **ColPali-style page-patch retrieval** extends the same
late-interaction idea to images: instead of OCR-then-embed-text, embed image patches of a rendered
page directly and do MaxSim against query-token embeddings, retrieving relevant pages without ever
converting them to text.

### 9.5 Decision table

| | Contextual retrieval | Late chunking | Late interaction (ColBERT) |
|---|---|---|---|
| **Cost** | LLM call per chunk (mitigated by prompt caching) — $1.02/M doc tokens, Anthropic's basis | Embedding calls only, no LLM | Embedding calls only, no LLM |
| **Determinism** | Non-deterministic (LLM-generated context varies run to run) | Fully deterministic | Fully deterministic |
| **Storage** | Same as normal chunk embeddings (context is folded into the text, then embedded normally) | Same as normal chunk embeddings | Per-token vectors — order of magnitude larger |
| **Model requirement** | Any embedding model | Long-context embedding model required | Any embedding model, but retrieval infra must support MaxSim |
| **Query-time cost** | Normal ANN search | Normal ANN search | MaxSim — more expensive per candidate |
| **Best fit** | Short-context embedding model, budget for LLM calls, want it done fast | Long documents, want zero LLM cost and full determinism | Precision-critical retrieval where storage/compute budget is generous; visual documents via ColPali |

Forward-references: chunk-boundary strategy generally, and where these three fixes sit relative to
fixed-size vs semantic chunking, is `02-chunking-and-document-processing.md`'s job. How the
retrieved chunks get combined with BM25 and reranking (the "+ Contextual BM25" and "+ Reranking"
rows in §9.2's table) is `04-retrieval-hybrid-and-reranking.md`'s job.

---

## 10. Domain adaptation: when to fine-tune and when not to

### 10.1 The decision boundary, in order

Fine-tuning an embedder is the most expensive and least reversible lever in this entire chapter —
more expensive than a model swap (§12), because it requires labeled or synthetic training data,
training infrastructure, and evaluation discipline to know whether it worked, on top of the
re-embedding cost every model change already carries. It should be the *last* thing you reach for,
in this order:

```
1. Better input_type / instruction usage (§3)         — near-zero cost, reversible instantly
2. Better chunking / context (§9)                       — no model change, reversible instantly
3. Hybrid retrieval (dense + BM25, see `04`)            — infra change, reversible
4. A reranker stage (see `04`)                          — infra change, reversible
5. THEN, if 1–4 haven't closed the gap: fine-tune       — data + training + irreversible-ish
```

The ordering isn't arbitrary — it's sorted by reversibility and cost. Everything above fine-tuning
can be turned off tomorrow if it doesn't help. Fine-tuning produces a model checkpoint that only
your corpus's re-embedded vectors are compatible with, and un-fine-tuning it means re-embedding
again. Try the cheap, reversible things first and measure each one against the golden set from §5
before spending a fine-tuning budget.

### 10.2 Who can even be fine-tuned

| Vendor | Fine-tuning availability |
|---|---|
| OpenAI | **Cannot** be fine-tuned — not offered as a product surface |
| Voyage AI | Enterprise service |
| Cohere | Enterprise service |
| Open-weight (Qwen3-Embedding, EmbeddingGemma, BGE, E5) | Fully fine-tunable with `sentence-transformers` |

This is a real constraint on the model-choice decision from §4–§5: if you might need to fine-tune
later, OpenAI is off the table entirely, and Voyage/Cohere require an enterprise relationship you
may not have. Open-weight models are the only tier where fine-tuning is a self-serve option.

### 10.3 Hard-negative mining — the dominant lever

If you do fine-tune, the single highest-leverage technique is **hard-negative mining**: instead of
training the contrastive objective (§2.1) against random or easy negatives, mine negatives that are
*close* to the positive in embedding space but are not actually relevant — the examples that force
the model to sharpen its decision boundary rather than learn a boundary so loose it's trivially
satisfied.

The trap in naive hard-negative mining: if you mine "the top-k nearest neighbors to the query that
aren't the labeled positive" as your negative set, you will routinely harvest **unlabelled
positives** — documents that are, in fact, relevant to the query, but weren't in your (necessarily
incomplete) label set. Training against those teaches the model that a *correct* answer is *wrong*,
which actively degrades retrieval quality rather than improving it.

**NV-Retriever (arXiv 2407.15831)** addresses this with **positive-aware mining**: use the labeled
positive's own relevance score as a threshold, and filter out any candidate negative whose score is
close to or above that threshold — the reasoning being that a candidate scoring nearly as well as
the known-good positive is more likely an unlabelled true positive than a genuine hard negative.
This one fix — thresholding against the positive's own score instead of blindly taking top-k — is
the difference between hard-negative mining that sharpens the model and hard-negative mining that
poisons it.

### 10.4 Synthetic query generation pipeline

When you don't have enough real query logs to fine-tune from, the standard pipeline is:

```
1. Generate chunk-grounded queries with an LLM — "given this chunk, write a
   question this chunk would answer."
2. Filter the generated queries (remove degenerate, too-generic, or
   off-topic ones — an LLM-as-judge pass, or a quick human spot-check).
3. Mine hard negatives for each (query, chunk) pair using §10.3's
   positive-aware method.
4. Fine-tune the contrastive objective on the resulting (query, positive,
   hard-negatives) triples.
```

Papers backing this pattern, by arXiv ID: domain-specific data generation for RAG adaptation
(2510.11217); LLM distillation for financial filings (2512.08088); multi-task retriever
fine-tuning (2501.04652); KG-driven fine-tuning (Springer, J. Intell. Inf. Syst. 2026). An
alternative to full fine-tuning worth knowing: **model merging** — combine synthetic-data-tuned
weights with the base model's weights rather than fully retraining, reported for biomedical
retrievers in "Less Finetuning, Better Retrieval" (arXiv 2602.04731).

### 10.5 Be honest about the "few hundred examples" claim

A claim that circulates in this space — that "a few hundred labeled examples is enough" to
meaningfully fine-tune an embedder with `sentence-transformers` — is a **secondary-source claim**,
not a verified guarantee on this fact sheet. Treat it the way this chapter treats every unsourced
number: as a hypothesis to test on your own corpus with your own eval, not as a planning figure you
budget against. It may well be directionally true for your domain; the only way to know is to run
the experiment and check recall@k before and after, the same discipline §5 argues for model
selection generally.

---

## 11. Multilingual and multimodal

### 11.1 Cross-lingual retrieval, and where it breaks

A multilingual embedding model is trained so that semantically equivalent text in different
languages lands near each other in the same space — a French query can retrieve an English
document. This works to the extent the model's training data covered that language pair with
enough contrastive signal; it degrades for low-resource language pairs, code-switched text, and
domain-specific terminology that doesn't have consistent translations across the training corpus
(legal and medical terms are common failure points). The MTEB Multilingual/MMTEB board (§5) is the
closest proxy available for cross-lingual quality, and it inherits every one of §5's caveats —
domain shift and contamination apply with at least as much force across languages as within one.
If cross-lingual retrieval is core to your product, the golden-set procedure in §5.3 should include
query/document pairs in every language pair you actually serve, not just English.

### 11.2 Multimodal — the architecturally important point

Two models on the fact sheet put more than text into a single shared vector space:

- **Cohere `embed-v4.0`**: text, images, and mixed text/image inputs (i.e., PDFs) all embed into
  one space, at up to 128k token context.
- **`gemini-embedding-2`**: text, image, audio, video, and PDF all share one 8,192-token budget and
  one embedding space, with PDFs costing 258 visual tokens per page on top of their text tokens.

The genuinely important architectural consequence: **a shared space lets you retrieve an image with
a text query, without OCR.** You don't extract text from a scanned diagram, embed the extracted
text, and hope the extraction was faithful — you embed the image directly, and a text query lands
close to it in the same space *if* the model's training taught it that correspondence. This
collapses an entire pipeline stage (OCR, its error modes, its own drift-and-versioning problem)
that a text-only architecture is forced to carry.

The failure mode this same architecture introduces: **PDF page budgets get consumed fast.**
258 visual tokens per page against an 8,192-token *shared* ceiling means a PDF over roughly 30
pages (before counting any of its text tokens) has already exhausted the budget on visual encoding
alone — and per §8, Gemini's documented behavior on over-limit input is to **silently truncate**.
A multimodal PDF pipeline built against `gemini-embedding-2` without an explicit page-budget check
is the multimodal-specific instance of the exact silent-truncation failure §8 already argues you
must guard against for text.

A shared space also changes what "filtered search" means: a query that should only match images,
or only match one tenant's documents, needs the modality and tenant predicates pushed down
alongside the vector search rather than applied as a post-filter over a mixed-modality result set —
the pushdown and filtered-search theory this leans on is
[`../databases/04-query-engine-internals.md`](../databases/04-query-engine-internals.md).

---

## 12. Drift, versioning, and migration

This is the operational heart of the chapter — the section that turns §1's abstract "it's a schema
decision" into something you actually run in production.

### 12.1 The non-negotiable rule

**An embedding model version IS a schema version. Record it on every vector.** Not on the
collection, not in a config file somewhere adjacent to the index — on the vector itself, or in
metadata joined to it at the row level, so that a query against the index can always answer "which
model produced this." The reason this has to be per-vector and not per-collection: a real corpus
accumulates vectors from more than one model version over its life — the day-one model, whatever
you migrate to, and possibly a false-start you rolled back from — and if the version tag lives
anywhere less granular than the vector, a partial migration (which is the normal state of a
migration in progress, §12.3) becomes silently unrecoverable: you can no longer tell which vectors
are safe to compare against which query embedding.

### 12.2 Why vectors from two models aren't comparable — restated with teeth

§1 stated this as thesis; here's the operational form. If you have vectors from Model A and Model
B sitting in the same index, and you compute similarity between a Model-A query vector and a
Model-B document vector, you get a number. That number is not garbage in the sense of being out of
range — it'll be a valid float, plausibly even in `[-1, 1]` if both sides happen to be normalized —
but it does not mean "semantic similarity" in any coherent sense, because the two vectors were
never trained to share a coordinate system. This is worse than a crash, because a crash tells you
something is wrong. A silently-computed, plausible-looking-but-meaningless similarity score just
degrades your recall in a way that's indistinguishable from "the model isn't very good today,"
which is exactly the kind of bug that survives in production for months. **The one documented
exception on this fact sheet is Voyage's 4-series shared embedding space (§4.1)** — same generation,
different size tiers, genuinely comparable. Everything else — different vendors, different
generations from the same vendor, your own fine-tune vs the base model — is not comparable, and the
per-vector version tag from §12.1 is what lets your infrastructure refuse to compare them instead
of silently doing it wrong.

### 12.3 Three kinds of drift

**Model version drift** — the vendor deprecates or replaces the model you built on. This isn't a
hypothetical: the fact sheet's own Voyage lineage shows it happening routinely inside a single
vendor's history — `voyage-01`, `voyage-lite-01`, `voyage-02`, `voyage-lite-02-instruct` are all
already deprecated, superseded by 2/3/3.5/4-series releases. A vendor's deprecation notice is a
forced migration on a timeline you don't control, which is exactly why the migration playbook in
§12.4 needs to be a rehearsed procedure, not something you improvise the first time it happens
under a deadline.

**Corpus drift** — your documents change distribution over time: new product lines, new
terminology, a different mix of document types than what you evaluated against originally. The
embedding model didn't change, but the thing it's representing did, and your golden set (§5) needs
periodic refresh to keep measuring the corpus you actually have, not the corpus you had when you
built the golden set.

**Query drift** — users start asking about things they didn't ask about before. A golden set built
from last year's query logs measures last year's retrieval problem. This is the same failure mode
as corpus drift, mirrored on the query side, and it argues for the same fix: periodically resample
real queries into the golden set rather than treating it as a fixed artifact.

### 12.4 The migration playbook

```
1. Dual index: stand up a second index alongside the production one, on the
   new model, without touching production traffic.
2. Shadow-embed: re-embed the corpus (or a representative sample first, if
   the corpus is large enough that a full re-embed is itself a significant
   cost decision — see §13) into the new index using the new model, with
   the CORRECT input_type/template per §3 on both the document and query
   side — this is the exact spot the §3 bug likes to hide during a
   migration, because it's new code, freshly written, unreviewed by anyone
   who's hit the bug before. This backfill is a pipeline-reliability problem
   before it's a modeling problem: it must not silently lose documents or
   double-embed them on retry, which is exactly the discipline covered in
   [`../sre-observability/28-telemetry-pipeline-reliability.md`](../sre-observability/28-telemetry-pipeline-reliability.md).
3. Run the golden set (§5) against BOTH indexes.
4. Compare recall@k between old and new, with a bootstrap confidence
   interval over the golden set (`../python-mastery/31-measurement-methodology.md`)
   — a point estimate alone can't tell you whether an apparent improvement
   is real or noise from a 50-query sample.
5. Cut over reads to the new index only once the new model's recall@k
   clears the old model's, with the interval accounted for — not just a
   higher point estimate.
6. Keep the OLD index live and queryable until the new one has run in
   production long enough to trust beyond the golden set alone — the
   golden set is necessary but not sufficient; it's 50 queries, not your
   full traffic distribution.
7. Drop the old index only after that gate passes.
```

Every step here is intentionally boring and slow, because the failure mode it's defending against —
cutting over to a worse model because a demo looked fine — is expensive to discover after the fact
and cheap to prevent with a held-open rollback path.

### 12.5 Re-embed cost, worked

```
re_embed_cost = total_corpus_tokens × price_per_token(new_model)
```

Worked example using OpenAI's verified prices from §4/§13 (`$0.02`/1M tokens for `3-small`,
`$0.13`/1M for `3-large`, `$0.10`/1M for the legacy `ada-002`) — illustrative corpus size, real
prices:

```
corpus = 500,000,000 tokens (illustrative — plug in your own corpus token count)

re-embed with text-embedding-3-small: 500M × ($0.02 / 1M) = $10.00
re-embed with text-embedding-3-large: 500M × ($0.13 / 1M) = $65.00
re-embed with ada-002 (legacy):       500M × ($0.10 / 1M) = $50.00
```

Note what falls out of this arithmetic for free: migrating *off* `ada-002` *onto* `3-small` is not
just a quality upgrade (per OpenAI's own MTEB table in §4, 62.3% vs 61.0%) — it's also a **5x price
cut** ($0.02 vs $0.10 per million tokens). "We already use ada-002" being framed as a reason not to
migrate has the cost argument backwards: staying is the expensive option, on both axes at once.

### 12.6 Vector metadata schema — what to record, concretely

§12.1 says "record the model version on every vector." This subsection says exactly *what* to
record and *why each field exists* — because "model version" alone is not enough to reconstruct
what happened to a vector or decide whether two vectors are comparable.

**The minimum viable version tag, as a structured record:**

```
embedding_config {
    model_name:       string    -- "text-embedding-3-small", "voyage-4", "embed-v4.0"
    model_version:    string    -- "3-small-2024-01-25" or the vendor's version string;
                                   vendors version silently (same name, different weights)
    dimensions:       int       -- the STORED dimension, after MRL truncation if applied
    full_dimensions:  int       -- the model's native output dimension before truncation
    quantization:     string    -- "float32", "float16", "int8", "binary", "ubinary", or "none"
    distance_metric:  string    -- "cosine", "dotproduct", "l2" — what the INDEX is configured for
    input_type:       string    -- "search_document" / "search_query" / null — what was used at
                                   embed time; store the DOCUMENT-side value on the vector, since
                                   query-side input_type is an ephemeral query-time concern
    normalized:       bool      -- whether L2 normalization was applied before storage
    context_method:   string    -- "none", "contextual_retrieval", "late_chunking" — from §9
    embedded_at:      timestamp -- when this vector was produced; critical for corpus-drift
                                   detection (§12.3) and for knowing which vectors predate a
                                   model's silent update
}
```

Why each field matters beyond `model_name`:

- **`dimensions` vs `full_dimensions`** — you need both because a vector stored at 256 dims could
  be a 256-dim model's native output or a 2048-dim model's MRL-truncated prefix. These are not
  the same representation, they are not comparable in the same index, and only the pair of numbers
  tells you which case you're in.
- **`quantization`** — a float32 vector and an int8 vector from the *same model at the same
  dimension* cannot share an index column (different byte widths, different distance computations).
  More subtly, the int8 calibration range (§7.4) is itself a parameter: vectors quantized against
  different calibration datasets are not directly comparable even at the same byte width.
- **`distance_metric`** — this is an index-level setting, not a vector-level one, but recording it
  alongside the vector catches the bug where someone creates a new index with a different metric
  than the old one and expects similarity scores to be comparable. They aren't.
- **`normalized`** — if a vector was stored un-normalized in a dot-product index, it's already
  wrong (§2.2). The boolean lets a migration script detect and fix this retroactively.
- **`embedded_at`** — a vendor's "same model name, updated weights" silent refresh means vectors
  embedded before and after the refresh are potentially in different spaces, even though
  `model_name` and `model_version` haven't changed. The timestamp is the fallback discriminator.

**In practice, most of these fields are collection-level, not row-level.** If your entire
collection uses one model at one dimension with one quantization scheme — which is the common case
for a single-purpose index — you store the config once as collection metadata and don't repeat it
per row. The per-row version tag from §12.1 becomes critical only during migrations (when two
configs coexist in the same collection) and in multi-model architectures (§12.9). The full
`embedding_config` record belongs in a metadata table or a config store joined by a version ID;
the per-vector payload carries just that version ID as a foreign key.

### 12.7 Database index configuration — pgvector and multi-model coexistence

The rest of this chapter is database-agnostic. This subsection is not — it gives you the concrete
SQL for pgvector (the most common vector store in PostgreSQL-based stacks) because the gap between
"version-tag your vectors" and "actually configure a database to enforce this" is where the bugs
live.

#### 12.7.1 One model, one collection — the simple case

```sql
-- pgvector: one embedding model, one dimension, one index.
-- This is the day-one setup that works until you need a second model.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id            BIGSERIAL PRIMARY KEY,
    content       TEXT NOT NULL,
    chunk_id      TEXT NOT NULL,        -- foreign key to chunk metadata
    embedding     vector(1024),         -- dimension must match model output exactly
    model_config  JSONB NOT NULL DEFAULT '{
        "model_name": "voyage-4",
        "model_version": "4.0",
        "dimensions": 1024,
        "quantization": "float32",
        "distance_metric": "cosine",
        "input_type": "search_document",
        "normalized": true
    }',
    embedded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW index — the parameters here are load-bearing; see
-- ../databases/11-hnsw-vector-search-internals.md for why.
CREATE INDEX idx_documents_embedding ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- At query time: set ef_search per session for recall/speed tradeoff.
SET hnsw.ef_search = 100;

SELECT id, content, 1 - (embedding <=> query_vector) AS similarity
FROM documents
ORDER BY embedding <=> query_vector  -- <=> is cosine distance in pgvector
LIMIT 20;
```

The `vector(1024)` column declaration is a **hard constraint**: pgvector will reject any insert
where the vector length doesn't match. This is good — it means a dimension mismatch between your
embedding pipeline and your schema is a loud error at insert time, not a silent corruption. But it
also means **you cannot store vectors of different dimensionalities in the same column**. A model
swap that changes dimensions (e.g. 1024 → 1536) requires a new column or a new table, not just
new data in the same column.

#### 12.7.2 pgvector operator classes — picking the right one

pgvector's distance metric is set by the **operator class** on the index, not by a query-time
flag. Pick wrong at index creation time and every query computes the wrong distance:

```sql
-- Cosine distance (1 - cosine_similarity). Use this when vectors are
-- L2-normalized and you want cosine semantics. The <=> operator.
CREATE INDEX idx_cosine ON documents
    USING hnsw (embedding vector_cosine_ops);

-- Inner product (negative, because pgvector's <#> returns negative inner
-- product for ORDER BY ASC to work). Use this when vectors are normalized
-- and you want dot-product speed without the division. The <#> operator.
CREATE INDEX idx_ip ON documents
    USING hnsw (embedding vector_ip_ops);

-- L2 (Euclidean) distance. Use this when you specifically need geometric
-- distance, not angular similarity. The <-> operator.
CREATE INDEX idx_l2 ON documents
    USING hnsw (embedding vector_l2_ops);
```

On L2-normalized vectors, all three rank identically (§2.2) — but **pgvector doesn't know your
vectors are normalized.** It uses whichever distance computation the operator class specifies,
whether or not that's the one your vectors were designed for. If you create a
`vector_ip_ops` index but your vectors aren't normalized, you get the §2.2 bug — magnitude
contaminates ranking — and pgvector won't warn you. The operator class is a decision you make
once, at `CREATE INDEX` time; verify it matches your normalization invariant before you have data
in the table, not after.

#### 12.7.3 Multiple models, same database — four patterns and when each wins

During a migration (§12.4) or in any multi-model architecture (§12.9), you need vectors from
different models to coexist in the same database. There are four distinct patterns for this, each
with different trade-offs around schema flexibility, write-path bloat, query complexity, and
long-term maintainability. The right choice depends on whether multi-model is a transitional state
(migration) or a permanent one (tiered models, modality-specific models, A/B testing).

Before the patterns: one invariant that applies to all of them. **During a partial migration, you
must embed the query with BOTH models and search BOTH indexes** (or both partitions). You cannot
search a mixed-model result set with a single query vector, because the old vectors and new vectors
are in different coordinate spaces (§12.2). The query-side cost doubles during migration — two
embedding API calls per query — but the alternative is silently broken retrieval on whatever
fraction of the corpus hasn't been migrated yet.

---

**Option A — Separate columns (same table)**

Add a new `vector(d)` column for each model version. Simple to understand, no JOINs needed.

```sql
ALTER TABLE documents
    ADD COLUMN embedding_v2 vector(1536);

CREATE INDEX idx_documents_embedding_v2 ON documents
    USING hnsw (embedding_v2 vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

ALTER TABLE documents
    ADD COLUMN embedding_v2_at TIMESTAMPTZ;
```

Query during migration — UNION both columns:

```sql
WITH old_results AS (
    SELECT id, content, 1 - (embedding <=> $1) AS similarity
    FROM documents
    WHERE embedding_v2 IS NULL
    ORDER BY embedding <=> $1  -- $1 = query from OLD model
    LIMIT 20
),
new_results AS (
    SELECT id, content, 1 - (embedding_v2 <=> $2) AS similarity
    FROM documents
    WHERE embedding_v2 IS NOT NULL
    ORDER BY embedding_v2 <=> $2  -- $2 = query from NEW model
    LIMIT 20
)
SELECT * FROM (
    SELECT * FROM old_results UNION ALL SELECT * FROM new_results
) combined ORDER BY similarity DESC LIMIT 20;
```

**When it wins:** small corpus, one-time migration you'll finish in days, ≤2 model versions in
the table's lifetime.

**When it hurts:**
- Schema sprawl: every migration adds columns and indexes. After three model changes the table
  has `embedding`, `embedding_v2`, `embedding_v3`, each with its own HNSW index.
- The re-embed step is an `UPDATE`, which under PostgreSQL's MVCC writes a full new heap tuple
  plus new TOAST chunks — see §12.7.6 for why this matters.
- Rollback is "null out a column on N million rows" + vacuum, not a clean drop.

---

**Option B — Separate tables per version**

A fresh table for each model version. The re-embed job is a pure INSERT pipeline.

```sql
CREATE TABLE document_embeddings_v2 (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id),
    embedding     vector(1536),
    embedded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_doc_emb_v2 ON document_embeddings_v2
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
```

Query during migration — same UNION pattern, but against two tables:

```sql
WITH old_results AS (
    SELECT d.id, d.content, 1 - (e.embedding <=> $1) AS similarity
    FROM document_embeddings_v1 e
    JOIN documents d ON d.id = e.document_id
    WHERE NOT EXISTS (
        SELECT 1 FROM document_embeddings_v2 e2
        WHERE e2.document_id = e.document_id
    )
    ORDER BY e.embedding <=> $1
    LIMIT 20
),
new_results AS (
    SELECT d.id, d.content, 1 - (e.embedding <=> $2) AS similarity
    FROM document_embeddings_v2 e
    JOIN documents d ON d.id = e.document_id
    ORDER BY e.embedding <=> $2
    LIMIT 20
)
SELECT * FROM (
    SELECT * FROM old_results UNION ALL SELECT * FROM new_results
) combined ORDER BY similarity DESC LIMIT 20;
```

**When it wins:** large corpus where re-embed takes days, you want zero interference with
production reads, and rollback = `DROP TABLE document_embeddings_v2`.

**When it hurts:**
- Table proliferation: `_v1`, `_v2`, `_v3` tables accumulate. Each needs its own indexes,
  vacuum config, and monitoring.
- JOINs required to get document content alongside similarity scores.
- If multi-model is permanent (not just a migration), the application layer must know which table
  to query for which model — routing logic lives in application code, not in the schema.

---

**Option C — Normalized embedding table with FK to a version registry (recommended default)**

This is the relational-database-native answer to the versioning problem: separate the *what* (the
document) from the *how it was embedded* (the model config), linked by foreign keys. One
`embedding_versions` table holds the model metadata. One `document_embeddings` table holds every
embedding ever produced, with a composite unique constraint on `(document_id, version_id)` — at
most one embedding per document per model version. New rows are always INSERTs, never UPDATEs.

This pattern is documented in production systems including the
[Pulso project](https://github.com/dinhostork/pulso/issues/25) (ArticleEmbedding with
`model_key` + `UniqueConstraint`), [Timescale's pgvector guide](https://github.com/timescale/docs/blob/latest/ai/key-vector-database-concepts-for-understanding-pgvector.md)
(document_embedding with FK to document), and is the schema recommended by the
[Lantern blog](https://lantern.dev/blog/async-embedding-tables) for async embedding generation
(separate embedding table to avoid TOAST bloat on the source table).

```sql
-- 1. Version registry — one row per embedding configuration.
--    This is the "schema" for your embedding layer.
CREATE TABLE embedding_versions (
    id              SERIAL PRIMARY KEY,
    model_name      TEXT NOT NULL,        -- "voyage-4", "text-embedding-3-small"
    model_version   TEXT NOT NULL,        -- "4.0", "2024-01-25"
    dimensions      INT NOT NULL,         -- stored dimension (after MRL truncation)
    full_dimensions INT NOT NULL,         -- model's native output dimension
    quantization    TEXT NOT NULL DEFAULT 'float32',  -- "float32","int8","binary"
    distance_metric TEXT NOT NULL DEFAULT 'cosine',   -- what the index uses
    input_type_doc  TEXT,                 -- "search_document" / "document" / null
    input_type_qry  TEXT,                 -- "search_query" / "query" / null
    context_method  TEXT NOT NULL DEFAULT 'none',  -- "contextual_retrieval","late_chunking"
    is_active       BOOLEAN NOT NULL DEFAULT false,  -- only ONE version serves queries
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes           TEXT,                 -- "migrating from v2, 67% complete"

    UNIQUE (model_name, model_version, dimensions, quantization)
);

-- 2. Embeddings table — one row per (document, version).
--    Always INSERT, never UPDATE. This is critical for avoiding MVCC bloat (§12.7.6).
CREATE TABLE document_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id      INT NOT NULL REFERENCES embedding_versions(id),
    embedding       vector(1024),         -- dimension matches version's config
    content_hash    TEXT NOT NULL,         -- hash of the source text that was embedded;
                                           -- enables idempotent re-embed (§12.7.7)
    embedded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (document_id, version_id)      -- at most one embedding per doc per version
);

-- 3. Partial index — ONLY on the active version. The planner skips inactive versions
--    entirely, so old/migrating embeddings don't bloat the hot search path.
CREATE INDEX idx_active_embeddings ON document_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE version_id = (SELECT id FROM embedding_versions WHERE is_active = true);
```

**The partial index is the key insight.** Instead of building a global HNSW index over all
embeddings (mixing model versions that shouldn't be compared), the `WHERE version_id = X` clause
means the index contains *only* vectors from the active model. The planner uses this index when
the query includes the matching filter. During migration, you build a second partial index for the
new version:

```sql
-- While migration is in progress: add an index for the new version too.
CREATE INDEX CONCURRENTLY idx_embeddings_v3 ON document_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE version_id = 3;
```

**Re-embedding is a pure INSERT pipeline:**

```sql
-- Find documents not yet embedded under the new version.
SELECT d.id, d.content
FROM documents d
WHERE NOT EXISTS (
    SELECT 1 FROM document_embeddings de
    WHERE de.document_id = d.id AND de.version_id = 3  -- new version
)
ORDER BY d.id
LIMIT 1000;

-- After embedding, insert (never update):
INSERT INTO document_embeddings (document_id, version_id, embedding, content_hash)
VALUES ($1, 3, $2, $3)
ON CONFLICT (document_id, version_id) DO NOTHING;  -- idempotent
```

**Query during migration — dual-query with version filtering:**

```sql
-- Application embeds query with BOTH models, then:
WITH old_results AS (
    SELECT de.document_id, d.content,
           1 - (de.embedding <=> $1) AS similarity  -- $1 = old model query vec
    FROM document_embeddings de
    JOIN documents d ON d.id = de.document_id
    WHERE de.version_id = 2  -- old version
      AND NOT EXISTS (
          SELECT 1 FROM document_embeddings de2
          WHERE de2.document_id = de.document_id AND de2.version_id = 3
      )
    ORDER BY de.embedding <=> $1
    LIMIT 20
),
new_results AS (
    SELECT de.document_id, d.content,
           1 - (de.embedding <=> $2) AS similarity  -- $2 = new model query vec
    FROM document_embeddings de
    JOIN documents d ON d.id = de.document_id
    WHERE de.version_id = 3  -- new version
    ORDER BY de.embedding <=> $2
    LIMIT 20
)
SELECT * FROM (
    SELECT * FROM old_results UNION ALL SELECT * FROM new_results
) combined ORDER BY similarity DESC LIMIT 20;
```

**Cutover — one UPDATE, zero re-embedding:**

```sql
-- Once all documents are embedded under v3 and golden-set evaluation passes:
BEGIN;
UPDATE embedding_versions SET is_active = false WHERE is_active = true;
UPDATE embedding_versions SET is_active = true WHERE id = 3;
COMMIT;

-- Drop the old version's partial index (the hot path no longer needs it).
DROP INDEX idx_active_embeddings;

-- Optionally: delete old version's embeddings to reclaim space.
-- Or keep them for rollback — they cost storage but enable instant revert.
DELETE FROM document_embeddings WHERE version_id = 2;
```

**Rollback — flip `is_active` back, old embeddings are still there:**

```sql
BEGIN;
UPDATE embedding_versions SET is_active = false WHERE id = 3;
UPDATE embedding_versions SET is_active = true WHERE id = 2;
COMMIT;
-- Old embeddings never left the table. Instant rollback, no re-embedding.
```

**When it wins:** production systems that will change models more than once, multi-tenant setups,
A/B testing embedding models, any case where you want rollback without re-embedding, systems
where the embedding layer is managed by a team that treats model versions as first-class schema.

**When it hurts:**
- JOIN required on every query (document_embeddings → documents). For most workloads the JOIN
  cost is negligible (it's a PK lookup after the ANN scan returns 20 IDs), but measure it.
- The partial index must be rebuilt when `is_active` changes (or you maintain one partial index
  per version and let the planner pick — slightly more storage, zero rebuild).
- More complex schema to set up than Options A/B. Worth it if you'll migrate more than once.

---

**Option D — Untyped vector column with expression + partial indexes per model_id**

pgvector supports an untyped `vector` column (no dimension specified) that accepts vectors of
*any* dimension. Combined with a `model_id` discriminator and partial expression indexes, this
stores everything in one table — even across dimension changes — and lets the planner pick the
right index per model. This is from
[pgvector's own README](https://github.com/pgvector/pgvector):

```sql
-- Single table, untyped vector column, model discriminator.
CREATE TABLE document_embeddings (
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    model_id      INT NOT NULL,           -- FK to embedding_versions if desired
    embedding     vector,                 -- untyped: any dimension accepted
    content_hash  TEXT NOT NULL,
    embedded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (document_id, model_id)
);

-- Expression + partial index: cast the untyped vector to the known dimension
-- for this specific model. The planner uses this index ONLY when the query
-- includes WHERE model_id = 2.
CREATE INDEX idx_emb_model_2 ON document_embeddings
    USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
    WHERE (model_id = 2);

-- Different model, different dimension — separate partial index.
CREATE INDEX idx_emb_model_3 ON document_embeddings
    USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
    WHERE (model_id = 3);
```

Query:

```sql
-- The planner picks idx_emb_model_3 because of WHERE model_id = 3.
SELECT de.document_id, d.content,
       1 - (de.embedding::vector(1536) <=> $1) AS similarity
FROM document_embeddings de
JOIN documents d ON d.id = de.document_id
WHERE de.model_id = 3
ORDER BY de.embedding::vector(1536) <=> $1
LIMIT 20;
```

**When it wins:** you need different dimensions in one table with minimal schema changes — add a
row to `embedding_versions`, create one partial index, done. No new columns, no new tables. The
cleanest DDL for ongoing multi-model coexistence.

**When it hurts:**
- The `::vector(d)` cast in every query and index definition is easy to get wrong — use the wrong
  dimension and pgvector silently truncates or pads with zeros depending on direction.
- No compile-time dimension safety: the untyped column accepts any length, so a pipeline bug that
  produces 768-dim vectors for a 1024-dim model inserts successfully and silently degrades
  retrieval (the partial index silently ignores rows that don't cast cleanly).
- Expression indexes can't use all pgvector optimizations available to natively-typed columns.

---

#### 12.7.4 Trade-off summary — which option when

| | **A: Separate columns** | **B: Separate tables** | **C: Normalized FK (recommended)** | **D: Untyped + partial index** |
|---|---|---|---|---|
| **Schema changes per migration** | ALTER TABLE add column + index | CREATE TABLE + index | INSERT into version table + index | INSERT into version table + index |
| **Write path** | UPDATE (MVCC bloat) | INSERT into new table | INSERT into same table | INSERT into same table |
| **Bloat risk (§12.7.6)** | **High** — UPDATE doubles heap + TOAST | **None** — insert-only | **None** — insert-only | **None** — insert-only |
| **Query complexity** | UNION, no JOINs | UNION + JOIN | UNION + JOIN + version filter | JOIN + version filter + cast |
| **Rollback** | Null column + vacuum | DROP TABLE | Flip `is_active`, old rows intact | Delete rows by model_id |
| **Multi-model permanent** | Poor (column sprawl) | OK (table sprawl) | **Best** (one table, N versions) | Good (one table, N indexes) |
| **Dimension safety** | Compile-time (`vector(d)`) | Compile-time | Compile-time | **Runtime only** (untyped) |
| **Best fit** | Quick one-off migration, small corpus | Large corpus, clean separation needed | **Production default** — multiple migrations, A/B, rollback | Advanced: mixed-dimension models, pgvector-native |

**Recommendation:** start with **Option C** unless you have a specific reason not to. It pays the
JOIN cost (negligible for most workloads — it's a PK lookup on 20 rows after the ANN scan) in
exchange for: insert-only writes (no MVCC bloat), instant rollback (flip a boolean), clean
multi-version coexistence (one table, N versions), and a version registry that serves as a
machine-readable audit trail of every embedding-layer change. Options A and B are simpler for
one-time migrations where you know you won't need rollback or multi-version permanently; Option D
is the pgvector-native alternative when you need mixed dimensions without schema changes.

#### 12.7.5 The dual-write pattern for zero-downtime migration

All four options above describe how to *store* vectors from two models. The dual-write pattern
describes how to *produce* them during migration without missing documents:

```
1. ENABLE DUAL WRITES — every new document that arrives during migration
   gets embedded with BOTH models and inserted into BOTH version slots.
   This prevents a gap where new documents only have old-model vectors
   and never get picked up by the backfill job.

2. BACKGROUND BACKFILL — a separate job scrolls through existing documents
   and embeds them with the new model. Keyed on (document_id, version_id)
   with ON CONFLICT DO NOTHING, so it's idempotent: dual-written documents
   that already have the new embedding are skipped, crashed batches resume
   from the last committed ID.

3. PROGRESS TRACKING — query the gap:
   SELECT COUNT(*) FROM documents d
   WHERE NOT EXISTS (
       SELECT 1 FROM document_embeddings de
       WHERE de.document_id = d.id AND de.version_id = 3
   );
   When this hits 0, the backfill is complete.

4. CUTOVER — flip the active version (Option C), swap the alias (Qdrant
   pattern), or switch the query routing. Dual writes can stop once the
   old version is dropped.

5. HOLD THE OLD VERSION — keep old embeddings queryable as rollback.
   Delete them only after the new version has survived real traffic long
   enough to trust (§12.4 step 6).
```

The dual-write pattern is documented across vector database vendors: Qdrant's
[named vectors migration](https://qdrant.tech/documentation/tutorials-operations/embedding-model-migration/)
(scroll + dual-write + alias swap), Google Cloud's
[zero-downtime embedding migration](https://medium.com/google-cloud/migrating-vector-embeddings-in-production-without-downtime-8a0464af6f55)
(shadow deployment + canary reads), and
[TianPan's reindex playbook](https://tianpan.co/blog/2026/07/05/retiring-an-embedding-model-reindex-without-downtime)
(insert-only backfill with content-hash idempotency). The mechanism differs by database; the
discipline is the same: dual-write new, backfill old, verify, cut over, hold rollback.

#### 12.7.6 PostgreSQL-specific: MVCC bloat and why INSERT beats UPDATE for re-embedding

This is the single most important PostgreSQL operational detail for embedding pipelines, and it's
the strongest argument for Options B/C/D over Option A.

PostgreSQL's MVCC (Multi-Version Concurrency Control) treats every `UPDATE` as a delete-of-the-old
+ insert-of-the-new. The old row version becomes a "dead tuple" that occupies space until
`VACUUM` reclaims it. For embedding vectors, this means:

```
One UPDATE of a 1536-dim float32 vector:
  old tuple:  1536 × 4 bytes = 6,144 bytes (dead, waiting for vacuum)
  new tuple:  1536 × 4 bytes = 6,144 bytes (live)
  total:      12,288 bytes temporarily, 6,144 bytes of dead space

Scale that to a corpus re-embed:
  500,000 documents × 6,144 bytes dead per UPDATE = 3.07 GB of dead tuples
  ...accumulating BEFORE autovacuum catches up.
```

This is worse than it looks because of **TOAST** (The Oversized-Attribute Storage Technique).
Vectors at 768 dimensions and above (~3 KB) exceed PostgreSQL's ~2 KB inline threshold and spill
to the TOAST relation — a separate physical table with **its own autovacuum schedule**. An UPDATE
that changes a TOASTed vector writes new TOAST chunks AND dead TOAST chunks, and the TOAST
table's bloat is invisible to `pg_stat_user_tables` (you need `pg_stat_all_tables` filtered to the
TOAST relation's OID). A re-embed job that UPDATEs 500K vectors can silently double a 400 GB
table overnight and leave autovacuum chewing through the TOAST relation for days.

**INSERT-only patterns (Options B/C/D) avoid this entirely.** New embeddings are INSERTs into rows
that never existed before — no dead tuples, no TOAST churn, no vacuum pressure. Old embeddings
sit untouched until you explicitly DELETE them after cutover, and that DELETE is a one-time
operation you can schedule during a maintenance window with aggressive vacuum settings, not a
continuous bloat source during the multi-day backfill.

If you must use Option A (UPDATE-based re-embedding), mitigate the bloat:

```sql
-- Aggressive autovacuum on the embedding table during re-embed.
ALTER TABLE documents SET (
    autovacuum_vacuum_scale_factor = 0.01,   -- vacuum at 1% dead tuples (default 20%)
    autovacuum_vacuum_cost_delay = 2,        -- less throttling during vacuum
    autovacuum_analyze_scale_factor = 0.01
);

-- After the re-embed job completes:
REINDEX INDEX CONCURRENTLY idx_documents_embedding_v2;  -- compact the HNSW index
VACUUM (VERBOSE, TOAST) documents;                       -- reclaim dead TOAST chunks
```

Even with these mitigations, UPDATE-based re-embedding puts sustained write pressure on the
production table (row-level locks per UPDATE) while INSERT-based patterns write to a cold path
that doesn't compete with read traffic.

#### 12.7.7 Idempotent re-embedding — content hash as a skip key

A re-embed backfill job will crash, get OOM-killed, hit API rate limits, or be interrupted by a
deploy. If restarting the job re-embeds documents it already processed, you're paying for the
same embedding API calls twice and generating unnecessary write IO. The fix is a content hash:

```sql
-- On INSERT, compute a hash of the source text that was embedded.
INSERT INTO document_embeddings (document_id, version_id, embedding, content_hash)
VALUES ($1, 3, $2, encode(sha256($3::bytea), 'hex'))
ON CONFLICT (document_id, version_id) DO NOTHING;

-- Backfill query: skip documents already embedded AND unchanged.
SELECT d.id, d.content
FROM documents d
LEFT JOIN document_embeddings de
    ON de.document_id = d.id AND de.version_id = 3
WHERE de.id IS NULL                    -- not yet embedded under this version
   OR de.content_hash != encode(sha256(d.content::bytea), 'hex')  -- content changed
ORDER BY d.id
LIMIT 1000;
```

The `content_hash` serves two purposes:
1. **Crash recovery** — if `(document_id, version_id)` already exists, `DO NOTHING` skips it.
   The job resumes from where it left off.
2. **Content drift detection** — if the source document was edited after embedding, the hash
   mismatch triggers a re-embed of that specific document, without re-embedding the entire corpus.

This pattern is documented in [LevelOp's versioning guide](https://levelop.dev/blog/vector-embedding-models-generation-versioning-drift)
as "key each vector on a content hash of its chunk plus the model version — if the hash and
version already exist in the store, skip it — turns a full rebuild into a cheap no-op when nothing
changed, and it turns a crash recovery into a resume rather than a restart."

#### 12.7.8 HNSW index parameters across model changes

HNSW's `m` (edges per node) and `ef_construction` (beam width during build) are not universal
constants — their optimal values depend on the embedding's dimensionality and the corpus size.
The general theory is in
[`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md);
the operational consequence for this chapter is: **changing the embedding model can change the
optimal index parameters, not just the data inside it.**

Rules of thumb (these are starting points to benchmark from, not final values):

```
Higher dimensionality → higher m may help (more edges to navigate a
    higher-dimensional graph) but costs more memory per node.
    Typical range: m=16 for dims ≤1024, m=24–32 for dims >1024.

Larger corpus → higher ef_construction helps build quality but slows
    index creation. Typical range: ef_construction=128 for <1M vectors,
    200–400 for 1M–10M, 400+ for >10M.

ef_search is a QUERY-TIME knob, not an index-build knob — you can
    change it per session without rebuilding anything.
    Higher ef_search = better recall, slower queries.
    Start at ef_search=100, raise until recall@k stops improving.
```

When you migrate models, rebuild the index from scratch with parameters appropriate to the new
dimensionality — don't assume the old model's `m=16` is still right for a model that outputs twice
as many dimensions.

#### 12.7.9 halfvec, bit, and sparsevec — pgvector's native quantized types

pgvector supports quantized storage types directly, which map to §7's quantization strategies
without needing to manage the byte packing yourself:

```sql
-- halfvec: float16 (2 bytes/dim instead of float32's 4 bytes). 2x savings.
-- Available since pgvector 0.7.0.
ALTER TABLE documents ADD COLUMN embedding_f16 halfvec(1024);

CREATE INDEX idx_f16 ON documents
    USING hnsw (embedding_f16 halfvec_cosine_ops);

-- bit: binary quantization (1 bit/dim). 32x savings vs float32.
-- Hamming distance search via <~> operator.
ALTER TABLE documents ADD COLUMN embedding_bin bit(1024);

CREATE INDEX idx_bin ON documents
    USING hnsw (embedding_bin bit_hamming_ops);

-- The bit column stores exactly d bits — no manual packbits needed,
-- pgvector handles the packing internally. The column width is d (bits),
-- not d/8 (bytes), in the type declaration.

-- sparsevec: sparse representation for high-dimensional, mostly-zero vectors.
-- Useful for SPLADE-style learned sparse representations, not for dense embeddings.
ALTER TABLE documents ADD COLUMN embedding_sparse sparsevec(30000);
```

The **two-stage rescore pattern from §7.3** implemented in pgvector:

```sql
-- Stage 1: fast Hamming search over binary index, retrieve 4x candidates.
WITH binary_candidates AS (
    SELECT id, embedding  -- keep the float32 column for rescoring
    FROM documents
    ORDER BY embedding_bin <~> $1::bit(1024)  -- $1 is binary-quantized query
    LIMIT 80  -- 4x the final top_k of 20
)
-- Stage 2: rescore the 80 candidates using float32 cosine similarity.
SELECT id, 1 - (embedding <=> $2) AS similarity  -- $2 is float32 query vector
FROM binary_candidates
ORDER BY embedding <=> $2
LIMIT 20;
```

This gives you binary-index speed for the corpus scan (the expensive part) and float32 precision
for the final ranking (the cheap part, because it only touches 80 rows).

### 12.8 The cascade — what changes when you change each parameter

This is the subsection that answers: "I changed X — what else breaks?" Every embedding parameter
change propagates downstream. The table below maps each change to its full blast radius.

| What you changed | What must be re-embedded | What index changes are needed | What query-time code changes | What you must NOT do |
|---|---|---|---|---|
| **Model (e.g. `ada-002` → `3-small`)** | Entire corpus — every vector | New index (likely new dimensions). New HNSW params if dims changed. New operator class if distance metric assumption changes. | Query embedding must use new model. `input_type`/template must be set correctly for new model (§3). Query-side caching (if any) must be invalidated. | Search old vectors with new model's query embedding — meaningless similarity (§12.2). |
| **Dimensions (MRL truncation, e.g. 1024 → 256)** | Entire corpus — every vector must be re-truncated and re-normalized (§6.2) | New `vector(256)` column or table. New index on that column. Old index at 1024 dims is a separate index. | Query vector must be truncated to same dimension and re-normalized. Similarity thresholds (if hardcoded, which they shouldn't be — §2.3) are no longer valid. | Mix 1024-dim and 256-dim vectors in the same column — pgvector rejects this at insert, but other stores may not. |
| **Quantization (e.g. float32 → binary)** | Not re-embedded, but every vector must be re-quantized from the float32 source | New column (`bit(d)` for binary, `halfvec(d)` for float16). New index with matching operator class (`bit_hamming_ops`). Keep float32 or int8 column for rescore stage. | Query must be quantized the same way for the first-stage search. Rescore pipeline (§7.3) must be implemented if not already. Similarity scores are no longer in the same range. | Drop the float32 vectors after quantizing — you need them for rescoring and for future re-quantization if you change calibration. |
| **Distance metric (e.g. cosine → dot product)** | Nothing — vectors stay the same | **Rebuild the index** with the new operator class. In pgvector, the operator class is baked into the index at creation time; you cannot change it without `DROP INDEX` + `CREATE INDEX`. | Query operator changes (`<=>` → `<#>` in pgvector). Score interpretation changes (cosine distance is `[0, 2]`, negative inner product is `(-∞, 0]`). | Assume scores from the old metric are comparable to scores from the new one — they aren't, even on the same vectors. Any hardcoded threshold breaks. |
| **`input_type` / template (§3)** | Entire corpus re-embedded if you were using the WRONG `input_type` and are fixing it. If you were already correct, nothing. | Index may need rebuild if the new embeddings' distribution is different enough that HNSW parameters should change (rare in practice). | Query-side `input_type` must be checked and corrected too — fixing document-side without fixing query-side (or vice versa) is a new bug. | Fix only one side. The document-side `input_type` and query-side `input_type` are a matched pair; changing one without the other creates a new asymmetry. |
| **Normalization (e.g. adding L2 norm where it was missing)** | Every un-normalized vector must be normalized in place or re-embedded | If switching from `vector_cosine_ops` to `vector_ip_ops` (which is now safe after normalization), rebuild the index. Otherwise no index change needed. | If query vectors were also un-normalized, normalize them too. If switching to dot-product operator, change query code. | Normalize new vectors while leaving old vectors un-normalized in the same index — this is the §2.2 bug, just introduced mid-corpus instead of at the start. |
| **Context method (e.g. none → contextual retrieval, §9.2)** | Every document must be re-processed (LLM call to generate context blurb) AND re-embedded with the prepended context | New vectors go into the index; old un-contextualized vectors should be replaced, not mixed. | Query embedding does NOT change — the query is embedded as-is; only the document side gets the prepended context. This asymmetry is intentional and correct. | Embed the query WITH a context blurb too — the context blurb is document-side only. |
| **Chunk boundaries (from `02`)** | Every affected document must be re-chunked AND re-embedded — new chunks may not have 1:1 correspondence with old chunks | Old chunk vectors must be deleted and replaced, not updated in place (chunk IDs change). Index is effectively rebuilt for the affected documents. | No query-side change, but result interpretation changes — different chunks may surface. | Leave old chunk vectors alongside new ones for the same source document — this doubles the document's representation in the index and biases retrieval toward it. |

### 12.9 Multi-model coexistence — when you run more than one embedding model by design

The migration in §12.4 and §12.7.3 treats multi-model as a *transitional* state you pass through
on the way to a single model. But some architectures run multiple embedding models *permanently*,
by design:

**Pattern 1 — Tiered models (Voyage 4-series shared space, §4.1).**
Embed the document corpus with the cheap model (`voyage-4-lite`), embed queries at request time
with the expensive model (`voyage-4-large`). Both land in the same coordinate space — no dual
index needed. This is the only case on this fact sheet where mixed-model vectors are comparable.

```sql
-- Single column, single index — the Voyage 4-series shared space makes this safe.
CREATE TABLE documents (
    id         BIGSERIAL PRIMARY KEY,
    content    TEXT NOT NULL,
    embedding  vector(1024),
    model_tier TEXT NOT NULL  -- "voyage-4-lite" (documents) or "voyage-4-large" (if re-embedded)
);

-- Query with voyage-4-large — works because same coordinate space.
SELECT id, 1 - (embedding <=> $1) AS similarity  -- $1 from voyage-4-large
FROM documents ORDER BY embedding <=> $1 LIMIT 20;
```

**Pattern 2 — Modality-specific models.**
Text documents embedded with a text model, images with a multimodal model, code with a code model.
Each modality has its own index (different dimensions, different distance properties), and the
query router decides which index to search based on query classification.

```sql
-- Three tables, three indexes, three embedding configs.
CREATE TABLE text_documents (
    id BIGSERIAL PRIMARY KEY, content TEXT,
    embedding vector(1024)  -- voyage-4 for text
);
CREATE TABLE image_documents (
    id BIGSERIAL PRIMARY KEY, image_url TEXT,
    embedding vector(1536)  -- embed-v4.0 for images
);
CREATE TABLE code_documents (
    id BIGSERIAL PRIMARY KEY, code TEXT, language TEXT,
    embedding vector(1024)  -- voyage-code-3 for code
);

-- Each gets its own HNSW index with params tuned to its dimensionality and corpus size.
```

The query router is load-bearing here: a text query sent to the image index (or vice versa)
doesn't crash — it returns results that are geometrically close but semantically unrelated,
because the embedding spaces encode different modalities. This is the multi-modal analog of the
§12.2 cross-model comparison bug, except it's by design rather than by accident, so it needs an
explicit routing layer rather than a per-vector version tag.

**Pattern 3 — A/B testing embedding models in production.**
Route a percentage of traffic to the new model's index, log recall metrics on both, and compare
over time before committing to a full migration. This is a more conservative version of §12.4's
playbook, stretched over weeks instead of done in a single cutover:

```sql
-- Application-level routing pseudocode.
-- model_assignment = hash(user_id) % 100
-- if model_assignment < 10:  → query new_index with new_model
-- else:                      → query old_index with old_model
-- Log: { user_id, model, query, results, clicks }
-- After 2 weeks: compare click-through and recall metrics by cohort.
```

This pattern catches quality differences that a 50-query golden set (§5.3) might miss — it
measures on real traffic, with real queries, at real scale — but it requires running two indexes
and two embedding-API call paths simultaneously, which is exactly the operational cost §12.4 was
designed to be fast enough to avoid. Use it when the corpus is large enough that a bad cutover
is expensive to reverse, and the golden set is too small to give you confidence on its own.

### 12.10 Version manifest — the artifact that ties it all together

All the per-vector metadata in §12.6, the index configurations in §12.7, and the cascade rules in
§12.8 converge into one operational artifact: a **version manifest** that captures the complete
state of your embedding layer at any point in time. This is the document you hand to an on-call
engineer when something looks wrong, and it's the document you diff against when planning a
migration.

```yaml
# embedding-manifest.yaml — checked into the repo, updated on every
# embedding-layer change, reviewed like any other schema change.

current_version: "v3"
created_at: "2026-03-15T00:00:00Z"

versions:
  v3:
    status: "active"           # active | migrating | deprecated | dropped
    model:
      name: "voyage-4"
      version: "4.0"
      vendor: "voyageai"
      input_type_document: "document"
      input_type_query: "query"
    representation:
      full_dimensions: 2048
      stored_dimensions: 1024  # MRL-truncated
      quantization: "float32"  # storage quantization
      binary_index: true       # binary column exists for first-stage search
      normalized: true
    index:
      database: "pgvector"
      distance_metric: "cosine"
      operator_class: "vector_cosine_ops"
      hnsw_m: 16
      hnsw_ef_construction: 200
      hnsw_ef_search: 100
    context:
      method: "contextual_retrieval"  # none | contextual_retrieval | late_chunking
      context_model: "claude-sonnet"  # LLM used for context generation
    table: "documents_v3"
    vector_column: "embedding"
    binary_column: "embedding_bin"

  v2:
    status: "deprecated"       # still queryable for rollback, not receiving new vectors
    model:
      name: "text-embedding-3-small"
      version: "2024-01-25"
      vendor: "openai"
      input_type_document: null    # OpenAI doesn't support asymmetric
      input_type_query: null
    representation:
      full_dimensions: 1536
      stored_dimensions: 1536
      quantization: "float32"
      binary_index: false
      normalized: true
    index:
      database: "pgvector"
      distance_metric: "cosine"
      operator_class: "vector_cosine_ops"
      hnsw_m: 16
      hnsw_ef_construction: 200
      hnsw_ef_search: 100
    context:
      method: "none"
    table: "documents_v2"
    vector_column: "embedding"

migration_log:
  - from: "v2"
    to: "v3"
    started: "2026-02-01"
    completed: "2026-03-15"
    reason: "asymmetric embedding support, MRL truncation for storage reduction"
    re_embed_cost: "$42.00"
    corpus_tokens: 2_100_000_000
    recall_delta: "+4.2% recall@20 (p<0.05, bootstrap CI on 50-query golden set)"
```

The manifest is version-controlled alongside the application code. A migration is a PR that
changes the manifest — reviewable, auditable, revertable. The `migration_log` section is the
institutional memory that prevents the next team from re-learning the same lessons: why you
moved, what it cost, and whether it was worth it.

Cross-references: the versioning discipline in §12.1 generalizes the same pattern this repo
already documents for observability schemas —
[`../sre-observability/34-schema-and-semantic-conventions-governance.md`](../sre-observability/34-schema-and-semantic-conventions-governance.md).
If the index shards across nodes, the dual-index cutover in §12.4 becomes a distributed-systems
problem in its own right —
[`../databases/12-replication-and-distributed-storage.md`](../databases/12-replication-and-distributed-storage.md).

---

## 13. Cost model for the representation layer

Symbolic formulas first, verified prices plugged in only where the fact sheet has them.

**One-time ingest** (embedding a corpus for the first time):

```
ingest_cost = corpus_tokens × price_per_token(model)
```

**Incremental ingest** (steady-state, new documents arriving):

```
daily_ingest_cost = daily_new_tokens × price_per_token(model)
```

**Per-query embedding** (embedding the query at request time — usually negligible per-query, but
multiplies by request volume):

```
query_cost = query_tokens × price_per_token(model) × queries_per_period
```

Query tokens are typically tiny (a handful to a few dozen tokens per query) relative to document
tokens, so at moderate request volumes this term is usually dwarfed by ingest cost — but it stops
being negligible at very high query volumes, and it's the term that scales with *traffic* rather
than *corpus size*, so it deserves its own line in a cost model rather than being folded into
"embedding cost" as one number.

**Full re-embed** (§12.5's formula, restated here for completeness):

```
re_embed_cost = total_corpus_tokens × price_per_token(new_model)
```

Verified prices available to plug into any of the above (OpenAI, `platform.openai.com/docs/pricing`,
checked 2026-08-08): `text-embedding-3-small` **$0.02**/1M tokens, `text-embedding-3-large`
**$0.13**/1M tokens, `text-embedding-ada-002` **$0.10**/1M tokens. Gemini's embedding-specific price
is explicitly **unverified** on this fact sheet — the pricing page scrape was ambiguous about which
row maps to the embedding endpoint, so it is deliberately omitted here rather than guessed; use
`price_per_token(gemini_model)` symbolically until you check the current pricing page yourself.
Voyage and Cohere per-token prices are likewise not on the fact sheet and are left as the same
symbolic term.

### 13.1 Storage cost dominates at scale — and that's why quantization and MRL are economic, not just clever

Compute cost (embedding calls) is a **one-time or incremental** cost proportional to *tokens
processed*. Storage cost is an **ongoing, monthly** cost proportional to *vectors held*, and it
compounds for as long as the index exists. At small corpus sizes, compute dominates the bill you
actually notice, because you pay it once, up front, in a single visible charge. At large corpus
sizes, storage dominates, because it's a recurring charge multiplied by every month the index
stays live — and unlike the one-time embedding charge, it doesn't stop.

This is the economic argument underneath §6 and §7, stated plainly: MRL truncation and quantization
aren't clever tricks for their own sake — they're the direct lever on the cost term that actually
grows without bound over the system's lifetime. An 8x MRL reduction (§6.3) or a 32x binary
quantization reduction (§7.1) applied once at ingest time keeps paying off every single month the
index exists, for the price of re-running the truncation/quantization step once. Compute
optimization saves you money once; storage optimization saves you money every month, forever, which
is why it's worth the engineering effort quantization actually requires (calibration datasets,
bit-packing correctness, the rescore pipeline) in a way that squeezing a one-time embedding bill
usually isn't.

Forward-reference: the full per-request/per-tenant/per-model cost attribution layer — where these
formulas turn into an actual dashboard — is `11-token-accounting-and-cost.md`'s job, and the
attribution *pattern* it reuses — turning a per-call cost into a per-tenant, per-model bill — is
already worked out in [`../sre-observability/31-finops-for-observability.md`](../sre-observability/31-finops-for-observability.md).
The label set you'd need for that dashboard (model version, tenant, chunk type) has the same
cardinality-explosion risk as any other high-cardinality metric labeling scheme, covered in
[`../sre-observability/18-cardinality-and-cost.md`](../sre-observability/18-cardinality-and-cost.md).

---

## 14. Anti-patterns

**1. Picking a model off the MTEB leaderboard.**
*Why it's tempting:* it's a ranked list, ranked lists feel like decisions made for you.
*What it costs:* you inherit contamination, saturation, and domain mismatch (§5) without knowing
it — your recall on your actual corpus could be meaningfully worse than a lower-ranked model's,
and you'd have no way to notice.
*Instead:* shortlist from the leaderboard, decide from your own golden set (§5.3).

**2. Embedding queries with the document `input_type`.**
*Why it's tempting:* it's one fewer branch in the code — same call, same params, for both sides.
*What it costs:* a silent retrieval-quality regression that no smoke test catches (§3.3).
*Instead:* always set the role-appropriate `input_type`/template, and add a test that asserts
query-side and document-side calls use different settings.

**3. Truncating dimensions without renormalizing.**
*Why it's tempting:* slicing a numpy array is one line; renormalizing feels like an extra,
skippable step.
*What it costs:* a broken cosine-vs-dot-product identity (§2.2), silently, on any index that uses
dot product internally.
*Instead:* always renormalize after truncation (§6.2) — cheap insurance, and a no-op if the vendor
already renormalized for you.

**4. Not recording the model version on the vector.**
*Why it's tempting:* the collection already "is" the v1 index, why tag every row.
*What it costs:* an unrecoverable partial migration the day you need to run two model versions
side by side (§12.1) — which is every migration.
*Instead:* version-tag at the vector/row level, non-negotiable, from day one.

**5. Letting truncation silently drop document tails.**
*Why it's tempting:* it's the vendor default; doing nothing is doing nothing.
*What it costs:* content in the back half of long documents becomes permanently unretrievable,
with no error anywhere to point at (§8.1).
*Instead:* force loud failure (`truncation=False`, `truncate=NONE`) in ingestion, and fix chunking
policy instead of tolerating silent drops.

**6. Comparing vectors across model versions.**
*Why it's tempting:* "it's basically the same model, one version newer" — it feels incremental.
*What it costs:* a plausible-looking, meaningless similarity score that degrades recall in a way
indistinguishable from "the model got worse" (§12.2).
*Instead:* treat every model version as a distinct, non-interoperable coordinate system unless a
vendor explicitly documents shared-space compatibility (Voyage 4-series is the one on this fact
sheet).

**7. Fine-tuning before fixing chunking.**
*Why it's tempting:* fine-tuning feels like the "serious" lever; chunking feels like a solved
problem you already did.
*What it costs:* an expensive, semi-irreversible training run that papers over a cheap, reversible
problem — and if the chunk fundamentally lacks the information a query needs (§9.1), no amount of
fine-tuning teaches the model to retrieve information that isn't there.
*Instead:* work the ordering in §10.1 — input_type, chunking/context, hybrid retrieval, reranking,
*then* fine-tune.

**8. Using `ada-002` in 2026.**
*Why it's tempting:* it's already integrated, migrating feels like unnecessary churn.
*What it costs:* worse retrieval (61.0% vs `3-small`'s 62.3% on OpenAI's own MTEB table) at a
**higher price** ($0.10 vs $0.02 per million tokens) — strictly dominated on both quality and cost
(§4, §12.5).
*Instead:* migrate to `3-small` or `3-large` using the playbook in §12.4; the migration cost is
almost certainly smaller than the ongoing overpayment.

**9. Benchmarking on the public dataset the model was trained on.**
*Why it's tempting:* public datasets are free, labeled, and ready to use.
*What it costs:* a benchmark number that measures memorization, not generalization — exactly the
contamination problem MTEB itself has (§5.2) — giving false confidence in a model that may not
transfer to your actual corpus.
*Instead:* evaluate on your own corpus with your own golden set; if you must use a public set,
verify it wasn't in the candidate models' training data (rarely possible with certainty, which is
itself an argument for the private golden set).

**10. Treating cosine and dot product as interchangeable on un-normalized vectors.**
*Why it's tempting:* the identity in §2.2 is easy to remember as "cosine and dot product are the
same thing" and forget the precondition.
*What it costs:* a ranking that silently rewards vector magnitude instead of semantic direction
(§2.2's second numpy example shows exactly this divergence).
*Instead:* normalize before using dot product as a proxy for cosine, always, and verify it in code
(§2.2's `assert` pattern) rather than trusting that upstream data is already normalized.

---

## 15. Mental models — the compressed set

1. **The embedding model is a schema decision, not a hyperparameter.** Its migration cost scales
   with corpus size, exactly like a database schema change (§1).
2. **"Semantic similarity" is not one thing — it's whatever the contrastive training pairs made
   it.** A model tuned on question-answer pairs and a model tuned on paraphrase pairs encode
   different notions of "close," even at the same dimensionality (§2.1).
3. **On normalized vectors, cosine, dot product, and inverse-Euclidean give the identical ranking
   — off normalized vectors, they don't.** Normalization is the precondition, not a formality
   (§2.2).
4. **A query and a document are different kinds of text; embedding them identically forfeits
   quality the model was explicitly trained to give you.** This is what asymmetric input types
   exist to fix (§3).
5. **MRL truncation is a trained property; PCA/SVD reduction is a post-hoc guess.** They look like
   the same operation and are not (§6.1).
6. **Truncating a normalized vector un-normalizes it — always re-normalize, unless the vendor
   states it does this for you.** Two models from the same vendor, one generation apart, can
   disagree on who owns this step (§6.2).
7. **Binary quantization trades precision for 32x space and up to 32x speed; rescoring buys most of
   the precision back.** ~92.5% retention alone, ~96% with rescore, on the one benchmark this
   fact sheet has for it (§7.1, §7.3).
8. **A packed binary vector has `d/8` bytes, not `d` bytes.** The single most common integration
   bug in binary-quantized vector databases (§7.2).
9. **Silent truncation on ingest is a data-loss bug wearing a config default's clothing.** Force
   loud failure in ingestion pipelines; tolerate silent truncation nowhere near where content gets
   permanently written to an index (§8.1).
10. **A chunk that lacks its own context is unretrievable no matter how good the model is.**
    Contextual retrieval, late chunking, and late interaction are three different answers to the
    same underlying problem, with real cost/determinism/storage tradeoffs, not a single obviously-
    correct fix (§9).
11. **Vectors from two model versions are not comparable, full stop — one documented exception on
    this whole fact sheet.** Voyage's 4-series shared space is the exception; assume every other
    model boundary requires a full re-embed (§12.2).
12. **Storage cost, not compute cost, is what makes quantization and MRL economically necessary at
    scale.** Compute is a one-time bill; storage is a monthly bill that compounds for the life of
    the index (§13.1).

---

## 16. Lab exercises

Every lab below produces an artifact and a number. Every number produced here is **rung 1 —
measured** (README §6): quote it with its dataset, its size, and its exact hit definition, every
time, or don't quote it at all. This document itself stays **rung 3 — studied** until these labs
have actually been run against a real corpus. Treat each lab as a test, not a one-off script —
[`../python-mastery/43-testing-strategy.md`](../python-mastery/43-testing-strategy.md) is how evals
get structured so they run again automatically instead of being re-derived by hand next time the
model changes. Where a lab produces a table of per-query results rather than a single number,
land it in DuckDB rather than a notebook variable —
[`../databases/21-in-process-olap-duckdb-chdb.md`](../databases/21-in-process-olap-duckdb-chdb.md)
is the intended home for eval results per README §5's P0 project, and every later lab that compares
runs (§16 Lab 8, and P0 generally) benefits from having prior runs already queryable there.

**Lab 1 — Verify the normalization identity empirically.**
*Goal:* prove §2.2's identity to yourself on real (not toy) embeddings, and prove it breaks without
normalization.
*Steps:* embed ~200 real chunks from any corpus you have with any model; compute cosine ranking,
dot-product ranking, and Euclidean ranking against a query embedding, on the L2-normalized vectors
— confirm all three orders match exactly. Then scale a subset of the vectors by an arbitrary factor
without renormalizing and show dot-product ranking diverges from cosine ranking.
*Artifact:* a short script plus a printed diff of the two rank orders (normalized vs
artificially-scaled).
*Success criterion:* exact rank agreement on normalized vectors; measurable, nonzero divergence
after breaking normalization.
*Time:* ~30 minutes.
*Unblocks:* nothing downstream directly — this is the foundational sanity check before trusting any
other lab's numbers.

**Lab 2 — The asymmetric-embedding A/B.**
*Goal:* measure the actual recall@10 cost of the §3 bug, on your own corpus — the cheapest real
experiment in this chapter.
*Steps:* using your P0 golden set (or a first draft of one, even 20 queries), embed every query
twice — once with the correct query-side `input_type`/template, once with the document-side
setting. Embed the corpus once, correctly, as documents. Measure recall@10 for both query
embeddings against the same document index.
*Artifact:* a table of recall@10, correct vs incorrect input_type, with the delta.
*Success criterion:* a measured, non-hypothetical recall@10 delta with a stated dataset and size —
even if the delta turns out small on your corpus, that's a real finding, not a failure.
*Time:* ~1 hour, given an existing golden set; ~1 afternoon if building the golden set from
scratch.
*Unblocks:* directly feeds P0 (eval harness) and is a template for every future model A/B.

**Lab 3 — MRL truncation sweep.**
*Goal:* find your corpus's actual dimensionality "knee," not the vendor's suggested one.
*Steps:* embed your corpus at full dimension with an MRL-supporting model; produce truncated,
re-normalized copies at 2048/1024/512/256 (adapt the specific set to your model's supported
lengths); measure recall@10 and total index size at each dimension.
*Artifact:* a recall-vs-dimension and size-vs-dimension table/plot.
*Success criterion:* an identified dimension where recall starts dropping meaningfully faster than
storage keeps shrinking — your own knee, stated with the corpus and query-set size it came from.
*Time:* ~2 hours (dominated by embedding-call time, not analysis).
*Unblocks:* `03-indexing-and-vector-stores.md` (the dimension you land on feeds directly into
index sizing).

**Lab 4 — Binary quantization + Hamming search + rescoring, from scratch.**
*Goal:* implement §7.1–§7.3 yourself in numpy, not through a vendor's `output_dtype` flag, so you
understand the mechanism before you rely on someone else's implementation of it.
*Steps:* binary-quantize your corpus embeddings (`np.packbits`), implement Hamming search
(XOR + popcount) for retrieval, implement the two-stage rescore against float32 query vectors, and
measure recall@10 with and without rescoring against your golden set.
*Artifact:* working quantize/search/rescore code plus a measured retention table (float32 baseline
vs binary-only vs binary+rescore).
*Success criterion:* your measured retention numbers are in the same *shape* as the HF-reported
~92.5%/~96% figures (same rough ballpark, not necessarily identical — different corpus, different
model) — and you can explain any large divergence.
*Time:* ~2–3 hours.
*Unblocks:* `03-indexing-and-vector-stores.md`, `04-retrieval-hybrid-and-reranking.md` (rescoring
is a reranking-stage pattern).

**Lab 5 — Prove the bit-packing arithmetic.**
*Goal:* make the §7.2 gotcha impossible to get wrong in your own pipeline.
*Steps:* for your model's actual dimensionality `d`, assert programmatically that
`packbits(binary_quantize(v)).shape == (n, d // 8)` (handling the `d` not divisible by 8 case with
`ceil`), and confirm your vector database's schema for the binary field matches that byte length,
not `d`.
*Artifact:* a passing assertion in code, plus a one-line note of what byte length your specific
vector DB config actually expects.
*Success criterion:* the assertion passes, and you've verified — not assumed — that your vector DB
agrees with it.
*Time:* ~20 minutes.
*Unblocks:* `03-indexing-and-vector-stores.md`.

**Lab 6 — Truncation audit.**
*Goal:* find out how much of your corpus your chosen model is silently damaging right now.
*Steps:* token-count every document in your corpus against your chosen model's context limit
(`tiktoken` for OpenAI's `cl100k_base`, or the equivalent for your model); count and report the
percentage exceeding the limit.
*Artifact:* a percentage, plus a list (or sample) of the offending documents — a natural fit for a
DuckDB table (`../databases/21-in-process-olap-duckdb-chdb.md`) if the corpus is large enough that
"a list" means thousands of rows, not a handful.
*Success criterion:* an exact measured percentage, stated with corpus size and the model's context
limit used — 0% is a valid and useful result.
*Time:* ~30–45 minutes.
*Unblocks:* `02-chunking-and-document-processing.md` (this number is a direct input to chunk-size
policy).

**Lab 7 — Contextual retrieval on 200 chunks.**
*Goal:* measure whether Anthropic's method (not their numbers) is worth it for you.
*Steps:* implement §9.2's contextualization prompt over 200 real chunks (with prompt caching on
the source documents, per Anthropic's own cost basis), measure recall@k before and after on your
golden set, and total the actual dollar cost you paid.
*Artifact:* a before/after recall table plus your measured $/M-document-tokens figure.
*Success criterion:* your own recall delta and your own dollar figure, explicitly compared against
Anthropic's reported 5.7%→3.7% and $1.02/M — with a stated verdict on whether it's worth it for
your corpus at your query volume.
*Time:* ~2–3 hours plus LLM API cost.
*Unblocks:* `02-chunking-and-document-processing.md`, `08-evaluation-methodology.md`.

**Lab 8 — Migration rehearsal.**
*Goal:* rehearse §12.4's playbook before you're forced to run it for real under a vendor
deprecation deadline.
*Steps:* version-stamp every vector in a test index; stand up a second, shadow index using a
different model (or even a different dimension/quantization setting on the same model, as a
lower-stakes rehearsal); run the golden set against both; produce a comparison report with a
bootstrap confidence interval over the recall@k delta (`../python-mastery/31-measurement-methodology.md`
for the bootstrap procedure itself).
*Artifact:* a comparison report: two recall@k numbers, their delta, and a confidence interval on
that delta.
*Success criterion:* a report you'd actually trust to make a cutover decision from — meaning the
interval is narrow enough, or the query-set large enough, to distinguish signal from noise.
*Time:* ~3–4 hours.
*Unblocks:* `12-serving-latency-and-caching.md`, and is the direct rehearsal for any future
real-vendor-deprecation event.

**Lab 9 — Full re-embed cost, computed for real.**
*Goal:* know the actual dollar number your embedding model decision costs, for your corpus, right
now.
*Steps:* count your corpus's total tokens; apply §13's formula against current verified prices for
your candidate models (or your current model, to know your switching cost).
*Artifact:* one number, with its derivation shown (token count × price, per candidate model) — if
you're computing this per-document rather than as one corpus-wide sum, a DuckDB query over the
same per-document token counts from Lab 6 (`../databases/21-in-process-olap-duckdb-chdb.md`) turns
this from a one-off calculation into something you can re-run after every ingest.
*Success criterion:* a written-down dollar figure you could hand to someone else as the cost of
"we're changing embedding models" — the real cost of the model decision, made concrete.
*Time:* ~15 minutes once corpus token count is known.
*Unblocks:* `11-token-accounting-and-cost.md`.

---

## 17. Interview questions and system design prompts

This section maps the chapter's content to the questions you'll actually face — in system design
rounds, ML-focused interviews, and architecture reviews. Each question below names the sections it
draws from and gives the answer structure an interviewer expects, not just the facts.

### 17.1 Conceptual questions — "explain X"

**Q: What is an embedding, and how is the notion of "similarity" defined?**
*Sections: §2.1, §2.2*
Lead with the one-sentence definition: a learned mapping `f: text → ℝ^d` trained via contrastive
learning so related inputs cluster and unrelated inputs separate. Then make the non-obvious point
that separates a strong answer from a textbook one: **"similarity" is not a universal property —
it's whatever the training pairs made it.** A model trained on (query, passage) pairs encodes a
different geometry than one trained on (sentence, paraphrase) pairs, even at the same
dimensionality. Mention InfoNCE loss and temperature `τ` only if the interviewer signals they want
math; the insight that matters is "the positive pairs define the space," not the formula.

Follow up with the metric identity: on L2-normalized vectors, cosine, dot-product, and
inverse-Euclidean rankings are identical. Breaking normalization breaks that identity — and it
breaks silently (§2.2). This is the bridge to a systems-thinking answer: "normalization is
load-bearing infrastructure, not a cosmetic step."

**Q: What are anisotropy and hubness, and why do they matter?**
*Section: §2.3*
Anisotropy: embeddings from transformer models cluster in a narrow cone rather than filling the
sphere uniformly, compressing the range of cosine similarities and making it hard to distinguish
"very relevant" from "somewhat relevant" by threshold alone. Hubness: certain points become
nearest neighbors to disproportionately many queries regardless of actual relevance — a
high-dimensional geometry artifact, not a model bug. Both argue against hardcoded similarity
thresholds (`cosine > 0.8 means relevant`) and in favor of rank-based metrics (recall@k, MRR,
nDCG). A strong answer names these as reasons to distrust a raw score and prefer set-based
evaluation (§8 in `08-evaluation-methodology.md`).

**Q: Explain the difference between symmetric and asymmetric embedding.**
*Section: §3*
A query and a document are different kinds of text. Asymmetric embedding gives the model a
signal about which role each input is playing — via `input_type` (Cohere, Voyage), string templates
(EmbeddingGemma), or free-text instructions (Qwen3-Embedding, Gemini embedding-2). The key
interview insight: **this bug fails silently.** The API call succeeds, a vector comes back, nothing
errors — retrieval quality just degrades, and nothing short of a golden-set recall measurement
catches it. Mention that OpenAI's `text-embedding-3-*` family doesn't expose the concept at all,
and that Gemini removed its typed `task_type` field between `embedding-001` and `embedding-2` and
replaced it with "put the instruction in the prompt yourself" — showing that even the same vendor
can break the API contract across generations.

**Q: What is Matryoshka Representation Learning (MRL)?**
*Section: §6*
MRL trains an embedding so that **prefixes are independently useful** — the model is supervised at
`O(log d)` prefix lengths during training, not post-hoc truncated. No additional forward pass
needed: embed once at full dimension, slice to any supported prefix. Contrast this with PCA/SVD,
which find directions of maximum variance in an already-trained space — OpenAI's own docs state
that even 10% PCA reduction "generally results in worse downstream performance." MRL avoids that
because the prefix property is baked into training, not retrofitted.

Critical follow-up: truncating a normalized vector un-normalizes it. You must renormalize after
slicing, or dot-product rankings silently break (§6.2). Two models from the same vendor (Gemini
`embedding-001` vs `embedding-2`) disagree on who owns this step.

**Q: Explain binary quantization and the rescore trick.**
*Sections: §7.1, §7.3*
Threshold each dimension at 0 → 1 bit per dimension instead of 32 → **32x** space reduction and
up to **32x** faster retrieval (XOR + popcount vs float multiply-adds). Retention: ~92.5% without
rescoring, ~96% with it (HF benchmark, 15 MTEB retrieval tasks, top-100, 4x rescore multiplier).
The rescore trick: retrieve `rescore_multiplier × top_k` candidates via cheap Hamming search over
binary vectors, then rescore that small candidate set against the **float32 query vector** and
int8/float32 document vectors. The asymmetry is the trick: one query stays full-precision (free),
millions of documents stay cheap (binary in RAM). Mention the bit-packing gotcha (§7.2): a
1024-dim model produces 128 bytes, not 1024 — the single most common integration bug.

### 17.2 System design round — "design a RAG pipeline"

These are the questions where everything in this chapter converges. The interviewer isn't asking
you to recite embedding facts — they're asking you to make connected decisions under constraints.

**Q: You're building a retrieval-augmented generation system for internal company documents.
Walk me through the embedding layer.**

Structure your answer as a chain of decisions, each justified by its downstream consequences:

```
1. MODEL SELECTION
   - Start with hard constraints: context length ≥ longest document,
     budget, open-weight requirement, multimodal if corpus has images/PDFs.
   - Shortlist 3–5 candidates from the landscape (§4), NOT by picking the
     top MTEB row — explain why leaderboards mislead (contamination,
     saturation, domain shift — §5.2).
   - Final decision: 50-query golden set on YOUR corpus, recall@k with
     bootstrap CI (§5.3).

2. ASYMMETRIC EMBEDDING
   - Set input_type/template correctly for query vs document sides (§3).
   - Add a test that asserts query-side and document-side calls use
     different settings — this bug is silent.

3. DIMENSIONALITY
   - Use MRL truncation, not PCA — trained prefix vs post-hoc guess (§6.1).
   - Find the "knee" via a truncation sweep on your corpus (Lab 3).
   - Always renormalize after truncation (§6.2).

4. QUANTIZATION
   - Binary for the hot path (in-memory, Hamming search), int8 on disk
     for rescore (§7.3–§7.5).
   - Calibrate int8 on a representative sample; recalibrate when corpus
     distribution shifts.
   - Stacking MRL + binary: reductions multiply (8x × 32x = 256x — §7.6).

5. CONTEXT AND TRUNCATION
   - Force loud failure on over-length input in ingestion (§8.1) —
     truncation=False (Voyage), truncate=NONE (Cohere), client-side
     token count for vendors with no flag.
   - Chunking policy must respect model's context ceiling.

6. REPRESENTATION GAPS
   - Contextual retrieval (§9.2) if using short-context model —
     LLM-generated blurb prepended before embedding.
   - Late chunking (§9.3) if using long-context model (Cohere 128k) —
     no LLM cost, fully deterministic.
   - Late interaction (§9.4, ColBERT) if precision-critical and budget
     allows per-token storage.

7. VERSIONING AND MIGRATION
   - Record model version per vector, not per collection (§12.1).
   - Dual-index migration: shadow-embed, golden-set comparison with CI,
     cut over only after new model clears old (§12.4).
```

**What interviewers are listening for:**
- You frame the embedding model as a schema decision with migration cost proportional to corpus
  size — not a config flag you tune later (§1).
- You mention silent failures: wrong `input_type`, un-renormalized truncation, silent context
  truncation. These show operational awareness.
- You quantify tradeoffs: MRL 8x reduction, binary 32x, stacked 256x — with the caveat that
  retention numbers are corpus-specific and must be measured.
- You name the one exception to "models aren't cross-compatible": Voyage 4-series shared space
  (§4.1).

**Q: How would you handle an embedding model migration in production?**
*Sections: §12.3, §12.4, §12.5*

Walk through the playbook step by step:

```
1. Dual index — new model alongside production, no traffic change.
2. Shadow-embed — re-embed corpus into new index. Watch for the §3 bug
   (wrong input_type in freshly written migration code). Treat this as
   a pipeline-reliability problem (at-least-once, idempotent writes,
   no silent document loss).
3. Golden-set comparison — recall@k on BOTH indexes, bootstrap CI over
   the 50-query golden set.
4. Cut over reads — only when new model clears old with interval
   accounted for.
5. Hold old index — keep it live and queryable as rollback until new
   index proves itself on real traffic, not just the golden set.
6. Drop old index — only after the confidence gate passes.
```

Compute the re-embed cost: `total_corpus_tokens × price_per_token(new_model)`. For a 500M-token
corpus on OpenAI: $10 on `3-small`, $65 on `3-large`, $50 on legacy `ada-002`. Note that
migrating off `ada-002` to `3-small` is both a quality upgrade AND a 5x price cut — "we already
use ada-002" has the cost argument backwards.

Three kinds of drift to name: model version drift (vendor deprecation), corpus drift (documents
change distribution over time), query drift (users start asking about new topics). All three
argue for periodic golden-set refresh.

### 17.3 Rapid-fire questions — short answers, deep signal

| Question | Strong answer (1–2 sentences) | Section |
|---|---|---|
| Why can't you incrementally migrate embedding models? | There's no meaningful in-between state — vectors from two models don't share a coordinate system, so a mixed index computes meaningless similarity scores. The migration unit is the entire corpus. | §1 |
| Cosine vs dot product — when does the choice matter? | On L2-normalized vectors, they rank identically. The choice matters only when normalization breaks — truncation (§6.2), quantization (§7), or pipeline bugs that skip the normalize step. | §2.2 |
| Why is `ada-002` a bad choice in 2026? | It's strictly dominated: worse retrieval (61.0% vs `3-small`'s 62.3% on OpenAI's own MTEB table) at 5x the price ($0.10 vs $0.02/M tokens). | §4, §14 |
| How do you evaluate an embedding model? | 50-query golden set on YOUR corpus, recall@k with bootstrap CI. Not MTEB — contamination, saturation, and domain shift make it a weak proxy (§5.2). | §5.3 |
| What's the difference between MRL and PCA for dimensionality reduction? | MRL trains the model so prefixes are independently useful; PCA fits variance directions post-hoc. OpenAI's docs: even 10% PCA reduction "generally results in worse downstream performance." | §6.1 |
| Binary quantization: how much space does it save? | 32x — one bit per dimension instead of 32. A 1024-dim model produces 128 bytes packed, not 1024. Add rescoring for ~96% retention (from ~92.5% without). | §7.1–§7.3 |
| What's the biggest silent failure in RAG pipelines? | Over-length document truncation at ingest — the vendor silently embeds the first half, the vector enters the index indistinguishable from a complete one, and the second half becomes permanently unretrievable. | §8.1 |
| When should you fine-tune an embedder? | Last resort, after fixing input_type (§3), chunking (§9), hybrid retrieval, and reranking (`04`). Fine-tuning is the most expensive, least reversible lever. | §10.1 |
| A chunk says "revenue grew 3%." Why might retrieval fail? | The chunk lacks context — no company name, no quarter, no year. The embedding faithfully encodes what's there; it can't encode what isn't. Fix: contextual retrieval (§9.2), late chunking (§9.3), or late interaction (§9.4). | §9.1 |
| How do you store vectors from multiple model versions safely? | Version-tag at the vector/row level, not the collection level. Refuse to compare vectors across versions at query time. The only documented exception: Voyage 4-series shared space. | §12.1 |

### 17.4 Architecture whiteboard prompts

These are the open-ended prompts interviewers use to see how you think through tradeoffs. For
each, the section references tell you where the facts live; the structure below tells you how to
present them.

**"Design the embedding layer for a legal document search system."**

Key moves:
- Legal documents are long → need a model with large context (Cohere 128k, or Voyage/Qwen3 at
  32k). EmbeddingGemma at 2k is disqualifying.
- Legal terminology is domain-specific → cross-lingual retrieval (§11.1) degrades on specialized
  vocabulary. If multilingual, golden set must include each language pair.
- Late chunking (§9.3) is a natural fit if using a long-context model — deterministic, no LLM
  cost. Contextual retrieval (§9.2) as the alternative if context window is shorter.
- Fine-tuning may be needed for domain-specific terminology (§10), but exhaust cheaper fixes
  first. Hard-negative mining with positive-aware filtering (§10.3) if you do.
- Cost at scale: legal corpora grow monotonically (documents are never deleted). Storage cost
  dominates (§13.1) → MRL truncation + quantization are not optional.

**"We have 100M documents and queries in 12 languages. Design the retrieval layer."**

Key moves:
- Multilingual embedding model is required (§11.1). Qwen3-Embedding ranks #1 on MTEB
  multilingual; Gemini `embedding-2` and Cohere `embed-v4` also support it.
- 100M documents at 1024 dims × 4 bytes = 400 GB float32. MRL to 256 dims = 100 GB. Binary
  quantization on top = ~3 GB in memory + int8 on disk for rescore. This is the §7.6 stacking
  argument — without it, the index doesn't fit in RAM.
- Golden set must cover every language pair served, not just English (§11.1).
- Migration at 100M scale is expensive — version-tag from day one (§12.1), rehearse the migration
  playbook (§12.4) before you need it.

**"Our recall dropped 5% after switching embedding models. Diagnose."**

Diagnostic checklist (ordered by likelihood and cost to check):
1. Wrong `input_type` on the query side in the new code (§3) — the #1 silent bug in migrations.
2. Truncation without renormalization (§6.2) — if the new model has different dimensionality.
3. Mixed-version vectors in the index (§12.2) — partial migration left old vectors alongside new.
4. Silent context truncation (§8.1) — new model has a shorter context limit than the old one.
5. int8 calibration mismatch (§7.4) — calibrated on old model's distribution, applied to new.
6. Corpus drift (§12.3) — the corpus changed since the golden set was built; the drop is real
   but not the new model's fault.

### 17.5 Common interview mistakes

**1. Saying "just use cosine similarity" without the normalization precondition.**
Interviewers will follow up: "what if the vectors aren't normalized?" If you can't explain
how dot-product ranking diverges from cosine ranking on un-normalized vectors (§2.2), you've
shown you memorized the metric without understanding the precondition.

**2. Treating MTEB as ground truth.**
Citing a leaderboard score as evidence a model is good for your task reveals you haven't
understood contamination, saturation, or domain shift (§5.2). The strong answer: "MTEB is a
shortlist, not a decision — the decision comes from a golden set on our own corpus."

**3. Recommending fine-tuning first.**
Fine-tuning is the most expensive, least reversible lever (§10.1). An interviewer asking about
retrieval quality wants to hear you exhaust the cheap, reversible fixes — input_type, chunking,
hybrid retrieval, reranking — before reaching for training infrastructure.

**4. Ignoring the migration cost of the embedding model decision.**
The embedding model is a schema decision (§1). If you propose a model without mentioning that
changing it later requires re-embedding the entire corpus at O(corpus-size) cost, you've missed
the most important operational property of the choice.

**5. Not mentioning silent failures.**
Strong candidates name specific ways things break without errors: wrong `input_type` (§3),
un-renormalized truncation (§6.2), silent context truncation (§8.1), mixed-version vectors
(§12.2). This is what distinguishes "read the docs" from "built and debugged a real system."

### 17.6 Behavioral and scenario questions

**Q: Tell me about a time you had to choose between two embedding models.**
*Framework:* constraints first (context length, cost, multimodal, open-weight), shortlist from
landscape, golden-set evaluation with recall@k, bootstrap CI to distinguish signal from noise.
The story should end with "and here's the measured recall difference on our corpus" — not
"the leaderboard said model A was better."

**Q: How would you convince your team to invest time in building a golden set?**
*Framework:* it's an afternoon of labeling, not a research project (§5.3). It's also not a one-off
— it's the foundation for every future model evaluation, every migration decision, and the eval
harness in `08-evaluation-methodology.md`. The cost of *not* having one is making schema-level
decisions (§1) based on vibes and leaderboard scores.

**Q: Your team wants to save money on vector storage. What do you recommend?**
*Framework:* two independent axes that multiply (§7.6). MRL truncation: 2048 → 256 = 8x (§6.3).
Binary quantization: 32x (§7.1). Stacked: 256x. But retention is corpus-specific — you need
the truncation sweep (Lab 3) and the quantization benchmark (Lab 4) before committing, because
the "knee" where recall drops faster than savings justify is different for every corpus.
Storage cost dominates at scale because it's a monthly recurring charge, not a one-time bill
(§13.1) — this is the economic argument that justifies the engineering effort.

---

## Rung ledger

This document is **rung 3 — studied** (README §6): it synthesizes verified vendor documentation
and published research, but nothing in it was produced by running code against a corpus of my own.
Every number carrying a source citation above is exactly as trustworthy as that citation and no
more — vendor-reported numbers are vendor-reported, leaderboard numbers are dated and secondary,
and Anthropic's contextual-retrieval figures are explicitly Anthropic's-method-on-Anthropic's-eval.
The moment the labs in §16 are run, their outputs become **rung 1 — measured**, and any number they
produce must always travel with its dataset, its size, and its exact hit definition attached — the
same discipline `../python-mastery/31-measurement-methodology.md` applies to every timing claim in
this repo. A recall@k number without that sentence attached is not a number worth keeping.
