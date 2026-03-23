# Vector Search Index Internals: IVF, Product Quantization, and Scalar Quantization

A staff-engineer-level guide to how vector search indexes actually work under the hood -- covering IVF (Inverted File Index) with Voronoi partitioning, Product Quantization (PQ), Optimized Product Quantization (OPQ), ScaNN's anisotropic quantization, scalar quantization (int8/binary), and Matryoshka embeddings. Includes ASCII diagrams, mathematical foundations, compression calculations, and production tradeoff analysis.

---

## Table of Contents

1. [The Fundamental Problem: Searching High-Dimensional Space](#1-the-fundamental-problem)
2. [IVF (Inverted File Index) Deep Dive](#2-ivf-inverted-file-index-deep-dive)
3. [Product Quantization (PQ) Deep Dive](#3-product-quantization-pq-deep-dive)
4. [Optimized Product Quantization (OPQ)](#4-optimized-product-quantization-opq)
5. [ScaNN: Anisotropic Vector Quantization](#5-scann-anisotropic-vector-quantization)
6. [Scalar Quantization](#6-scalar-quantization)
7. [Matryoshka Embeddings](#7-matryoshka-embeddings)
8. [Composite Indexes: IVF-Flat vs IVF-PQ vs IVF-HNSW](#8-composite-indexes)
9. [Production Decision Matrix](#9-production-decision-matrix)

---

## 1. The Fundamental Problem

### Why Brute-Force Fails at Scale

Given N vectors of dimension D, a brute-force nearest neighbor search computes distance to every vector:

```
Brute-force k-NN search:

  Query vector q (D dimensions)
         │
         ▼
  ┌──────────────────────────────────────────┐
  │  Compare q to EVERY vector in database   │
  │                                          │
  │  d(q, x₁) = 0.87                        │
  │  d(q, x₂) = 0.23   ← candidate          │
  │  d(q, x₃) = 0.91                        │
  │  ...                                     │
  │  d(q, xₙ) = 0.45                        │
  │                                          │
  │  N distance computations                 │
  │  Each: O(D) multiply-adds               │
  │  Total: O(N × D)                        │
  └──────────────────────────────────────────┘

  For 1B vectors, D=768:
    1,000,000,000 × 768 = 768 billion FLOPs per query
    Memory: 1B × 768 × 4 bytes = 3.07 TB (float32)
```

Vector search indexes solve this with two orthogonal strategies:

```
Strategy 1: SEARCH FEWER VECTORS (Partitioning)
  └── IVF, HNSW, tree-based indexes
  └── Reduce N to a small subset

Strategy 2: MAKE EACH COMPARISON CHEAPER (Compression)
  └── Product Quantization, Scalar Quantization
  └── Reduce D or reduce bytes-per-dimension

Best approach: COMBINE BOTH
  └── IVF-PQ: partition + compress = fast AND small
```

---

## 2. IVF (Inverted File Index) Deep Dive

### 2.1 Voronoi Partitioning of Vector Space

IVF partitions the vector space into K regions using **Voronoi cells** (also called Dirichlet tessellation). Each region contains all points closer to its centroid than to any other centroid.

```
Voronoi Partitioning (2D visualization of high-D space):

  Given K=6 centroids (★), space divides into cells:

  ┌─────────────────────────────────────────────┐
  │            ╱    ╲                            │
  │     ★ C₁ ╱      ╲   ★ C₂                   │
  │   •  •  ╱   •    ╲  •  •  •                │
  │  •    • ╱  •       ╲   •                    │
  │ ───────╱────────────╲──────────             │
  │       ╱  ★ C₃   •   ╲     ★ C₄            │
  │      ╱ •    •   •  •  ╲  •   •  •          │
  │     ╱    •        •    ╲    •               │
  │ ───╱────────────────────╲──────────         │
  │   ╱   •    ★ C₅    •    ╲   ★ C₆          │
  │  ╱  •    •    •   •      ╲ •    •          │
  │ ╱     •          •        ╲   •             │
  └─────────────────────────────────────────────┘

  Mathematical definition of Voronoi cell Vᵢ:
    Vᵢ = { x ∈ ℝᴰ : ‖x - cᵢ‖ ≤ ‖x - cⱼ‖  ∀ j ≠ i }

  where cᵢ is centroid i, and ‖·‖ is the distance metric (L2/cosine).

  Properties:
  - Every point belongs to exactly one cell
  - Cell boundaries are (D-1)-dimensional hyperplanes
  - Adjacent cells share a boundary face
  - Cells are convex polytopes in ℝᴰ
```

### 2.2 K-Means Clustering for Centroid Selection

The centroids are computed via **k-means clustering** on the training data (or a representative sample):

```
K-Means for IVF Centroid Training:

  Input:  Training vectors X = {x₁, x₂, ..., xₜ}, desired K clusters
  Output: Centroids C = {c₁, c₂, ..., cₖ}

  Algorithm (Lloyd's iteration):
  ─────────────────────────────────────────────────────

  Step 0: Initialize K centroids (random or k-means++)

    k-means++ initialization (preferred):
    1. Pick c₁ uniformly at random from X
    2. For i = 2..K:
       - For each x, compute D(x) = min distance to existing centroids
       - Pick next centroid with probability ∝ D(x)²
       - This spreads centroids apart, avoiding poor local minima

  Step 1: ASSIGN each vector to nearest centroid

    For each xⱼ:
      a(xⱼ) = arg min ‖xⱼ - cᵢ‖²
               i∈{1..K}

  Step 2: UPDATE each centroid to mean of assigned vectors

    For each cᵢ:
      cᵢ = (1/|Sᵢ|) × Σ xⱼ     where Sᵢ = { xⱼ : a(xⱼ) = i }
                      xⱼ∈Sᵢ

  Step 3: Repeat Steps 1-2 until convergence
    (convergence = centroids move less than ε, or max iterations)

  ─────────────────────────────────────────────────────

  Typical configurations:
  ┌─────────────────┬──────────┬────────────────────────────┐
  │ Dataset Size    │ K (nlist)│ Vectors per cell (avg)     │
  ├─────────────────┼──────────┼────────────────────────────┤
  │ 100K            │ 256      │ ~390                       │
  │ 1M              │ 1,024    │ ~976                       │
  │ 10M             │ 4,096    │ ~2,441                     │
  │ 100M            │ 16,384   │ ~6,103                     │
  │ 1B              │ 65,536   │ ~15,258                    │
  └─────────────────┴──────────┴────────────────────────────┘

  Rule of thumb: K ≈ √N  (for balanced tradeoff)
  More aggressive: K ≈ 4×√N (for faster queries, more cells to probe)
```

### 2.3 The Inverted File Structure

After clustering, the index stores an **inverted list** mapping each centroid to its member vectors:

```
IVF Index Structure:

  Centroid Table (coarse quantizer):
  ┌────────┬────────────────────────────────────┐
  │  ID    │  Centroid Vector (D dimensions)     │
  ├────────┼────────────────────────────────────┤
  │  0     │  [0.12, -0.34, 0.56, ..., 0.78]   │
  │  1     │  [0.91, 0.23, -0.45, ..., 0.11]   │
  │  ...   │  ...                               │
  │  K-1   │  [-0.33, 0.67, 0.89, ..., -0.12]  │
  └────────┴────────────────────────────────────┘

  Inverted Lists (posting lists):
  ┌────────┬──────────────────────────────────────────────────┐
  │ Cell 0 │ → [id₃, vec₃] → [id₇, vec₇] → [id₂₂, vec₂₂]  │
  │ Cell 1 │ → [id₁, vec₁] → [id₅, vec₅]                    │
  │ Cell 2 │ → [id₂, vec₂] → [id₉, vec₉] → [id₁₅, vec₁₅]  │
  │   ...  │   ...                                            │
  │ Cell K │ → [id₄, vec₄]                                   │
  └────────┴──────────────────────────────────────────────────┘

  Each posting list entry contains:
  - Vector ID (int64): 8 bytes
  - Vector data: depends on variant
    - IVF-Flat: full float32 vector (D × 4 bytes)
    - IVF-PQ:   PQ code (m bytes, where m = number of subquantizers)
    - IVF-SQ:   scalar-quantized vector (D bytes for int8)
```

### 2.4 Query Processing and the nprobe Parameter

```
IVF Search Algorithm:

  Input:  Query vector q, parameter nprobe, desired top-k
  Output: Approximate k nearest neighbors

  Phase 1 -- COARSE SEARCH (find nearest centroids):
  ─────────────────────────────────────────────────────
  Compare q to all K centroids → select nprobe nearest ones
  Cost: O(K × D)    ← cheap since K << N

    q ──compare──► [c₀, c₁, c₂, ..., cₖ₋₁]
                         │
                    Sort by distance
                         │
                    Take top nprobe
                         ▼
                   {c₃, c₇, c₁₂}  (if nprobe=3)

  Phase 2 -- FINE SEARCH (scan posting lists):
  ─────────────────────────────────────────────────────
  For each of the nprobe selected cells:
    Scan ALL vectors in that cell's posting list
    Compute exact (or approximate) distance to q
    Maintain a max-heap of top-k results

    Cell 3:   d(q, x₁₂)=0.21, d(q, x₅₅)=0.89, d(q, x₃₃)=0.15, ...
    Cell 7:   d(q, x₇)=0.33,  d(q, x₉₁)=0.12, ...
    Cell 12:  d(q, x₄₄)=0.67, d(q, x₈₈)=0.09, ...

    Cost: O(nprobe × avg_vectors_per_cell × D)

  Total vectors scanned: nprobe × (N / K)
```

**nprobe tradeoffs with concrete numbers:**

```
nprobe Effect on Recall vs Speed (1M vectors, K=1024, D=128):

  ┌────────┬──────────────┬───────────────┬──────────────────┐
  │ nprobe │ Recall@10    │ Search Time   │ Vectors Scanned  │
  ├────────┼──────────────┼───────────────┼──────────────────┤
  │ 1      │ ~45-55%      │ ~0.1 ms       │ ~976             │
  │ 5      │ ~70-80%      │ ~0.4 ms       │ ~4,880           │
  │ 10     │ ~82-90%      │ ~0.8 ms       │ ~9,760           │
  │ 20     │ ~90-95%      │ ~1.5 ms       │ ~19,520          │
  │ 50     │ ~96-98%      │ ~3.5 ms       │ ~48,800          │
  │ 100    │ ~98-99%      │ ~7.0 ms       │ ~97,600          │
  │ 256    │ ~99.5%+      │ ~18 ms        │ ~250,000         │
  │ 1024   │ 100% (exact) │ ~70 ms        │ 1,000,000 (all)  │
  └────────┴──────────────┴───────────────┴──────────────────┘

  Key insight: Recall grows sub-linearly while cost grows linearly.
  Sweet spot is typically nprobe = 1-10% of nlist.

  Diminishing returns curve:

  Recall
  100%|                                    ●───────── nprobe=1024
     |                              ●
  95%|                        ●
     |                  ●
  90%|            ●
     |       ●
  80%|   ●
     | ●
  50%|●
     └──┬──┬──┬───┬───┬───┬───┬───┬───────── nprobe
        1  5  10  20  50 100 200 500 1024
```

### 2.5 Residual Vectors

In IVF-PQ, instead of encoding the raw vector, we encode the **residual** -- the difference between the vector and its cell centroid. This is more precise because residuals have lower norm and variance.

```
Residual Vector Computation:

  Original vector:     x = [0.82, -0.45, 0.91, 0.33, -0.67, 0.14, ...]
  Assigned centroid:   c = [0.80, -0.40, 0.85, 0.30, -0.70, 0.10, ...]
                          ─────────────────────────────────────────────
  Residual vector:     r = x - c
                       r = [0.02, -0.05, 0.06, 0.03,  0.03, 0.04, ...]

  Why residuals are better for PQ:
  ─────────────────────────────────
  The original vectors might span a wide range (e.g., [-1, +1]).
  After subtracting the centroid, residuals are clustered near zero
  with much smaller variance.

  ‖r‖ << ‖x‖   (residual norm is much smaller)

  Smaller range → k-means codebooks can represent residuals
  with finer granularity → lower quantization error.

  Visual intuition:

  Before (raw vectors):              After (residual vectors):
  ┌──────────────────────┐           ┌──────────────────────┐
  │              •        │           │                      │
  │          •  •  •      │           │         • •          │
  │        •  ★  •  •     │           │        •★•••         │
  │          •   •        │           │         ••           │
  │              •        │           │          •           │
  │  Wide spread around ★ │           │ Tight cluster at 0   │
  └──────────────────────┘           └──────────────────────┘
    PQ must cover large range          PQ covers small range
    → coarse approximation             → fine approximation
```

### 2.6 Multi-Probe Approaches

Standard IVF only searches the `nprobe` nearest cells. **Multi-probe** strategies search additional cells that are likely to contain true nearest neighbors, especially near cell boundaries.

```
Multi-Probe IVF Strategy:

  Problem: Boundary vectors may be closer to the query than
  vectors in the query's assigned cell.

  ┌──────────────────────────────────────┐
  │           │                          │
  │    C₁    │     C₂                   │
  │     ★    │      ★                   │
  │       •  │  •                       │
  │      • q │ • ← these points in C₂   │
  │       • ◆│  •   are CLOSER to q     │
  │        • │      than some in C₁     │
  │          │                          │
  └──────────────────────────────────────┘

  q is assigned to C₁, but nearest neighbors may be in C₂

  Multi-probe approaches:
  ─────────────────────────────────────────

  1. STANDARD nprobe:
     Simply search the nprobe closest centroids.
     Most common approach in practice (FAISS, Milvus, etc.)

  2. BOUNDARY-AWARE PROBING:
     For each query, compute distance to cell boundaries.
     If d(q, boundary) < threshold, also search adjacent cell.
     Focuses extra computation where it helps most.

  3. RADIUS-BASED PROBING:
     Search all cells whose centroid is within distance
     r = α × d(q, nearest_centroid) of the query.
     α > 1.0 is a tunable expansion factor.

  4. ADAPTIVE nprobe:
     Different queries get different nprobe values:
     - Query near a centroid → low nprobe (confident assignment)
     - Query near boundary → high nprobe (ambiguous assignment)

     nprobe(q) = base_nprobe × (d₂/d₁)
     where d₁ = dist to nearest centroid
           d₂ = dist to 2nd nearest centroid
     Ratio d₂/d₁ ≈ 1.0 means boundary → probe more cells

  5. SPILLING (used in ScaNN/SOAR):
     During index construction, assign each vector to MULTIPLE
     cells if it is near a boundary. Increases index size but
     improves recall without increasing nprobe at query time.

     Assignment rule: assign x to cell i if
       d(x, cᵢ) ≤ (1 + ε) × d(x, c_nearest)
     where ε is the spill factor (e.g., 0.1)
```

---

## 3. Product Quantization (PQ) Deep Dive

### 3.1 Subspace Decomposition

Product Quantization decomposes the D-dimensional space into **m** disjoint subspaces, each of dimension D/m (called D* or d_sub).

```
Vector Splitting into Subspaces:

  Original vector x ∈ ℝᴰ (D = 128, float32):

  x = [0.12, -0.34, 0.56, 0.78, 0.91, 0.23, -0.45, 0.11, ..., -0.33, 0.67]
       ├───── 128 dimensions ──────────────────────────────────────────────┤

  Split into m=8 subvectors of D*=16 dimensions each:

  ┌─────────────────┬─────────────────┬───┬─────────────────┐
  │   Sub-vector 1  │   Sub-vector 2  │...│   Sub-vector 8  │
  │   dims [0:16)   │   dims [16:32)  │   │   dims [112:128)│
  │   16 dims       │   16 dims       │   │   16 dims       │
  └─────────────────┴─────────────────┴───┘─────────────────┘

  Each subvector u_j = x[j×D* : (j+1)×D*]

  Formal decomposition:
    x = [u₁, u₂, ..., u_m]
    where uⱼ ∈ ℝ^(D/m), j = 1..m

  The product quantizer is defined as:
    q(x) = [q₁(u₁), q₂(u₂), ..., q_m(u_m)]

  where each qⱼ is an independent sub-quantizer with its own codebook.
```

### 3.2 Codebook Training via K-Means per Subspace

```
PQ Codebook Training:

  For EACH of the m subspaces independently:

  Step 1: Extract sub-vectors from training data
    For subspace j, extract { u_j^(1), u_j^(2), ..., u_j^(T) }
    from all T training vectors.

  Step 2: Run k-means with k* centroids (typically k*=256)
    Produces codebook Cⱼ = { cⱼ,₁, cⱼ,₂, ..., cⱼ,ₖ* }
    Each centroid cⱼ,ᵢ ∈ ℝ^(D/m)

  Why k*=256?
    256 centroids = 8 bits = 1 byte per subspace index
    This is the sweet spot: 2⁸ = 256 gives fine granularity
    while keeping the code at exactly 1 byte.

  ┌─────────────────────────────────────────────────────────┐
  │              PQ Codebook Structure                      │
  │                                                         │
  │  Subspace 1:  C₁ = {c₁,₁, c₁,₂, ..., c₁,₂₅₆}        │
  │               each centroid: 16-dim vector              │
  │               total: 256 × 16 × 4 = 16 KB              │
  │                                                         │
  │  Subspace 2:  C₂ = {c₂,₁, c₂,₂, ..., c₂,₂₅₆}        │
  │               each centroid: 16-dim vector              │
  │               total: 256 × 16 × 4 = 16 KB              │
  │                                                         │
  │  ...                                                    │
  │                                                         │
  │  Subspace 8:  C₈ = {c₈,₁, c₈,₂, ..., c₈,₂₅₆}        │
  │               each centroid: 16-dim vector              │
  │               total: 256 × 16 × 4 = 16 KB              │
  │                                                         │
  │  Total codebook memory: 8 × 16 KB = 128 KB             │
  │  (stored once, shared across all vectors)               │
  └─────────────────────────────────────────────────────────┘

  Encoding a vector:
  ─────────────────
  For each subspace j, find nearest centroid:
    code_j = arg min ‖uⱼ - cⱼ,ᵢ‖²
             i∈{1..256}

  PQ code for x = [code₁, code₂, ..., code_m]
                 = [  47,   182,   ...,    93 ]    ← m bytes total
```

### 3.3 Asymmetric Distance Computation (ADC) vs Symmetric (SDC)

```
Two Modes of Distance Approximation:

═══════════════════════════════════════════════════════════════
  SYMMETRIC DISTANCE COMPUTATION (SDC)
═══════════════════════════════════════════════════════════════

  Both query AND database vectors are quantized to PQ codes.

  d̃_SDC(x, y) = Σ  ‖cⱼ,code_j(x) - cⱼ,code_j(y)‖²
                j=1..m

  Precomputation:
    For each subspace j, build a 256×256 distance table:
    T_j[a][b] = ‖cⱼ,ₐ - cⱼ,ᵦ‖²

  Distance lookup: m table lookups + (m-1) additions.

  Error: quantization error in BOTH x and y.

  ┌─────────────┐        ┌─────────────┐
  │ Query x     │        │ Database y  │
  │ [47,182,93] │        │ [12,199,67] │
  │ (PQ code)   │        │ (PQ code)   │
  └──────┬──────┘        └──────┬──────┘
         │                      │
         └───► T₁[47][12] + T₂[182][199] + ... + T₈[93][67]
                   = approximate d(x,y)


═══════════════════════════════════════════════════════════════
  ASYMMETRIC DISTANCE COMPUTATION (ADC) ← PREFERRED
═══════════════════════════════════════════════════════════════

  Query is kept as FULL vector; only database vectors are quantized.

  d̃_ADC(q, y) = Σ  ‖qⱼ - cⱼ,code_j(y)‖²
                j=1..m

  where qⱼ is the j-th subvector of the UNQUANTIZED query.

  Precomputation (per query):
    For each subspace j, compute distance from qⱼ to all 256 centroids:
    T_j[i] = ‖qⱼ - cⱼ,ᵢ‖²

    This builds m tables, each of size 256.
    Cost: m × 256 × D/m = 256 × D multiplications.

    For D=128, m=8: 256 × 128 = 32,768 FLOPs (tiny, done once per query)

  Distance lookup (per database vector):
    d̃(q, y) = T₁[code₁(y)] + T₂[code₂(y)] + ... + T_m[code_m(y)]
    Cost: m lookups + (m-1) additions = O(m) per vector.

  ┌──────────────────┐        ┌─────────────┐
  │ Query q          │        │ Database y  │
  │ [full float32    │        │ [12,199,67] │
  │  vector, D dims] │        │ (PQ code)   │
  └────────┬─────────┘        └──────┬──────┘
           │                         │
     Build lookup tables             │
           │                         │
           ▼                         ▼
     T₁[12] + T₂[199] + ... + T₈[67] = approximate d(q,y)

  Error: quantization error in y ONLY (not in query).

═══════════════════════════════════════════════════════════════
  COMPARISON
═══════════════════════════════════════════════════════════════

  ┌──────────────┬────────────────┬────────────────┐
  │ Property     │ SDC            │ ADC            │
  ├──────────────┼────────────────┼────────────────┤
  │ Query stored │ As PQ code     │ As full vector │
  │ Error source │ Both sides     │ DB side only   │
  │ Accuracy     │ Lower          │ Higher         │
  │ Speed        │ Similar        │ Similar        │
  │ Query memory │ m bytes        │ D×4 bytes      │
  │ Use case     │ Distributed    │ Standard       │
  │              │ (query=code)   │ (query=full)   │
  └──────────────┴────────────────┴────────────────┘

  Recommendation: ALWAYS use ADC unless you must store queries as
  compressed codes (e.g., in a distributed setting where queries
  are also indexed).
```

### 3.4 Memory Compression Ratios

```
Detailed Memory Calculation:

═══════════════════════════════════════════════════════════════
  EXAMPLE: 128-dimensional float32 vectors, 1 million vectors
═══════════════════════════════════════════════════════════════

  UNCOMPRESSED (float32):
    Per vector: 128 dims × 4 bytes = 512 bytes
    1M vectors: 512 MB

  PQ with m=8 subquantizers, k*=256 (8-bit codes):
    Per vector: 8 bytes (one byte per subquantizer)
    1M vectors: 8 MB
    Codebook:   8 subspaces × 256 centroids × 16 dims × 4 bytes = 128 KB
    Total:      ~8.1 MB
    Compression: 512 MB / 8 MB = 64x

  PQ with m=16 subquantizers, k*=256:
    Per vector: 16 bytes
    1M vectors: 16 MB
    Codebook:   16 × 256 × 8 × 4 = 128 KB
    Total:      ~16.1 MB
    Compression: 512 MB / 16 MB = 32x
    Note: higher accuracy than m=8 (finer subspaces)

  PQ with m=32 subquantizers, k*=256:
    Per vector: 32 bytes
    1M vectors: 32 MB
    Codebook:   32 × 256 × 4 × 4 = 128 KB
    Total:      ~32.1 MB
    Compression: 512 MB / 32 MB = 16x

  PQ with m=64 subquantizers, k*=256:
    Per vector: 64 bytes
    1M vectors: 64 MB
    Codebook:   64 × 256 × 2 × 4 = 128 KB
    Total:      ~64.1 MB
    Compression: 512 MB / 64 MB = 8x

═══════════════════════════════════════════════════════════════
  COMPRESSION RATIO TABLE (128-dim float32 → PQ code)
═══════════════════════════════════════════════════════════════

  ┌─────┬──────────┬──────────┬──────────┬──────────────────────┐
  │  m  │ d_sub    │ Bytes/vec│ Compress.│ Typical Recall@10    │
  │     │ (D/m)    │ (code)   │ Ratio    │ (1M vecs, nprobe=20) │
  ├─────┼──────────┼──────────┼──────────┼──────────────────────┤
  │  8  │ 16       │ 8        │ 64x      │ ~60-70%              │
  │ 16  │  8       │ 16       │ 32x      │ ~75-85%              │
  │ 32  │  4       │ 32       │ 16x      │ ~85-92%              │
  │ 64  │  2       │ 64       │  8x      │ ~90-95%              │
  │ 128 │  1       │ 128      │  4x      │ ~93-97%              │
  └─────┴──────────┴──────────┴──────────┴──────────────────────┘

  General formula:
    Uncompressed:   D × 4 bytes
    PQ compressed:  m × ⌈log₂(k*)⌉/8 bytes
    Compression:    (D × 4) / (m × ⌈log₂(k*)⌉/8)

    For k*=256 (8-bit): Compression = (D × 4) / m = 4D/m

═══════════════════════════════════════════════════════════════
  COMPARISON WITH OTHER METHODS (128-dim, 1M vectors)
═══════════════════════════════════════════════════════════════

  ┌────────────────────┬───────────┬────────────┬────────────┐
  │ Method             │ Bytes/vec │ Total (1M) │ Compress.  │
  ├────────────────────┼───────────┼────────────┼────────────┤
  │ float32 (raw)      │ 512       │ 512 MB     │ 1x         │
  │ float16            │ 256       │ 256 MB     │ 2x         │
  │ int8 (SQ)          │ 128       │ 128 MB     │ 4x         │
  │ PQ (m=32, k*=256)  │ 32        │ 32 MB      │ 16x        │
  │ PQ (m=8, k*=256)   │ 8         │ 8 MB       │ 64x        │
  │ Binary (1-bit)     │ 16        │ 16 MB      │ 32x        │
  │ PQ4 (4-bit codes)  │ 4         │ 4 MB       │ 128x       │
  └────────────────────┴───────────┴────────────┴────────────┘
```

### 3.5 PQ Search Performance Breakdown

```
Why PQ Distance Computation is Fast:

  BRUTE FORCE (float32):
    d(q, x) = Σ (qᵢ - xᵢ)²     for i=1..128
            = 128 subtractions + 128 multiplications + 127 additions
            = 383 FLOPs per distance

  ADC with PQ (m=8):
    d̃(q, x) = T₁[code₁] + T₂[code₂] + ... + T₈[code₈]
             = 8 table lookups + 7 additions
             = 15 operations per distance

    Precompute tables: 8 × 256 × 16 = 32,768 FLOPs (once per query)

    Speedup per distance: 383/15 ≈ 25x
    But we also read 8 bytes instead of 512 bytes → 64x less memory bandwidth
    → In practice, ~5-20x speedup (memory-bandwidth-bound workloads see more)
```

---

## 4. Optimized Product Quantization (OPQ)

### 4.1 The Problem with Standard PQ

Standard PQ splits dimensions in order: `[0:16], [16:32], ...`. This is arbitrary and can be suboptimal when dimensions have correlated information.

```
Why PQ Subspace Splits Can Be Suboptimal:

  Consider 4D vectors with strong correlation between dims 1&3:

  Standard PQ split (m=2):
    Subspace A: [dim1, dim2]    ← dim1 is correlated with dim3
    Subspace B: [dim3, dim4]    ← dim3 is correlated with dim1

    The correlation between dim1 and dim3 is LOST because they
    are in different subspaces, quantized independently.

  Data distribution in subspace A:     After OPQ rotation:
  ┌───────────────────┐                ┌───────────────────┐
  │      • • • •      │                │ •                 │
  │    • • • • • •    │                │   •               │
  │  • • • • • • • •  │                │     •  •          │
  │    • • • • • •    │                │       •  •        │
  │      • • • •      │                │         •  •      │
  │                   │                │           •       │
  │  Circular spread  │                │  Axis-aligned     │
  │  (hard to cluster)│                │  (easy to cluster)│
  └───────────────────┘                └───────────────────┘
```

### 4.2 OPQ Rotation Matrix

OPQ learns an orthogonal rotation matrix **R** that transforms vectors before PQ encoding, minimizing quantization error.

```
OPQ Algorithm:

  Goal: Find rotation R and codebooks C that minimize:
    E = Σ ‖Rxᵢ - q(Rxᵢ)‖²
       i=1..N

  where q(·) is the PQ quantizer applied to the rotated vector.

  ─────────────────────────────────────────────────────────────
  Training (alternating optimization):
  ─────────────────────────────────────────────────────────────

  Input:  Training vectors X, number of subspaces m, centroids k*
  Output: Rotation matrix R ∈ ℝ^(D×D), PQ codebooks C

  Initialize: R = I (identity), train initial PQ codebooks

  Repeat until convergence:

    Step 1: Fix R, optimize codebooks C
      - Rotate all training data: X' = RX
      - Train standard PQ codebooks on X'
      - (standard k-means per subspace)

    Step 2: Fix C, optimize rotation R
      - For each training vector xᵢ:
        - Compute its PQ reconstruction: x̂ᵢ = q(Rxᵢ)
      - Solve the Orthogonal Procrustes problem:
        - R* = arg min ‖RX - X̂‖_F  subject to R^T R = I
                R
        - Solution via SVD: if UΣV^T = X̂X^T, then R* = VU^T

  Typically converges in 5-10 iterations.

  ─────────────────────────────────────────────────────────────
  Encoding and Search:
  ─────────────────────────────────────────────────────────────

  Indexing:  x → Rx → PQ_encode(Rx) → [code₁, ..., code_m]
  Querying:  q → Rq → build ADC tables → scan PQ codes

  The rotation R is applied to both database vectors (during indexing)
  and query vectors (during search). Since R is orthogonal, it
  preserves all distances: ‖Rx - Ry‖ = ‖x - y‖.

  ┌───────┐      ┌───────────┐      ┌──────────┐      ┌──────┐
  │ Raw x │ ───► │ Rotate Rx │ ───► │ PQ encode│ ───► │ Code │
  └───────┘      └───────────┘      └──────────┘      └──────┘

  ┌───────┐      ┌───────────┐      ┌──────────┐      ┌──────┐
  │ Query │ ───► │ Rotate Rq │ ───► │ADC tables│ ───► │Search│
  └───────┘      └───────────┘      └──────────┘      └──────┘


  Two Solution Variants:
  ─────────────────────────────────────────────────────────────

  1. OPQP (Parametric):
     - Assumes Gaussian data distribution
     - Solves R analytically using eigendecomposition
     - Balances variance across subspaces
     - Faster to train, works well for "well-behaved" data

  2. OPQNP (Non-Parametric):
     - Makes no distributional assumptions
     - Uses iterative alternating optimization (above)
     - More general, slightly more expensive to train
     - Better for data with complex structure

  Performance impact:
  ┌──────────┬────────────────┬────────────────┐
  │ Method   │ Recall@10      │ Training Time  │
  ├──────────┼────────────────┼────────────────┤
  │ PQ       │ ~78%           │ 1x             │
  │ OPQ      │ ~85%           │ 3-5x           │
  └──────────┴────────────────┴────────────────┘
  (Same compression ratio, same search speed -- only training is slower.)
```

---

## 5. ScaNN: Anisotropic Vector Quantization

### 5.1 The Insight: Not All Error Is Equal

Google's ScaNN (Scalable Nearest Neighbors) introduced **anisotropic vector quantization** -- a quantization method specifically designed for Maximum Inner Product Search (MIPS).

```
The Problem with Isotropic Quantization for MIPS:

  Standard PQ minimizes: E = ‖x - q(x)‖²
  This treats all directions of error equally (isotropic).

  But for inner product search ⟨q, x⟩, errors in different
  directions have DIFFERENT impacts on ranking:

  Query q ────────────────►

              x (original)
             ╱
            ╱ ← error parallel to x (changes ⟨q,x⟩ a LOT)
           ╱
          ●───── q(x) (quantized)
           ╲
            ╲ ← error perpendicular to x (changes ⟨q,x⟩ a LITTLE)
             ╲

  Inner product: ⟨q, x⟩ = ‖q‖ × ‖x‖ × cos(θ)

  Parallel error:      changes ‖x‖ → directly scales ⟨q,x⟩
  Perpendicular error: changes direction → small effect on ⟨q,x⟩
                        (for small errors, cos changes slowly)
```

### 5.2 Anisotropic Loss Function

```
ScaNN's Anisotropic Quantization Loss:

  Standard (isotropic) PQ loss:
    L_iso = E[ ‖x - q(x)‖² ]
          = E[ ‖ε_∥‖² + ‖ε_⊥‖² ]     (parallel + perpendicular error)

  ScaNN's anisotropic loss:
    L_aniso = E[ w_∥ × ‖ε_∥‖² + w_⊥ × ‖ε_⊥‖² ]

  where:
    ε_∥  = component of (x - q(x)) parallel to x
    ε_⊥  = component of (x - q(x)) perpendicular to x
    w_∥  >> w_⊥  (parallel error is penalized much more heavily)

  The weight ratio is controlled by a parameter t (threshold):

    w_∥ = 1
    w_⊥ = 1/t²     where t ≥ 1

  When t = 1:  isotropic (standard PQ, all directions equal)
  When t → ∞:  only parallel error matters (pure direction preservation)

  ─────────────────────────────────────────────────────────────
  Geometric Interpretation:
  ─────────────────────────────────────────────────────────────

  Isotropic loss:                   Anisotropic loss (t=10):
  (circular error contours)         (elliptical error contours)

     │    ╱─╲                          │    ╱───────╲
     │  ╱     ╲                        │  ╱           ╲
     │╱    ●    ╲  x                   │╱──────●───────╲  x
     │  ╱     ╲                        │  ╱           ╲
     │    ╲─╱                          │    ╲───────╱
                                           ▲
                                           │
                                   Tolerate more ⊥ error
                                   to minimize ∥ error

  Result: quantized vector q(x) is biased to preserve the
  DIRECTION of x rather than its exact position. This means
  ⟨q, q(x)⟩ ≈ ⟨q, x⟩ for most queries q (better MIPS accuracy).
```

### 5.3 ScaNN Architecture

```
ScaNN Three-Phase Search Pipeline:

  ┌───────────────────────────────────────────────────────┐
  │  Phase 1: PARTITIONING (IVF-like)                     │
  │                                                       │
  │  K-means with K partitions + spilling                 │
  │  Query → find top-p partitions → retrieve candidates  │
  │  Uses SOAR (Spilling with Orthogonality-Amplified     │
  │  Residuals) for improved boundary handling             │
  └───────────────────────┬───────────────────────────────┘
                          │ candidates (e.g., 10K vectors)
                          ▼
  ┌───────────────────────────────────────────────────────┐
  │  Phase 2: SCORING (Anisotropic PQ)                    │
  │                                                       │
  │  Compute approximate inner products using             │
  │  anisotropic-quantized codes (ADC-style)              │
  │  Select top-k' candidates (k' > k)                   │
  └───────────────────────┬───────────────────────────────┘
                          │ top-k' candidates (e.g., 100)
                          ▼
  ┌───────────────────────────────────────────────────────┐
  │  Phase 3: RESCORING (exact reranking)                 │
  │                                                       │
  │  Recompute EXACT inner products for top-k' candidates │
  │  using full-precision vectors stored on disk/memory   │
  │  Return final top-k results                           │
  └───────────────────────────────────────────────────────┘

  SOAR (2024 improvement):
  ─────────────────────────
  Problem: Spilling (assigning vectors to multiple partitions)
  increases index size and search cost.

  SOAR transforms residual vectors to amplify their component
  orthogonal to the partition centroid difference. This makes
  it easier to find vectors near partition boundaries without
  increasing the number of probed partitions.

  Result: ~2x speedup at same recall, or higher recall at same speed.


  Performance (ann-benchmarks.com, glove-100-angular):
  ┌────────────┬──────────────┬──────────────┐
  │ System     │ QPS @95%     │ QPS @99%     │
  │            │ Recall       │ Recall       │
  ├────────────┼──────────────┼──────────────┤
  │ ScaNN      │ ~18,000      │ ~8,000       │
  │ HNSW       │ ~9,000       │ ~3,500       │
  │ FAISS IVF  │ ~7,000       │ ~2,500       │
  └────────────┴──────────────┴──────────────┘
  (ScaNN handles ~2x more queries per second than alternatives)
```

---

## 6. Scalar Quantization

### 6.1 INT8 Scalar Quantization

```
Scalar Quantization: float32 → int8

  Maps each floating-point dimension independently to an 8-bit integer.

  Process:
  ─────────────────────────────────────────────────────────────

  Step 1: Compute per-dimension statistics from training data
    For each dimension d:
      min_d = min(x_d for all training vectors)
      max_d = max(x_d for all training vectors)

  Step 2: Quantize each value
    quantized_d = round( (x_d - min_d) / (max_d - min_d) × 255 )

    This maps [min_d, max_d] → [0, 255] (uint8)
    Or use signed: maps to [-128, 127] (int8)

  Step 3: Store quantized vector + scale/offset metadata

  ─────────────────────────────────────────────────────────────
  Example (D=128):
  ─────────────────────────────────────────────────────────────

  Original:   [0.734, -0.213, 0.891, ..., -0.556]   ← float32
  Per-dim:    min=-1.0, max=1.0 (hypothetical)
  Quantized:  [  217,     99,   241, ...,     56 ]   ← uint8

  Reconstruction:
    x̂_d = quantized_d / 255 × (max_d - min_d) + min_d
    x̂   = [0.733, -0.224, 0.890, ..., -0.561]

  Max quantization error per dimension: (max_d - min_d) / 255
  For range [-1, 1]: max error = 2/255 ≈ 0.0078 per dimension

  ─────────────────────────────────────────────────────────────
  Memory:
  ─────────────────────────────────────────────────────────────

  float32:  128 dims × 4 bytes = 512 bytes per vector
  int8:     128 dims × 1 byte  = 128 bytes per vector
  + metadata: 2 × 4 bytes per dimension (min, max) = 1 KB (shared)

  Compression: 4x
  Accuracy loss: typically < 1% recall degradation
  Speed gain: 3-4x (SIMD int8 ops are faster than float32)

  ┌──────────────────────────────────────────────────────────┐
  │  INT8 works well because:                                │
  │                                                          │
  │  1. Embedding dimensions typically have bounded range    │
  │  2. 256 quantization levels capture enough granularity   │
  │  3. Modern CPUs have fast int8 SIMD (AVX-512 VNNI)      │
  │  4. Errors in individual dimensions average out over D   │
  │     (law of large numbers: relative error ∝ 1/√D)       │
  └──────────────────────────────────────────────────────────┘
```

### 6.2 Binary Quantization

```
Binary Quantization: float32 → 1-bit

  The most aggressive scalar quantization: each dimension
  becomes a single bit.

  Encoding rule:
    bit_d = 1  if x_d > 0
    bit_d = 0  if x_d ≤ 0

  (Some variants use the median or mean instead of 0 as threshold.)

  ─────────────────────────────────────────────────────────────
  Example (D=128):
  ─────────────────────────────────────────────────────────────

  Original:   [0.734, -0.213, 0.891, -0.556, 0.102, ...]
  Binary:     [  1,      0,     1,      0,      1,   ...]
  Packed:     [0b10101...] → stored as 128/8 = 16 bytes

  ─────────────────────────────────────────────────────────────
  Distance computation: Hamming distance via bit operations
  ─────────────────────────────────────────────────────────────

  d_hamming(a, b) = popcount(a XOR b)

  This is EXTREMELY fast:
    XOR:      single CPU instruction (bitwise)
    popcount: single CPU instruction (POPCNT)

  For 128-bit vectors: 2 × 64-bit XOR + 2 POPCNT operations
  vs. 128 multiplications + 127 additions for float32

  ─────────────────────────────────────────────────────────────
  Memory and Speed:
  ─────────────────────────────────────────────────────────────

  ┌──────────────┬───────────┬────────────┬──────────────────┐
  │ Metric       │ float32   │ int8       │ Binary (1-bit)   │
  ├──────────────┼───────────┼────────────┼──────────────────┤
  │ Bytes/vector │ 512       │ 128        │ 16               │
  │ Compression  │ 1x        │ 4x         │ 32x              │
  │ Dist. speed  │ 1x        │ ~3.5x      │ ~25x             │
  │ Recall@10    │ 100%      │ ~98-99%    │ ~70-85%*         │
  └──────────────┴───────────┴────────────┴──────────────────┘

  * Binary quantization recall depends HEAVILY on the embedding model.
    Models with clear positive/negative dimension semantics (like
    Cohere embed-v3, some OpenAI models) work much better.

  ─────────────────────────────────────────────────────────────
  When binary quantization works well vs poorly:
  ─────────────────────────────────────────────────────────────

  WORKS WELL:
  - High-dimensional embeddings (D ≥ 768)
    More dimensions → bit errors average out → reliable
  - Models trained with binary quantization in mind
  - Two-phase retrieval (binary for shortlisting, float for rerank)
  - Cohere embed-v3, OpenAI text-embedding-3-large

  WORKS POORLY:
  - Low-dimensional embeddings (D < 256)
    Too few bits → Hamming distance has low resolution
  - Models where sign is not semantically meaningful
  - When you need > 90% recall without reranking
```

### 6.3 Two-Phase Retrieval with Quantization

```
Oversampling + Reranking Pattern:

  The standard production pattern for quantized search:

  Phase 1: Quantized search (fast, approximate)
  ─────────────────────────────────────────────
    Search quantized index for top-k' candidates
    where k' = oversample_factor × k

    Typical oversample factors:
    - int8:    k' = 1.5× to 2× k
    - binary:  k' = 5× to 10× k
    - PQ:      k' = 2× to 5× k

  Phase 2: Exact reranking (slower, precise)
  ─────────────────────────────────────────────
    Fetch full float32 vectors for k' candidates (from disk/RAM)
    Compute exact distances
    Return top-k

  ┌──────────────────────────────────┐
  │ 1M vectors (quantized, in RAM)  │
  │          │                       │
  │    Binary search: 0.2ms         │
  │          │                       │
  │   Top-100 candidates             │
  │          │                       │
  └──────────┼───────────────────────┘
             ▼
  ┌──────────────────────────────────┐
  │ Fetch 100 float32 vectors       │
  │ from disk/memory-mapped file    │
  │          │                       │
  │    Exact rerank: 0.1ms          │
  │          │                       │
  │   Return top-10                  │
  └──────────────────────────────────┘

  Total: ~0.3ms with 95%+ recall
  Memory: 16 MB (binary) + 512 MB (float32, can be on disk)
```

---

## 7. Matryoshka Embeddings

### 7.1 Concept: Nested Representations

Matryoshka Representation Learning (MRL) trains embeddings where the **first d dimensions** form a valid d-dimensional embedding for any d. Named after Russian nesting dolls.

```
Matryoshka Embedding Structure:

  A single 768-dim embedding contains nested sub-embeddings:

  [v₁, v₂, ..., v₆₄, v₆₅, ..., v₁₂₈, v₁₂₉, ..., v₂₅₆, ..., v₇₆₈]
   ├──── 64-dim ────┤
   ├────────── 128-dim ──────────┤
   ├──────────────── 256-dim ────────────────┤
   ├────────────────────────── 768-dim (full) ────────────────────────┤

  Each prefix is a VALID embedding:
    x[:64]   → 64-dim embedding   (most compressed, fastest)
    x[:128]  → 128-dim embedding  (good balance)
    x[:256]  → 256-dim embedding  (high quality)
    x[:768]  → 768-dim embedding  (full quality)

  Key property: x[:64] is NOT a different embedding.
  It IS the first 64 values of x[:768].
```

### 7.2 Training Mechanism

```
MRL Training (Multi-Scale Loss):

  Standard contrastive training:
    L = contrastive_loss(f(x))     # using full D-dim output

  Matryoshka training:
    L = Σ  αₐ × contrastive_loss(f(x)[:dₐ])
       d∈D

  where D = {64, 128, 256, 512, 768} (a set of target dimensions)
  and αₐ are weights (often uniform: αₐ = 1 for all d)

  ─────────────────────────────────────────────────────────────
  Training step:
  ─────────────────────────────────────────────────────────────

  Input batch: (query, positive, negatives)

  1. Forward pass → get 768-dim embeddings for all inputs

  2. Compute loss at EACH granularity:
     L₆₄  = contrastive_loss( embeddings[:, :64]  )
     L₁₂₈ = contrastive_loss( embeddings[:, :128] )
     L₂₅₆ = contrastive_loss( embeddings[:, :256] )
     L₅₁₂ = contrastive_loss( embeddings[:, :512] )
     L₇₆₈ = contrastive_loss( embeddings[:, :768] )

  3. Total loss:
     L = L₆₄ + L₁₂₈ + L₂₅₆ + L₅₁₂ + L₇₆₈

  4. Backpropagate → model learns to frontload important
     information into early dimensions.

  ─────────────────────────────────────────────────────────────
  Why it works:
  ─────────────────────────────────────────────────────────────

  The multi-scale loss forces the network to encode:
  - The MOST important semantic information in dims 1-64
  - Progressively finer details in later dimensions
  - A natural information hierarchy

  Analogy: progressive JPEG encoding
    First bytes  → blurry image (coarse semantics)
    More bytes   → sharper image (fine semantics)
    All bytes    → full quality (complete representation)
```

### 7.3 Performance and Comparison

```
Matryoshka vs Standard Embeddings (retrieval benchmarks):

  ┌──────────┬────────────────────┬────────────────────┬──────────┐
  │ Dims     │ Matryoshka         │ Standard (trained   │ PCA from │
  │          │ (truncated)        │ at this dim)        │ 768-dim  │
  ├──────────┼────────────────────┼────────────────────┼──────────┤
  │ 768      │ 100% (baseline)    │ 100%               │ N/A      │
  │ 256      │ ~99.0%             │ ~99.2%             │ ~96%     │
  │ 128      │ ~97.5%             │ ~97.8%             │ ~93%     │
  │ 64       │ ~95.0%             │ ~95.5%             │ ~87%     │
  │ 32       │ ~90.0%             │ ~91.0%             │ ~78%     │
  └──────────┴────────────────────┴────────────────────┴──────────┘

  Key observations:
  1. Matryoshka at dim d ≈ independently trained model at dim d
  2. Matryoshka >> PCA dimensionality reduction (PCA is not trained
     for retrieval quality; MRL is)
  3. One model serves ALL dimensionalities (no need for separate models)

  ─────────────────────────────────────────────────────────────
  Matryoshka + Quantization (stacking compressions):
  ─────────────────────────────────────────────────────────────

  Full:       768 dims × 4 bytes = 3,072 bytes     1x
  MRL-256:    256 dims × 4 bytes = 1,024 bytes     3x
  MRL-128:    128 dims × 4 bytes =   512 bytes     6x
  MRL-64:     64  dims × 4 bytes =   256 bytes    12x

  MRL-256 + int8:   256 dims × 1 byte = 256 bytes  12x
  MRL-128 + int8:   128 dims × 1 byte = 128 bytes  24x
  MRL-128 + binary: 128 dims × 1 bit  =  16 bytes  192x

  ┌────────────────────────────────────────────────────────────┐
  │  PRODUCTION RECIPE: "Matryoshka Funnel"                    │
  │                                                            │
  │  Stage 1: MRL-64 + binary → 8 bytes per vector            │
  │           Retrieve top-1000 candidates (sub-millisecond)   │
  │                                                            │
  │  Stage 2: MRL-256 + int8 → 256 bytes per vector           │
  │           Rerank top-1000 → top-100                        │
  │                                                            │
  │  Stage 3: Full 768-dim float32 → 3072 bytes per vector    │
  │           Rerank top-100 → final top-10                    │
  │                                                            │
  │  All three stages use the SAME embedding model output.     │
  │  Just different prefixes and quantization levels.          │
  └────────────────────────────────────────────────────────────┘

  Models supporting Matryoshka:
  - OpenAI text-embedding-3-small/large (configurable dims param)
  - Nomic embed-v1.5
  - Cohere embed-v3
  - Mixedbread mxbai-embed-large-v1
  - Jina embeddings-v2
```

---

## 8. Composite Indexes

### 8.1 IVF-Flat

```
IVF-Flat: Exact distances within partitions.

  Structure:
    Coarse quantizer: K centroids (k-means)
    Posting lists: FULL float32 vectors

  ┌──────────────────────────────────────────────────┐
  │ Centroid 0 → [id₃: full_vec₃] [id₇: full_vec₇] │
  │ Centroid 1 → [id₁: full_vec₁] [id₅: full_vec₅] │
  │ ...                                              │
  └──────────────────────────────────────────────────┘

  Pros:
  ✓ Exact distances within probed cells (no quantization error in fine search)
  ✓ Simple to implement and debug
  ✓ Good recall even with low nprobe

  Cons:
  ✗ Memory: same as brute force (N × D × 4 bytes + centroids)
  ✗ Search: still computes exact distances per scanned vector
  ✗ Cannot fit billions of vectors in RAM

  Best for:
  - Datasets < 10M vectors
  - When recall > 99% is required
  - When memory is not the bottleneck
  - As a baseline before trying compression

  Memory (1M vectors, D=128): 512 MB + 4 MB (centroids) ≈ 516 MB
```

### 8.2 IVF-PQ

```
IVF-PQ: Compressed distances within partitions.

  Structure:
    Coarse quantizer: K centroids (k-means)
    Posting lists: PQ codes (residual vectors)

  ┌──────────────────────────────────────────────────────────┐
  │ Centroid 0 → [id₃: pq_code₃] [id₇: pq_code₇]          │
  │ Centroid 1 → [id₁: pq_code₁] [id₅: pq_code₅]          │
  │ ...                                                      │
  │                                                          │
  │ pq_code = PQ_encode(vector - centroid)  ← residual       │
  │ Each code: m bytes (typically 8-64 bytes per vector)     │
  └──────────────────────────────────────────────────────────┘

  Search algorithm:
  1. Find nprobe nearest centroids to query q
  2. For each probed centroid cᵢ:
     a. Compute residual: r_q = q - cᵢ
     b. Build ADC lookup tables for r_q
     c. Scan PQ codes in posting list using table lookups
  3. Return top-k across all probed lists

  Pros:
  ✓ Massive memory reduction (16-64x compression)
  ✓ Fast distance computation (m table lookups vs D FLOPs)
  ✓ Scales to billions of vectors in RAM
  ✓ Residual encoding improves PQ accuracy

  Cons:
  ✗ Approximate distances → lower recall than IVF-Flat
  ✗ Needs careful tuning (m, k*, nprobe, nlist)
  ✗ Training is slower (k-means for centroids + PQ codebooks)
  ✗ Not suitable when recall > 98% is strictly required
    (use reranking to close the gap)

  Best for:
  - Datasets > 10M vectors
  - Memory-constrained environments
  - Throughput-critical applications

  Memory (1M vectors, D=128, m=32):
    PQ codes: 32 MB
    Centroids: 4 MB
    Codebooks: 128 KB
    Total: ~36 MB   (vs 516 MB for IVF-Flat → 14x smaller)
```

### 8.3 IVF-HNSW

```
IVF-HNSW: Graph-based coarse quantizer.

  Standard IVF uses brute-force search over K centroids in the
  coarse search phase: O(K × D). For large K (e.g., 65536),
  this becomes slow.

  IVF-HNSW replaces the brute-force coarse search with an HNSW
  graph over the centroids, reducing coarse search to O(log K × D).

  ┌─────────────────────────────────────────────────────────────┐
  │                                                             │
  │  HNSW graph over K centroids          Inverted Lists        │
  │  ┌─────────────────────────┐                                │
  │  │  Layer 2:  c₅ ─── c₁₂  │                                │
  │  │             │           │                                │
  │  │  Layer 1: c₃─c₅─c₈─c₁₂│    c₃ → [pq_codes...]         │
  │  │           │ │ │ │ │    │    c₅ → [pq_codes...]         │
  │  │  Layer 0: c₁c₃c₅c₇c₈  │    c₈ → [pq_codes...]         │
  │  │           c₉c₁₁c₁₂    │    ...                         │
  │  └─────────────────────────┘                                │
  │                                                             │
  │  Query: use HNSW to find nprobe nearest centroids in        │
  │  O(log K) time, then scan their posting lists.              │
  └─────────────────────────────────────────────────────────────┘

  Coarse search comparison (K=65536):
  ┌────────────┬────────────────────┬───────────────────┐
  │ Method     │ Coarse search cost │ Time              │
  ├────────────┼────────────────────┼───────────────────┤
  │ IVF-Flat   │ O(K × D)           │ ~5-10 ms          │
  │ IVF-HNSW   │ O(log K × D)       │ ~0.05-0.2 ms     │
  └────────────┴────────────────────┴───────────────────┘

  Extra memory: HNSW graph over K centroids ≈ K × M × 8 bytes
  For K=65536, M=32: ~16 MB (negligible vs posting lists)

  Best for:
  - Very large K (> 4096 centroids)
  - Billion-scale datasets with K=65536+
  - When coarse search is the bottleneck
```

### 8.4 Variant Comparison

```
═══════════════════════════════════════════════════════════════
  DECISION TABLE: Which IVF variant?
═══════════════════════════════════════════════════════════════

  ┌──────────┬────────┬─────────┬────────┬──────────┬─────────┐
  │ Variant  │ Memory │ Speed   │ Recall │ Scale    │ Tuning  │
  ├──────────┼────────┼─────────┼────────┼──────────┼─────────┤
  │ IVF-Flat │ High   │ Medium  │ High   │ ≤10M     │ Easy    │
  │ IVF-SQ8  │ Medium │ Fast    │ High   │ ≤100M    │ Easy    │
  │ IVF-PQ   │ Low    │ V.Fast  │ Medium │ ≤10B     │ Medium  │
  │ IVF-HNSW │ High*  │ V.Fast  │ High   │ ≤100M    │ Medium  │
  │ HNSW+PQ  │ Low    │ Fast    │ Med-Hi │ ≤1B      │ Hard    │
  └──────────┴────────┴─────────┴────────┴──────────┴─────────┘

  * IVF-HNSW posting lists still store full vectors unless combined with PQ.

  FAISS index factory strings:
  ┌────────────────────────────────────────────────────────────┐
  │ "IVF4096,Flat"          → IVF-Flat, 4096 cells            │
  │ "IVF4096,PQ32"          → IVF-PQ, 32 bytes per code       │
  │ "IVF4096,PQ32x8"        → IVF-PQ, 32 subquants, 8 bits   │
  │ "OPQ32,IVF4096,PQ32"    → OPQ rotation + IVF-PQ           │
  │ "IVF4096_HNSW32,PQ32"   → HNSW coarse quant + IVF-PQ     │
  │ "IVF4096,SQ8"           → IVF with int8 scalar quant      │
  │ "OPQ32,IVF4096_HNSW32,PQ32" → full pipeline               │
  └────────────────────────────────────────────────────────────┘
```

---

## 9. Production Decision Matrix

```
═══════════════════════════════════════════════════════════════
  CHOOSING AN INDEX: FLOWCHART
═══════════════════════════════════════════════════════════════

  How many vectors?
        │
        ├── < 50K ──────────► Brute force (Flat index)
        │                     No index needed. Linear scan is fast enough.
        │
        ├── 50K - 1M ───────► HNSW (pure graph)
        │                     Best recall/speed. Memory: ~1.5x vectors.
        │
        ├── 1M - 100M ──────► IVF-Flat or IVF-SQ8
        │   │                 Good balance. SQ8 if memory matters.
        │   │
        │   └── Need < 50ms → IVF-HNSW coarse quantizer
        │       latency?
        │
        ├── 100M - 1B ──────► IVF-PQ or IVF-HNSW+PQ
        │                     PQ compression essential at this scale.
        │                     Add OPQ if training time is acceptable.
        │
        └── > 1B ───────────► IVF-PQ with disk-based posting lists
                              or distributed sharding + IVF-PQ per shard.

═══════════════════════════════════════════════════════════════
  COMPRESSION TECHNIQUES: WHEN TO USE WHAT
═══════════════════════════════════════════════════════════════

  ┌────────────────────┬──────────┬──────────┬──────────────────┐
  │ Technique          │ Compress │ Recall   │ When to use      │
  ├────────────────────┼──────────┼──────────┼──────────────────┤
  │ Matryoshka (MRL)   │ 3-12x   │ 95-99%   │ First choice if  │
  │ (dim reduction)    │          │          │ model supports it│
  │                    │          │          │                  │
  │ Scalar (int8)      │ 4x      │ 98-99%   │ Always safe.     │
  │                    │          │          │ Universal.       │
  │                    │          │          │                  │
  │ Binary (1-bit)     │ 32x     │ 70-85%   │ Only for         │
  │                    │          │          │ shortlisting +   │
  │                    │          │          │ reranking.       │
  │                    │          │          │                  │
  │ PQ (m=32)          │ 16x     │ 85-92%   │ Large scale.     │
  │                    │          │          │ Tune m for       │
  │                    │          │          │ accuracy needs.  │
  │                    │          │          │                  │
  │ OPQ + PQ           │ 16x     │ 88-95%   │ When PQ recall   │
  │                    │          │          │ is not enough.   │
  │                    │          │          │                  │
  │ MRL + int8         │ 12-24x  │ 95-97%   │ Best             │
  │                    │          │          │ bang-for-buck.   │
  │                    │          │          │                  │
  │ MRL + binary       │ 96-192x │ 80-90%   │ Extreme scale,   │
  │ + rerank           │          │          │ with reranking.  │
  └────────────────────┴──────────┴──────────┴──────────────────┘

═══════════════════════════════════════════════════════════════
  PARAMETER CHEAT SHEET
═══════════════════════════════════════════════════════════════

  IVF:
    nlist (K):     √N for balanced, 4√N for speed-oriented
    nprobe:        1-10% of nlist for 90%+ recall
    Training data: at least 30×K vectors recommended

  PQ:
    m (subquantizers): D/4 to D/2 (more = better recall, more memory)
    k* (centroids):    256 (always; 8-bit codes)
    D must be divisible by m

  OPQ:
    Same params as PQ
    Extra: 5-10 iterations of alternating optimization
    Training cost: 3-5x standard PQ

  Scalar Quantization:
    int8: no tuning needed (99% of the time, just enable it)
    binary: test with your specific model first

  Matryoshka:
    Choose smallest dim where recall meets requirements
    Test: 768 → 256 → 128 → 64, measure recall at each

  HNSW (for IVF-HNSW coarse quantizer):
    M: 16-64 (connectivity, 32 is typical)
    efConstruction: 200-500 (build quality)
    efSearch: 50-200 (search quality)
```

---

## References

This guide synthesizes information from the original research papers and technical documentation:

- Jegou, Douze, Schmid. "Product Quantization for Nearest Neighbor Search." IEEE TPAMI, 2011.
- Ge, He, Ke, Sun. "Optimized Product Quantization." IEEE TPAMI, 2013.
- Guo et al. "Accelerating Large-Scale Inference with Anisotropic Vector Quantization." ICML, 2020 (ScaNN).
- Kusupati et al. "Matryoshka Representation Learning." NeurIPS, 2022.
- FAISS documentation (Meta). https://github.com/facebookresearch/faiss
- Milvus documentation. https://milvus.io/docs
- Pinecone Learning Center. https://www.pinecone.io/learn/
- Qdrant documentation. https://qdrant.tech/articles/
