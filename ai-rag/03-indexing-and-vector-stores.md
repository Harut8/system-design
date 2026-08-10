# 03 — Indexing and vector stores

> **Prerequisites:** [`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md)
> — **this chapter does not re-derive HNSW.** The insertion algorithm, the level-selection
> distribution, the neighbor-selection heuristic, the complexity analysis and the memory formula are
> all there, in depth. Read it first; this chapter starts where it stops, at the point where you
> have to choose parameters for a corpus you actually own.
> [`../databases/11-vector-search-internals.md`](../databases/11-vector-search-internals.md)
> (IVF, product quantization, the ANN tradeoff space more broadly),
> [`../databases/06-indexing-internals.md`](../databases/06-indexing-internals.md) (an index is a
> tradeoff, not a win — the single most load-bearing idea in this chapter),
> [`../databases/03-access-methods-and-table-scans.md`](../databases/03-access-methods-and-table-scans.md)
> (selectivity and access-method choice — §7's filtered-search problem *is* a selectivity problem
> wearing a different hat),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (§12's chunk
> arithmetic is the input to every sizing calculation here),
> [`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md)
> (§3 is a measurement protocol and it is worthless without this).
>
> **Feeds into:** [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md)
> (the candidate-generation stage of the cascade is this index, and §3's operating point is that
> stage's latency budget), [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§3.1's
> distinction between *index recall* and *eval recall* is a prerequisite for reading any number in
> that chapter correctly), [`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md)
> (§12's per-query cost is the non-token half of unit economics),
> [`12-serving-latency-and-caching.md`](12-serving-latency-and-caching.md) (§9's residency choice
> decides your p99 shape long before caching does),
> [`15-ingestion-pipelines-and-freshness.md`](15-ingestion-pipelines-and-freshness.md) (§8 is the
> index side of incremental update; `02` §9 was the chunk side),
> [`16-multi-tenancy-and-isolation.md`](16-multi-tenancy-and-isolation.md) (§7.6 — a namespace is a
> filter strategy, and usually the right one).
>
> **THESIS:** the index does not *produce* recall. Parsing, chunking and embedding decided what is
> findable at all; the index decides what fraction of that you actually get back, and at what price.
> So an ANN index is best understood as a **deliberate recall-loss budget with a dollar figure
> attached** — three sources of loss (graph approximation, quantization error, filter interaction),
> each individually measurable, each individually purchasable back with memory or latency.
>
> Two consequences follow, and they are the spine of this chapter. First: unlike almost every other
> number in this track, **index recall has exact ground truth available for free** — brute-force
> search over your own corpus. You never have to guess and you never have to trust a vendor
> benchmark. Second: **the number you measure unfiltered does not hold once a `WHERE` clause is
> attached**, and filtered queries are the overwhelming majority of real production traffic. Almost
> every published vector-database comparison measures the case that doesn't matter.

---

## Contents

1. [Thesis, restated as an engineering claim](#1-thesis-restated-as-an-engineering-claim)
2. [The four decisions hiding inside "which vector database"](#2-the-four-decisions-hiding-inside-which-vector-database)
3. [Measure index recall before you tune anything](#3-measure-index-recall-before-you-tune-anything)
4. [HNSW parameters in anger](#4-hnsw-parameters-in-anger)
5. [The memory arithmetic](#5-the-memory-arithmetic)
6. [Quantization, and the rescoring trick that makes it work](#6-quantization-and-the-rescoring-trick-that-makes-it-work)
7. [Filtered search — the actual hard problem](#7-filtered-search--the-actual-hard-problem)
8. [Updates, deletes, and index drift](#8-updates-deletes-and-index-drift)
9. [Where the bytes live: RAM, SSD, object storage](#9-where-the-bytes-live-ram-ssd-object-storage)
10. [pgvector versus a dedicated store](#10-pgvector-versus-a-dedicated-store)
11. [The 2026 landscape, as axes rather than a leaderboard](#11-the-2026-landscape-as-axes-rather-than-a-leaderboard)
12. [Cost model for the index layer](#12-cost-model-for-the-index-layer)
13. [Anti-patterns](#13-anti-patterns)
14. [Mental models — the compressed set](#14-mental-models--the-compressed-set)
15. [Lab exercises](#15-lab-exercises)

---

## 1. Thesis, restated as an engineering claim

`02` §1 laid out the chain of ceilings: what the parser extracted ⊇ what survived normalization ⊇
what a chunk boundary preserved ⊇ what the model could represent. This chapter is the last link:

```
    ⊇ what the index actually returns at k
```

The index is the only stage in that chain whose loss you can measure *exactly*, on your own data,
without labeling anything. That single property should reorganize how you work on it.

### 1.1 Two different words spelled "recall"

This is the most common source of confused arguments about retrieval quality, and it takes thirty
seconds to fix.

| | **Index recall** (this chapter) | **Eval recall@k** (`02` §11, `08`) |
|---|---|---|
| Ground truth | exact k-NN under the same distance metric | human/LLM relevance labels |
| Cost of ground truth | one brute-force pass, free | days of labeling |
| What a miss means | the ANN structure failed to find a vector it should have | the embedding model, chunking, or corpus failed |
| Typical target | 0.95–0.99 | whatever your product needs |
| Who can fix it | you, by turning a knob | nobody, quickly |

Index recall asks: *of the true nearest neighbours by cosine distance, how many did the graph
return?* It says nothing about whether those neighbours are relevant. A system with index recall
1.00 and an embedding model that doesn't understand your domain retrieves the wrong documents,
perfectly.

The two compose multiplicatively — approximately, and only approximately, because the vectors an
ANN index misses are systematically the harder ones (§3.4). If eval recall@10 with exact search
would be 0.80, and your index has recall@10 of 0.90, end-to-end you get *at most* 0.72 and usually
slightly less.

**The engineering consequence:** measure index recall first, get it to a stated target, and then
*hold it fixed* while you work on everything else. If index recall is drifting while you A/B an
embedding model, your A/B is measuring both and attributing all of it to the model.

### 1.2 Three sources of loss, and their independent knobs

| Loss source | Mechanism | Knob | Cost of buying it back |
|---|---|---|---|
| **Graph approximation** | greedy traversal terminates before finding the true nearest neighbours | `ef_search`, `M` | latency (linear-ish in `ef_search`), memory (`M`) |
| **Quantization error** | compressed vectors reorder distances near the boundary | bit depth, rescoring/oversampling | memory ↔ latency; rescoring converts one to the other |
| **Filter interaction** | the graph's connectivity assumptions break when most nodes are excluded | filter strategy (§7) | usually a different index topology, not a knob |

They are separable, and you should separate them when debugging. Recall dropped after you enabled
binary quantization? That is the second row. Recall is fine unfiltered and terrible with
`WHERE tenant_id = ?`? That is the third row and no amount of `ef_search` will fix it properly.

### 1.3 What this chapter deliberately does not contain

`../databases/11-hnsw-vector-search-internals.md` already covers the algorithm at 85KB of depth:
level selection, the `mL = 1/ln(M)` derivation, `SEARCH-LAYER`, the neighbor-selection heuristic,
complexity proofs, memory formulas, SIFT1M numbers. Re-deriving any of it here would be padding.

What is *not* in that document, and is here, is everything that only shows up when the index has an
owner: how to build ground truth for your own corpus, what filtered queries do to the recall you
measured, what happens to a graph after six months of deletes, where the bytes live and what that
does to p99, and the arithmetic that decides whether this fits in Postgres.

---

## 2. The four decisions hiding inside "which vector database"

Nearly every "Qdrant vs Milvus vs pgvector" argument is actually an argument about one cell in this
table, with the other three left implicit. Naming them separately makes the argument tractable.

| Decision | Options | What it actually controls | Reversible? |
|---|---|---|---|
| **Partitioning structure** | flat (brute force), IVF (clustering), graph (HNSW, Vamana/DiskANN), hybrid (IVF+graph) | the recall–latency curve's *shape*; build cost; update behaviour | rebuild |
| **Representation** | fp32, fp16, int8 scalar, 4/2/1-bit, product quantization | bytes per vector; where the recall ceiling sits before rescoring | rebuild |
| **Residency** | RAM, local NVMe, network SSD, object storage + cache | p50, p99, cold-start latency, and the dominant cost line | usually a migration |
| **Filter strategy** | post-filter, pre-filter + scan, in-graph predicate traversal, partitioning/namespaces, iterative scan | whether filtered queries work at all (§7) | sometimes a knob, often a topology change |

Two things about this table matter more than its contents.

**They compose, and the composition is where the surprises live.** Binary quantization plus
in-graph filtered traversal is not "the sum of two independent recall hits" — the filter shrinks the
candidate pool exactly where the quantizer's ranking is least reliable. Measure the configuration
you will ship, not the sum of its parts.

**Three of the four are effectively rebuilds.** That makes them schema decisions in exactly the
sense `01` §12 and `02` §9 mean it: their migration cost is O(corpus), and you should version-stamp
them (`index_version` alongside `chunker_version` and `embedding_model_version`) so that "which
vectors are in the old configuration?" is a query rather than a guess.

---

## 3. Measure index recall before you tune anything

This section is a protocol. Everything after it assumes you have run it.

### 3.1 You get ground truth for free — use it

For index recall, ground truth is *exact* k-NN under the same distance metric. That is a brute-force
scan. On 1M × 768 fp32 vectors that is roughly 3 GB of arithmetic per query — trivially
parallelizable, and you only need it for a few hundred queries, once.

```python
# Ground truth for index recall. Note what is NOT here: no labels, no judges,
# no golden set. This measures the index against the metric it claims to use.
import numpy as np

def exact_knn(corpus: np.ndarray, queries: np.ndarray, k: int) -> np.ndarray:
    """corpus: (N, d) L2-normalized. queries: (Q, d) L2-normalized.
    Returns (Q, k) array of corpus indices, best first.

    L2-normalized inputs mean inner product == cosine similarity. If your
    vectors are not normalized and your index uses cosine, normalize HERE too —
    computing ground truth under a different metric than the index uses is the
    single most common way to produce a recall number that means nothing.
    """
    out = np.empty((queries.shape[0], k), dtype=np.int64)
    # Chunk the corpus so the score matrix stays in cache-friendly territory.
    for q0 in range(0, queries.shape[0], 64):
        q = queries[q0:q0 + 64]
        scores = q @ corpus.T                      # (64, N)
        idx = np.argpartition(-scores, k, axis=1)[:, :k]
        # argpartition doesn't sort; sort the k survivors by score.
        rows = np.arange(idx.shape[0])[:, None]
        order = np.argsort(-scores[rows, idx], axis=1)
        out[q0:q0 + 64] = idx[rows, order]
    return out


def index_recall_at_k(truth: np.ndarray, got: np.ndarray, k: int) -> float:
    """Mean over queries of |truth_k ∩ got_k| / k."""
    return float(np.mean([
        len(set(t[:k]) & set(g[:k])) / k for t, g in zip(truth, got)
    ]))
```

**Use real queries, not sampled corpus vectors.** Sampling 500 documents from the corpus and using
their embeddings as queries is the standard shortcut and it inflates recall, because a document
vector is trivially its own nearest neighbour and sits in a dense region of the space by
construction. Real query vectors land in sparser regions, which is where greedy graph traversal is
worst. If you have no query log yet, use the golden set from `02` §11 / lab 4 — the queries there
are at least real.

### 3.2 The only comparable number is latency at fixed recall

Two configurations reported as "12 ms" and "9 ms" tell you nothing if one is at recall 0.91 and the
other at 0.99. Two reported as "recall 0.95" and "recall 0.97" tell you nothing if one takes 4 ms
and the other 60 ms. The recall–latency curve is the object; a single point on it is not a result.

So the reporting unit is: **p50 and p99 latency at recall ≥ target**, with the target stated. Sweep
`ef_search`, record both axes, and pick the operating point.

```python
# The sweep. This is the whole tuning loop for a graph index; everything else
# in §4 is about which curve you are sweeping along.
import time

def sweep_ef(client, queries, truth, k, ef_values, target_recall=0.95):
    rows = []
    for ef in ef_values:
        client.set_ef_search(ef)
        lat, got = [], []
        for qv in queries:
            t0 = time.perf_counter()
            ids = client.search(qv, k=k)
            lat.append((time.perf_counter() - t0) * 1000)
            got.append(ids)
        rows.append({
            "ef": ef,
            "recall": index_recall_at_k(truth, np.array(got), k),
            "p50_ms": float(np.percentile(lat, 50)),
            "p99_ms": float(np.percentile(lat, 99)),
        })
    feasible = [r for r in rows if r["recall"] >= target_recall]
    best = min(feasible, key=lambda r: r["p50_ms"]) if feasible else None
    return rows, best
```

Two measurement notes that `../python-mastery/31-measurement-methodology.md` argues at length and
that get skipped here constantly:

- **Warm up, and say whether you did.** The first queries against a fresh index pay page faults,
  cache misses, and in some stores a lazy index load. A cold p50 and a warm p50 can differ by two
  orders of magnitude (§9.2 has a published example). Both numbers are legitimate; conflating them
  is not.
- **Report a confidence interval on recall.** With 200 queries, a measured recall of 0.95 has a
  standard error of roughly ±0.015. A 0.5-point "improvement" from a tuning change is noise. Use
  enough queries that the difference you care about is larger than the interval, and bootstrap the
  interval rather than assuming one.

### 3.3 The default-settings trap

The most common broken vector-store comparison is: install two stores, insert the same vectors, run
the same queries, report QPS. This measures the two vendors' *default parameter choices*, not the
two systems.

Defaults genuinely differ. pgvector's HNSW defaults are `m = 16, ef_construction = 64`, with
`hnsw.ef_search` defaulting to 40. Other stores ship higher `ef_construction` and different
query-time defaults. A store that ships conservative defaults will look "slower and more accurate";
one that ships aggressive defaults will look "faster and less accurate". Neither fact is about the
implementation.

**The fix is mechanical:** tune each system to the same measured recall target on the same data,
then compare latency and cost. If a system cannot reach your recall target at any setting, that is
a real and reportable finding — and a much stronger one than a QPS number.

### 3.4 Averages hide the failures that matter

Mean recall over queries is the headline, but the distribution is where the operational risk is. An
index at mean recall 0.95 might be returning 1.00 for 90% of queries and 0.5 for 10% — and that 10%
is not random. Graph traversal fails hardest on:

- queries far from any dense cluster (rare topics, unusual phrasings);
- queries in high-hubness regions, where a few hub vectors dominate the candidate list (`01` §2.3);
- queries whose true neighbours are in a part of the graph reachable only through nodes your filter
  excluded (§7).

Report the fraction of queries below a per-query recall floor alongside the mean — e.g. "mean
recall@10 = 0.96; 4% of queries below 0.8". That second number is what turns into user-visible
"the search just doesn't find that document" reports, and the mean will never show it to you.

---

## 4. HNSW parameters in anger

The mechanism is in `../databases/11-hnsw-vector-search-internals.md` §7. This is the operational
delta: what you can change when, in what order, and what breaks.

### 4.1 The build/query split is the whole ergonomics story

| Parameter | When it's fixed | Changing it costs | What it buys |
|---|---|---|---|
| `M` (max connections per node) | build time | full index rebuild | a better recall–latency *curve*; more memory, permanently |
| `ef_construction` | build time | full index rebuild | a better graph, so the same `ef_search` reaches higher recall; build time only, no runtime memory |
| `ef_search` | per query | nothing — it's a session variable | movement *along* the curve |

This asymmetry dictates the tuning order, and it is the opposite of what people usually do:

1. **Pick `M` and `ef_construction` once, generously, and stop thinking about them.** They are
   expensive to revisit and the penalty for over-provisioning is bounded (memory for `M`, wall-clock
   for `ef_construction`). The penalty for under-provisioning is a rebuild.
2. **Sweep `ef_search` against your ground truth (§3.2).** This is free and reversible and it is
   where 90% of your achievable improvement lives.
3. **Only if the curve at your recall target is still too slow, go back and raise `M`** and rebuild.
   Then re-sweep, because the whole curve moved.

Doing it in the other order — rebuilding with different `M` values while leaving `ef_search` at a
default — burns hours per iteration to explore a dimension you could have explored for free.

### 4.2 `ef_search` must be at least `k`, and defaults do not know your `k`

`ef_search` is the size of the dynamic candidate list. It bounds how many results the search can
possibly return. pgvector's default is 40; if you ask for `LIMIT 100`, you get *at most* 40 rows
back from the index, silently, with no error. pgvector's own FAQ names this as the answer to "why
are there fewer results after adding an HNSW index?" — and adds two more causes: dead tuples (§8)
and filtering conditions (§7).

Treat `ef_search ≥ k` as a hard invariant and assert it in code:

```python
# Assert, don't hope. This is a correctness bug that presents as a quality bug,
# which is the worst kind to debug.
def search(client, qv, k):
    ef = client.get_ef_search()
    if ef < k:
        raise ValueError(
            f"ef_search={ef} < k={k}: the index physically cannot return k results"
        )
    return client.search(qv, k=k)
```

A useful default heuristic to start from, before you have swept: `ef_search = max(2*k, 100)`. Not
because 2× is principled, but because it is far enough above `k` that you are measuring the graph
rather than the truncation, which is what you want on the first measurement.

### 4.3 What `M` interacts with

`M` controls out-degree, and therefore both memory (§5) and how many distance computations a
traversal step costs. The useful operational statements:

- **Higher intrinsic dimensionality wants higher `M`.** A 3072-dimension model with genuinely
  high-dimensional structure needs more edges to keep the graph navigable than a 384-dimension model
  does. This is why "use `M = 16`" is bad advice stated without a model.
- **`M` does not need to grow with corpus size; `ef_search` does.** HNSW's search complexity is
  logarithmic in N, but the constant matters: at 100× the corpus, the same `ef_search` explores the
  same *absolute* number of candidates out of a 100× larger space, and recall drops. Re-sweep
  `ef_search` after any large ingest. This is the single most common cause of "retrieval quality
  degraded and nobody changed anything".
- **Clustered corpora are harder.** If your corpus has tight topic clusters with sparse regions
  between them (a very common shape for enterprise document sets), traversal between clusters
  depends on a small number of long edges. Higher `ef_construction` helps more than higher `M` here,
  because the neighbor-selection heuristic (`databases/11` §6.2) is what preserves those long edges.

### 4.4 Build cost is a real operational constraint

Index build is not free and it is on the critical path for every reindex — which, per `01` §12 and
`02` §9, you will do more often than you expect.

For pgvector specifically, three things dominate:

```sql
-- 1. The graph must fit in maintenance_work_mem or builds get dramatically slower.
--    pgvector emits an explicit NOTICE when it spills:
--      NOTICE: hnsw graph no longer fits into maintenance_work_mem after 100000 tuples
--      DETAIL: Building will take significantly more time.
--    Watch for it. Do not set this so high that the server OOMs.
SET maintenance_work_mem = '8GB';

-- 2. Parallel build workers default to 2. This is usually leaving a lot on the table.
SET max_parallel_maintenance_workers = 7;   -- plus the leader
SET max_parallel_workers = 16;              -- default is 8; raise if you raise the above

-- 3. Build the index AFTER bulk load, never before.
--    Inserting into an existing HNSW index pays graph maintenance per row.
```

Progress is observable, which matters when a build is the thing standing between you and a
deploy:

```sql
SELECT phase, round(100.0 * tuples_done / nullif(tuples_total, 0), 1) AS "%"
FROM pg_stat_progress_create_index;
-- HNSW phases: initializing, loading tuples
-- IVFFlat phases are reported in blocks rather than tuples
```

The general point beyond pgvector: **measure build wall-clock as a first-class number**, because it
sets your reindex cadence, and reindex cadence sets how fast you can iterate on everything upstream
in `01` and `02`. A 14-hour rebuild means one experiment per day.

### 4.5 The IVF alternative, and when it's the right call

pgvector also ships IVFFlat, and the tradeoff it names is worth internalizing because it recurs
across stores: *"faster build times and less memory than HNSW, but lower query performance in terms
of the speed–recall tradeoff."*

That is exactly the shape of the decision. IVF is the right call when build time or memory is the
binding constraint and you can tolerate a worse operating point — bulk-loaded corpora that are
rebuilt wholesale, or memory-constrained deployments. It is the wrong call when the corpus updates
continuously, because IVF's centroids go stale as the distribution shifts and there is no cheap
incremental fix.

pgvector's stated tuning rules for IVFFlat are unusually concrete and worth writing down because
getting `lists` wrong is the usual reason people conclude "IVF doesn't work":

- Build the index **after** the table has data (centroids are learned from it — an IVF index built
  on an empty table is meaningless).
- `lists` ≈ `rows / 1000` up to 1M rows; `sqrt(rows)` above 1M.
- `probes` ≈ `sqrt(lists)` as a starting point; `probes` is the runtime knob, analogous to
  `ef_search`, and defaults to 1 (which will look catastrophic if you never set it).
- Setting `probes = lists` gives exact search — at which point the planner stops using the index.

---

## 5. The memory arithmetic

This is arithmetic, not measurement, and it should be done on a napkin *before* you pick a store —
it eliminates most of the option space in five minutes.

### 5.1 The formula

```
bytes_per_vector ≈ (bytes_per_dimension × dimensions)   # the vector itself
                 + (edge_bytes × M × layer_factor)      # the graph
                 + id_and_payload_overhead              # ids, tombstones, metadata pointers
```

`../databases/11-hnsw-vector-search-internals.md` §9 derives the graph term properly. The
approximation that survives contact with a spreadsheet: **graph overhead is roughly `M × 8–10`
bytes per vector** (4-byte neighbour IDs, doubled edges on layer 0 in most implementations, plus a
small tail for upper layers, which hold about `1/(M-1)` of the nodes each).

pgvector publishes its per-type storage exactly, which makes it a good calibration reference:

| Type | Storage | Max dims (column) | Max dims (indexable with HNSW) |
|---|---|---|---|
| `vector` (fp32) | `4 × dimensions + 8` bytes | 16,000 | **2,000** |
| `halfvec` (fp16) | `2 × dimensions + 8` bytes | 16,000 | **4,000** |
| `bit` (binary) | `dimensions / 8 + 8` bytes | — | **64,000** |
| `sparsevec` | `8 × non-zero + 16` bytes | 16,000 non-zero | **1,000 non-zero** |

Note the third column: it is a hard constraint that decides architectures. A 3072-dimension
embedding **cannot** be HNSW-indexed as `vector` in pgvector. Your options are `halfvec` (fits, and
costs almost nothing in recall — see §6.2), binary quantization with rescoring (§6.5), Matryoshka
truncation to ≤2000 dims if the model supports it (`01` §5), or a different store. This one line in
the docs invalidates a lot of "we'll just use Postgres" plans, and it is better to find it now.

### 5.2 Worked table

Fully in-RAM HNSW, `M = 16`, ~150 bytes/vector of id + payload overhead, fp32:

| Chunks | 384-dim | 768-dim | 1536-dim | 3072-dim |
|---:|---:|---:|---:|---:|
| 1M | ~1.8 GB | ~3.3 GB | ~6.4 GB | ~12.6 GB |
| 10M | ~18 GB | ~33 GB | ~64 GB | ~126 GB |
| 100M | ~180 GB | ~330 GB | ~640 GB | ~1.26 TB |
| 1B | ~1.8 TB | ~3.3 TB | ~6.4 TB | ~12.6 TB |

*(Derivation, 768-dim/10M: `(4 × 768) + (16 × 9) + 150 = 3072 + 144 + 150 = 3366` bytes ×
10M ≈ 33.7 GB. Every input is stated; recompute it with your own `M` and payload size rather than
trusting the table.)*

Read the table for the crossovers rather than the values:

- **Below ~5M chunks at ≤768 dims**, everything fits on a commodity box and none of the exotic
  machinery in §6 or §9 is worth its complexity. Most RAG systems live here permanently.
- **Around 10–50M chunks**, dimension choice becomes the dominant cost lever — the 384→3072 column
  spread is 7×, far more than any store-vs-store difference. This is why `01`'s dimensionality
  discussion is a cost discussion.
- **Above ~100M chunks**, in-RAM fp32 stops being a sensible default and §6 (quantization) and §9
  (residency) stop being optimizations and become the architecture.

Cross-check this against `02` §12.2 before believing it: chunk *count* comes from chunk size and
overlap, and `02` §5.5's `1/(1-f)` overlap inflation lands directly in the left column here. A 20%
overlap decision made carelessly in the chunker is a 25% line item in this table, forever.

### 5.3 What the table leaves out, and when it matters

- **The payload.** Chunk text stored alongside vectors is frequently *larger* than the vectors. A
  512-token chunk is roughly 2 KB of UTF-8, against 3 KB for a 768-dim fp32 vector. Some stores keep
  payloads on disk by default and some keep them hot; check, because it moves the number by ~60%.
- **Build headroom.** Building typically needs the graph plus working memory; provisioning exactly
  the steady-state footprint means builds fail or thrash.
- **Replicas.** Multiply by the replica count. This is obvious and is nonetheless the most common
  factor-of-three sizing error.
- **The filesystem cache.** With SSD-resident indexes the OS page cache is doing the real work, and
  "how much RAM" becomes "how much of the hot set stays cached" — a different and harder question
  (§9.2).

---

## 6. Quantization, and the rescoring trick that makes it work

### 6.1 The one structural idea

Naive framing: "compress the vectors, accept worse recall." That framing makes quantization look
like a bad trade, and it is why people avoid it far past the point where it's free.

The actual framing: **use a cheap representation to decide where to look, and an expensive
representation to decide what to return.**

```
    ┌───────────────────────────────────────────────────────────┐
    │  traversal:  quantized vectors, in RAM, ~32× smaller      │
    │              → produce k × oversample candidates          │
    ├───────────────────────────────────────────────────────────┤
    │  rescoring:  full-precision vectors, from RAM or disk     │
    │              → re-rank those candidates exactly, return k │
    └───────────────────────────────────────────────────────────┘
```

Traversal touches thousands of vectors and must be fast and resident. Rescoring touches `k ×
oversample` vectors — a few hundred — and can afford to be exact and even to hit disk. The quantizer
only has to be good enough to keep the true neighbours *inside the oversampled candidate set*; it
does not have to rank them correctly. That is a much weaker requirement, and it is why aggressive
quantization works far better than the compression ratio suggests.

This is the same shape as the retrieval cascade in `04` §1 — cheap-and-wide then
expensive-and-narrow — one layer down the stack. Once you see it here you will see it everywhere.

### 6.2 The compression ladder

| Representation | Bytes/dim | Compression | Typical recall behaviour | Rescoring |
|---|---:|---:|---|---|
| fp32 | 4 | 1× | baseline | n/a |
| fp16 / bf16 (`halfvec`) | 2 | 2× | usually negligible loss | rarely needed |
| int8 scalar | 1 | 4× | small loss, well understood | optional |
| 4-bit | 0.5 | 8× | small loss at high dims | recommended |
| 2-bit | 0.25 | 16× | moderate | required |
| 1-bit (binary) | 0.125 | 32× | large without rescoring | **required** |
| Product quantization | tunable | up to ~64× | largest loss; slowest to score | required |

Two vendor-published anchors, quoted with their conditions because they are someone else's rung 1:

- **Qdrant** states binary quantization gives good accuracy with OpenAI `text-embedding-ada-002`
  (1536-dim, dbpedia dataset): **0.98 recall@100 with 4× oversampling**; and with Cohere
  `embed-english-v2.0` (4096-dim, Wikipedia): **0.98 recall@50 with 2× oversampling**. It also states
  the constraint plainly — binary quantization "is only efficient for high-dimensional vectors and
  requires a centered distribution of vector components," and that models with lower dimensionality
  may need different parameters.
- **Milvus** reports its `IVF_RABITQ` 1-bit index compressing the main index to 1/32 of original
  size, and with an optional SQ8 refinement layer holding ~95% recall at roughly 1/4 the original
  memory footprint, serving ~3× the QPS.

Both are vendor benchmarks on public datasets. They establish that the technique *can* work at those
dimensionalities; they do not establish what it does on your corpus, which §3 tells you how to find
out in an afternoon.

The dimensionality caveat is the load-bearing part. Qdrant's own docs explain why 2-bit and 1.5-bit
variants exist: *"One-bit compression resulted in significant data loss and precision drops for
vectors smaller than a thousand dimensions."* If you are running a 384-dim model, binary
quantization is probably not for you, and the useful rung on the ladder is int8 or 4-bit.

### 6.3 Newer quantizers: what actually changed

Naive binary quantization is "keep the sign of each component." That throws away magnitude entirely
and handles values near zero terribly — a component at +0.001 and one at +0.9 both become `1`.

The 2024–2026 generation attacks exactly that:

- **RaBitQ** (Gao, SIGMOD 2024) applies a random rotation before quantizing and normalizes relative
  to the dataset centroid, then projects onto the nearest hypercube vertex. The rotation spreads
  information evenly across dimensions so no single component dominates the error, and — the part
  that matters practically — it gives an *unbiased distance estimator with an error bound*, rather
  than a heuristic. Shipping in Milvus as `IVF_RABITQ`.
- **Qdrant's TurboQuant** offers 4-bit (8×, the default), 2-bit (16×), 1.5-bit (24×) and 1-bit (32×)
  encodings. Qdrant's own summary of when to use what: at 4× compression use scalar quantization; at
  8× use 4-bit TurboQuant; at 16×/24×/32× the binary and TurboQuant variants are comparable, with
  *"binary quantization faster, TurboQuant better recall"*; beyond that, product quantization only if
  memory dominates and accuracy and speed do not.
- **Multi-bit binary quantization** (Qdrant 1.15+) uses 2 bits to represent three buckets (`-1`, `0`,
  `1`) explicitly, which directly addresses the near-zero problem; 1.5-bit shares the zero bit
  between component pairs as a middle point.
- **Statistical binary quantization** (pgvectorscale) applies per-dimension statistics rather than a
  global sign threshold.

The durable takeaway is not the product names, which will rotate. It is: **quantization stopped
being a simple accuracy-for-memory trade and became a design space with error bounds**, and the
default rung moved from "int8 if you must" to "4-bit is a reasonable default at ≥768 dims."

### 6.4 Oversampling is the knob that actually matters

```
oversample = 2.0, k = 10  →  retrieve 20 candidates with quantized vectors,
                             rescore all 20 exactly, return the best 10
```

Qdrant exposes this directly as `oversampling` (since v1.3.0) with `rescore` (on by default for
binary and for TurboQuant's 1/1.5/2-bit modes). It is a *query-time* parameter, which puts it in the
same privileged category as `ef_search`: free to sweep, no rebuild, reversible.

The cost model is the thing to internalize:

- Rescoring cost is `k × oversample` full-precision distance computations. At k=10 and 4×
  oversampling that's 40 — nothing, if the vectors are in RAM.
- **If the full-precision vectors are on disk, it's 40 random reads**, which is emphatically not
  nothing and can dominate the query. Qdrant says this explicitly: rescoring "may decrease search
  speed, especially if the original vectors are stored on disk. In such cases, it is recommended to
  disable rescoring."
- So the real decision is three-way, not two-way: *quantized in RAM + full-precision in RAM* (fast,
  expensive), *quantized in RAM + full-precision on SSD* (cheap, slower tail, usually correct at
  scale), *quantized only, no rescore* (cheapest, and you must prove the recall is acceptable).

Sweep `oversample` on the same axes as §3.2 — recall against p50/p99 — and pick the point. It is a
15-minute experiment that routinely recovers most of a 32× memory saving.

### 6.5 Doing it in pgvector

pgvector has no built-in quantized index type, but it has the primitives, and the pattern is
instructive because it makes the two-stage structure explicit in SQL:

```sql
-- Half precision: usually the free win. Also the way past the 2,000-dim
-- HNSW index limit on the `vector` type.
CREATE INDEX ON items USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);

