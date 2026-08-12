# Appendix D — Document processing benchmarks and best choices

> **Prerequisites:** [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md)
> (the pipeline model, parsing tiers, and the ceiling-chain argument — this appendix gives you
> current numbers to put inside that framework, not a replacement for it),
> [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) (embedding model
> landscape — §4's bake-off complements this appendix's parser bake-off),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (retrieval strategy
> interacts with chunk quality — bad parses cannot be reranked into relevance).
>
> **Feeds into:** [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (the eval harness
> you need to verify any choice made here against your own corpus),
> [`15-ingestion-pipelines-and-freshness.md`](15-ingestion-pipelines-and-freshness.md) (parser and
> chunker throughput directly determine ingestion SLOs),
> [`appendix-e-deployment-and-compute.md`](appendix-e-deployment-and-compute.md) (where the tools
> chosen here actually run, what hardware they need, and what they cost — §9.3 shows the parser
> tier choice moving total ingestion cost by three to four orders of magnitude),
> [`labs/document-processing/`](labs/document-processing/) (the bake-off lab that makes every claim
> here falsifiable against your own documents).
>
> **THESIS:** benchmark tables are not decision procedures. Every number below was measured on
> someone else's corpus, under someone else's definition of "correct." The table tells you which
> two or three tools to evaluate on *your* documents — never which one to deploy. If you skip the
> lab and ship from a table, you inherited a decision instead of making one, and `02` §3.3's
> leaderboard caution applies in full. That said: running the lab without a shortlist is wasted
> effort, and producing that shortlist is exactly what this appendix is for.

---

## Contents

1. [How to use this appendix](#1-how-to-use-this-appendix)
2. [PDF and document parsers](#2-pdf-and-document-parsers)
3. [Parser benchmark landscape](#3-parser-benchmark-landscape)
4. [Parser profiles](#4-parser-profiles)
5. [Parser decision matrix](#5-parser-decision-matrix)
6. [Chunking strategies](#6-chunking-strategies)
7. [Chunking tools](#7-chunking-tools)
8. [Embedding models — the current field](#8-embedding-models--the-current-field)
9. [Vector stores and retrieval infrastructure](#9-vector-stores-and-retrieval-infrastructure)
10. [Rerankers](#10-rerankers)
11. [End-to-end RAG evaluation frameworks](#11-end-to-end-rag-evaluation-frameworks)
12. [Recommended stacks by use case](#12-recommended-stacks-by-use-case)
13. [Anti-patterns](#13-anti-patterns)
14. [Mental models — the compressed set](#14-mental-models--the-compressed-set)

---

## 1. How to use this appendix

This is a reference, not a narrative. It is designed to go stale — benchmark numbers move faster
than any document, and a score printed here is a snapshot, not a commitment. The useful parts are:

- **The shortlisting tables** (§5, §8, §9) — these narrow the field from "dozens of options" to
  "evaluate these two or three," which is the step that actually saves time.
- **The decision matrices** — these encode trade-offs that are more stable than benchmark scores:
  cost vs accuracy, self-hosted vs API, throughput vs fidelity.
- **The recommended stacks** (§12) — opinionated starting points by use case, each with a rationale
  you can audit and a "swap when" note for the component most likely to be wrong.

When a number below looks decisive, suspect it. The correct response to a decisive benchmark
number is to reproduce it on your own data.

---

## 2. PDF and document parsers

`02` §3 established the parser as the ceiling of the entire pipeline. This section gives you
the current landscape for that ceiling.

### 2.1 The three tiers, with current representatives

| Tier | Mechanism | Representative tools | Typical speed | Typical accuracy | Cost model |
|---|---|---|---|---|---|
| **1 — Geometric extraction** | Glyph positions → heuristic lines/blocks | PyMuPDF, pypdf, pdfplumber, pdfminer.six | 50–200 pages/sec | 30–50% on complex layouts | CPU-milliseconds, free |
| **2 — Layout models** | Detection model segments page into regions, extracts per region | Docling, Marker v2, MinerU, Unstructured (`hi_res`), Surya | 0.5–5 pages/sec | 50–77% on benchmarks | CPU/GPU-seconds, self-hosted or per-page API |
| **3 — VLM page understanding** | Vision-language model reads rendered page | GPT-4.1 vision, Claude vision, Gemini, olmOCR 2 | 0.1–1 pages/sec | 80–92% edit similarity | LLM token cost per page ($0.01–0.05/page) |

The tier boundaries are porous and moving. Docling's `Granite-Docling-258M` is a 258M-parameter
model that parses a page in a single pass — it is technically a layout model but runs at speeds
closer to geometric extraction on GPU. The distinction that matters is not the mechanism but the
*cost-accuracy tradeoff curve* you're on.

### 2.2 Open-source parsers — current field

| Parser | License | Key strengths | Key weaknesses | Format support |
|---|---|---|---|---|
| **Docling** (IBM / LF AI) | MIT | Widest format support (PDF, DOCX, PPTX, HTML, images, AsciiDoc); structured `DoclingDocument` output; `Granite-Docling-258M` (Apache 2.0); pip-installable | Table accuracy lags Marker/MinerU on complex layouts; GPU recommended for reasonable throughput | PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, AsciiDoc |
| **Marker v2** | GPL-3.0 | Highest open-source accuracy on olmOCR-Bench (~76%); good multi-column and table handling; active development | GPL license restricts commercial use without negotiation; GPU required; primarily PDF-focused | PDF, images, EPUB, MOBI |
| **MinerU** (OpenDataLab) | AGPL-3.0 | Strong accuracy (~73% on olmOCR-Bench); good table/formula extraction; comprehensive pipeline | Slow (0.5 pages/sec CPU); AGPL license; heavy dependencies | PDF, images |
| **Unstructured** (open-source) | Apache 2.0 | Broadest ecosystem integration (LangChain, LlamaIndex native); `strategy` parameter for per-doc tier selection; partitions into typed elements | Accuracy depends heavily on strategy choice; `hi_res` requires model downloads; API version is separate product | PDF, DOCX, PPTX, HTML, email, images, Markdown, RST, CSV, TSV, code |
| **PyMuPDF / pymupdf4llm** | AGPL-3.0 | Fastest CPU extraction; `pymupdf4llm` emits Markdown-formatted text preserving some structure; mature, well-maintained | No layout model — fails on multi-column, complex tables; AGPL license | PDF, EPUB, XPS, images |
| **Surya** | GPL-3.0 | Strong OCR + layout detection; line-level recognition; multilingual | Primarily a recognition engine, not a full document parser; GPL | PDF, images |
| **olmOCR** (AI2) | Apache 2.0 | Uses vision LLMs for page understanding; hosts the olmOCR-Bench leaderboard; research-grade | Resource-intensive; not designed as a production library | PDF, images |
| **Nougat** (Meta) | CC-BY-NC | Academic paper specialist; outputs LaTeX/Markdown from scientific PDFs | Narrow domain; non-commercial license; slow; hallucinates on non-academic docs | PDF (academic) |
| **pypdf** | BSD | Pure Python, zero dependencies, pip-installable | Geometric extraction only; poor on anything beyond simple single-column | PDF |
| **pdfplumber** | MIT | Excellent table extraction via geometric heuristics; visual debugging | Slow on large documents; geometric only; no layout understanding | PDF |
| **Camelot / Tabula** | MIT / MIT | Purpose-built for table extraction; Camelot offers lattice + stream modes | Tables only, not full document parsing; maintenance varies | PDF (tables only) |

### 2.3 Cloud/API parsers

| Service | Provider | Strengths | Pricing (approx.) | When to use |
|---|---|---|---|---|
| **Azure Document Intelligence** | Microsoft | Highest printed-text OCR accuracy (~96%); prebuilt models for invoices, receipts, ID documents; table extraction with confidence scores | $1.50/1K pages (read); $10/1K pages (prebuilt) | Enterprise with Azure footprint; regulated industries needing confidence scores |
| **Amazon Textract** | AWS | Strong table and forms extraction; query-based extraction; AWS ecosystem integration | $1.50/1K pages (detect text); $15/1K pages (tables) | Enterprise with AWS footprint; form-heavy corpora |
| **Google Document AI** | Google | Strong multilingual OCR; specialized processors for specific document types; Vertex integration | $1.50/1K pages (OCR); custom pricing for specialized | Multilingual corpora; Google Cloud ecosystem |
| **LlamaParse** (LlamaIndex) | LlamaIndex | Built for RAG; outputs Markdown; handles complex layouts; LlamaIndex-native | $0.003/page (free tier: 1K pages/day) | LlamaIndex users; cost-sensitive with decent accuracy needs |
| **Mathpix** | Mathpix | Best-in-class LaTeX/math extraction; outputs structured Markdown/LaTeX | $0.01–0.04/page | STEM documents, equations, scientific papers |
| **Reducto** | Reducto | Optimized for RAG with chunk-aware parsing; fast API | Custom pricing | High-volume production RAG pipelines |

---

## 3. Parser benchmark landscape

### 3.1 Current benchmarks

| Benchmark | What it measures | Corpus | Methodology | Limitations |
|---|---|---|---|---|
| **olmOCR-Bench** (AI2) | Edit similarity between extracted text and ground truth | ~1K diverse pages: academic, financial, scanned, multi-column | Normalized edit distance; per-page scoring | Single metric (edit similarity) doesn't capture table structure fidelity |
| **pdf-parser-benchmark** (Applied AI) | Column-order correctness, row-band detection, table extraction | Curated set of challenging PDFs with known ground truth | Per-feature correctness assessment | Smaller dataset; emphasizes specific failure modes |
| **FinTabNet** | Table detection and structure recognition in financial documents | ~113K tables from annual reports | IoU-based detection + cell adjacency for structure | Tables only; financial domain only |
| **PubLayNet** | Document layout analysis (text, title, list, table, figure) | ~360K document images from PubMed | Object detection metrics (mAP) | Academic papers only |
| **DocLayNet** (IBM) | Document layout analysis across domains | ~80K pages: financial, scientific, legal, manual, patent, government | Object detection metrics | Layout detection only, not text extraction quality |

### 3.2 olmOCR-Bench results snapshot (mid-2026)

These numbers are sourced from public leaderboard data and practitioner reports. **Verify against
the primary source before making a decision** — `02` §3.3's caution applies.

| Parser | Overall accuracy (edit sim.) | Speed (pages/sec) | Notes |
|---|---|---|---|
| GPT-4.1 vision | ~89–92% | 0.2–0.5 | Highest accuracy; $0.03–0.05/page; non-deterministic |
| Gemini 2.5 Flash | ~85–88% | 0.3–0.8 | Good cost/accuracy ratio at VLM tier |
| Marker v2 | ~76% | 2.5–3.0 (GPU) | Best open-source; GPL-3.0 |
| MinerU | ~73% | 0.5–0.8 (GPU) | Strong on formulas; AGPL |
| olmOCR 2 | ~70% | 0.3–0.5 | Uses LLMs internally; Apache 2.0 |
| Docling | ~50% | 1.5–3.0 (GPU) | Widest format support; MIT; structured output |
| Unstructured (`hi_res`) | ~45–55% | 1.0–2.0 | Accuracy varies heavily by document type |
| PyMuPDF | ~35–40% | 100–200 | Fast geometric extraction; fails on complex layouts |
| pypdf | ~30–35% | 80–150 | Simplest; worst on anything non-trivial |

**The gap between VLM-based and layout-model parsers is roughly 15–20 points.** The gap between
layout models and geometric extractors is another 20–30 points. Whether either gap matters depends
entirely on your corpus — a born-digital, single-column corpus will see little difference between
tiers, and a multi-column scanned corpus will see the full gap.

### 3.3 What benchmarks don't capture

Five things matter in production that benchmarks consistently miss:

1. **Table structure fidelity** — edit similarity treats tables as text, so a table with correct
   text but destroyed column associations scores high. The parser that scores 75% overall may score
   90% on prose and 30% on tables, and it's the 30% that determines whether your financial RAG
   system answers "What was Q3 revenue?" or hallucinates.

2. **Failure mode distribution** — a parser that fails loudly (empty output) is preferable to one
   that fails silently (plausible-looking garbage). The latter populates your index with confident
   wrong answers. `02` §3.2's extraction-yield gate catches the first; catching the second requires
   corpus-specific validation.

3. **Header/footer handling** — benchmark scores rarely penalize header/footer injection because
   ground truth usually excludes them. In production, "Confidential — Page 7" spliced into every
   chunk degrades both retrieval and generation.

4. **Throughput at scale** — a parser that's 5% more accurate but 10x slower may not be viable at
   100K documents. Throughput determines your ingestion SLO (`15`), and the cost formula in
   `02` §12 shows that parsing is often the bottleneck.

5. **Determinism** — VLM-based parsers produce different text on re-runs. That breaks `02` §9's
   chunk identity scheme, which breaks incremental update, which breaks freshness SLOs. If you use
   a VLM parser, you need a different idempotency strategy.

---

## 4. Parser profiles

### 4.1 Docling

Docling is the tool with the widest format coverage and the most structured output in the
open-source field. Its `DoclingDocument` is a typed object model — not a string — with headings,
paragraphs, tables, lists, figures, and their nesting relationships. This matters because it
makes `02` §6.3's structure-aware splitting trivial: you split on the document model's element
boundaries, not on regex-matched headings in a text blob.

```
pip install docling

Formats: PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, AsciiDoc, CSV
Model:   Granite-Docling-258M (Apache 2.0, ~500MB)
GPU:     recommended but not required (CPU fallback works, 3-5x slower)
```

**When to pick it:** your corpus is multi-format (not just PDFs), you need structured output for
downstream processing, and MIT licensing is a hard requirement.

**When not to:** PDF-only corpus where table accuracy is the deciding metric — Marker and MinerU
both outperform on complex table layouts. Verify on your own tables before committing.

### 4.2 Marker v2

Marker is the accuracy leader in open-source PDF parsing. It combines layout detection, OCR, and
text extraction into a single pipeline that outputs clean Markdown. Version 2 (July 2026) uses a
transformer-based architecture that significantly improved multi-column and table handling.

```
pip install marker-pdf

Formats: PDF, images, EPUB, MOBI
License: GPL-3.0 — commercial use requires a license from Datalab
GPU:     required for practical throughput
```

**When to pick it:** PDF-dominant corpus, accuracy is the primary metric, and either GPL is
acceptable or you're willing to negotiate a commercial license.

**When not to:** multi-format corpus (Marker is PDF-centric), MIT/Apache license requirement,
or CPU-only infrastructure.

### 4.3 Unstructured

Unstructured occupies a unique position: it is both a library and a framework. The `partition`
functions accept a `strategy` parameter that selects the parsing tier per document (`fast` for
geometric, `hi_res` for layout models, `ocr_only` for scans, `auto` to choose per document).
This makes it the easiest tool for implementing `02` §3.3's per-document tier selection.

```
pip install unstructured

Formats: PDF, DOCX, PPTX, HTML, email (.eml, .msg), images, Markdown, RST, CSV, TSV, code
License: Apache 2.0 (open-source); Unstructured API is a separate commercial product
GPU:     required for hi_res strategy
```

**When to pick it:** you need ecosystem integration (native LangChain/LlamaIndex support),
multi-format with per-document strategy selection, or you're already in their commercial API.

**When not to:** raw accuracy on complex PDFs is the deciding factor — `hi_res` mode lags
Marker and MinerU on dense layouts. The open-source library and the commercial API have
diverged in capabilities.

### 4.4 PyMuPDF / pymupdf4llm

PyMuPDF is the speed champion. `pymupdf4llm` is a wrapper that formats extracted text as
Markdown, preserving some structure (headings, bold, lists) from font metadata. For born-digital,
single-column documents, its output is often indistinguishable from a layout model's — at 50–100x
the speed.

```
pip install pymupdf4llm

Formats: PDF, EPUB, XPS, images
License: AGPL-3.0 (commercial license available from Artifex)
GPU:     not needed — CPU-only, zero ML dependencies
```

**When to pick it:** born-digital single-column corpus, speed/cost are dominant constraints, or
you need a baseline to benchmark layout models against.

**When not to:** multi-column, table-heavy, or scanned documents. `02` §3.2's failure modes
(column interleaving, table flattening) apply in full.

---

## 5. Parser decision matrix

Use this to shortlist, not to decide. The decision comes from running your shortlist against your
own documents in `labs/document-processing/`, where `bakeoff.py` implements the tiers above as
swappable adapters over a corpus whose right answer is known by construction:

```
bakeoff.py --list                            # adapters by tier, and the document classes below
bakeoff.py --only pdf --tier 1 2             # every parser, both tiers, same bytes
bakeoff.py --only probe --doc invoice        # one document class, parse → gate → chunk
bakeoff.py --only probe --doc invoice --parser docling --chunker semchunk
```

Three results from that lab qualify the table below, and all three are things a benchmark score
cannot express (§3.3):

- **Marker outscores Docling by 26 points on olmOCR-Bench and interleaves a two-column page that
  Docling reads correctly.** Edit similarity barely moves when column *order* is wrong, because
  every token is still present.
- **Docling defeats the extraction gates you would use at tier 1.** On a PDF with an unreadable
  font it renders the page and returns clean, plausible, unverifiable text — perfect script
  sanity, no glyph leakage, nothing to alert on.
- **MinerU and Marker cannot be installed in the same virtualenv** (`transformers<5` vs `>=5.12`),
  so a two-tool shortlist drawn from this table may not be installable as a pair.

| Constraint | First choice | Second choice | Avoid |
|---|---|---|---|
| **Max accuracy, cost secondary** | VLM (GPT-4.1 vision / Claude) | Marker v2 | Geometric extractors |
| **Self-hosted, MIT/Apache license** | Docling | Unstructured (Apache 2.0) | Marker (GPL), MinerU (AGPL) |
| **Self-hosted, accuracy over license** | Marker v2 | MinerU | pypdf, PyMuPDF (on complex layouts) |
| **Throughput > 50 pages/sec** | PyMuPDF / pymupdf4llm | pypdf | Any layout model or VLM |
| **Multi-format corpus** | Docling | Unstructured | Marker (PDF-only) |
| **Table-heavy corpus** | VLM tier + HTML serialization | Marker v2 / MinerU | Geometric extractors |
| **Scanned documents** | Azure Document Intelligence | Surya + Marker | pypdf, pdfplumber (no OCR) |
| **Budget < $0** | Docling (MIT) or PyMuPDF for simple docs | Unstructured `fast` | Cloud APIs |
| **Academic / STEM** | Marker v2 or Mathpix (API) | Nougat (if non-commercial OK) | Generic geometric extractors |
| **Enterprise, regulated** | Azure Document Intelligence | Amazon Textract | Self-hosted without audit trail |

---

## 6. Chunking strategies

`02` §5–§6 covers the theory. This section gives you current benchmark numbers and tool mappings.

### 6.1 Strategy comparison

| Strategy | How it works | Retrieval accuracy (benchmarks) | Best for | Worst for |
|---|---|---|---|---|
| **Fixed-size** | Split at N tokens with M overlap | Baseline; no benchmark consistently measures against it | Uniform text (novels, transcripts) | Structured documents where boundaries carry meaning |
| **Recursive character** | Split on `\n\n`, then `\n`, then `. `, then ` `, at max chunk size | ~69% in published comparisons; strong default | General-purpose; mixed corpora | Documents where paragraph breaks don't align with topics |
| **Sentence-based** | Split on sentence boundaries (spaCy, NLTK, regex) | ~65% | Preserving sentence integrity | Languages where sentence detection is unreliable |
| **Semantic** | Embed consecutive segments, split where cosine similarity drops | ~54% in published comparisons (counter-intuitively lower than recursive) | Topically diverse documents with clear topic shifts | Uniform text; adds embedding cost and latency at ingest |
| **Document-structure-aware** | Split on heading boundaries from the parser's document model | ~87% in clinical study | Structured documents (manuals, specs, legal); requires a parser that emits structure | Unstructured text; dependent on parser quality |
| **Parent-document** | Index small chunks, retrieve, then expand to parent context for generation | +5–15% over flat chunking in practitioner reports | Table-heavy, context-dependent passages | Simple Q&A where expansion adds noise |
| **Late chunking** (Jina) | Embed the full document, then split the embedding; each chunk retains full-document context | Emerging; limited independent benchmarks | Long documents where local chunks lack context | Short documents; only works with compatible models (Jina v3) |
| **Agentic chunking** | LLM reads the document and decides chunk boundaries | Highest potential accuracy; 3–10x cost of other methods | High-value, low-volume corpora where chunk quality justifies LLM cost | Cost-sensitive; high-volume |

### 6.2 The recursive-vs-semantic surprise

The most-cited chunking benchmark (Feb 2026) found recursive character splitting at 512 tokens
outperformed semantic chunking by 15 points (69% vs 54%). This is counter-intuitive — a smarter
strategy should win — but it makes sense once you consider the failure mode:

Semantic chunking splits where embedding similarity drops. But embedding similarity is a noisy
signal at the local level — a pronoun referring to the previous paragraph has low similarity to the
next topic, but it's not a good split point because the pronoun is now orphaned. Recursive
splitting's dumb-but-consistent boundaries produce chunks of predictable size that the embedding
model was benchmarked on, and predictability wins over cleverness when the cleverness introduces
variance.

The document-structure-aware strategy beats both (87%) because it uses *structural* signals —
headings, section breaks — rather than *semantic* signals. This requires a parser that emits
structure (Docling's `DoclingDocument`, Unstructured's typed elements, or Markdown headings),
which brings the chunking conversation back to the parsing decision: a structure-aware chunker
is only as good as the structure it receives.

### 6.3 Chunk size: the tradeoffs, quantified

| Chunk size (tokens) | Pros | Cons | Best for |
|---|---|---|---|
| 128–256 | Precise retrieval; low noise in top-k | Loses context; more chunks to embed and index; higher cost | Fact-lookup, FAQ, definition-style Q&A |
| 256–512 | Good balance of precision and context; most-benchmarked range | Standard trade-off | General-purpose RAG; the safe default |
| 512–1024 | More context per chunk; fewer chunks; lower index cost | Dilutes relevance signal; risks including irrelevant content | Narrative text, summarization, documents with long arguments |
| 1024–2048 | Near-section-level retrieval | Embedding models degrade on long inputs; large context window consumption | Parent-document retrieval (index small, return large) |

**The 512-token default exists because it's where most embedding models were trained and
benchmarked.** It is not a principled optimum — it's a local equilibrium between model capability
and retrieval granularity. If your embedding model supports and was trained on longer inputs
(Cohere embed-v4: 128K context; Jina v3: 8K), the optimal chunk size shifts upward, and the
correct answer is to measure it on your own eval set.

---

## 7. Chunking tools

| Tool | Strategies available | Ecosystem | Differentiator |
|---|---|---|---|
| **LangChain text splitters** | Recursive character, HTML, Markdown, code (by language), token-based, sentence | LangChain native; most tutorials use it | Widest adoption; `RecursiveCharacterTextSplitter` is the most-used chunker in production |
| **LlamaIndex node parsers** | Sentence, token, semantic, hierarchical, markdown, code, JSON | LlamaIndex native | `SentenceSplitter` is battle-tested; hierarchical parser supports parent-document natively |
| **Chonkie** | Token, word, sentence, semantic, SDPM, late, neural | Standalone (505KB); no framework dependency | Purpose-built for chunking; 4.82 MB/s throughput; 9 strategies in one library; supports `tokenizers`, `tiktoken`, `autotiktokenizer` |
| **Unstructured chunkers** | By-title (structure-aware), by-page, by-similarity, basic | Unstructured native | Operates on typed `Element` objects from Unstructured's partition; structure-aware by default |
| **Docling chunker** | Hierarchical (follows `DoclingDocument` structure) | Docling native | Chunking preserves the document model's tree structure; metadata (heading path, page, bbox) propagated automatically |
| **Semantic Chunker** (various) | Embed-then-split based on similarity drops | Standalone or embedded in LangChain/LlamaIndex | The canonical semantic chunking implementation; Greg Kamradt's original + derivatives |

**Recommendation:** start with `RecursiveCharacterTextSplitter` at 512 tokens / 50-token overlap
as a baseline. Switch to structure-aware chunking (Docling chunker or Unstructured `by_title`) if
your parser emits structure. Evaluate with your golden set before adopting semantic or late chunking
— the complexity cost is real and the accuracy benefit is not guaranteed.

---

## 8. Embedding models — the current field

`01` covers the theory and the schema-decision argument. This section gives you the current
leaderboard and a decision matrix.

### 8.1 Top models (MTEB v2, mid-2026)

| Model | Provider | MTEB avg | Dimensions | Max tokens | Pricing (per 1M tokens) | Key properties |
|---|---|---|---|---|---|---|
| **Qwen3-Embedding-8B** | Alibaba (open-weight) | ~70.6 | 4096 (MRL: 256–4096) | 32K | Self-hosted | Current MTEB leader (open-weight); MRL support; multilingual |
| **Cohere embed-v4** | Cohere | ~69 | 1024 (MRL: 256–1024) | 128K | $0.10 | Multimodal (text + images); 128K context; 100+ languages; binary quantization native |
| **Voyage AI voyage-3-large** | Voyage AI | ~68 | 1024 | 32K | $0.18 | Strong on code and technical text; instruction-tuned |
| **text-embedding-3-large** | OpenAI | ~66 | 3072 (MRL: 256–3072) | 8K | $0.13 | MRL support; widely deployed; predictable |
| **Jina Embeddings v3** | Jina AI | ~66 | 1024 (MRL: 32–1024) | 8K | $0.02 | Best cost/quality ratio; late chunking support; task-specific LoRA adapters |
| **text-embedding-3-small** | OpenAI | ~62 | 1536 | 8K | $0.02 | Good budget option; MRL support |
| **BGE-M3** | BAAI (open-weight) | ~64 | 1024 | 8K | Self-hosted | Dense + sparse + ColBERT in one model; strong multilingual; Apache 2.0 |
| **NV-Embed-v2** | NVIDIA (open-weight) | ~68 | 4096 | 32K | Self-hosted | Strong on retrieval tasks specifically; large dimensions |
| **Nomic Embed v2** | Nomic (open-weight) | ~63 | 768 (MRL: 64–768) | 8K | $0.008 / self-hosted | Apache 2.0; MRL; efficient; Nomic Atlas integration |
| **GTE-Qwen2-7B** | Alibaba (open-weight) | ~67 | 3584 | 32K | Self-hosted | Strong multilingual; long context |

### 8.2 Embedding decision matrix

| Constraint | First choice | Second choice | Notes |
|---|---|---|---|
| **Max quality, API** | Cohere embed-v4 | Voyage-3-large | Cohere wins on multilingual + multimodal; Voyage on code |
| **Max quality, self-hosted** | Qwen3-Embedding-8B | NV-Embed-v2 | Both need GPU; Qwen has MRL, NV-Embed is retrieval-focused |
| **Best cost/quality API** | Jina v3 ($0.02/1M) | text-embedding-3-small ($0.02/1M) | Jina outperforms at the same price; late chunking is a bonus |
| **Multilingual** | Cohere embed-v4 | BGE-M3 | Cohere: 100+ languages; BGE-M3: open-weight, dense+sparse |
| **Code / technical** | Voyage-3-large | Qwen3-Embedding-8B | Voyage is specifically strong on code retrieval |
| **Multimodal (text + images)** | Cohere embed-v4 | — | Currently the only production multimodal embedding API |
| **Hybrid search (single model)** | BGE-M3 | — | Emits dense, sparse, and ColBERT vectors from one forward pass |
| **Maximum context** | Cohere embed-v4 (128K) | Qwen3-Embedding-8B (32K) | Long context ≠ good retrieval on long inputs — measure it |
| **Budget zero** | Nomic Embed v2 (Apache 2.0) | BGE-M3 (Apache 2.0) | Both self-hostable on modest GPU |

### 8.3 The open-weight quality gap has closed

The headline from the 2025–2026 MTEB cycle: open-weight models (Qwen3-Embedding, GTE-Qwen2,
BGE-M3, NV-Embed) now match or exceed proprietary API models on aggregate benchmarks. The
remaining advantage of API models is operational — no GPU infrastructure, no model serving, no
version management — not quality. If you have GPU capacity and the engineering to serve a model,
the quality argument for an API is gone.

The cost argument is less clear. Jina v3 at $0.02/1M tokens is cheaper than self-hosting a 7B
model on anything smaller than a dedicated A100 at sustained utilization. The break-even depends
on your embedding volume, your GPU cost, and whether your team can keep a model server running —
the same calculation as any build-vs-buy decision.

---

## 9. Vector stores and retrieval infrastructure

`03` covers index internals (HNSW parameters, quantization, filtered search). This section covers
the *products* you build on top of those internals.

### 9.1 Current landscape

| Store | Type | Hybrid search | Key strength | Operational model | License |
|---|---|---|---|---|---|
| **pgvector / pgvecto.rs** | Extension on PostgreSQL | BM25 via `pg_search` or application-side | Single-database simplicity; SQL joins across vectors and metadata; ACID | Self-hosted or managed Postgres (Supabase, Neon, RDS) | PostgreSQL / Apache 2.0 |
| **Qdrant** | Purpose-built vector DB | Native sparse vectors + dense; built-in RRF | Fastest P99 latency (~12ms at 10M vectors); Rust performance; rich filtering | Self-hosted or Qdrant Cloud | Apache 2.0 |
| **Weaviate** | Purpose-built vector DB | Native BM25 + dense hybrid | Strongest native hybrid search composition; GraphQL API; multi-tenancy native | Self-hosted or Weaviate Cloud | BSD-3-Clause |
| **Milvus / Zilliz** | Purpose-built vector DB | Sparse + dense; GPU-accelerated indexing | Highest scale ceiling (billions of vectors); GPU-accelerated; strong RBAC | Self-hosted or Zilliz Cloud | Apache 2.0 |
| **Chroma** | Embedded vector DB | Limited | Simplest API; in-process for prototyping; SQLite backend | Embedded or self-hosted | Apache 2.0 |
| **FAISS** (Meta) | Library (not a database) | No (dense only) | Fastest raw ANN search; GPU-accelerated; battle-tested at Meta scale | Library you integrate | MIT |
| **LanceDB** | Embedded, columnar | Via Lance format | Disk-based (no server); multimodal native; versioned datasets | Embedded | Apache 2.0 |
| **Pinecone** | Managed service | Sparse + dense in one index | Zero-ops; serverless tier; metadata filtering | Fully managed only | Proprietary |
| **Vespa** | Search platform | Native hybrid (BM25 + ANN) | Full search platform with ML serving; strongest for search-heavy workloads | Self-hosted or Vespa Cloud | Apache 2.0 |

### 9.2 Vector store decision matrix

| Constraint | First choice | Second choice | Notes |
|---|---|---|---|
| **Already have Postgres** | pgvector | — | Under ~5M vectors, pgvector eliminates an entire service; `03` §8 covers the parameters |
| **Max performance, self-hosted** | Qdrant | Milvus (with GPU) | Qdrant for latency; Milvus for throughput at extreme scale |
| **Best hybrid search** | Weaviate | Qdrant | Weaviate's hybrid composition is the most expressive; Qdrant is faster |
| **Billion-scale** | Milvus / Zilliz | Vespa | Both designed for this; pgvector and Chroma are not |
| **Zero-ops** | Pinecone | Qdrant Cloud / Weaviate Cloud | Pinecone if you want fully managed; cloud-hosted if you want escape hatch |
| **Prototyping** | Chroma | LanceDB | Both are embedded, pip-installable, zero-config |
| **Search-heavy (not just RAG)** | Vespa | Weaviate | Vespa is a search platform, not just a vector store |
| **Budget zero, small corpus** | pgvector | Chroma | pgvector if you have Postgres; Chroma if you don't |

### 9.3 The hybrid retrieval baseline

`04` makes the argument in detail. The compressed version for this appendix:

**Dense-only retrieval is not the baseline anymore. Hybrid (dense + BM25) with a reranker is.**
Practitioner reports from 2024–2026 consistently show +5–15% recall improvement from adding a
BM25 branch with RRF fusion, and another +3–8% from adding a cross-encoder reranker. The cost
of the BM25 branch is near-zero (it's a text index), and the reranker cost is proportional to
top-k, not corpus size.

If you are running dense-only retrieval and haven't tested hybrid, that is the single
highest-ROI change available to you before touching the parser or the embedding model.

---

## 10. Rerankers

| Reranker | Type | Latency (100 passages) | Quality | Cost |
|---|---|---|---|---|
| **Cohere Rerank v3.5** | API (cross-encoder) | ~200ms | Strongest on general-domain; multilingual | $2/1K searches |
| **Jina Reranker v2** | API (cross-encoder) | ~150ms | Strong; cost-effective | $0.02/1K searches |
| **Voyage Rerank 2** | API (cross-encoder) | ~180ms | Strong on code/technical | $0.05/1K searches |
| **BGE-Reranker-v2.5-gemma2** | Open-weight | ~300ms (GPU) | Near-API quality; Apache 2.0 | Self-hosted GPU |
| **cross-encoder/ms-marco-MiniLM-L-12** | Open-weight | ~100ms (GPU) | Good baseline; fast | Self-hosted; CPU-viable |
| **ColBERT v2** | Late interaction | ~50ms | Different paradigm; per-token matching | Self-hosted |
| **FlashRank** | Open-weight (small) | ~50ms (CPU) | Fastest; lowest quality of the group | Self-hosted; CPU-only |

**Recommendation:** start with Cohere Rerank or Jina Reranker for quality; move to
BGE-Reranker-v2.5 if you need self-hosted at comparable quality. The reranker is applied to
top-k only (typically 20–100 passages), so even API pricing is negligible relative to embedding
or generation costs.

---

## 11. End-to-end RAG evaluation frameworks

`08` and `09` will cover evaluation methodology and infrastructure in depth. This section is a
tool-selection aid.

| Framework | Approach | Key metrics | Integration | Best for |
|---|---|---|---|---|
| **RAGAS** | Reference-free LLM-as-judge; modular metrics | Faithfulness, answer relevancy, context precision, context recall, answer correctness | LangChain, LlamaIndex, standalone | Prototyping and rapid iteration; no ground truth required; community standard |
| **DeepEval** | Pytest-native; metric plugins; CI/CD-first | Faithfulness, hallucination, answer relevancy, contextual relevancy/precision/recall, bias, toxicity, G-Eval, summarization | Pytest, CI/CD pipelines | Engineering teams that want eval as tests; regression gates; broadest metric set |
| **TruLens** | Feedback functions over traces; production monitoring | Groundedness, relevance (question→context, context→answer), moderation | LangChain, LlamaIndex; Snowflake | Production monitoring alongside development eval |
| **Arize Phoenix** | Trace-based eval with LLM-as-judge; OTEL native | Retrieval metrics, generation metrics, custom LLM evals | OTEL, LangChain, LlamaIndex | Teams already using OTEL traces; connecting eval to observability |
| **Braintrust** | Eval platform with scoring, comparison, versioning | Custom scorers, LLM-as-judge, human eval | API-based; framework-agnostic | Teams that want a managed eval platform; A/B comparison of pipeline versions |
| **LangSmith** | LangChain's eval/observability platform | Custom evaluators, LLM-as-judge, human annotation | LangChain native | LangChain users; tightly integrated with LangChain traces |

### 11.1 Key metrics defined

| Metric | What it measures | Why it matters |
|---|---|---|
| **Faithfulness** | Does the answer contain only information supported by the retrieved context? | Detects hallucination — the generator making things up beyond what retrieval provided |
| **Answer relevancy** | Does the answer address the question asked? | Detects off-topic answers even when context is correct |
| **Context precision** | Of the retrieved chunks, what fraction is actually relevant? | Retrieval noise — irrelevant chunks waste context window and can mislead generation |
| **Context recall** | Of the relevant information in the corpus, what fraction was retrieved? | Retrieval coverage — missed relevant chunks mean the generator can't know |
| **Hallucination rate** | Fraction of generated claims not grounded in context | The production metric users care about most |
| **Answer correctness** | Semantic similarity + factual overlap with ground truth | Requires ground truth; the gold standard when you have it |

### 11.2 A finding worth internalizing

Practitioner analyses from 2025–2026 consistently report that **generation failures account for
28–42% of hallucinations** — cases where the correct context was retrieved but the model ignored
it, misread it, or confabulated beyond it. This means even a perfect retrieval system inherits a
hallucination floor from the generation model. Evaluation frameworks that measure only retrieval
metrics miss this entirely, which is why faithfulness (a generation metric) belongs in every eval
suite alongside recall (a retrieval metric).

---

## 12. Recommended stacks by use case

These are starting points. Every component has a "swap when" note — the thing most likely to be
wrong for your specific case.

### 12.1 High-accuracy enterprise

For regulated industries, financial documents, legal corpora — where accuracy justifies cost.

| Component | Choice | Rationale |
|---|---|---|
| **Parser** | Azure Document Intelligence (primary) + Marker v2 (self-hosted fallback) | Azure for confidence scores and audit trail; Marker for documents that stay on-prem |
| **Chunker** | Structure-aware (Docling chunker or Unstructured `by_title`), 512 tokens, with parent-document retrieval for tables | Structure-aware captures section boundaries; parent-doc retrieval preserves table context |
| **Embeddings** | Cohere embed-v4 | 128K context; multilingual; multimodal; binary quantization for cost control |
| **Vector store** | pgvector (< 5M vectors) or Qdrant (> 5M) | pgvector for single-database simplicity; Qdrant when you outgrow it |
| **Reranker** | Cohere Rerank v3.5 | Strongest general-domain reranker |
| **Eval** | DeepEval + golden set | Pytest-native; CI regression gates; faithfulness + retrieval metrics |

**Swap when:** Marker's GPL blocks you → switch to Docling (MIT) with accuracy trade-off
measured. Azure pricing is prohibitive at volume → VLM on high-value docs, Docling on bulk.

### 12.2 Cost-effective / startup

For teams optimizing cost per query at decent quality — the 80/20 stack.

| Component | Choice | Rationale |
|---|---|---|
| **Parser** | Docling (MIT, free) or PyMuPDF for simple docs | Docling for complex layouts; PyMuPDF for born-digital single-column |
| **Chunker** | `RecursiveCharacterTextSplitter`, 512 tokens, 50-token overlap | Proven default; no additional dependencies |
| **Embeddings** | Jina v3 ($0.02/1M tokens) or Nomic Embed v2 (self-hosted, free) | Jina for API simplicity; Nomic for zero marginal cost |
| **Vector store** | pgvector (if you have Postgres) or Chroma (prototyping) | No additional infrastructure |
| **Reranker** | Jina Reranker v2 ($0.02/1K searches) or FlashRank (free, CPU) | Jina for quality; FlashRank for zero cost |
| **Eval** | RAGAS (free, reference-free) | No ground truth required; fast iteration |

**Swap when:** table-heavy corpus and PyMuPDF is destroying tables → upgrade parser to Docling or
Marker (the parser is the ceiling). Retrieval quality plateaus → add hybrid BM25 before changing
anything else.

### 12.3 Multilingual

For corpora spanning multiple languages or non-English-primary systems.

| Component | Choice | Rationale |
|---|---|---|
| **Parser** | Docling (handles non-Latin scripts) + Azure Document Intelligence (strongest multilingual OCR) | Both handle CJK, Arabic, Cyrillic; Azure for scans |
| **Chunker** | Sentence-based (spaCy with language-specific model) or recursive character with language-aware sentence detection | Sentence boundaries differ by language; fixed-size token splits break mid-word in agglutinative languages |
| **Embeddings** | Cohere embed-v4 (100+ languages) or BGE-M3 (self-hosted, strong multilingual) | Cohere for breadth; BGE-M3 for self-hosted + hybrid (dense+sparse) |
| **Vector store** | Weaviate (native multi-tenancy for per-language indexes) or Qdrant | Per-language indexes avoid cross-language noise in retrieval |
| **Reranker** | Cohere Rerank v3.5 (multilingual) | Few rerankers handle non-English well; Cohere is the strongest |
| **Eval** | RAGAS with multilingual judge model | Ensure the judge model handles the target language |

**Swap when:** single language dominates (>90% of corpus) → use a language-specific embedding
model instead of a multilingual one; monolingual models typically outperform multilingual on their
target language.

### 12.4 Code and technical documentation

For API docs, codebases, technical specs, developer-facing knowledge bases.

| Component | Choice | Rationale |
|---|---|---|
| **Parser** | tree-sitter (code) + Docling or Marker (documentation) | tree-sitter gives AST-level structure for code; Docling for everything else |
| **Chunker** | Code: AST-aware splitting (by function/class) via LangChain `Language` splitter or tree-sitter; Docs: recursive character, 512 tokens | Code has natural boundaries (functions, classes); splitting mid-function destroys context |
| **Embeddings** | Voyage-3-large (strongest on code retrieval) or Qwen3-Embedding-8B (self-hosted) | Voyage is specifically benchmarked on code; Qwen for self-hosted |
| **Vector store** | Qdrant or pgvector | Low latency for developer-facing tools |
| **Reranker** | Voyage Rerank 2 (code-aware) | Specifically tuned for code/technical retrieval |
| **Eval** | DeepEval with code-specific golden set | Test with real developer queries, not synthetic ones |

**Swap when:** documentation is the bottleneck, not code → simplify the parser stack; tree-sitter
is unnecessary for Markdown docs. Mixed code+prose queries → ensure the embedding model handles
both well (Voyage and Qwen do; test on your query distribution).

---

## 13. Anti-patterns

1. **Picking from a table instead of from an eval.** This appendix narrows your options from
   twenty to three. It does not pick for you. The team that deploys the top-ranked parser without
   running it against their own five worst documents will discover — in production — that their
   five worst documents are the ones the benchmark didn't cover.

2. **Optimizing the embedding model while feeding it broken parses.** `02` §1's ceiling chain:
   no embedding model recovers information the parser destroyed. If your retrieval quality is
   capped, check the parse output before upgrading the model.

3. **Using semantic chunking because it sounds smarter.** The benchmark data says recursive
   splitting outperforms semantic chunking in general-purpose settings. Semantic chunking has a
   narrower win condition (topically diverse documents with clear topic shifts) than its popularity
   suggests. Measure before adopting.

4. **Running dense-only retrieval in 2026.** Hybrid retrieval (dense + BM25) with a reranker is
   the minimum viable baseline. Dense-only leaves 5–15% recall on the table for near-zero
   additional cost. If you haven't tested hybrid, that is your highest-ROI next step.

5. **Trusting benchmark numbers without checking the corpus.** A parser that scores 76% on
   olmOCR-Bench may score 40% on your legal contracts or 90% on your single-column reports.
   Benchmark corpora are never your corpus. The score tells you whom to evaluate, not what to
   deploy.

6. **Ignoring the license.** Marker is GPL-3.0. MinerU is AGPL-3.0. PyMuPDF is AGPL-3.0.
   Deploying these in a commercial product without understanding the implications is a legal
   risk, not a technical one.

7. **Skipping the reranker to save money.** A cross-encoder reranker operates on top-k only
   (20–100 passages per query). At $0.02–$2.00 per 1K searches, it is the cheapest component
   in the pipeline relative to its impact. The cost of *not* reranking is answered in
   irrelevant passages consuming your context window and your generation budget.

8. **Evaluating retrieval without evaluating generation.** 28–42% of hallucinations come from
   generation failures, not retrieval failures. An eval suite that measures only recall@k will
   miss nearly half the problem. Always include faithfulness alongside retrieval metrics.

---

## 14. Mental models — the compressed set

- **The parser is the ceiling.** Everything downstream — chunking, embedding, retrieval — can
  only preserve or lose what the parser extracted. Benchmark the parser first.

- **Benchmarks are shortlists, not decisions.** They tell you which three tools to evaluate.
  The decision comes from running them on your own documents.

- **Recursive > semantic, structure-aware > both.** In general-purpose settings, dumb-but-consistent
  chunking beats clever-but-variable chunking. Structure-aware chunking beats both, but requires
  a parser that emits structure.

- **Hybrid + reranker is the baseline.** Dense-only retrieval is the 2023 default. The 2026
  default is dense + BM25 + reranker. The cost difference is negligible; the quality difference
  is not.

- **The open-weight embedding gap closed.** Qwen3-Embedding and BGE-M3 match API models on
  benchmarks. The remaining API advantage is operational, not quality.

- **Generation hallucinates even with perfect retrieval.** Measure faithfulness, not just recall.
  The eval suite that only checks retrieval will miss 30–40% of the problem.

- **Every number in this appendix is a snapshot.** The landscape moves quarterly. The mental
  models move slowly. Trust the models, verify the numbers.
