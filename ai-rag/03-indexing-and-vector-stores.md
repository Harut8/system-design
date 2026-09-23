# 03 — Indexing and vector stores

> **What this chapter gives you:** how vector indexes actually work (with the math), what every
> parameter means numerically (`M`, `ef_construction`, `ef_search`, `nlist`, `nprobe`, bits,
> oversampling), how to measure recall correctly, how to size memory and cost on a napkin, how
> filtered search breaks and how to fix it, when to use pgvector vs a dedicated store, interview
> questions (§16), and real-world incident cases with numbers (§17).
>
> **Related:** [`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md)
> (HNSW proofs and deeper derivations — optional; §2 here is self-contained),
> [`../databases/11-vector-search-internals.md`](../databases/11-vector-search-internals.md)
> (broader ANN survey), [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md)
> (§12 gives the chunk count that every sizing calculation here starts from),
> [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) (dimensions,
> normalization, quantization from the model side).
>
> **Feeds into:** [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md)
> (this index is the candidate-generation stage), [`08-evaluation-methodology.md`](08-evaluation-methodology.md)
> (§1.1's index-recall vs eval-recall distinction), `12-serving-latency-and-caching.md` (§9),
> `15-ingestion-pipelines-and-freshness.md` (§8), `16-multi-tenancy-and-isolation.md` (§7).
>
> **The one-paragraph summary.** A vector index exists because exact nearest-neighbour search costs
> `N × d` multiply-adds per query — 7.7 billion for 10M × 768-dim vectors, ~0.3 s of pure memory
> bandwidth. An approximate (ANN) index computes a few thousand distances instead and answers in ~1
> ms, at the price of sometimes missing a true neighbour. That miss rate is **index recall**, and it
> is the one quality number in the RAG stack you can measure exactly and for free (brute-force
> search is the ground truth). Recall is lost in three places — graph/cluster approximation,
> quantization, and filters — and each has its own knob with a price in RAM or latency. The
> unfiltered recall number almost nobody's production traffic sees; filtered queries (`WHERE
> tenant_id = ?`) are where indexes actually fail.

---

## Contents

1. [The problem, the metrics, and what "recall" means exactly](#1-the-problem-the-metrics-and-what-recall-means-exactly)
2. [How the indexes work — the math behind every parameter](#2-how-the-indexes-work--the-math-behind-every-parameter)
3. [Measure index recall before you tune anything](#3-measure-index-recall-before-you-tune-anything)
4. [HNSW parameters in anger](#4-hnsw-parameters-in-anger)
5. [The memory arithmetic](#5-the-memory-arithmetic)
6. [Quantization, and the rescoring trick that makes it work](#6-quantization-and-the-rescoring-trick-that-makes-it-work)
7. [Filtered search — the actual hard problem](#7-filtered-search--the-actual-hard-problem)
8. [Updates, deletes, and index drift](#8-updates-deletes-and-index-drift)
9. [Where the bytes live: RAM, SSD, object storage](#9-where-the-bytes-live-ram-ssd-object-storage)
10. [pgvector versus a dedicated store](#10-pgvector-versus-a-dedicated-store)
11. [The 2026 landscape — what each store actually offers, and how to pick](#11-the-2026-landscape--what-each-store-actually-offers-and-how-to-pick)
12. [Cost model for the index layer](#12-cost-model-for-the-index-layer)
13. [Anti-patterns](#13-anti-patterns)
14. [Mental models — the compressed set](#14-mental-models--the-compressed-set)
15. [Lab exercises](#15-lab-exercises)
16. [Interview questions and system design prompts](#16-interview-questions-and-system-design-prompts)
17. [Real-world cases — incidents with numbers](#17-real-world-cases--incidents-with-numbers)

---

## 1. The problem, the metrics, and what "recall" means exactly

### 1.0 Why an index at all — the brute-force cost

Given a query vector `q ∈ ℝ^d` and a corpus `X = {x_1 … x_N}`, the retriever needs the `k` vectors
closest to `q`. Exact search (a "flat" index) computes all `N` distances:

```
cost_exact = N × d multiply-adds       (+ a top-k heap: O(N log k), negligible)
bytes read = N × d × bytes_per_dim     (every vector, every query)
```

| Corpus | Multiply-adds / query | Bytes read / query (fp32) | Single query, ~100 GB/s memory bandwidth |
|---|---:|---:|---:|
| 100K × 768 | 77 M | 307 MB | ~3 ms |
| 1M × 768 | 768 M | 3.1 GB | ~30 ms |
| 10M × 768 | 7.7 B | 30.7 GB | ~300 ms |
| 100M × 1536 | 154 B | 614 GB | doesn't fit in RAM on one box |

Brute force is **memory-bandwidth bound**, not compute bound: each vector is read once and used for
one dot product. (That is also why *batched* brute force — the ground-truth computation in §3.1 —
is cheap: a `(Q × d) @ (d × N)` matrix multiply reuses each corpus vector across all queries.)

An ANN index cuts the per-query work to a few thousand distance computations:

```
HNSW, 10M vectors, M=16, ef_search=100:
  ≈ 2,000–4,000 distance computations × 768 dims ≈ 2–3 M multiply-adds
  ≈ 1 ms, vs ~300 ms brute force  →  ~2,500–5,000× less work
```

That speed-up is the entire reason the index exists. The price is that the index **may skip a
vector that was actually among the top-k**. How often it does that is *recall*.

**Rule of thumb from the table:** below ~100K vectors (or ~100K rows surviving a filter), brute
force is a few milliseconds and needs no index, no tuning and has recall 1.0. Many per-tenant
workloads live entirely here.

### 1.0.1 Distance metrics — the formulas, and why three of them rank identically

| Metric | Formula | Smaller/larger = closer | pgvector op | Qdrant | FAISS |
|---|---|---|---|---|---|
| Euclidean (L2) | `‖q − x‖₂ = √Σ(qᵢ − xᵢ)²` | smaller | `<->` | `Euclid` | `METRIC_L2` (squared) |
| Inner product | `q · x = Σ qᵢxᵢ` | larger | `<#>` (returns **negative** IP) | `Dot` | `METRIC_INNER_PRODUCT` |
| Cosine distance | `1 − (q · x) / (‖q‖ ‖x‖)` | smaller | `<=>` | `Cosine` | IP on normalized vectors |
| Hamming (binary) | `popcount(q XOR x)` | smaller | `<~>` | (internal, BQ) | `IndexBinary*` |
| L1 (Manhattan) | `Σ abs(qᵢ − xᵢ)` | smaller | `<+>` | `Manhattan` | `METRIC_L1` |

The identity that matters: if `‖q‖ = ‖x‖ = 1` (L2-normalized), then

```
‖q − x‖² = ‖q‖² + ‖x‖² − 2 q·x = 2 − 2 cos(q, x)
```

so L2 distance, inner product and cosine produce **the same ranking**. That is why most stores
normalize on insert for cosine and then compute a plain dot product (cheapest). It also means:
**if your vectors are not normalized, those three metrics give different top-k**, and ground truth
computed under one metric against an index built with another produces a meaningless recall number.
(Normalization is covered from the model side in `01` §2.2.)

pgvector detail that bites people: `<#>` returns the *negative* inner product so that `ORDER BY ...
ASC` works; the operator class must match the operator (`vector_cosine_ops` ↔ `<=>`,
`vector_ip_ops` ↔ `<#>`, `vector_l2_ops` ↔ `<->`) or **the planner silently skips the index** and
runs a sequential scan.

### 1.1 Recall — the exact definitions

Let `G_k(q)` be the true top-k for query `q` (from brute force, same metric) and `A_k(q)` be what the
index returned.

**recall@k** (the standard "k-NN recall"):

```
recall@k(q) = |A_k(q) ∩ G_k(q)| / k
recall@k    = mean over all test queries
```

Worked example, k = 10:

```
truth  G_10 = {7, 12, 31, 44, 58, 63, 70, 81, 90, 99}
index  A_10 = {7, 12, 31, 44, 58, 63, 70, 81, 15, 23}
overlap = 8   →   recall@10 = 0.80
```

The index found 8 of the 10 true nearest neighbours; 90 and 99 were missed and replaced by 15 and
23, which are *slightly farther* (not garbage — typically they are neighbours #11–#20). That is the
character of ANN error: it swaps borderline neighbours, it does not return random vectors.

**Two variants you will meet:**

| Name | Definition | Where you see it | Why it exists |
|---|---|---|---|
| `k-recall@R` (e.g. "10-recall@100") | `\|G_k ∩ A_R\| / k` — fraction of the true top-k found anywhere in the top-R returned | FAISS docs, quantization with rescoring | when a rescorer/reranker will re-sort the top-R, only *presence* in R matters |
| `1-recall@1` | fraction of queries whose single true nearest neighbour is ranked #1 | ANN papers | stricter; sensitive to ties |
| distance-threshold recall | an ANN result counts as a hit if `dist(q, a) ≤ dist(q, g_k) × (1 + ε)` | ann-benchmarks, corpora with duplicates | exact-duplicate chunks create ties; ID-based recall punishes the index for picking the "wrong" twin |

If your corpus has many near-duplicate chunks (boilerplate footers, repeated headers — see `02`
§4), use the distance-threshold form, or ID-based recall will under-report.

**Confidence interval.** Recall is a mean over queries, so its standard error is

```
SE = s / √Q        s = standard deviation of per-query recall, Q = number of queries
95% CI ≈ recall ± 1.96 × SE
```

Example: 200 queries, per-query recall std `s = 0.12` → `SE = 0.0085` → 95% CI ≈ ±0.017. So
"0.953 vs 0.948" from a 200-query run is noise. Per-query recall is bounded and skewed, so in
practice bootstrap the interval (resample queries with replacement 1,000×) rather than trusting the
normal approximation.

**Index recall is not eval recall.** They share a name and nothing else:

| | **Index recall** (this chapter) | **Eval recall@k** (`02` §11, `08`) |
|---|---|---|
| Question it answers | did the index return the vectors *closest by the metric*? | did retrieval return the chunks a *human would call relevant*? |
| Ground truth | exact k-NN (brute force) | human/LLM relevance labels |
| Cost of ground truth | one batched matrix multiply, minutes | days of labeling |
| A miss means | the ANN structure skipped a closer vector | the embedding model / chunking / corpus failed |
| Typical target | 0.95–0.99 | whatever your product needs |
| Fixed by | turning a knob (`ef_search`, `nprobe`, oversampling) | model, chunking, hybrid search, reranking |

An index with recall 1.00 on top of an embedding model that doesn't understand your domain returns
the wrong documents — perfectly. The two roughly multiply: if exact search would give eval
recall@10 = 0.80 and your index recall@10 = 0.90, end-to-end is *at most* ≈ 0.72, and usually a bit
less because the vectors the index misses are systematically the hard ones (§3.4).

**Practical consequence:** fix index recall at a stated target (say ≥ 0.95) first and hold it
constant while you A/B embedding models or chunkers. Otherwise your A/B measures two things and
blames one.

**The other numbers you report alongside recall:**

| Metric | Definition | Typical unit |
|---|---|---|
| p50 / p95 / p99 latency | percentile of per-query wall time, *after warm-up* | ms |
| QPS | queries/second at a stated concurrency and recall | q/s |
| Build time | wall-clock to build the index from scratch | minutes–hours |
| Bytes/vector | resident memory ÷ N (vector + graph + overhead) | bytes |
| Tail-recall fraction | share of queries with per-query recall below a floor (e.g. < 0.8) | % |

A result is always a **pair**: *latency at a given recall*. "4 ms" alone or "0.97 recall" alone is
half a measurement (§3.2).

### 1.2 Three sources of loss, and their independent knobs

| Loss source | What physically happens | Knob | What buying it back costs | Concrete example |
|---|---|---|---|---|
| **Graph / cluster approximation** | HNSW's beam search stops in a local minimum; IVF's true neighbour sits in a cell that wasn't probed | `ef_search` (HNSW), `nprobe` (IVF); build-time `M`, `ef_construction`, `nlist` | latency roughly linear in `ef_search`/`nprobe`; RAM for `M` | `ef_search` 40 → 200: recall 0.91 → 0.98, p50 1.1 → 3.5 ms (illustrative shape) |
| **Quantization error** | compressed distances are noisy, so two candidates at 0.301 and 0.305 swap order | bits per dim; oversampling + rescoring | rescoring = `k × oversample` extra exact distances (cheap in RAM, random reads on disk) | binary quantization, no rescore: 0.80; with 4× oversample + rescore: 0.97 |
| **Filter interaction** | the filter excludes most graph nodes, so the traversal can't route through them, or post-filtering drops most of the top-k | filter strategy (§7) — often a different index layout, not a knob | build complexity, per-tenant indexes | `WHERE tenant_id = 42` matching 0.5% of rows: 10 requested, 0–1 returned |

The example numbers in the table are illustrative of the *shape*; §3 and the labs in §15 are how
you get your own.

They are separable, and debugging requires separating them:

```
1. Run unfiltered, full precision (no quantization) → recall R1.   Low?  → graph knobs (§4)
2. Enable quantization, same ef                     → recall R2.   R1−R2 large? → oversampling/bits (§6)
3. Add the production filter, filtered ground truth → recall R3.   R2−R3 large? → filter strategy (§7)
```

---

## 2. How the indexes work — the math behind every parameter

You cannot tune a parameter you can't explain. This section gives the mechanism of each index
family precisely enough to predict what a knob will do before you turn it.

### 2.1 Flat (brute force)

Stores the raw vectors; scans all of them. Recall 1.0 by definition. Cost `O(N·d)` per query
(§1.0). Use it when `N` (or the filtered subset) is under ~100K, for ground truth, and inside
other indexes as the final rescoring step. Every store has it: pgvector (no index / seq scan),
FAISS `IndexFlatIP` / `IndexFlatL2`, Qdrant `exact=True`.

### 2.2 IVF — inverted file (clustering)

**Build.** Run k-means on (a sample of) the corpus to get `nlist` centroids `c_1 … c_nlist`. Assign
every vector to its nearest centroid. Each centroid owns a "list" (a cell) of vectors.

**Query.** Compute distance from `q` to all `nlist` centroids, pick the `nprobe` closest, and
brute-force only the vectors in those lists.

```
cost_IVF ≈ nlist × d               (compare to centroids)
         + nprobe × (N / nlist) × d  (scan the probed lists; N/nlist = avg list length)
```

**Why `nlist ≈ √N`.** With `nprobe = 1`, minimize `f(nlist) = nlist + N/nlist`:
`f'(nlist) = 1 − N/nlist² = 0` → `nlist = √N`. That is where pgvector's "`lists = sqrt(rows)` above
1M rows" comes from. FAISS's guideline is `nlist` between `4·√N` and `16·√N`, because you will
probe more than one list and smaller, more numerous lists give finer control.

**Worked example.** `N = 10M`, `d = 768`, `nlist = 16,384` (≈ 5·√N), `nprobe = 64`:

```
avg list length     = 10,000,000 / 16,384 ≈ 610 vectors
centroid distances  = 16,384
scanned vectors     = 64 × 610 ≈ 39,000       (0.39% of the corpus)
total distances     ≈ 55,000   vs 10,000,000 brute force  → ~180× less work
```

**Where recall is lost.** A true neighbour that lives just across a cell boundary, in a cell whose
centroid is not among the `nprobe` nearest, is never looked at. Raising `nprobe` sweeps in more
neighbouring cells: `nprobe = nlist` is exact search. Recall vs `nprobe` rises steeply then
flattens; typical useful range is `nprobe/nlist` of 0.5%–5%.

**Operational properties that follow from the math:**

- **Centroids are learned from data.** Build on an empty table → garbage centroids (pgvector says:
  create the index *after* loading). FAISS warns if you train on fewer than ~39 × `nlist` points.
- **Centroids go stale.** New data drifting to a new topic lands in whichever old cell is least bad;
  lists become unbalanced (one list of 50K, most of 600), latency and recall both degrade. Fix =
  retrain + rebuild. That's why IVF suits bulk-loaded, periodically rebuilt corpora.
- **Cheap to build, small in memory**: overhead is the centroids (`nlist × d × 4` bytes = 50 MB for
  16,384 × 768) plus a list ID per vector. No graph.

### 2.3 HNSW — Hierarchical Navigable Small World graph

HNSW is the default in pgvector, Qdrant, Weaviate, Milvus, Elasticsearch/OpenSearch (Lucene), and
Redis. Every node is a vector; edges connect it to nearby vectors; search walks the graph greedily
toward the query.

#### 2.3.1 The structure: layers and how a node's level is chosen

- **Layer 0** contains every vector. Each node keeps up to `M_max0 = 2·M` neighbours.
- **Layer l > 0** contains a random subset; each node keeps up to `M` neighbours.
- A node's top level is drawn at insert time:

```
level = floor(−ln(U) × mL),   U ~ Uniform(0,1),   mL = 1 / ln(M)
⇒ P(level ≥ l) = M^(−l)
```

So each layer holds ~1/M of the nodes of the layer below. For `N = 10M`, `M = 16`:

| Layer | Expected nodes | Role |
|---:|---:|---|
| 0 | 10,000,000 | fine-grained search (all vectors) |
| 1 | 625,000 | |
| 2 | 39,000 | |
| 3 | 2,400 | |
| 4 | 150 | |
| 5 | ~10 | entry region |

Number of layers ≈ `log_M(N)` = `ln(10⁷)/ln(16)` ≈ 5.8. The upper layers are a skip-list-like
"express highway" with long edges; layer 0 is the local street map.

#### 2.3.2 The search algorithm, and what `ef_search` literally is

```
SEARCH(q, k, ef):
    ep = entry_point                              # a node on the top layer
    for layer = top .. 1:                         # descend: greedy, beam width 1
        ep = GREEDY_CLOSEST(q, ep, layer)         # hop to any closer neighbour until none is closer
    W = SEARCH_LAYER(q, ep, ef, layer=0)          # beam search at layer 0 with beam width ef
    return best k of W

SEARCH_LAYER(q, ep, ef, layer):
    C = min-heap {ep}        # candidates still to expand, closest first
    W = max-heap {ep}        # best ef found so far, farthest on top  ← |W| ≤ ef
    visited = {ep}
    while C not empty:
        c = pop closest from C
        if dist(q, c) > dist(q, farthest in W): break        # nothing left can improve W
        for n in neighbours(c, layer):
            if n in visited: continue
            visited.add(n)
            if |W| < ef or dist(q, n) < dist(q, farthest in W):
                push n to C and W
                if |W| > ef: pop farthest from W
    return W
```

**`ef_search` is the size of the result heap `W`** — the beam width at layer 0. Three consequences
fall straight out of the pseudocode:

1. **`ef_search < k` means fewer than `k` results.** `W` can never hold more than `ef` items. This is
   pgvector's "`LIMIT 100` returns 40 rows" behaviour (default `hnsw.ef_search = 40`, §4.2).
2. **Larger `ef` → the search keeps more "second-best" paths alive**, so it's less likely to get
   trapped in a local minimum. That's what buys recall.
3. **Cost ≈ nodes expanded × neighbours per node.** Roughly `ef` to `2·ef` nodes get expanded at
   layer 0, each touching up to `2·M` neighbours (minus already-visited). With `M = 16`, `ef = 100`:
   ~100–200 expansions × up to 32 neighbours ≈ **2,000–4,000 distance computations**. Latency grows
   roughly linearly with `ef`; recall grows with diminishing returns.

**A toy trace.** Query `q`; distances from `q` shown next to each node; entry point at layer 0 is
`A`.

```
Graph edges (layer 0):  A–B, A–C, A–D, B–E, B–F, E–G, E–H, G–I, H–K, I–J
dist(q, ·):  A .60  B .45  C .70  D .52  E .30  F .41  G .22  H .35  I .25  J .28  K .24
True top-3: G .22, K .24, I .25
```

| ef | Path | Result top-3 | recall@3 |
|---|---|---|---|
| 1 (pure greedy) | A → B → E → G; G's neighbours (E, I): I .25 > G .22 → stop | only {G} — `ef=1` can return 1 item | 1/3 |
| 3 | expands A, B, E (W keeps H .35 alive), G, I, J; then next candidate H .35 > worst in W (J .28) → **stop before expanding H** | {G .22, I .25, J .28} | 2/3 — K missed |
| 5 | W's worst is now F .41, so H .35 is still worth expanding → finds K .24 | {G, K, I} | 3/3 |

`K` is reachable only through `H`, which is a *worse* node than several already in the beam. A
narrow beam discards `H`; a wider beam keeps it long enough to discover `K`. That is exactly what
raising `ef_search` does, at the cost of expanding more nodes.

#### 2.3.3 `ef_construction` and `M` — the build-time parameters

**Insert(x)** runs the same search with beam width `ef_construction` on each layer from the node's
level down to 0, then connects `x` to up to `M` (or `2M` on layer 0) of the candidates found.
Neighbours whose lists overflow get pruned back to `M`.

- **`ef_construction`**: how hard the build searches for each new node's neighbours. Higher →
  better neighbour lists → the *same* `ef_search` reaches higher recall later. Costs build time
  only (roughly linear), no runtime memory. Defaults: pgvector 64, Qdrant 100, FAISS 40; common
  production values 100–400. Must be ≥ `M`.
- **`M`**: the out-degree. Higher → more routes, better recall at equal `ef_search`, more memory
  (§5: ≈ `M × 8–10` bytes/vector), and more distance computations per expansion. Defaults:
  pgvector 16, Qdrant 16, hnswlib 16; common range 8–64. Higher-dimensional / harder data wants
  more.

Build cost ≈ `N × ef_construction × M × log(N) × d` distance work — which is why a 50M-vector build
takes hours and why §4.4 cares about parallel workers.

#### 2.3.4 The neighbour-selection heuristic (why HNSW keeps long edges)

Naively connecting a new node to its `M` closest candidates produces clusters with no edges between
them. HNSW instead picks neighbours with a diversity rule:

```
for candidate e in candidates sorted by dist(x, e):
    keep e  iff  dist(x, e) < dist(e, r)  for every already-kept neighbour r
```

"Keep `e` only if it's closer to `x` than to anything I already connected to." This skips redundant
neighbours in the same direction and preserves edges pointing toward other clusters — the edges
that make clustered enterprise corpora navigable (§4.3). Vamana/DiskANN uses the same idea with a
relaxation factor `α > 1` (`α·dist(e, r) > dist(x, e)`), keeping even more long-range edges.

#### 2.3.5 Complexity summary

| | HNSW | IVF-Flat | Flat |
|---|---|---|---|
| Query distance computations | ~`ef × M` (+ `log N` upper-layer hops) | `nlist + nprobe × N/nlist` | `N` |
| Build | `O(N log N × ef_c × M)` — slow | k-means + one assignment pass — fast | none |
| Extra memory | graph: `≈ M × 8–10` B/vector | ~8 B/vector + centroids | none |
| Incremental inserts | good (graph grows) | ok, but centroids go stale | trivial |
| Deletes | soft-delete (tombstones, §8) | easy (remove from list) | trivial |
| Filter behaviour | graph fragments under strict filters (§7) | pre-filter per list is natural | trivial |

### 2.4 DiskANN / Vamana — the SSD-resident graph

A single-layer graph built with the `α`-relaxed pruning above, so that search needs few hops. Full
vectors and adjacency lists live on SSD, laid out so that one hop = one 4 KB page read; a
PQ-compressed copy of every vector lives in RAM to decide *which* neighbour to hop to.

```
query latency ≈ hops × SSD random-read latency  (≈ 50–100 µs on NVMe)
            ≈ 30–100 hops × ~100 µs  ≈ 3–10 ms, with a beam width W that issues W reads in parallel
RAM ≈ PQ codes only (e.g. 32–96 B/vector) instead of full vectors + graph
```

It trades a few milliseconds of latency for ~10–30× less RAM. pgvectorscale's StreamingDiskANN,
Milvus `DISKANN`, and Azure/SQL Server implementations follow this design.

### 2.5 Quantization — how the compressed numbers are computed

§6 covers *when* to use each. Here is *what* each does to a vector.

**Scalar (int8).** Per dimension (or globally), map the value range to 256 levels:

```
Δ    = (max − min) / 255
code = round((x − min) / Δ)          ∈ {0 … 255}      1 byte instead of 4
x̂    = min + code × Δ                max error per dimension = Δ / 2
```

Example: normalized 768-dim vectors have components with std ≈ `1/√768 ≈ 0.036`; if `min/max` are
clipped at the 1st/99th percentile, say ±0.12 (Qdrant's `quantile: 0.99` does exactly this
clipping), then `Δ = 0.24/255 ≈ 0.00094` and the max error is ~0.0005 per dimension, ~1.3% of a
typical component. That's why int8 loses almost nothing: 4× smaller, tiny error.

**Binary (1-bit).** `bᵢ = 1 if xᵢ > 0 else 0`. Distance = Hamming = `popcount(a XOR b)`. For
1536 dims: 24 × 64-bit XOR + POPCNT instead of 1536 float multiply-adds. 32× smaller.

Why it only works at high dimension — the math: if the vectors' coordinates behave like random
projections (which a random rotation, as in RaBitQ, enforces), each bit disagrees with probability
`p = θ/π`, where `θ` is the angle between the two vectors. So

```
E[Hamming] = d × θ/π         std[Hamming] = √(d × p × (1−p))
```

Compare a true neighbour at θ = 40° with a distractor at θ = 45°:

| d | E[Hamming] true vs distractor | Gap | std | Gap / std |
|---:|---|---:|---:|---:|
| 384 | 85.3 vs 96.0 | 10.7 | ~8.1 | **1.3** — often swapped |
| 1536 | 341 vs 384 | 43 | ~16.3 | **2.6** — mostly correct |
| 3072 | 683 vs 768 | 85 | ~23 | **3.7** — reliable |

The signal grows with `d`, the noise with `√d`, so separability grows with `√d`. That is the
mathematical reason behind Qdrant's statement that one-bit compression loses too much below ~1,000
dimensions (§6.2), and why rescoring the top candidates with full vectors fixes most of the error:
binary only needs to get the true neighbour into the top `k × oversample`, not rank it exactly.

**Product quantization (PQ).** Split each `d`-dim vector into `m` sub-vectors of `d/m` dims. Run
k-means with 256 centroids *per sub-space*. Store each vector as `m` one-byte centroid IDs.

```
d = 768, m = 96 → 96 sub-vectors of 8 dims → code = 96 bytes (vs 3,072 fp32 = 32× smaller)
codebooks = m × 256 × (d/m) × 4 B = 256 × d × 4 B = 786 KB total (shared by all vectors)

Asymmetric distance (ADC) at query time:
  1. for each sub-space j, compute dist(q_j, centroid_j,c) for all 256 c   → m × 256 table (96 KB)
  2. dist(q, x) ≈ Σ_j table[j][code_j(x)]                                    → m lookups + adds
```

Per-vector cost is `m` table lookups instead of `d` multiply-adds. PQ reaches higher compression
than scalar or binary (you choose `m`), at the cost of training and the largest accuracy loss;
`IVF_PQ` (IVF cells + PQ codes) is the classic billion-scale FAISS configuration.

### 2.6 Parameter cheat sheet

| Parameter | Index | Exact meaning | Build or query | Defaults (pgvector / Qdrant / FAISS) | Typical range | Raise it when |
|---|---|---|---|---|---|---|
| `M` / `m` | HNSW | max neighbours per node (2·M on layer 0) | build | 16 / 16 / (constructor arg, 32 common) | 8–64 | recall plateaus below target at high `ef_search`; high-dim data |
| `ef_construction` / `ef_construct` | HNSW | beam width when inserting | build | 64 / 100 / 40 | 100–500 | same `ef_search` gives lower recall than expected; clustered data |
| `ef_search` / `hnsw_ef` / `efSearch` | HNSW | beam width at query (size of `W`) | query | 40 / (= `ef_construct`) / 16 | `k` … 20·`k` | recall below target; must be ≥ `k` |
| `lists` / `nlist` | IVF | number of k-means cells | build | (you set) | `√N` … `16·√N` | lists grow long (latency) |
| `probes` / `nprobe` | IVF | cells scanned per query | query | 1 / — / 1 | 0.5–5% of `nlist` | recall below target |
| `m` (PQ) | PQ | number of sub-quantizers (bytes/vector at 8 bits) | build | — / — / you set | `d/16` … `d/4` | quantized recall too low |
| `oversampling` | quantized | fetch `k × oversampling` candidates then rescore | query | — / 1.0 (rescore on for BQ) / `k_factor` | 1.5–8 | quantized recall below target |
| `max_scan_tuples` | pgvector iterative scan | stop after visiting this many tuples | query | 20,000 | 10K–100K+ | filtered queries under-return |

### 2.7 The same knobs in four libraries

```python
# hnswlib — the reference HNSW implementation
import hnswlib
idx = hnswlib.Index(space="cosine", dim=768)
idx.init_index(max_elements=N, M=16, ef_construction=200)   # build-time
idx.add_items(X, ids)
idx.set_ef(100)                                              # query-time ef_search
labels, dists = idx.knn_query(Q, k=10)
```

```python
# FAISS — HNSW, IVF, IVF-PQ
import faiss
faiss.normalize_L2(X); faiss.normalize_L2(Q)                 # cosine == IP on unit vectors

hnsw = faiss.IndexHNSWFlat(768, 16, faiss.METRIC_INNER_PRODUCT)
hnsw.hnsw.efConstruction = 200
hnsw.add(X)
hnsw.hnsw.efSearch = 100
D, I = hnsw.search(Q, 10)

quant = faiss.IndexFlatIP(768)
ivf = faiss.IndexIVFFlat(quant, 768, 16384, faiss.METRIC_INNER_PRODUCT)
ivf.train(X_sample)                                          # k-means; ≥ 39 × nlist points
ivf.add(X)
ivf.nprobe = 64
D, I = ivf.search(Q, 10)

ivfpq = faiss.IndexIVFPQ(quant, 768, 16384, 96, 8)           # m=96 sub-quantizers, 8 bits each
```

```sql
-- pgvector
CREATE TABLE chunks (id bigserial PRIMARY KEY, tenant_id int, embedding vector(768));
-- load data first, then:
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

BEGIN;
SET LOCAL hnsw.ef_search = 100;                              -- per transaction
SELECT id FROM chunks ORDER BY embedding <=> $1 LIMIT 10;
COMMIT;
-- Verify the index is used:  EXPLAIN SELECT ... → "Index Scan using chunks_embedding_idx"
```

```python
# Qdrant
from qdrant_client import QdrantClient, models
c = QdrantClient(url="http://localhost:6333")
c.create_collection(
    "chunks",
    vectors_config=models.VectorParams(size=768, distance=models.Distance.COSINE),
    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=200),
)
hits = c.query_points(
    "chunks", query=qv, limit=10,
    search_params=models.SearchParams(hnsw_ef=100, exact=False),  # exact=True → brute-force ground truth
)
```

### 2.8 Putting it together: the four decisions behind "which vector database"

Choosing a vector store is really four separate decisions. Arguments like "Qdrant vs Milvus vs
pgvector" usually conflate them:

| Decision | Options | What it controls | Changing it later |
|---|---|---|---|
| **Index structure** | flat, IVF, HNSW, DiskANN/Vamana, IVF+graph | recall–latency curve, build time, update behaviour (§2.1–2.4) | rebuild |
| **Vector representation** | fp32, fp16, int8, 4/2/1-bit, PQ | bytes/vector, recall before rescoring (§2.5, §6) | rebuild |
| **Residency** | RAM, NVMe/mmap, object storage + cache | p50/p99, cold start, the dominant cost line (§9) | migration |
| **Filter strategy** | post-filter, pre-filter + scan, filter-aware graph, iterative scan, per-tenant partitions | whether `WHERE`-filtered queries return correct results (§7) | knob to topology change |

Two rules:

- **Measure the combination you'll ship.** Binary quantization + a 1%-selective filter is not
  "quantization loss + filter loss": the filter shrinks the candidate pool exactly where the
  quantized ranking is least reliable.
- **Version-stamp the index config.** Three of the four require a rebuild to change, so record
  `index_version` (type, `M`, `ef_construction`, quantization) next to `embedding_model_version` and
  `chunker_version` (`01` §12, `02` §9).

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

The mechanism is in §2.3 (and, with proofs, `../databases/11-hnsw-vector-search-internals.md` §7). This is the operational
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
  because the neighbor-selection heuristic (§2.3.4) is what preserves those long edges.

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

§2.3 explains where the graph term comes from (`../databases/11-hnsw-vector-search-internals.md` §9 derives it fully). The
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

## 11. The 2026 landscape — what each store actually offers, and how to pick

Vendor rankings go stale within months, and each vendor's own benchmark favours its defaults (§3.3).
This section gives you three durable tools instead: a feature matrix, a decision flow with numeric
thresholds, and a one-week bake-off protocol. Check feature rows against the current docs of the
version you'll deploy; they move every release.

### 11.1 The nine properties that actually differ between stores

| Property | Options you'll see | What it decides | Where in this chapter |
|---|---|---|---|
| **Index family** | flat; IVF; HNSW; DiskANN/Vamana; IVF + graph | recall–latency curve, build time, update behaviour | §2, §4 |
| **Quantization** | fp16; int8; 4/2/1-bit; PQ; RaBitQ-style | bytes/vector → whether it fits in RAM | §2.5, §6 |
| **Residency** | RAM; mmap/SSD; object storage + NVMe cache | p50/p99, cold start, $/GB | §9 |
| **Filtering** | post-filter; pre-filter scan; filter-aware graph; iterative scan | whether `WHERE`-filtered queries return correct results | §7 |
| **Hybrid / sparse** | none; built-in BM25; sparse vectors; server-side fusion (RRF) | how much of `04`'s hybrid retrieval you write yourself | `04` §5 |
| **Multi-tenancy** | collection/namespace per tenant; partition key; partial index | small-tenant recall and isolation | §7.3, `16` |
| **Write path** | synchronous indexing; buffered; async; WAL + segments | read-your-writes, ingest throughput | §8.3 |
| **Update model** | in-place graph; segments + compaction; rebuild-only | behaviour after months of deletes | §8 |
| **Operations** | library; Postgres extension; single binary; distributed cluster; managed SaaS | who is on call, and for what | §10 |

### 11.2 Feature matrix (verify against current docs before deciding)

| System | Deployment | Index types | Quantization | Filtering | Hybrid / sparse | Tenancy primitive | License |
|---|---|---|---|---|---|---|---|
| **pgvector** | Postgres extension | HNSW, IVFFlat | `halfvec` (fp16), `bit` + `binary_quantize`, `sparsevec` | SQL `WHERE`; iterative scan (0.8+); partial indexes | Postgres full-text + your own RRF in SQL | table partitions, partial indexes, row-level security | PostgreSQL |
| **pgvectorscale** | Postgres extension (on pgvector) | StreamingDiskANN | statistical binary quantization | label-based filtered DiskANN | same as Postgres | same as Postgres | PostgreSQL |
| **Qdrant** | single binary; distributed mode; managed cloud | HNSW (+ exact search) | scalar int8, binary (1/1.5/2-bit), product; TurboQuant | filterable HNSW via payload indexes (create before ingest) | sparse vectors; Query API fusion (RRF, DBSF) | payload-based tenant partitioning or collection per tenant | Apache 2.0 |
| **Milvus / Zilliz** | Lite (embedded), standalone, distributed on Kubernetes; managed (Zilliz) | FLAT, IVF_FLAT, IVF_SQ8, IVF_PQ, HNSW, DISKANN, SCANN, IVF_RABITQ, GPU indexes | SQ8, PQ, RaBitQ | boolean expressions; partition key | sparse index; built-in BM25 (2.5+) | partition key, partitions, collections, databases | Apache 2.0 |
| **Weaviate** | single node or cluster; managed | HNSW, flat, dynamic (flat → HNSW as it grows) | PQ, BQ, SQ | filtered HNSW with inverted index | built-in BM25 + vector hybrid with fusion | native multi-tenancy (one shard per tenant, can be offloaded) | BSD-3 |
| **Elasticsearch** | cluster; managed | Lucene HNSW, flat | int8, int4, BBQ (better binary quantization) | Lucene filters applied during HNSW search | best-in-class BM25; RRF retriever | index per tenant or filtered alias | Elastic / SSPL / AGPL |
| **OpenSearch** | cluster; managed (AWS) | HNSW, IVF (Lucene, Faiss engines) | fp16, int8, binary, PQ (Faiss) | efficient filtering (engine-dependent) | BM25 + hybrid query with score normalization | index per tenant or filters | Apache 2.0 |
| **LanceDB** | embedded library; managed | IVF_PQ, IVF + HNSW variants | PQ, SQ | SQL-like prefilter / postfilter | full-text search index | table per tenant | Apache 2.0 |
| **turbopuffer** | managed only | centroid-based ANN on object storage | (internal) | attribute filters | BM25 full-text + vector | namespace (cheap, many) | proprietary |
| **Pinecone** | managed only (serverless) | proprietary | proprietary | metadata filters | sparse-dense vectors | namespace | proprietary |
| **Redis** | in-memory; managed | HNSW, FLAT | fp16 / (version-dependent) | tag/numeric filters | full-text in Query Engine | key prefix / index per tenant | RSAL / SSPL / AGPL |
| **FAISS** | library (C++/Python), not a database | Flat, IVF, IVF-PQ, HNSW, CAGRA on GPU | SQ, PQ, OPQ, binary | ID selector only | none | none — you build it | MIT |

How to read it: every product can do "vector search". They differ in the last five columns, and
those columns are what your requirements from §5, §7 and §9 will actually test.

### 11.3 Decision flow with numbers

```
START: compute N (chunks, from 02 §12), d (dims), selectivity distribution (§7.4),
       tenant count and tenant-size distribution, QPS, freshness promise (§8.3)

1. Is N (or every tenant's N) < ~100K?
      yes → brute force. pgvector without an HNSW index, or any store's exact mode.
            Recall 1.0, a few ms, nothing to tune. Stop.

2. Do you already run Postgres, and is N < ~10–50M and d ≤ 4,000 (halfvec)?
      yes → pgvector (HNSW, halfvec). Filters in SQL, ACLs joined in the same transaction.
            Add pgvectorscale if memory is tight (DiskANN) or filters are selective.
            Leave only when a §10.4 trigger fires.

3. Do you already run Elasticsearch/OpenSearch, and is hybrid (BM25 + vector) the main need?
      yes → use its kNN (HNSW + int8/BBQ) with RRF. One system, best lexical engine.

4. Many tenants (thousands+), most of them small and idle?
      yes → namespace-native stores: turbopuffer / Pinecone serverless (object storage, pay per
            use) or Weaviate/Qdrant multi-tenancy. Measure cold-start latency (§9.2, Case 7).

5. Single large corpus, N > ~100M, or you need GPU/DiskANN/IVF-PQ choices?
      yes → Milvus/Zilliz (widest index menu, distributed) or a DiskANN-based design (§2.4).

6. Otherwise (10M–100M, dedicated store, strong filtering needs):
      → Qdrant or Weaviate. Pick by team familiarity and managed-offering pricing after the
        bake-off in §11.5.
```

The thresholds are orders of magnitude, not hard lines: 10–50M vectors in Postgres works fine on a
large box if the vectors aren't fighting the OLTP working set for RAM.

### 11.4 Real-world scenarios

| Scenario | Numbers | Choice | Why |
|---|---|---|---|
| Internal wiki / support search for one company | 500K chunks × 1024 dims; 5 QPS; ACL filter per user group | **pgvector**, HNSW, `halfvec` | ~1.2 GB index; ACLs are a SQL join in the same transaction; zero new infrastructure |
| B2B SaaS, 3,000 tenants, power-law sizes | 60M chunks total; median tenant 8K; top 10 tenants 50% | **Postgres partitioned by tenant** (exact scan for small tenants, HNSW on large partitions), or **Qdrant** with tenant-partitioned payload index | small tenants get recall 1.0 via brute force; large tenants get their own graph (§17 Case 2) |
| E-commerce product search, hybrid is critical | 20M products; SKU and brand exact matches matter; 500 QPS | **Elasticsearch/OpenSearch**, BM25 + kNN + RRF | lexical precision on SKUs, facets and aggregations already there |
| "Chat with your files" consumer app | 2M users × ~2K chunks; each user active a few times a week | **turbopuffer / Pinecone serverless / LanceDB on S3** | 98% of namespaces idle at any moment; object storage cost ≪ RAM; pre-warm on session start (§17 Case 7) |
| Web-scale semantic search / dedup | 1B+ vectors × 768 dims; batch + online | **Milvus** (IVF_PQ / DISKANN / GPU) or **FAISS** in a custom service | fp32 in RAM would be ~3.4 TB; needs PQ/DiskANN and sharding (§16.2) |
| Prototype / notebook / edge device | < 1M vectors, no server | **FAISS / LanceDB / pgvector in Docker** | embedded, zero ops; migrate later from the persisted artifacts (`02` §2) |

### 11.5 The one-week bake-off protocol

Vendor benchmarks and leaderboards measure their data at their defaults. Run this on yours:

```
Day 1  Data: 1–5M real chunks (or 10% of prod), real embeddings, 500 real queries,
       the real filter distribution (§7.4). Compute exact ground truth (§3.1), unfiltered AND filtered.
Day 2  Load into each candidate (2–3 max). Record: load time, build time, RAM, disk.
Day 3  Tune each to the SAME recall target (e.g. recall@10 ≥ 0.95) — sweep ef/nprobe/oversampling.
       Record p50/p99 at that recall, single-client and at target concurrency.
Day 4  Filtered: recall + latency per selectivity band. Hybrid if you need it.
Day 5  Operations: delete 20% and re-measure recall; kill a node; restore from backup;
       upgrade a version; measure time-to-searchable for a new document.
       Price: monthly cost at your size × replicas (§12), managed vs self-hosted.
```

Score with explicit weights decided **before** Day 1 so the result can't be argued backwards:

| Criterion | Example weight | Measured as |
|---|---:|---|
| Filtered recall at target latency | 30% | traffic-weighted recall (Lab 5) at p99 ≤ budget |
| Monthly cost at 12-month projected size | 25% | §12 four-line model |
| Operational fit | 20% | team already runs it? backup/restore, upgrades, on-call knowledge |
| Freshness / write path | 10% | seconds from upsert to searchable; read-your-writes support |
| Hybrid & features | 10% | BM25, sparse, fusion, multi-tenancy primitive |
| Lock-in / exit cost | 5% | open source? export path? |

### 11.6 Questions to ask any vendor

1. At our N and d, how much RAM per replica at fp32, and with your recommended quantization?
2. What happens to filtered recall at 0.1% selectivity, and which filter strategy do you use?
3. Are deletes tombstones? How does compaction work, and what does it cost at query time?
4. How long until an upserted vector is searchable? Is read-your-writes available?
5. Which query-time knobs are exposed (`ef`, oversampling, exact mode for ground truth)?
6. Can we export all vectors and payloads in bulk? In what format?
7. What does a cold namespace/collection cost in first-query latency?
8. Pricing: what dimension is billed (storage, read units, write units, pods), and what does *our*
   traffic cost?

A vendor that can't answer 2, 3 and 7 with numbers hasn't run those experiments. That means you
will have to run them yourself.

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

## 16. Interview questions and system design prompts

Same format as `01` §17: each question names the sections it draws from and gives the answer
structure an interviewer is listening for, not just the facts.

### 16.1 Conceptual questions — "explain X"

**Q: Why do we need an approximate index at all? When is brute force fine?**
*Sections: §1.0, §2.1*
Do the arithmetic out loud: exact search is `N × d` multiply-adds and reads every vector. 10M ×
768 fp32 = 30.7 GB read per query ≈ 300 ms on memory bandwidth alone. HNSW computes ~2–4K distances
≈ 1 ms. Then the strong half of the answer: brute force is the *right* choice under ~100K vectors,
for per-tenant subsets that small, and always for ground truth. Candidates who reach for HNSW for a
20K-document corpus signal they don't know the cost curve.

**Q: Explain how HNSW search works, and what `ef_search`, `M`, `ef_construction` do.**
*Sections: §2.3*
Structure: (1) layered graph, level drawn as `floor(−ln U / ln M)` so each layer has ~1/M of the
nodes below → ~`log_M N` layers; (2) greedy descent with beam width 1 on the upper layers; (3)
beam search at layer 0 with a result heap of size `ef_search`, stopping when the closest unexpanded
candidate is farther than the worst result. Then the parameters: `ef_search` = beam width at query
time (free to change, latency ~linear, must be ≥ k); `M` = out-degree (memory ≈ `M × 8–10` B/vector,
rebuild to change); `ef_construction` = beam width during insert (build time only, better graph).
Bonus points: the diversity heuristic for neighbour selection and why it preserves long edges
(§2.3.4).

**Q: What does "recall 0.95" mean for a vector index? How do you measure it?**
*Sections: §1.1, §3.1*
`recall@k = |ANN_k ∩ Exact_k| / k`, averaged over real queries, with exact top-k from brute force
under the *same metric*. Must add: (a) use real query vectors, not sampled corpus vectors
(inflates recall); (b) report it with a CI (`SE = s/√Q`) and with latency, since a recall number
without latency is half a result; (c) this is *index* recall, not relevance — an index at 1.00
recall still returns wrong documents if the embedding is wrong.

**Q: HNSW vs IVF — when would you choose each?**
*Sections: §2.2, §2.3, §4.5*
IVF: k-means into `nlist ≈ √N…16√N` cells, scan `nprobe` cells. Fast build, low memory, but
centroids go stale under continuous inserts and recall at a given latency is worse. HNSW: best
recall–latency curve, handles incremental inserts, but slow build, ~130–150 B/vector graph
overhead at M=16, and deletes are tombstones. Choose IVF (or IVF-PQ) for bulk-loaded, periodically
rebuilt, memory-constrained or billion-scale corpora; HNSW for continuously updated, latency-critical
ones up to the RAM you can afford.

**Q: Explain product quantization.**
*Section: §2.5*
Split a `d`-dim vector into `m` sub-vectors, k-means 256 centroids per sub-space, store `m` bytes.
At query time build an `m × 256` distance table once; each vector's distance is `m` lookups + adds.
Example: 768-dim, `m = 96` → 96 B vs 3,072 B (32×). Trade-off: training step, largest accuracy loss;
always rescore the top candidates with full vectors when accuracy matters.

**Q: Why does binary quantization work for 3072-dim embeddings but not for 384-dim?**
*Sections: §2.5, §6.2*
Hamming distance on sign bits estimates the angle: each bit differs with probability `θ/π`. The
separation between a true neighbour and a distractor grows with `d`, noise with `√d`, so
separability ∝ `√d`. At 40° vs 45°: gap/std ≈ 1.3 at d=384, ≈ 3.6 at d=3072. Hence binary +
oversampling + rescoring at high dimensions; int8 or 4-bit at low dimensions.

**Q: Why is filtered vector search hard?**
*Section: §7*
Post-filter: retrieve k then drop non-matching → at selectivity `s` you keep ~`k × s` results (10 ×
1% = 0.1 rows). Pre-filter + brute force: correct but linear in the number of matching rows.
In-graph filtering: excluded nodes can't be traversed, so the graph falls apart into disconnected
pieces under strict filters. Name the three regimes (>20%, 0.1–20%, <0.1%) and the real fixes:
filter-aware graphs (ACORN, Qdrant payload edges, Filtered DiskANN), iterative scans (pgvector
0.8+), or partitioning by tenant.

**Q: What happens when you delete vectors from an HNSW index?**
*Section: §8.1*
Soft delete: node stays, marked dead, because removing it would break paths through it. Deleted
nodes still use memory, still cost distance computations, and still occupy slots in the `ef` beam —
so at 30% tombstones `ef = 100` behaves like ~70 and recall decays with no deploy. Fix: monitor
tombstone ratio, compaction/VACUUM, periodic rebuild.

### 16.2 System design round

**Q: Design vector search for a B2B SaaS knowledge base: 2,000 tenants, 50M chunks total, 1024-dim
embeddings, p95 < 100 ms end-to-end, strict tenant isolation.**

```
1. SIZE IT (§5)
   50M × (1024 × 4 + 16 × 9 + 150) ≈ 50M × 4.39 KB ≈ 220 GB fp32 in RAM — before replicas.
   halfvec: ≈ 50M × 2.34 KB ≈ 117 GB.  int8 + rescore-from-disk: ≈ 50M × 1.3 KB ≈ 66 GB resident.

2. LOOK AT THE TENANT DISTRIBUTION (§7.2, §9.3)
   Tenant sizes are usually power-law: e.g. top 20 tenants = 60% of chunks, median tenant ≈ 5K chunks.
   - Median tenant at 5K chunks: brute force is < 1 ms. No ANN needed.
   - Big tenants (1–5M chunks): need their own HNSW index.
   - A single global index with WHERE tenant_id = ? puts every small tenant at < 0.01%
     selectivity → post-filter returns nothing, in-graph filter collapses.

3. INDEX LAYOUT
   - Partition by tenant: per-tenant collection/namespace (Qdrant/turbopuffer) or
     Postgres partitioned table / partial indexes for large tenants; small tenants scanned exactly.
   - Tenant isolation becomes structural (can't leak across partitions) — also a security win.

4. REMAINING FILTERS INSIDE A TENANT (ACL, doc type, date)
   - Measure selectivity distribution (Lab 5). Use iterative scan (pgvector) or
     payload-indexed filterable HNSW (Qdrant; create payload indexes BEFORE ingest).

5. TUNING
   - Ground truth per tenant size band; sweep ef_search to recall ≥ 0.95 at p99 within the
     retrieval share of the latency budget (e.g. 20 ms of 100 ms; the rest is rerank + LLM).

6. OPERATIONS
   - Tombstone ratio + segment count alerts; rebuild cadence from Lab 7.
   - index_version stamped per vector; shadow-build + swap for rebuilds.
```

**What interviewers are listening for:** you computed memory before naming a product; you noticed
the tenant-size distribution makes the global-index + filter design fail for small tenants; you
used brute force where it's cheaper; you tied the recall target to a latency budget.

**Q: Design semantic search over 1B vectors (768-dim) with a limited budget.**

```
fp32 in RAM: 1B × ~3.37 KB ≈ 3.4 TB → ~30 × 128 GB nodes before replicas. Too expensive.
Options, all using "cheap to search, exact to rank" (§6.1):
  a) IVF-PQ (FAISS): nlist = 65,536 (≈ 2√N), m = 96 → ~100 B/vector ≈ 100 GB in RAM, rescore top-100
     from SSD-resident full vectors.
  b) DiskANN (pgvectorscale / Milvus DISKANN): PQ codes in RAM (~64–96 GB), graph + full vectors
     on NVMe (~3.5 TB), ~5–10 ms p50.
  c) Binary quantization + 4× oversampling + rescore from SSD: 1B × 96 B = 96 GB of bits in RAM.
Then shard by ID hash across nodes, scatter-gather top-k, merge.
Decision driver: QPS. Low QPS → DiskANN on a few NVMe nodes. High QPS → IVF-PQ in RAM, more replicas.
```

**Q: pgvector or a dedicated vector database for our new RAG feature?**
*Section: §10*
Start from "Postgres unless a named trigger fires": transactions across vectors + ACLs, real SQL
filters and joins, one system to operate. Then check the triggers: > ~10–50M vectors competing
with the OLTP working set, dimensions > 2,000 (HNSW limit on `vector` — use `halfvec` up to 4,000),
thousands of tenant namespaces, native hybrid scoring, rebuild times blocking iteration. Name the
middle path (pgvectorscale, VectorChord).

### 16.3 Rapid-fire questions

| Question | Strong answer | Section |
|---|---|---|
| What is `ef_search`? | Size of the result heap (beam width) in HNSW's layer-0 search. Query-time, ≥ k, latency ~linear in it. | §2.3.2 |
| Why does `LIMIT 100` return 40 rows in pgvector? | `hnsw.ef_search` defaults to 40; the beam can't hold more than 40 results. Set `ef_search ≥ k`. | §4.2 |
| How many layers does HNSW have for 10M vectors, M=16? | `log_16(10⁷) ≈ 5.8` → ~6 layers; layer 1 has ~625K nodes. | §2.3.1 |
| Memory of HNSW for 10M × 1536-dim fp32, M=16? | `(6,144 + 144 + 150) B × 10M ≈ 64 GB`, × replicas. | §5 |
| Optimal IVF `nlist`? | `√N` minimizes centroid + scan cost with 1 probe; FAISS recommends 4√N–16√N. | §2.2 |
| What does `nprobe = nlist` give you? | Exact search — and in pgvector the planner stops using the index. | §2.2, §4.5 |
| Cosine vs dot product vs L2? | Identical ranking on normalized vectors (`‖a−b‖² = 2 − 2cos`). Different otherwise. | §1.0.1 |
| What's oversampling? | Retrieve `k × o` candidates with quantized vectors, rescore exactly, return k. Query-time knob. | §6.4 |
| int8 quantization error? | `Δ/2` per dim with `Δ = range/255` — about 1% of a typical component. 4× smaller, near-zero recall loss. | §2.5 |
| Why does recall degrade after big ingests with no config change? | Same `ef_search` explores the same absolute number of nodes in a larger graph. Re-sweep after growth. | §4.3 |
| Why does recall degrade over months with no ingest growth? | Tombstones consume the `ef` beam and traversal. Rebuild/compact. | §8.1 |
| Post-filter at 1% selectivity, k=10 — expected results? | `10 × 0.01 = 0.1` rows. | §7.1 |
| When is a Qdrant payload index "useless"? | When created after ingestion — the extra filter edges are only built for data indexed after it exists. | §7.3 |
| Max dims for pgvector HNSW? | `vector` 2,000; `halfvec` 4,000; `bit` 64,000; `sparsevec` 1,000 non-zeros. | §5.1 |

### 16.4 Debugging prompts — "here are the symptoms, diagnose"

**"Search quality dropped last week. Nobody deployed anything."**
Ordered checklist: (1) corpus grew — compare N now vs when `ef_search` was tuned; re-run the §3
sweep; (2) tombstone ratio / dead tuples (§8.1); (3) segment count — compaction falling behind
(§8.2); (4) new tenants/filters shifted the selectivity mix (§7.4); (5) only then look upstream
(embedding version drift, `01` §12). The key move is *measuring index recall first*, because it
separates index problems from relevance problems in minutes.

**"Our small customers say search returns nothing; big customers are fine."**
Selectivity. Global index + `WHERE tenant_id` → post-filtering or graph fragmentation for tenants
at < 1% of rows. Confirm by bucketing per-tenant result counts by tenant size. Fix: iterative scan,
filter-aware index, or per-tenant partitions; small tenants → exact scan.

**"We enabled binary quantization and recall went from 0.97 to 0.78."**
Check: dimension (< ~1,000 → binary is the wrong rung), is rescoring enabled, what's the
oversampling factor, are the originals on disk (rescoring from disk may have been disabled for
latency). Sweep oversampling 1→8 before concluding anything (§6.4).

**"p50 is 8 ms in the benchmark, p50 is 600 ms in production."**
Warm vs cold: object-storage or mmap'd index with a working set larger than cache; per-tenant
traffic too sparse to keep namespaces warm (§9.2). Or: rescoring from disk at high oversampling
(§6.4). Or: `EXPLAIN` shows a sequential scan because the operator doesn't match the index's
operator class (§1.0.1).

### 16.5 Napkin-math questions (with answers)

1. *How long to brute-force 1M × 1024 fp32 on one core at ~10 GB/s?* 4.1 GB → ~0.4 s. With 16 cores
   and ~100 GB/s bandwidth: ~40 ms.
2. *IVF with N = 100M, nlist = 40,000, nprobe = 40: how many vectors scanned?* 100M/40,000 = 2,500
   per list × 40 = 100K vectors (0.1%) + 40K centroids.
3. *PQ with d = 1536, m = 192: bytes/vector, compression?* 192 B vs 6,144 B → 32×.
4. *Binary 3072-dim: bytes/vector?* 3072/8 = 384 B (vs 12,288 B fp32).
5. *200 queries, per-query recall std 0.1 — smallest recall difference you can trust?* SE =
   0.1/√200 = 0.007; 95% CI ≈ ±0.014 → differences below ~0.02 are noise.
6. *30% tombstones, `ef_search` = 200 — effective beam?* ≈ 140 live candidates.

### 16.6 Common interview mistakes

1. **Naming a product before doing the memory math.** The sizing (§5) eliminates most options in
   five minutes; start there.
2. **Quoting recall without latency, or QPS without recall.** Always a pair (§3.2).
3. **Confusing index recall with retrieval relevance.** "Our recall is 0.99" means nothing about
   answer quality (§1.1).
4. **Ignoring filters.** Designing for unfiltered top-k when every production query has a tenant or
   ACL filter (§7).
5. **Treating build-time parameters as tunable knobs.** `M` and `ef_construction` require a rebuild;
   `ef_search`, `nprobe` and oversampling don't — tune those first (§4.1).
6. **Forgetting deletes and updates.** A freshly built benchmark index isn't what runs after six
   months (§8).

---

## 17. Real-world cases — incidents with numbers

These are **composite scenarios** built from failure modes documented in the pgvector, Qdrant and
turbopuffer docs cited in this chapter and from common production patterns. They aren't specific
companies' post-mortems. Numbers are illustrative but internally consistent: you can recompute every
one of them.

### Case 1 — "`LIMIT 50` returns 40 rows"

**Setup.** Support-ticket search on pgvector, 3M chunks, HNSW with defaults. The product added a
"show more" button that raised `LIMIT` from 10 to 50.

**Symptom.** Every query returns exactly 40 rows. No error. The UI shows "40 results" for queries
that obviously have hundreds of matches.

**Diagnosis.** `hnsw.ef_search` defaults to 40, and the HNSW result heap can't hold more than `ef`
items (§2.3.2).

**Fix.**
```sql
SET hnsw.ef_search = 100;   -- or SET LOCAL per request: max(2 * limit, 100)
```
Plus the application guard from §4.2 (`assert ef_search >= k`).

**Lesson.** This is a correctness bug that looks like a quality bug. Every search call path should
tie `ef_search` to `k`.

### Case 2 — Small tenants get empty results

**Setup.** Multi-tenant SaaS on a dedicated store, one global HNSW index of 40M chunks, queries with
`filter: tenant_id = X`. The store's filter path was post-filtering on top of a `k × 10` candidate
fetch.

**Symptom.** Enterprise customers are happy. 70% of tickets saying "search is broken" come from
tenants with < 20K chunks.

**Math.** A 20K-chunk tenant is `20,000 / 40,000,000 = 0.05%` of the index. Fetching `10 × 10 = 100`
candidates and post-filtering leaves an expected `100 × 0.0005 = 0.05` results. For the 2M-chunk
tenant (5%), the same fetch leaves ~5 — degraded but not empty, so nobody noticed there.

**Fix.** Split by tenant size: tenants < 100K chunks → exact scan of the tenant's vectors (100K ×
1024 ≈ 400 MB read → ~5 ms with a filter index on `tenant_id`, recall 1.0); larger tenants → their
own collection/partition with its own HNSW. Measured with §7.4's per-selectivity-band table:
traffic-weighted recall went from "0.97 unfiltered" to a true 0.96 across bands, where the old
design's true traffic-weighted recall had been ~0.6.

**Lesson.** The unfiltered benchmark number described a system nobody used. Measure recall per
selectivity band with filtered ground truth.

### Case 3 — Recall decays over six months with no deploys

**Setup.** Internal wiki search, HNSW, `ef_search = 64` tuned at launch on 2M chunks to recall 0.96.
Documents are edited often; each edit deletes old chunks and inserts new ones.

**Symptom.** Relevance complaints increase slowly. Offline eval on the golden set drops 6 points.
The embedding model, chunker and config are unchanged.

**Measurements.**
- Live chunks: 2M → 5M (2.5× growth).
- Tombstones: 38% of graph nodes are deleted (heavy edit churn).
- Index recall@10 at `ef_search = 64`, re-measured with the §3 harness: **0.84**.

**Diagnosis.** Two effects stacked: the same `ef` explores the same absolute number of nodes in a
2.5× bigger graph (§4.3), and ~38% of the beam is wasted on dead nodes (§8.1).

**Fix.** Rebuild (drops tombstones) → recall 0.91 at `ef = 64`; re-sweep → `ef = 128` reaches 0.965
at p99 +1.8 ms. Added alerts: tombstone ratio > 20%, and "N grew > 50% since last `ef` sweep".
Monthly rebuild via shadow-index-and-swap (§8.4).

**Lesson.** Index recall is a number that decays. Re-measure it on a schedule, not only at launch.

### Case 4 — 3072-dim embeddings on pgvector

**Setup.** Team picks OpenAI `text-embedding-3-large` (3072 dims) and pgvector.

**Symptom.** `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)` fails: `vector` columns
can only be HNSW-indexed up to 2,000 dimensions (§5.1).

**Options, with sizes for 8M chunks:**

| Option | Bytes/vector (vector only) | 8M chunks | Notes |
|---|---:|---:|---|
| `halfvec(3072)` expression index | 6,152 | ~49 GB | fits the 4,000-dim limit; negligible recall loss |
| Request `dimensions=1536` (Matryoshka) and store `vector(1536)` | 6,152 | ~49 GB | same bytes as halfvec-3072; quality loss measured on golden set |
| `dimensions=1536` + `halfvec` | 3,080 | ~25 GB | both levers composed (§6.6) |
| binary index + rescore on `halfvec` | 392 in index | ~3 GB index + 49 GB table | §6.5 SQL pattern |

**Decision.** `dimensions=1536` + `halfvec`: measured golden-set recall within the CI of full
3072-dim, index fits in RAM next to the OLTP working set.

**Lesson.** Check hard limits (§5.1) before choosing the model; dimension is a cost and compatibility
decision, not just a quality one.

### Case 5 — A payload index added after ingest

**Setup.** Qdrant, 30M vectors ingested, then a `doc_type` payload index added so users can filter
to "policy documents" (~2% of vectors).

**Symptom.** Filtered queries have recall ~0.7 and higher latency; unfiltered is 0.97.

**Diagnosis.** Qdrant's filterable HNSW adds extra graph edges based on payload indexes, but only
for data indexed after the payload index exists (§7.3). The graph was built without them.

**Fix.** Recreate the collection with payload indexes defined *before* ingest (or trigger a full
re-index). Filtered recall at 2% selectivity returns to ~0.95. Runbook updated: "create all payload
indexes at collection creation".

**Lesson.** Filter-aware index structures are build-time decisions. Adding a filter later can
require a rebuild.

### Case 6 — Cost cut 10× with quantization and rescoring

**Setup.** 60M chunks × 1536 dims, fp32 HNSW all in RAM, 2 replicas.

**Before.** `60M × 6.44 KB ≈ 386 GB` per replica → 2 × ~400 GB RAM nodes.

**Change.** Binary quantization in RAM (`1536/8 = 192 B` + graph ≈ 340 B/vector ≈ 20 GB), full
vectors on local NVMe, oversampling swept `{1, 2, 4, 8}`:

| Oversample | recall@10 | p50 | p99 |
|---:|---:|---:|---:|
| 1 (no rescore) | 0.82 | 2 ms | 6 ms |
| 2 | 0.93 | 3 ms | 9 ms |
| 4 | 0.97 | 4 ms | 14 ms |
| 8 | 0.985 | 6 ms | 25 ms |

**Decision.** 4× oversampling: recall 0.97 (target 0.95), p99 14 ms within a 30 ms budget. RAM per
replica: ~386 GB → ~20 GB + NVMe. Monthly index cost dropped roughly an order of magnitude.

**Lesson.** Quantization plus rescoring is a two-stage cascade, and oversampling is the one knob to
sweep. Measure p99, not only p50, because rescoring reads from disk.

### Case 7 — Cold namespaces on object storage

**Setup.** Per-user "chat with your files" product on an object-storage-backed store, 400K users,
each with their own namespace (avg 3K chunks).

**Symptom.** Benchmark p50 ≈ 15 ms; production p50 ≈ 800 ms.

**Diagnosis.** Access logs: median user queries once every ~2 days. Cache TTL is hours, so ~90% of
first-in-session queries hit a cold namespace (§9.2).

**Fix.** Warm the namespace when the user opens the app (a pre-flight query fired from the session
start), so the cold read overlaps with the user typing. Cold-hit rate on real queries → ~15%,
blended p50 ≈ 30 ms.

**Lesson.** For object-storage architectures, measure inter-arrival time per namespace before
believing a warm benchmark.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — why post-filtering under-returns,
why a strict filter disconnects an HNSW graph, why tombstones consume the `ef_search` budget, why
quantize-then-rescore has a weaker accuracy requirement than quantize-and-return — are derivable from
the algorithm as described in `../databases/11-hnsw-vector-search-internals.md` and from the vendor
documentation cited inline. The arithmetic in §5 and §12 is derivable rather than measured: every
input is labeled as an assumption and every output is checkable with a calculator.
§2's formulas (HNSW level distribution, the layer-0 beam search, IVF cost and the `√N` optimum,
scalar/binary/PQ encodings, the `θ/π` Hamming estimate) are standard results from the HNSW, FAISS
and SimHash/RaBitQ literature; the §2.3.2 toy trace was checked by running the pseudocode. The
§17 cases are composites whose numbers are illustrative and recomputable, not measurements.

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