-- Binary quantization: index the sign vector, search by Hamming distance.
-- <~> is Hamming; binary_quantize() maps each component to a bit.
SELECT * FROM items
ORDER BY binary_quantize(embedding)::bit(3) <~> binary_quantize('[1,-2,3]')
LIMIT 5;

-- The version you actually ship: oversample on the binary index, rescore
-- exactly with the original vectors. This is §6.1's diagram, in SQL.
SELECT * FROM (
    SELECT * FROM items
    ORDER BY binary_quantize(embedding)::bit(3) <~> binary_quantize('[1,-2,3]')
    LIMIT 20                                    -- oversample = 4× for k=5
) sub
ORDER BY embedding <=> '[1,-2,3]'               -- exact cosine rescore
LIMIT 5;
```

The inner `LIMIT` is the oversampling factor. It is the one number to sweep.

### 6.6 Matryoshka truncation is a different lever — compose them

Truncating a Matryoshka-trained embedding from 3072 to 768 dimensions (`01` §5) also reduces bytes
per vector by 4×, and it is *not* quantization: it discards dimensions the model was trained to make
discardable, rather than reducing the precision of every dimension.

They compose. 3072 → truncate to 1024 → int8 is 12× smaller than 3072 fp32, and the two error
sources are largely independent. They also have different failure signatures: truncation degrades
gracefully and predictably (the model was trained for it), quantization degrades unpredictably near
distance boundaries. Prefer truncation first for that reason — and note that truncation shrinks the
*rescoring* vectors too, while quantization typically does not.

---

## 7. Filtered search — the actual hard problem

Everything above concerns unfiltered top-k. Almost no production query is unfiltered. Queries look
like *"nearest neighbours in tenant 4471, in documents this user may read, from the last 90 days,
excluding archived."* This is where vector indexes actually break, and where published benchmarks
are least informative.

### 7.1 The naive strategies and why each fails

**Post-filtering** — retrieve top-k by vector, then drop the non-matching ones:

```python
results = index.search(qv, k=10)
results = [r for r in results if r.tenant_id == 4471]   # ← may return zero rows
```

Correct only when the filter is weakly selective. At 1% selectivity, expect ~0.1 surviving results
out of 10. The failure mode is *silent under-return*, not an error, and it is worst exactly for the
smallest tenants — who then report "search doesn't work for us" while it works fine for everyone
else. Retrieving `k / selectivity` candidates to compensate means retrieving 1,000 candidates for
0.1% selectivity, which is no longer a cheap query.

**Pre-filtering with an exact scan** — resolve the predicate first, brute-force the survivors:

Correct always, and fast when the filter is *strongly* selective (a few thousand rows is a trivial
brute-force). It degrades linearly and becomes unusable in the middle of the selectivity range.

**Naive pre-filtering inside graph traversal** — traverse HNSW, skip non-matching nodes:

This is the one that looks right and is subtly broken. HNSW's connectivity guarantees assume you can
route *through* any node. When most nodes are excluded, the traversal can no longer reach whole
regions of the graph — the surviving subgraph is disconnected, and the search terminates early in a
local minimum. Recall collapses in a way that no `ef_search` increase reliably fixes, because the
path simply isn't there.

### 7.2 The selectivity curve — three regimes

| Selectivity (fraction matching) | What works | Why |
|---|---|---|
| **> ~20%** (weak filter) | plain ANN + post-filter | enough survivors; graph intact |
| **< ~0.1%** (strong filter) | pre-filter + exact scan | few enough rows that brute force is fast |
| **~0.1%–20%** (the middle) | **needs a real strategy** | too many to scan, too few to keep the graph navigable |

Qdrant's docs describe this exact shape and are blunt about the middle: *"On one hand, we cannot
apply a full scan on too many vectors. On the other hand, the HNSW graph starts to fall apart when
using filters that are too strict."*

The thresholds are corpus- and dimension-dependent — treat 0.1% and 20% as the right *order of
magnitude*, not as constants, and measure your own (lab 5).

### 7.3 The strategies that actually work in the middle

**In-graph predicate traversal (ACORN).** *ACORN: Performant and Predicate-Agnostic Search Over
Vector Embeddings and Structured Data* (Patel, Kraft, Guestrin, Zaharia) extends HNSW with
"predicate subgraph traversal" — emulating traversal over the subgraph induced by the predicate,
without building an index per predicate. The design goal in the name is the important part:
*predicate-agnostic*. Earlier work supported only restricted predicate sets (small equality sets),
which is useless for real filters that combine ranges, sets and booleans. Implementable as an
extension to existing HNSW libraries, which is why it has propagated into products.

**Extra graph edges from payload indexes (Qdrant's filterable HNSW).** Qdrant extends the HNSW graph
with additional edges derived from indexed payload values, so that traversal has routes that stay
inside the filtered subset. The operational catch is a real footgun and is worth putting in your
runbook: *"For the HNSW graph to be optimized for filtered search, it's highly recommended to create
all payload indices immediately after collection creation, before ingesting data. Extra edges for
the HNSW graph can only be generated after payload index creation."* Add a payload index after
loading 50M vectors and you get the index without the edges — filtered recall stays bad and nothing
tells you why.

**Label-aware graphs (filtered DiskANN).** pgvectorscale implements label-based filtered vector
search based on Microsoft's Filtered DiskANN research, attaching labels to graph nodes so traversal
can respect them. Same family of idea, different substrate (disk-resident Vamana rather than
in-memory HNSW).

**Iterative index scans (pgvector 0.8+).** A different and pleasingly simple answer: keep scanning
more of the index until enough post-filter survivors accumulate.

```sql
-- Strict: results in exact distance order.
SET hnsw.iterative_scan = strict_order;

-- Relaxed: slightly out of distance order, better recall. Usually the right choice
-- for RAG, where a reranker (04 §7) is about to reorder everything anyway.
SET hnsw.iterative_scan = relaxed_order;

-- The safety valves — an iterative scan that never finds enough matches must stop.
SET hnsw.max_scan_tuples = 20000;      -- approximate; does not affect the initial scan
SET hnsw.scan_mem_multiplier = 2;      -- try raising this if max_scan_tuples doesn't help

-- IVFFlat has the analogous pair: ivfflat.iterative_scan, ivfflat.max_probes.
```

pgvector's framing of the underlying problem is the clearest one-sentence statement of it anywhere:
*"With approximate indexes, queries with filtering can return less results since filtering is
applied after the index is scanned."* Everything in this section is a different answer to that
sentence.

Note the `strict_order` / `relaxed_order` choice is genuinely yours to make and RAG usually wants
relaxed: you are feeding a reranker, and exact distance ordering of the candidate set has no value
downstream. If you need strict ordering with relaxed scanning, pgvector's docs point at a
materialized CTE.

**Partitioning — the escape hatch that beats all of the above.** If the filter is
low-cardinality and stable (tenant, region, language, document collection), do not filter: *put the
vectors in different indexes.* One index per tenant turns a 0.1%-selectivity filtered query into an
unfiltered query against a small index. Recall is a solved problem again, and the operating point
you measured in §3 actually applies.

The cost is many small indexes — per-index overhead, more objects to manage, and a rebalancing
problem when one tenant is 1000× the others. Stores expose this as namespaces, collections, or
partitions; pgvector's version is partial indexes, which its docs recommend explicitly *"if filtering
by only a few distinct values."* `16-multi-tenancy-and-isolation.md` is where this decision gets
made properly; the point here is that it is an *index* decision, not just an isolation one.

### 7.4 The measurement rule this section exists to establish

**Measure recall per selectivity band, with the filter applied to ground truth.**

The ground truth for a filtered query is the exact k-NN *among matching rows only*. Comparing
filtered ANN results against unfiltered ground truth produces a meaningless number — and it is the
default thing that happens if you reuse the §3.1 harness without thinking.

```python
def filtered_ground_truth(corpus, mask, queries, k):
    """mask: (N,) boolean — rows matching the predicate.
    Returns indices into the ORIGINAL corpus, so results are comparable to
    what the index returns.
    """
    idx = np.flatnonzero(mask)
    sub = corpus[idx]
    local = exact_knn(sub, queries, min(k, len(idx)))
    return idx[local]
```

Then report a table, not a number:

| Selectivity band | Queries | recall@10 | p50 ms | p99 ms |
|---|---:|---:|---:|---:|
| 100% (unfiltered) | 200 | 0.98 | — | — |
| 10–50% | 200 | | | |
| 1–10% | 200 | | | |
| 0.1–1% | 200 | | | |
| < 0.1% | 200 | | | |

If your production traffic is 80% in the 0.1–1% band, the unfiltered row is decoration. Weight the
bands by your actual query mix — which requires knowing your actual query mix, which is itself worth
the twenty minutes it takes to find out.

---

## 8. Updates, deletes, and index drift

Every benchmark you will read measures a freshly built index. Yours will be six months old.

### 8.1 Deletion is not deletion

You cannot cheaply remove a node from an HNSW graph. Removing it would orphan the edges that route
*through* it, and repairing that means re-running neighbor selection for every node that pointed at
it. So every implementation soft-deletes: the node stays in the graph, marked dead, and is filtered
out of results.

Three consequences, in increasing order of how much they surprise people:

1. **Deleted vectors still cost memory.** Delete 30% of your corpus and the index does not shrink.
2. **Deleted vectors still cost traversal.** The search still walks through them; they are still
   distance computations.
3. **Deleted vectors consume your `ef_search` budget.** They occupy slots in the dynamic candidate
   list before being filtered out. At 30% tombstones, an `ef_search` of 100 is doing the work of
   about 70. **Recall degrades over time with no configuration change and no deploy** — which makes
   it a genuinely hard incident to diagnose, because the usual first question ("what changed?") has
   the answer "nothing."

pgvector surfaces this in its FAQ as one of the causes of fewer-than-expected results ("dead tuples")
alongside `ef_search` and filtering. In Postgres the mechanism is familiar — `VACUUM` reclaims dead
tuples — and the operational advice is the standard one: watch `n_dead_tup`, and be aware that a
heavily updated vector table needs more aggressive autovacuum settings than its row count suggests,
because each dead tuple is large.

### 8.2 The segment/compaction model, and why it's everywhere

Most dedicated stores solve this the way LSM trees solve it
(`../databases/13-lsm-trees-and-compaction.md` is the reference and the analogy is nearly exact):

```
    writes → in-memory buffer → sealed immutable segment (own HNSW graph)
                                          │
                                          ▼
                               background compaction:
                          merge segments, drop tombstones, rebuild graph
```

Queries fan out across segments and merge results. This makes writes cheap and deletes eventually
free, at the cost of:

- **Query latency proportional to segment count.** More segments, more graphs to traverse. Your p99
  is partly a function of how far behind compaction is.
- **Compaction competing with queries** for CPU and IO. The classic 3am latency spike.
- **Recall varying with segment structure**, because per-segment top-k then merge is not identical to
  global top-k. Usually a small effect; occasionally not.

The operational ask is modest and almost always skipped: **monitor segment count and tombstone ratio
as first-class metrics**, and alert on them. They are leading indicators for a class of quality
regression that has no other early signal. `../sre-observability/12-alerting.md` for how to set the
thresholds without generating noise.

### 8.3 Freshness versus recall

New vectors are not searchable until they are indexed. Every store makes a different choice about
what happens in between, and the choice is usually configurable and usually left at a default nobody
chose deliberately:

- **Index immediately on insert** — searchable at once, expensive writes, and bulk loads crawl.
- **Buffer, then bulk-index at a threshold** — fast writes, and a window where new documents are
  invisible or served by a linear scan of the buffer.
- **Index asynchronously** — fast writes, eventual searchability, and a read-your-writes problem
  that surfaces as "I just uploaded that document and search can't find it."

That last one is a product decision disguised as a configuration flag. If your product says "your
document is ready" the moment upload completes, you have promised read-your-writes and need to
either index synchronously or scan the buffer. Decide it deliberately and write the decision down;
`15-ingestion-pipelines-and-freshness.md` is where the staleness SLO gets defined.

### 8.4 The rebuild path is not optional

Given §8.1–8.3, periodic full rebuild is part of operating an index, not an admission of failure.
The pattern is the same shadow-index-and-swap from `01` §12 and `02` §9:

1. Build the new index alongside the old, from the persisted intermediate artifacts (`02` §2 —
   this is why you kept them).
2. Run the §3 recall harness against **both**. This is the whole reason the harness exists: it turns
   "the rebuild looks fine" into a number.
3. Run the golden set (`02` §11) against both, so you catch quality regressions that recall misses.
4. Swap atomically. Keep the old index until you've watched the new one under real traffic.

Rebuild cadence is set by §4.4's build wall-clock and by how fast tombstones accumulate. Measure
both and you can state the cadence instead of guessing it.

---

## 9. Where the bytes live: RAM, SSD, object storage

Three architectures. This choice moves cost by an order of magnitude and p99 by two, and it is
usually made implicitly by picking a product.

### 9.1 The three shapes

| | **All in RAM** | **SSD-resident (DiskANN family)** | **Object storage + cache** |
|---|---|---|---|
| Query p50 | ~1–10 ms | ~5–30 ms | ~10–20 ms warm, ~1 s cold |
| Cost driver | RAM $/GB-month | NVMe $/GB-month | S3 $/GB-month (~1–2 orders cheaper) |
| Cold start | index load time | mmap, fast | first query pays object-storage reads |
| Scales to | RAM you can buy | disk you can buy | effectively unbounded |
| Best for | one hot corpus, latency-critical | large single corpus, cost-sensitive | many namespaces, spiky/sparse access |
| Worst for | large corpora | very high QPS | latency-critical uniform traffic |

**The SSD family** (DiskANN/Vamana and descendants) is not "HNSW on disk" — it is a graph designed
so that traversal touches few enough pages to make SSD viable, with a compressed in-memory
representation guiding the search and full vectors read from disk only when needed. That is §6.1's
two-stage structure again, with the storage hierarchy as the second stage. pgvectorscale's
StreamingDiskANN brings this shape into Postgres.

**The object-storage family** is the genuinely new architecture of the last few years, and it exists
because of a workload observation: many RAG systems are not one big corpus with uniform traffic, they
are *thousands of small per-tenant corpora with wildly uneven access*. Keeping 10,000 tenant indexes
resident in RAM when 200 are active at any moment is paying for 98% idle capacity.

turbopuffer's published architecture is a clean illustration of the tradeoffs, and its numbers show
the shape well: data lives on object storage, is cached on NVMe after first access, and queries route
to the node holding the cache. Its stated figures — *first query to a namespace p50 = 874 ms for 1M
documents; subsequent cached queries p50 = 14 ms for 1M documents* — make the cold/warm cliff
explicit rather than hiding it. Writes go through a WAL on object storage: *p50 = 165 ms for 500 kB*,
*~10,000+ vectors/sec*, with *one WAL entry per namespace per second* (concurrent writes group-commit,
so a write can wait up to a second).

Those are vendor figures for one system, quoted with conditions. What is durable is the *shape*: a
~60× cold/warm ratio, writes measured in hundreds of milliseconds, and a per-namespace commit
cadence. Any object-storage-native design will have that shape; the constants will differ.

### 9.2 Cold start is a product decision

The cold/warm cliff is the defining property of tier three and it must be designed around, not
discovered in production. The options are the usual cache-warming ones and they are all *product*
choices:

- **Pre-flight/warm queries** on a signal that predicts real traffic — user opens the app, session
  starts, a scheduled job fires. turbopuffer explicitly supports this pattern.
- **Pinning** the namespaces you know are hot.
- **Accepting it** and telling the user, for genuinely cold-path workloads (a quarterly report over
  an archive) where a one-second first query is fine.

The mistake is measuring p50 on a warm cache in a benchmark, shipping, and then discovering that your
actual traffic pattern — one query per tenant per hour — means *every* query is cold. Your benchmark
measured a case that never occurs. Sample your real inter-arrival times per namespace before
believing any warm number.

### 9.3 The cost inversion

At 100M × 768-dim fp32, ~330 GB (§5.2):

- **RAM:** roughly the memory of a large instance, priced accordingly, continuously.
- **NVMe:** perhaps an order of magnitude cheaper per GB-month, with a latency penalty measured in
  milliseconds.
- **Object storage:** roughly two orders of magnitude cheaper per GB-month than RAM, plus request
  costs, plus a cache tier sized to the *working set* rather than the corpus.

The inversion that decides the architecture: **if your working set is a small fraction of your
corpus, tier three is dramatically cheaper; if it is most of your corpus, tier one is dramatically
faster and the cost gap narrows.** So the number to measure before choosing is not corpus size — it
is *what fraction of your namespaces are touched in a five-minute window*. That is a query against
your access logs and it should precede the architecture decision, not follow it.

---

## 10. pgvector versus a dedicated store

The most common real decision in this space. It deserves to be made on thresholds rather than vibes.

### 10.1 What Postgres gives you that is easy to undervalue

- **Transactions across vectors and metadata.** Insert a chunk, its vector, its ACL row, and its
  audit record atomically. In a two-system architecture, this is a distributed-transaction problem
  you will solve badly.
- **Real joins and real predicates.** Filters are SQL — arbitrary boolean expressions over indexed
  columns, joins against permission tables, subqueries. Compare that with a payload filter DSL. §7's
  problem does not go away, but you can express the predicate.
- **One system to operate.** Backups, PITR, replication, monitoring, access control, and an on-call
  rotation that already knows it. This is the largest and least-quantified term in the comparison,
  and it is why the honest default for a team under ~10M chunks is "use Postgres."
- **Your data is already there.** No sync pipeline, no dual-write consistency problem, no "the vector
  store and the database disagree about which documents exist" incident.

### 10.2 What it costs you

- **The 2,000-dimension HNSW index limit on `vector`** (§5.1). Workable via `halfvec` (4,000), binary
  quantization (64,000), subvector indexing, or truncation — but it is a real constraint that shapes
  the design, and it is better encountered here than in week three.
- **Memory pressure is shared.** The HNSW graph competes with the buffer cache that the rest of your
  application depends on. A vector workload can quietly degrade unrelated OLTP queries — a failure
  mode a separate store cannot have.
- **Build time and vacuum behaviour** on large tables (§4.4, §8.1).
- **No native sparse/lexical scoring integrated with vector scoring.** Postgres full-text search
  exists and works, but you are assembling hybrid retrieval yourself (`04` §5) rather than getting an
  RRF retriever from the engine.
- **Single-node write scaling.** Read replicas help reads. Sharding vectors across Postgres nodes is
  a project.

### 10.3 The middle path

`pgvectorscale` and `VectorChord` are extensions that add dedicated-store index technology *inside*
Postgres. pgvectorscale specifically adds StreamingDiskANN (disk-resident graph), statistical binary
quantization, and label-based filtered search from Microsoft's Filtered DiskANN work — i.e. one
answer each to §9, §6 and §7, without giving up §10.1.

Its README claims, on 50M × 768-dim Cohere embeddings, *28× lower p95 latency and 16× higher query
throughput than Pinecone's storage-optimized (s1) index at 99% recall, at 75% less cost when
self-hosted on EC2*. That is a vendor benchmark comparing against a specific competitor tier, and
should be read as "this class of technique closes the gap that motivated leaving Postgres" rather
than as a number you can quote. Which, conveniently, is a hypothesis §3 lets you test on your own
corpus in an afternoon.

### 10.4 Thresholds for leaving

Stay on Postgres unless you can name which of these you've hit:

| Trigger | Why it forces the move |
|---|---|
| Vectors don't fit alongside your OLTP working set | you're now trading application latency for search latency |
| Sustained high-QPS vector traffic starving other queries | resource isolation is the actual requirement |
| You need filtered recall in the 0.1–20% band and iterative scans aren't enough | §7.3's stronger strategies aren't all available |
| You need per-tenant namespaces in the thousands | partial indexes stop being ergonomic |
| You need native hybrid retrieval with fused scoring | `04` §5 becomes application code you maintain |
| Rebuild wall-clock blocks your iteration speed | §4.4 — this is a real and underrated trigger |

"Everyone uses a vector database" is not on the list. Neither is "we might scale later" — the
migration path from pgvector to a dedicated store is well-trodden and the vectors are regenerable
from the artifacts you kept (`02` §2).

---

## 11. The 2026 landscape, as axes rather than a leaderboard

A ranked list of vector databases is stale in a quarter. The axes are stable, and once you can place
a system on them you can evaluate next year's entrant without a blog post.

| Axis | Options you'll see | Why it decides things |
|---|---|---|
| **Index family** | HNSW; IVF; DiskANN/Vamana; hybrid; brute-force-with-SIMD | sets the recall–latency curve and the update story (§4, §8) |
| **Quantization** | none; fp16; scalar/int8; PQ; binary; RaBitQ/TurboQuant; statistical BQ | sets bytes/vector and whether §5.2 fits (§6) |
| **Residency** | RAM; mmap/SSD; object storage + cache | sets cost and p99 shape (§9) |
| **Filter strategy** | post-filter; pre-filter scan; in-graph traversal; label-aware graph; iterative scan; partitioning | decides whether real queries work (§7) |
| **Sparse/hybrid** | none; BM25 built in; learned sparse; server-side fusion (RRF) | decides how much of `04` is your code |
| **Multi-tenancy primitive** | namespace/collection; partition key; partial index; nothing | decides §7.6's escape hatch and `16`'s isolation story |
| **Consistency & durability** | WAL-backed; eventual; read-your-writes options | decides §8.3's freshness promise |
| **Update model** | in-place; segment + compaction; rebuild-only | decides operational burden over months (§8.2) |
| **Operational surface** | embedded library; extension; single binary; distributed cluster; managed | decides who carries the pager |

Coarse placements as of 2026, stated at a level that will age acceptably:

- **pgvector (+ pgvectorscale / VectorChord)** — HNSW and IVFFlat in Postgres; fp16/binary via type
  primitives; RAM/shared-buffers residency; iterative scans and partial indexes for filtering;
  Postgres everything else. Extensions add DiskANN-family indexes, better BQ, and label-based
  filtering.
- **Qdrant** — HNSW; broad quantization menu including TurboQuant and multi-bit binary; filterable
  HNSW via payload-index-derived edges; collections as the tenancy primitive; single binary,
  straightforward to operate.
- **Milvus / Zilliz** — the widest index menu including `IVF_RABITQ`; distributed by design;
  segment-based with explicit compaction; the most moving parts, and the most headroom.
- **Weaviate** — HNSW with quantization; modules and built-in hybrid search; opinionated schema
  model.
- **Elasticsearch / OpenSearch** — Lucene HNSW alongside a mature lexical engine; server-side RRF
  (`04` §5); the strongest choice when you already run it and hybrid is the point.
- **LanceDB** — columnar/embedded, object-storage-friendly, strong for analytical access over
  embeddings and for local development.
- **turbopuffer / object-storage-native** — the tier-three architecture in §9, aimed at
  many-namespace workloads with uneven access.
- **Pinecone and other managed services** — the operational-surface axis taken to its conclusion;
  evaluate on §3's methodology and on cost per query, since the internals are deliberately opaque.

**Do not choose from this list.** Choose by writing down your row of the axes table — your chunk
count from `02` §12, your dimension count from `01`, your selectivity distribution from §7.4, your
namespace count, your freshness promise — and then seeing which systems can express it. Usually two
or three can, and the decision collapses to operational preference, which is a much easier argument
to have.

---

## 12. Cost model for the index layer

`11-token-accounting-and-cost.md` handles tokens. The index is the other half, and it is the half
that is fixed cost — it accrues whether or not anyone queries.

### 12.1 The four lines

```
monthly_index_cost =
      memory_cost        # bytes from §5 × replicas × $/GB-month
    + storage_cost       # vectors + payloads + intermediate artifacts (02 §2)
    + query_compute      # QPS × latency × $/core-hour, or per-request pricing
    + build_compute      # rebuild wall-clock × rebuild frequency × instance cost
```

The line people forget is the last one. If rebuilding takes 8 hours on a large instance and you
rebuild weekly (embedding-model changes, chunker changes, tombstone accumulation), that is ~32
instance-hours/month of compute that appears in no capacity plan.

### 12.2 Worked example — 10M chunks, 1536-dim

Take `02` §12's output as input. Configurations, using §5's formula:

| Configuration | Bytes/vector | Index size | Where it lives |
|---|---:|---:|---|
| fp32, in RAM | ~6,300 | ~63 GB | RAM |
| fp16 (`halfvec`), in RAM | ~3,200 | ~32 GB | RAM |
| int8 scalar + rescore from RAM | ~1,700 | ~17 GB + 61 GB fp32 | RAM + RAM |
| int8 scalar + rescore from SSD | ~1,700 | ~17 GB | RAM + SSD |
| binary + 4× oversample, rescore from SSD | ~440 | ~4.4 GB | RAM + SSD |
| object storage + NVMe cache | ~6,300 | ~63 GB | S3 + cache sized to working set |

*(1536-dim: fp32 = 4×1536 = 6,144 B; + M=16 graph ≈ 144 B; + ~150 B overhead. int8 = 1×1536 = 1,536.
binary = 1536/8 = 192.)*

The spread between the first and fifth rows is **~14× in resident bytes**, and the second-to-last row
does it while keeping exact rescoring. That is a much larger lever than any store-vs-store choice,
and it is entirely within your control with a `SET` and a rebuild.

### 12.3 Cost per query, and what it's for

```
cost_per_query ≈ (monthly_index_cost / monthly_queries) + marginal_compute_per_query
```

The reason to compute this — beyond finance — is that it makes the quality/cost tradeoff a single
surface, which is the thesis of `11` and the reason P2 exists in the README's project ladder. "We
raised `ef_search` from 100 to 400 and gained 1.2 points of recall@10" is an incomplete sentence. The
complete one ends "…and moved p99 from 14 ms to 46 ms and cost per query from $0.00003 to $0.00009."

At low query volumes the fixed cost dominates and cost-per-query is dominated by *idle capacity* —
which is the entire argument for §9's tier three, and the reason a per-tenant-index architecture on
RAM-resident HNSW gets expensive faster than anyone expects.

---

## 13. Anti-patterns

**Reporting QPS without recall, or recall without latency.** Half of an operating point is not a
result (§3.2). Present the curve or present a point on it with both coordinates.

**Comparing stores at their default settings.** You measured two vendors' default-parameter opinions
(§3.3). Tune both to the same measured recall, then compare.

**Building ground truth from corpus vectors instead of query vectors.** Inflates recall, because
document vectors sit in dense regions and are their own nearest neighbour (§3.1).

**Measuring unfiltered recall for a filtered workload.** The single most common broken vector
benchmark. If 80% of production queries carry a `WHERE`, the unfiltered number describes a system you
don't run (§7.4).

**Comparing filtered ANN results against unfiltered ground truth.** Produces a number that isn't
recall of anything. The filter must be applied to the ground truth too (§7.4).

**Leaving `ef_search` at its default with a larger `k`.** pgvector's default of 40 silently caps
`LIMIT 100` at 40 rows. Assert `ef_search ≥ k` (§4.2).

**Never re-sweeping `ef_search` after the corpus grows.** The same `ef` explores the same absolute
number of candidates in a much larger space. This is the mechanism behind "quality degraded and
nobody changed anything" (§4.3).

**Adding payload indexes after ingestion in a store that derives graph edges from them.** You get the
payload index without the filtered-search benefit, and no error (§7.3).

**Quantizing without rescoring, then concluding quantization doesn't work.** The whole design depends
on the two-stage structure. Naive 1-bit quantization on a 384-dim model with no oversampling *should*
perform badly; that finding says nothing about the technique (§6.1, §6.4).

**Rescoring from disk at high oversampling without measuring p99.** `k × oversample` random reads on
the query path is a latency bomb that a p50 measurement will not find (§6.4).

**Treating deletion as free.** Tombstones consume memory, traversal, and — the one that bites —
`ef_search` budget, so recall decays silently over months (§8.1).

**Not monitoring segment count and tombstone ratio.** These are the only leading indicators for an
entire class of quality regression (§8.2).

**Choosing an object-storage-backed store, benchmarking warm, and shipping cold traffic.** If each
tenant queries once an hour, every query is a first query (§9.2).

**Leaving pgvector because of a scaling problem you have not measured.** §10.4 lists the triggers.
"We might scale later" is not one, and the migration is not hard.

**Treating the index as the place retrieval quality comes from.** It is the place quality is
*lost*. If eval recall is bad at index recall 0.99, the problem is upstream and no amount of tuning
here will find it (§1.1).

---

## 14. Mental models — the compressed set

1. **The index doesn't produce recall, it loses it — measurably, in three separable ways.** Graph
   approximation, quantization error, filter interaction. Debug them separately; they have different
   fixes (§1.2).
2. **Index recall and eval recall are different words.** One has free exact ground truth and a knob;
   the other needs labels and has no knob. Never quote one as the other (§1.1).
3. **You get exact ground truth for free. There is no excuse for guessing.** One brute-force pass
   over your own corpus, a few hundred real queries, and every parameter question becomes empirical
   (§3.1).
4. **The comparable unit is latency at fixed recall.** A recall number without latency, or a latency
   number without recall, is half an operating point (§3.2).
5. **Default settings are a vendor's opinion, not a property of the system.** Tune both sides to the
   same recall before comparing anything (§3.3).
6. **Build-time parameters are schema decisions; query-time parameters are free.** So sweep
   `ef_search` first and exhaustively, and only then consider rebuilding for `M` (§4.1).
7. **`ef_search` must be ≥ `k`, and must grow with the corpus.** Both are silent failures — fewer
   results than requested, and recall that decays as you ingest (§4.2, §4.3).
8. **Quantize for traversal, rescore for ranking.** The quantizer only has to keep true neighbours
   inside the oversampled candidate set, not rank them. That much weaker requirement is why 32×
   compression is viable at all — and it is the same cascade shape as `04` §1 (§6.1).
9. **Binary quantization is a high-dimensional technique.** Below ~1,000 dimensions it loses too
   much, which is why 2-bit and 1.5-bit variants exist (§6.2).
10. **Oversampling is a query-time knob, so sweep it like `ef_search`** — and price it in random
    reads if the full-precision vectors are on disk (§6.4).
11. **Filtered search has three regimes, and the middle one breaks graph indexes.** Too many rows to
    scan, too few to keep the graph navigable. That middle band is where production lives (§7.2).
12. **Partitioning beats filtering when the predicate is low-cardinality and stable.** A per-tenant
    index turns a hard filtered query into an easy unfiltered one (§7.3).
13. **Filtered recall must be measured against filtered ground truth, per selectivity band.** Anything
    else is measuring a system you don't run (§7.4).
14. **Deletion is a tombstone, and tombstones eat your `ef_search` budget.** Recall degrades over
    months with no change and no deploy — the hardest kind of regression to diagnose (§8.1).
15. **Cold start is a product decision.** A 60× cold/warm ratio is fine if traffic is bursty per
    namespace and catastrophic if it's uniformly sparse. Measure your inter-arrival times before
    choosing the architecture (§9.2).
16. **Choose a store by writing down your axes row, not by reading a ranked list.** Chunk count,
    dimensions, selectivity distribution, namespace count, freshness promise — that row usually leaves
    two or three viable systems (§11).

---

## 15. Lab exercises

Every lab produces an artifact and a number. Every number produced here is **rung 1 — measured**
(README §6): quote it with its corpus, its size, its dimension count, its `k`, and its recall target,
every time, or don't quote it. This document stays **rung 3 — studied** until these have been run
against a real corpus.

**Lab 1 — Build the ground-truth harness.**
*Goal:* the artifact every other lab in this chapter depends on.
*Steps:* implement §3.1's `exact_knn` and `index_recall_at_k` over your own embedded corpus (from
`02`'s pipeline). Use 200+ **real** queries — from a query log if you have one, from the `02` lab 4
golden set otherwise. Verify the harness by running it against a brute-force "index": recall must be
exactly 1.00. If it isn't, your metric or your normalization disagrees between harness and store, and
every number you'd have produced would be wrong.
*Artifact:* a ground-truth file (`query_id → top-k corpus ids`), a scorer, and a passing 1.00
self-check.
*Success criterion:* the self-check passes, and you can state your `k`, your metric, and your query
provenance in one sentence.
*Time:* ~2 hours.
*Unblocks:* every other lab here, and P1.

**Lab 2 — The `ef_search` sweep and your operating point.**
*Goal:* find the cheapest configuration that hits your recall target, and know the shape of the
curve around it.
*Steps:* sweep `ef_search` across at least six values spanning `k` to ~20×`k`. Record recall@k, p50,
p99 at each. Warm up before timing and say so. Bootstrap a 95% CI on recall
(`../python-mastery/31-measurement-methodology.md`). Also record the **fraction of queries below a
per-query recall floor of 0.8** (§3.4) at each point.
*Artifact:* a table with CIs and a recall-vs-p50 plot, plus one sentence naming the chosen operating
point and why.
*Success criterion:* you can say "we run at recall 0.9X, p50 Y ms, p99 Z ms, and Q% of queries fall
below 0.8" without looking anything up.
*Time:* ~2 hours given lab 1.
*Unblocks:* labs 3–6, and `04`'s latency budget.

**Lab 3 — Rebuild at a different `M`, and find out if it was worth it.**
*Goal:* test §4.1's claim that `ef_search` is where the achievable improvement lives.
*Steps:* rebuild at `M ∈ {8, 16, 32}` holding `ef_construction` fixed. For each, re-run lab 2's full
sweep — the curve moved, so a single point is not comparable. Record index size and build wall-clock
for each. Compare p50 at your fixed recall target across the three.
*Artifact:* three curves on one plot, plus a table of index size and build time per `M`.
*Success criterion:* a stated `M` with the memory and build-time cost of that choice written down —
and an honest answer to whether the rebuild bought anything the sweep hadn't already.
*Time:* ~half a day, mostly builds.
*Unblocks:* §5's sizing, and your reindex cadence.

**Lab 4 — Quantization ladder with rescoring.**
*Goal:* find how far down §6.2's ladder your corpus goes before recall breaks, which is the largest
single cost lever available to you.
*Steps:* build at fp32, fp16, and the most aggressive quantization your store offers. For each,
sweep the oversampling factor `{1, 2, 4, 8}` with rescoring on. Record recall@k, p50, p99, and
resident bytes. Then repeat the most aggressive configuration with rescoring **off**, to see what
rescoring is actually buying. If your embeddings are Matryoshka-trained, add a truncated-dimension
row (§6.6) to compare the two levers directly.
*Artifact:* a table of (representation × oversample) → recall, p50, p99, bytes; and a stated choice.
*Success criterion:* a configuration chosen for a stated reason with its recall CI, plus a
one-sentence answer to "how many bytes per vector did we save and what did it cost in p99?"
*Time:* ~half a day.
*Unblocks:* §12's cost model, and P4's sizing.

**Lab 5 — The filtered-recall table. Do not skip this one.**
*Goal:* find out whether the number from lab 2 describes your actual workload. It probably doesn't.
*Steps:* first, characterize your real filters — sample your query log (or your product spec) and
compute the **selectivity distribution** of the predicates actually used. Then build filtered ground
truth per §7.4 and measure recall, p50 and p99 in each of the five selectivity bands. Weight by your
measured query mix to get a single traffic-weighted recall number. Compare that against lab 2's
unfiltered figure.
*Artifact:* the §7.4 table, plus your selectivity histogram, plus the traffic-weighted recall.
*Success criterion:* you can state the gap between your unfiltered recall and your traffic-weighted
recall. If it's large, you have found the most important number in this chapter.
*Time:* ~4 hours.
*Unblocks:* §7.3's strategy choice, `16-multi-tenancy-and-isolation.md`, and P4.

**Lab 6 — Fix the filtered case.**
*Goal:* close whatever gap lab 5 found, and measure what the fix cost.
*Steps:* pick the strategy your store supports — iterative scans, filterable HNSW with payload
indexes created *before* ingest, label-aware graph, or partitioning into per-tenant indexes. Rebuild
as needed. Re-run lab 5's table. Record what the fix cost in build time, index size, and p99. If you
chose partitioning, also record per-index overhead × index count, since that's the term that decides
whether it scales.
*Artifact:* before/after filtered-recall tables plus a cost delta.
*Success criterion:* traffic-weighted recall at or above your target, with the cost of getting there
written down — including "the fix wasn't worth it, we partitioned instead" as a good outcome.
*Time:* ~1 day.
*Unblocks:* P1's service, and `16`.

**Lab 7 — Tombstone decay simulation.**
*Goal:* measure §8.1 rather than believing it, and set your rebuild cadence from data.
*Steps:* starting from a freshly built index at your operating point, delete 10% / 20% / 30% / 40% of
the corpus at random (or better, following your real deletion pattern, which is probably not random).
Re-measure recall and p50 at each stage with `ef_search` held fixed. Then rebuild and re-measure.
Estimate your real tombstone accumulation rate from production data, and convert the curve into a
rebuild cadence.
*Artifact:* a recall-and-latency-vs-tombstone-fraction curve, plus a stated rebuild cadence with its
justification.
*Success criterion:* a cadence you can defend, and a monitoring threshold on tombstone ratio derived
from where the curve turns.
*Time:* ~3 hours.
*Unblocks:* `15-ingestion-pipelines-and-freshness.md`, and your alerting (§8.2).

**Lab 8 — Sizing sheet and the store decision.**
*Goal:* convert every number above into the decision this chapter exists to support.
*Steps:* build the §5.2 table for *your* chunk count (from `02` §12) and dimension count, at your
chosen `M` and payload size, at your replica count. Add your labs 4–7 results as configuration rows.
Fill in §11's axes table for your requirements, then place two or three candidate systems on it.
Compute §12's four cost lines for each. Write the decision and the triggers (§10.4) that would
reverse it.
*Artifact:* a one-page sizing sheet and a written decision with its reversal triggers.
*Success criterion:* someone else could read the page and reach the same decision — and, six months
later, could tell whether a trigger has fired.
*Time:* ~3 hours.
*Unblocks:* P1 and P4.

**Lab 9 — Cold-start reality check.** *(Only if you are considering an object-storage-backed store.)*
*Goal:* find out whether your traffic pattern makes §9.2's cliff irrelevant or fatal.
*Steps:* from your access logs, compute the distribution of inter-arrival times *per namespace*.
Estimate, given a stated cache TTL, what fraction of queries would be cold. Then measure actual cold
and warm p50/p99 against a real deployment. Multiply through.
*Artifact:* a cold-query-fraction estimate and a blended latency figure with its assumptions stated.
*Success criterion:* a defensible answer to "what will our p50 actually be", as opposed to the
vendor's warm number.
*Time:* ~3 hours.
*Unblocks:* §9's architecture choice, and `12-serving-latency-and-caching.md`.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — why post-filtering under-returns,
why a strict filter disconnects an HNSW graph, why tombstones consume the `ef_search` budget, why
quantize-then-rescore has a weaker accuracy requirement than quantize-and-return — are derivable from
the algorithm as described in `../databases/11-hnsw-vector-search-internals.md` and from the vendor
documentation cited inline. The arithmetic in §5 and §12 is derivable rather than measured: every
input is labeled as an assumption and every output is checkable with a calculator.

**Verified against primary sources, read directly:** pgvector's README (type storage formulas, the
2,000 / 4,000 / 64,000 / 1,000 index dimension limits, `m = 16` and `ef_construction = 64` build
defaults, `hnsw.ef_search = 40` and `ivfflat.probes = 1` query defaults, the `iterative_scan`
strict/relaxed modes and `max_scan_tuples` / `scan_mem_multiplier`, the IVFFlat `lists` and `probes`
heuristics, `maintenance_work_mem` and `max_parallel_maintenance_workers` build guidance, the
binary-quantize-then-rescore SQL pattern, and the FAQ's three causes of fewer-than-expected results);
Qdrant's quantization and indexing documentation (the compression-vs-method table, TurboQuant bit
depths, `oversampling` and `rescore` semantics and defaults, the 1.5-/2-bit rationale, the binary
quantization model results quoted in §6.2, and the filterable-HNSW extra-edge mechanism with its
create-payload-indexes-first requirement); turbopuffer's published architecture page (the cold/warm
p50 figures, WAL write latency and throughput, and the one-entry-per-second commit cadence);
pgvectorscale's README (StreamingDiskANN, statistical binary quantization, Filtered DiskANN labels,
and the Pinecone comparison claim); and the arXiv records for HNSW (Malkov & Yashunin, 1603.09320)
and ACORN (Patel, Kraft, Guestrin & Zaharia, 2403.04871).

**Someone else's rung 1, quoted with conditions attached:** Qdrant's binary-quantization recall
figures (0.98 recall@100 at 4× oversampling for `text-embedding-ada-002` on dbpedia; 0.98 recall@50 at
2× oversampling for Cohere `embed-english-v2.0` on Wikipedia); Milvus's `IVF_RABITQ` figures (1/32
index size at 1 bit, ~95% recall at ~1/4 memory with SQ8 refinement, ~3× QPS); pgvectorscale's
Pinecone s1 comparison (28× lower p95, 16× throughput, 75% less cost at 99% recall on 50M × 768-dim
Cohere embeddings). All three are vendor benchmarks on public datasets. They establish that a
technique can work under stated conditions. They do not establish what it does on your corpus, and
§3 exists so that you never have to rely on them for that.

**Deliberately not in this document:** any cross-store QPS or recall leaderboard, because §3.3 argues
such comparisons are usually measuring default settings rather than systems, and because I have not
run them. The latency ranges in §9.1 are order-of-magnitude orientation, not measurements — they are
there to show the *shape* of the three architectures and should not be quoted as numbers. The
selectivity thresholds in §7.2 are stated as orders of magnitude for the same reason; lab 5 is how
you get yours.

The labs in §15 are what convert this to **rung 1 — measured**, and their outputs must always travel
with their corpus, dimension count, `k`, recall target, and — for anything in §7 — their selectivity
band.
