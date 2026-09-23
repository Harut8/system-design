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

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
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
15. [How to use these benchmarks in an interview](#15-how-to-use-these-benchmarks-in-an-interview)
16. [Real-world cases — incidents with numbers](#16-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** Building a RAG system means picking tools at every step: a parser to read the
PDFs, a chunker to cut them up, an embedding model, a vector store, a reranker, and an evaluation
tool. There are dozens of options for each. Vendors and leaderboards publish scores, but those
scores were measured on *someone else's* documents. This appendix is a menu with prices and rough
scores. It helps you pick two or three candidates per step. It cannot pick the winner for you —
only a test on your own documents can.

**A real-world example.** An insurance company wants a claims assistant over **20,000 pages** of
PDFs: two-column policy booklets, statements full of fee tables, and about 10% scanned letters.
All numbers below are illustrative, computed from the ranges in this appendix.

1. **Pick from the table alone.** A developer sees that a VLM parser scores highest (§3.2) and
   plans to use it everywhere. At ~0.3 pages/sec one worker needs ~18.5 hours, and at ~$0.011 per
   page the bill is ~$220 per full re-parse. Affordable once, painful if every re-index costs that.
2. **Pick the fastest tool.** PyMuPDF does all 20,000 pages in ~3.3 minutes (100 pages/sec) for
   free. But on the two-column booklets it mixes the left and right columns together, and fee
   tables come out as one long line of numbers. The bot answers "What is the annual fee on plan B?"
   with plan A's fee.
3. **Do what this appendix says.** Use the tables to shortlist three parsers (say PyMuPDF,
   Docling, and a VLM for scans only). Run them on the 50 worst pages in the lab
   (`labs/document-processing/`). Suppose Docling reads the columns right and takes ~2.8 hours on
   one GPU (2 pages/sec). Send only the ~2,000 scanned pages to the VLM (~$22). Total: good
   quality at about a tenth of the all-VLM cost.
4. **Then the rest of the stack.** Recursive chunks of ~512 tokens, a mid-priced embedding model,
   hybrid search (vector + keyword), a reranker on the top 50, and faithfulness checks in the eval
   suite. Each choice comes from a table below, and each is checked on your own test questions.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Parser | turns a PDF/DOCX into text (and ideally structure: headings, tables) | a typist copying a printed page |
| Geometric extraction (tier 1) | reads letters by their position on the page, no AI | copying word by word, left to right, ignoring columns |
| Layout model (tier 2) | an AI model finds regions (columns, tables) first, then reads each | first marking the columns with a pen, then copying |
| VLM parser (tier 3) | a vision-language model "looks at" the page image and writes it out | asking a person to read the page aloud |
| OCR | turning pixels of a scanned image into letters | reading a faxed letter |
| Benchmark | a fixed test set everyone scores their tool on | a standard exam |
| Leaderboard | the ranking of scores on a benchmark | the class ranking after the exam |
| Chunk / chunker | a piece of a document stored for search / the tool that cuts it | cutting a book into index cards |
| Embedding model | turns text into a list of numbers so similar texts sit close | GPS coordinates for meaning |
| MTEB | the most common embedding benchmark (many tasks, averaged) | a decathlon score |
| Vector store | a database that finds the nearest vectors fast | the library catalogue |
| Hybrid search | vector search plus keyword (BM25) search, merged | asking both a librarian and the index at the back of the book |
| Reranker | a slower, smarter model that re-sorts the top results | a second, careful reader checking the shortlist |
| Faithfulness | does the answer stick to what the retrieved text says | a student answering only from the open textbook |
| License (GPL/AGPL/MIT/Apache) | legal terms for using the code in a product | the rental contract for a tool |

### Symbols and parameters used in this chapter

This appendix has few formulas. It is full of numbers, though. Here is what each column and knob
means.

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| pages/sec | how many pages a parser handles per second on one machine | 0.1 (VLM) – 200 (PyMuPDF) | 2 pages/sec → 20,000 pages in ~2.8 hours |
| $/page, $/1K pages | parsing price | $0.0015 – $0.05 per page | Azure Read at $1.50/1K pages → 20,000 pages ≈ $30 |
| olmOCR-Bench score | % of small pass/fail checks (text present, reading order, table cell) the output passes | 30% – 85% | 76% → 76 of every 100 checks passed |
| Edit similarity | how few character edits turn the output into the correct text (1 = identical) | 0 – 1 | "Invoce" vs "Invoice" → 1 edit, very similar |
| mAP, IoU | layout-detection scores: how well predicted boxes overlap the true regions | 0 – 1 | a table box that covers 90% of the real table → IoU ≈ 0.9 |
| chunk size | length of one chunk, in tokens | 256 – 1,024 (default 512) | 512 tokens ≈ one page of text |
| overlap | tokens repeated between neighbouring chunks | 0 – 20% of chunk size | 50 tokens of a 512-token chunk |
| tokens | pieces of words the models count | ~0.75 English words per token | 1,000 tokens ≈ 750 words |
| MTEB avg | an embedding model's average score across many tasks | 60 – 72 | 70 vs 64 → clearly better tier; 69 vs 68 → noise |
| `d` (dimensions) | how many numbers are in one embedding | 256 – 4,096 | 1,024 numbers per chunk |
| MRL | "Matryoshka" training: you can cut the vector shorter and it still works | e.g. 4,096 → 256 | store 256 numbers instead of 1,024 to save 75% memory |
| max tokens | the longest input an embedding model accepts | 512 – 128K | a 512-token limit cuts off longer chunks |
| $/1M tokens | embedding API price | $0.02 – $0.18 | 20,000 pages × ~600 tokens = 12M tokens → $0.24 at $0.02 |
| `N` | number of vectors in the store | 10K – 1B | 20,000 pages × ~2 chunks = 40,000 vectors |
| top-k | how many results you keep from a search | 5 – 100 | rerank the top 50, send the best 5 to the LLM |
| P99 latency | 99% of queries are faster than this | 5 – 200 ms | P99 = 12 ms → 1 query in 100 is slower |
| RRF | Reciprocal Rank Fusion: merges two ranked lists by rank position | constant 60 | a doc ranked 1st by BM25 and 3rd by vectors ends near the top |
| $/1K searches | reranker price per 1,000 queries | $0 – $2 | Cohere at $2/1K → 1M queries ≈ $2,000 |
| recall@k | share of the relevant chunks that appear in the top k | 0.7 – 0.95 | 4 of 5 relevant chunks found → 0.8 |
| context precision / recall | share of retrieved chunks that are relevant / share of needed info that was retrieved | 0 – 1 | 3 of 5 retrieved chunks useful → precision 0.6 |
| faithfulness | share of the answer's claims supported by the retrieved text | target > 0.9 | 9 of 10 claims supported → 0.9 |
| hallucination rate | share of claims not supported by any source | target < 5% | 1 made-up claim in 20 → 5% |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. How to use this appendix

> **In plain words.** This page is a menu, not a verdict. Use it to cut dozens of tools down to two or three, then test those on your own files.
>
> **Real-world example.** A legal team has 40 candidate tools across the stack. The tables cut that to 3 parsers, 2 embedding models and 2 vector stores; a one-week test on 100 of their own contracts picks the winners.

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

> **In plain words.** A parser turns a file into text. Cheap parsers read letters by position; smarter ones use AI to find columns and tables first; the smartest read the page like a person, but cost more and run slower.
>
> **Real-world example.** A bank has 100,000 statement pages. PyMuPDF at ~100 pages/sec finishes in ~17 minutes for free; Azure Read at $1.50/1K pages costs ~$150; a VLM at ~$0.011/page would cost ~$1,100 (illustrative list prices).

`02` §3 established the parser as the ceiling of the entire pipeline. This section gives you
the current landscape for that ceiling.

### 2.1 The three tiers, with current representatives

| Tier | Mechanism | Representative tools | Typical speed | Typical accuracy | Cost model |
|---|---|---|---|---|---|
| **1 — Geometric extraction** | Glyph positions → heuristic lines/blocks | PyMuPDF, pypdf, pdfplumber, pdfminer.six | 50–200 pages/sec | 30–50% on complex layouts | CPU-milliseconds, free |
| **2 — Layout models** | Detection model segments page into regions, extracts per region | Docling, Marker v2, MinerU, Unstructured (`hi_res`), Surya | 0.5–5 pages/sec | 50–77% on benchmarks | CPU/GPU-seconds, self-hosted or per-page API |
| **3 — VLM page understanding** | Vision-language model reads rendered page | GPT-4.1 vision, Claude vision, Gemini, olmOCR 2 | 0.1–1 pages/sec | Highest on hard pages (see §3.2 caveats) | LLM token cost per page (~$0.003–0.01/page at list prices, illustrative) |

Speed and accuracy columns are illustrative ranges, not measurements; §3.2 explains where the
accuracy figures come from. The VLM cost is computed for one page as ~1.5K input + ~1K output
tokens: about $0.011 at GPT-4.1 list prices ($2 / $8 per 1M) and about $0.003 at Gemini 2.5 Flash
prices ($0.30 / $2.50 per 1M). Check current prices before you budget.

The tier boundaries are porous and moving. Docling's `Granite-Docling-258M` is a 258M-parameter
model that parses a page in a single pass — it is technically a layout model but runs at speeds
closer to geometric extraction on GPU. The distinction that matters is not the mechanism but the
*cost-accuracy tradeoff curve* you're on.

### 2.2 Open-source parsers — current field

| Parser | License | Key strengths | Key weaknesses | Format support |
|---|---|---|---|---|
| **Docling** (IBM / LF AI) | MIT | Widest format support (PDF, DOCX, PPTX, HTML, images, AsciiDoc); structured `DoclingDocument` output; `Granite-Docling-258M` (Apache 2.0); pip-installable | Table accuracy lags Marker/MinerU on complex layouts; GPU recommended for reasonable throughput | PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, AsciiDoc |
| **Marker v2** | GPL-3.0 | Among the highest-scoring open-source pipelines on olmOCR-Bench (~76%, Datalab-reported); good multi-column and table handling; active development | GPL license restricts commercial use without negotiation; GPU required; primarily PDF-focused | PDF, images, EPUB, MOBI |
| **MinerU** (OpenDataLab) | AGPL-3.0 | Strong accuracy (~73% on olmOCR-Bench); good table/formula extraction; comprehensive pipeline | Slow (0.5 pages/sec CPU); AGPL license; heavy dependencies | PDF, images |
| **Unstructured** (open-source) | Apache 2.0 | Broadest ecosystem integration (LangChain, LlamaIndex native); `strategy` parameter for per-doc tier selection; partitions into typed elements | Accuracy depends heavily on strategy choice; `hi_res` requires model downloads; API version is separate product | PDF, DOCX, PPTX, HTML, email, images, Markdown, RST, CSV, TSV, code |
| **PyMuPDF / pymupdf4llm** | AGPL-3.0 | Fastest CPU extraction; `pymupdf4llm` emits Markdown-formatted text preserving some structure; mature, well-maintained | No layout model — fails on multi-column, complex tables; AGPL license | PDF, EPUB, XPS, images |
| **Surya** | GPL-3.0 | Strong OCR + layout detection; line-level recognition; multilingual | Primarily a recognition engine, not a full document parser; GPL | PDF, images |
| **olmOCR** (AI2) | Apache 2.0 | Uses vision LLMs for page understanding; hosts the olmOCR-Bench leaderboard; research-grade | Resource-intensive; not designed as a production library | PDF, images |
| **Nougat** (Meta) | MIT code, CC-BY-NC weights | Academic paper specialist; outputs LaTeX/Markdown from scientific PDFs | Narrow domain; non-commercial license; slow; hallucinates on non-academic docs | PDF (academic) |
| **pypdf** | BSD | Pure Python, zero dependencies, pip-installable | Geometric extraction only; poor on anything beyond simple single-column | PDF |
| **pdfplumber** | MIT | Excellent table extraction via geometric heuristics; visual debugging | Slow on large documents; geometric only; no layout understanding | PDF |
| **Camelot / Tabula** | MIT / MIT | Purpose-built for table extraction; Camelot offers lattice + stream modes | Tables only, not full document parsing; maintenance varies | PDF (tables only) |

### 2.3 Cloud/API parsers

| Service | Provider | Strengths | Pricing (approx.) | When to use |
|---|---|---|---|---|
| **Azure Document Intelligence** | Microsoft | Strong printed-text OCR (vendor-reported accuracy; verify on your scans); prebuilt models for invoices, receipts, ID documents; table extraction with confidence scores | $1.50/1K pages (read); $10/1K pages (prebuilt) | Enterprise with Azure footprint; regulated industries needing confidence scores |
| **Amazon Textract** | AWS | Strong table and forms extraction; query-based extraction; AWS ecosystem integration | $1.50/1K pages (detect text); $15/1K pages (tables) | Enterprise with AWS footprint; form-heavy corpora |
| **Google Document AI** | Google | Strong multilingual OCR; specialized processors for specific document types; Vertex integration | $1.50/1K pages (OCR); custom pricing for specialized | Multilingual corpora; Google Cloud ecosystem |
| **LlamaParse** (LlamaIndex) | LlamaIndex | Built for RAG; outputs Markdown; handles complex layouts; LlamaIndex-native | Credit-based; cost per page depends on parsing mode (check current pricing) | LlamaIndex users; cost-sensitive with decent accuracy needs |
| **Mathpix** | Mathpix | Best-in-class LaTeX/math extraction; outputs structured Markdown/LaTeX | $0.01–0.04/page | STEM documents, equations, scientific papers |
| **Reducto** | Reducto | Optimized for RAG with chunk-aware parsing; fast API | Custom pricing | High-volume production RAG pipelines |

Prices are list prices at the time of writing and change often; treat them as order-of-magnitude
guides. Accuracy claims in the "Strengths" column are vendor positioning, not independent
measurements.

---

## 3. Parser benchmark landscape

> **In plain words.** A benchmark is a standard exam for parsers. Each exam tests certain things and ignores others, and many scores are reported by the tool's own authors. Treat a score as a hint about where to look.
>
> **Real-world example.** A parser passes 76% of olmOCR-Bench's checks. On your insurance tables it may still mix up columns, because the exam had only a few tables like yours.

### 3.1 Current benchmarks

| Benchmark | What it measures | Corpus | Methodology | Limitations |
|---|---|---|---|---|
| **olmOCR-Bench** (AI2) | Share of pass/fail unit tests the parser's output passes (text present/absent, reading order, table cells, math) | ~1,400 PDFs with ~7,000 unit tests: arXiv math, old scans, tables, headers/footers, multi-column, tiny text | Each test is a simple fact check on the output; score = % of tests passed | Tests check specific facts, not full-document fidelity; English-heavy; many scores on the leaderboard are self-reported by the tool's authors |
| **pdf-parser-benchmark** (Applied AI; community project, verify before citing) | Column-order correctness, row-band detection, table extraction | Curated set of challenging PDFs with known ground truth | Per-feature correctness assessment | Smaller dataset; emphasizes specific failure modes |
| **FinTabNet** | Table detection and structure recognition in financial documents | ~113K tables from annual reports | IoU-based detection + cell adjacency for structure | Tables only; financial domain only |
| **PubLayNet** | Document layout analysis (text, title, list, table, figure) | ~360K document images from PubMed | Object detection metrics (mAP) | Academic papers only |
| **DocLayNet** (IBM) | Document layout analysis across domains | ~80K pages: financial, scientific, legal, manual, patent, government | Object detection metrics | Layout detection only, not text extraction quality |

### 3.2 olmOCR-Bench results snapshot (mid-2026)

These numbers are **illustrative** — a mix of AI2's published olmOCR-Bench results, tool authors'
own reports, and practitioner reports, rounded into ranges. Several rows (the general-purpose VLMs,
Unstructured, PyMuPDF, pypdf) are not official leaderboard entries at all. The score is a
unit-test pass rate, not edit similarity. **Verify against the primary source before making a
decision** — `02` §3.3's caution applies.

| Parser | Overall score (illustrative) | Speed (pages/sec) | Notes |
|---|---|---|---|
| GPT-4.1 vision | not an official entry; practitioner estimates vary widely | 0.2–0.5 | ~$0.01/page at list prices (§2.1); non-deterministic |
| Gemini 2.5 Flash | not an official entry; practitioner estimates vary widely | 0.3–0.8 | Cheaper VLM option (~$0.003/page, §2.1) |
| olmOCR 2 | ~82% (AI2-reported, on AI2's own benchmark) | 0.3–0.5 (GPU) | Fine-tuned VLM built for this task; Apache 2.0 |
| Marker v2 | ~76% (Datalab-reported) | 2.5–3.0 (GPU) | Among the best layout-model pipelines; GPL-3.0 |
| MinerU | ~73% | 0.5–0.8 (GPU) | Strong on formulas; AGPL |
| Docling | ~50% | 1.5–3.0 (GPU) | Widest format support; MIT; structured output |
| Unstructured (`hi_res`) | ~45–55% | 1.0–2.0 | Accuracy varies heavily by document type |
| PyMuPDF | ~35–40% | 100–200 | Fast geometric extraction; fails on complex layouts |
| pypdf | ~30–35% | 80–150 | Simplest; worst on anything non-trivial |

**Treat the rough ordering as the signal, not the exact numbers.** Task-tuned VLM parsers tend to
score several points above the best layout-model pipelines, and layout models score far above
geometric extractors on hard pages. Note that general-purpose VLMs do not automatically win: in
AI2's published comparisons, general models such as GPT-4o scored below task-tuned OCR models.
Whether either gap matters depends
entirely on your corpus — a born-digital, single-column corpus will see little difference between
tiers, and a multi-column scanned corpus will see the full gap.

### 3.3 What benchmarks don't capture

Five things matter in production that benchmarks consistently miss:

1. **Table structure fidelity** — text-similarity metrics treat tables as text, and a handful of
   table unit tests cannot cover every table shape, so a table with correct text but destroyed
   column associations can still score well. The parser that scores 75% overall may score
   (illustratively) 90% on prose and 30% on tables, and it's the 30% that determines whether your financial RAG
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

> **In plain words.** Four popular parsers, each with a short "pick it when / avoid it when". The big differences are file types supported, accuracy on hard layouts, speed, and license.
>
> **Real-world example.** A startup that must ship a closed-source product avoids Marker (GPL) and PyMuPDF (AGPL) unless they buy a license, and starts with Docling (MIT).

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
GPU:     recommended for hi_res strategy (runs on CPU, but slowly)
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

> **In plain words.** Find your main constraint in the left column and read across: first choice, backup, what to avoid. Then test the first two on your own worst documents.
>
> **Real-world example.** A hospital needs self-hosting and an Apache/MIT license: the row points to Docling, then Unstructured. They run both on 50 scanned discharge letters and keep the one that gets the tables right.

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

- **Marker scores ~26 points above Docling in the §3.2 snapshot (~76% vs ~50%) and still
  interleaves a two-column page that Docling reads correctly.** An aggregate score barely moves
  when one page's column *order* is wrong, because every token is still present and only a few
  tests check reading order.
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
| **Budget ≈ $0** | Docling (MIT) or PyMuPDF for simple docs | Unstructured `fast` | Cloud APIs |
| **Academic / STEM** | Marker v2 or Mathpix (API) | Nougat (if non-commercial OK) | Generic geometric extractors |
| **Enterprise, regulated** | Azure Document Intelligence | Amazon Textract | Self-hosted without audit trail |

---

## 6. Chunking strategies

> **In plain words.** Chunking cuts documents into pieces for search. Simple cutting by size and paragraph works surprisingly well; cutting at real headings works better when the parser keeps the headings.
>
> **Real-world example.** A 30-page HR policy cut every 512 tokens gives ~40 chunks; one chunk may start mid-sentence. Cut at headings instead, and the "Parental leave" section stays in one piece.

`02` §5–§6 covers the theory. This section gives you current benchmark numbers and tool mappings.

### 6.1 Strategy comparison

The accuracy column mixes numbers from different studies, corpora and metrics. Read it as "what
someone once measured", not as a ranking — only a comparison run on one corpus can rank strategies.

| Strategy | How it works | Retrieval accuracy (benchmarks) | Best for | Worst for |
|---|---|---|---|---|
| **Fixed-size** | Split at N tokens with M overlap | Baseline; no benchmark consistently measures against it | Uniform text (novels, transcripts) | Structured documents where boundaries carry meaning |
| **Recursive character** | Split on `\n\n`, then `\n`, then `. `, then ` `, at max chunk size | ~69% in one widely shared comparison; strong default | General-purpose; mixed corpora | Documents where paragraph breaks don't align with topics |
| **Sentence-based** | Split on sentence boundaries (spaCy, NLTK, regex) | ~65% | Preserving sentence integrity | Languages where sentence detection is unreliable |
| **Semantic** | Embed consecutive segments, split where cosine similarity drops | ~54% in published comparisons (counter-intuitively lower than recursive) | Topically diverse documents with clear topic shifts | Uniform text; adds embedding cost and latency at ingest |
| **Document-structure-aware** | Split on heading boundaries from the parser's document model | ~87% in one clinical-domain study (different corpus; not comparable to the other rows) | Structured documents (manuals, specs, legal); requires a parser that emits structure | Unstructured text; dependent on parser quality |
| **Parent-document** | Index small chunks, retrieve, then expand to parent context for generation | +5–15% over flat chunking in practitioner reports | Table-heavy, context-dependent passages | Simple Q&A where expansion adds noise |
| **Late chunking** (Jina) | Run the model over the full document, then average the token vectors inside each chunk's span; each chunk vector carries full-document context | Emerging; limited independent benchmarks | Long documents where local chunks lack context | Short documents; needs a long-context model whose per-token outputs you can pool (e.g., Jina v3) — most embedding APIs don't expose that |
| **Agentic chunking** | LLM reads the document and decides chunk boundaries | No independent benchmark; cost is one LLM pass over the corpus, far above other methods | High-value, low-volume corpora where chunk quality justifies LLM cost | Cost-sensitive; high-volume |

### 6.2 The recursive-vs-semantic surprise

One widely shared chunking comparison (early 2026; we could not trace it to a peer-reviewed
source, so treat the numbers as practitioner-reported) found recursive character splitting at 512
tokens outperformed semantic chunking by 15 points (69% vs 54%). This is counter-intuitive — a smarter
strategy should win — but it makes sense once you consider the failure mode:

Semantic chunking splits where embedding similarity drops. But embedding similarity is a noisy
signal at the local level — a pronoun referring to the previous paragraph has low similarity to the
next topic, but it's not a good split point because the pronoun is now orphaned. Recursive
splitting's dumb-but-consistent boundaries produce chunks of predictable size that the embedding
model was benchmarked on, and predictability wins over cleverness when the cleverness introduces
variance.

The document-structure-aware strategy tends to beat both — the 87% figure comes from a different
study on clinical documents, so it is suggestive, not a head-to-head result — because it uses
*structural* signals —
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

> **In plain words.** These are the libraries that do the cutting. Most teams start with LangChain's recursive splitter and move to a structure-aware chunker when their parser outputs headings.
>
> **Real-world example.** A team uses `RecursiveCharacterTextSplitter(chunk_size=512)` and gets 512-*character* chunks (~120 tokens), four times smaller than planned. `from_tiktoken_encoder` fixes it.

| Tool | Strategies available | Ecosystem | Differentiator |
|---|---|---|---|
| **LangChain text splitters** | Recursive character, HTML, Markdown, code (by language), token-based, sentence | LangChain native; most tutorials use it | Widest adoption; `RecursiveCharacterTextSplitter` is probably the most widely used chunker |
| **LlamaIndex node parsers** | Sentence, token, semantic, hierarchical, markdown, code, JSON | LlamaIndex native | `SentenceSplitter` is battle-tested; hierarchical parser supports parent-document natively |
| **Chonkie** | Token, word, sentence, semantic, SDPM, late, neural | Standalone (small install); no framework dependency | Purpose-built for chunking; vendor-reported size and throughput figures are high, verify on your data; many strategies in one library; supports `tokenizers`, `tiktoken`, `autotiktokenizer` |
| **Unstructured chunkers** | By-title (structure-aware), by-page, by-similarity, basic | Unstructured native | Operates on typed `Element` objects from Unstructured's partition; structure-aware by default |
| **Docling chunker** | Hierarchical (follows `DoclingDocument` structure) | Docling native | Chunking preserves the document model's tree structure; metadata (heading path, page, bbox) propagated automatically |
| **Semantic Chunker** (various) | Embed-then-split based on similarity drops | Standalone or embedded in LangChain/LlamaIndex | The canonical semantic chunking implementation; Greg Kamradt's original + derivatives |

**Recommendation:** start with `RecursiveCharacterTextSplitter` at 512 tokens / 50-token overlap
as a baseline. (Its default `chunk_size` counts *characters*; build it with
`RecursiveCharacterTextSplitter.from_tiktoken_encoder(...)` if you mean tokens.) Switch to structure-aware chunking (Docling chunker or Unstructured `by_title`) if
your parser emits structure. Evaluate with your golden set before adopting semantic or late chunking
— the complexity cost is real and the accuracy benefit is not guaranteed.

---

## 8. Embedding models — the current field

> **In plain words.** An embedding model turns text into numbers so similar meanings sit close together. Top models score within a few points of each other; price, license, language and input length often matter more.
>
> **Real-world example.** Embedding 12M tokens (20,000 pages) costs ~$0.24 at $0.02/1M or ~$1.56 at $0.13/1M. The price gap is small; the difference between a 512-token and an 8K-token limit can matter much more.

`01` covers the theory and the schema-decision argument. This section gives you the current
leaderboard and a decision matrix.

### 8.1 Top models (MTEB, mid-2026)

The "MTEB avg" column is approximate and **mixes leaderboards**: some numbers are from the
multilingual MTEB, some from the older English MTEB, and most are provider-reported. Use it to
group models into "top tier / good / budget", not to rank neighbours. Prices are list prices at the
time of writing.

| Model | Provider | MTEB avg | Dimensions | Max tokens | Pricing (per 1M tokens) | Key properties |
|---|---|---|---|---|---|---|
| **Qwen3-Embedding-8B** | Alibaba (open-weight) | ~70.6 (multilingual MTEB, provider-reported) | 4096 (MRL: 32–4096) | 32K | Self-hosted | Top of the multilingual MTEB at release (open-weight); MRL support; Apache 2.0 |
| **Cohere embed-v4** | Cohere | ~69 (approx.) | 1536 (MRL: 256–1536) | 128K | ~$0.12 (text) | Multimodal (text + images); 128K context; 100+ languages; binary quantization native |
| **Voyage AI voyage-3-large** | Voyage AI | ~68 (approx.) | 1024 (MRL: 256–2048) | 32K | $0.18 | Strong on code and technical text; instruction-tuned |
| **text-embedding-3-large** | OpenAI | ~64.6 (English MTEB, OpenAI-reported) | 3072 (MRL: shortenable) | 8K | $0.13 | MRL support; widely deployed; predictable |
| **Jina Embeddings v3** | Jina AI | ~65 (English MTEB, approx.) | 1024 (MRL: 32–1024) | 8K | ~$0.02 (approx.) | Strong cost/quality ratio; weights are CC-BY-NC (API or commercial license for business use); late chunking support; task-specific LoRA adapters |
| **text-embedding-3-small** | OpenAI | ~62.3 (English MTEB, OpenAI-reported) | 1536 (MRL: shortenable) | 8K | $0.02 | Good budget option |
| **BGE-M3** | BAAI (open-weight) | ~64 (approx.) | 1024 | 8K | Self-hosted | Dense + sparse + ColBERT in one model; strong multilingual; MIT license |
| **NV-Embed-v2** | NVIDIA (open-weight) | ~72 (English MTEB, NVIDIA-reported) | 4096 | 32K | Self-hosted | Strong on retrieval tasks specifically; large dimensions; **CC-BY-NC — non-commercial only** |
| **Nomic Embed v2** | Nomic (open-weight) | ~63 (approx.) | 768 (MRL: 256–768) | 512 | API or self-hosted | Apache 2.0; MoE, multilingual; MRL; short 512-token input limit |
| **GTE-Qwen2-7B** | Alibaba (open-weight) | ~70 (English MTEB, provider-reported) | 3584 | 32K | Self-hosted | Strong multilingual; long context; Apache 2.0 |

### 8.2 Embedding decision matrix

| Constraint | First choice | Second choice | Notes |
|---|---|---|---|
| **Max quality, API** | Cohere embed-v4 | Voyage-3-large | Cohere wins on multilingual + multimodal; Voyage on code |
| **Max quality, self-hosted** | Qwen3-Embedding-8B | NV-Embed-v2 | Both need GPU; Qwen has MRL; NV-Embed-v2's license is non-commercial — use GTE-Qwen2-7B instead for a commercial product |
| **Best cost/quality API** | Jina v3 ($0.02/1M) | text-embedding-3-small ($0.02/1M) | Similar price; Jina tends to score higher on public benchmarks — check on your data; late chunking is a bonus |
| **Multilingual** | Cohere embed-v4 | BGE-M3 | Cohere: 100+ languages; BGE-M3: open-weight, dense+sparse |
| **Code / technical** | Voyage-3-large | Qwen3-Embedding-8B | Voyage is specifically strong on code retrieval |
| **Multimodal (text + images)** | Cohere embed-v4 | voyage-multimodal-3 | Several providers now offer multimodal embeddings; compare on your own image-plus-text queries |
| **Hybrid search (single model)** | BGE-M3 | — | Emits dense, sparse, and ColBERT vectors from one forward pass |
| **Maximum context** | Cohere embed-v4 (128K) | Qwen3-Embedding-8B (32K) | Long context ≠ good retrieval on long inputs — measure it |
| **Budget zero** | BGE-M3 (MIT) | Nomic Embed v2 (Apache 2.0) | Both self-hostable on modest GPU; Nomic v2 takes only 512 tokens per input |

### 8.3 The open-weight quality gap has closed

The headline from the 2025–2026 MTEB cycle: open-weight models (Qwen3-Embedding, GTE-Qwen2,
BGE-M3, NV-Embed) now match or exceed proprietary API models on aggregate benchmarks. The
remaining advantage of API models is operational — no GPU infrastructure, no model serving, no
version management — not quality. If you have GPU capacity and the engineering to serve a model,
the quality argument for an API is gone.

The cost argument is less clear. At ~$0.02/1M tokens, a cheap API is hard to beat unless you keep
a GPU busy most of the time (see `appendix-e` §9.3 for the break-even arithmetic). The break-even depends
on your embedding volume, your GPU cost, and whether your team can keep a model server running —
the same calculation as any build-vs-buy decision.

---

## 9. Vector stores and retrieval infrastructure

> **In plain words.** A vector store keeps the embeddings and finds the nearest ones fast. If you already run Postgres and have a few million vectors, pgvector is usually enough; dedicated stores help at larger scale or with heavy hybrid search.
>
> **Real-world example.** An intranet with 2M chunks runs fine on pgvector in the existing database. A marketplace with 500M product vectors needs a store built for that scale (Milvus, Vespa).

`03` covers index internals (HNSW parameters, quantization, filtered search). This section covers
the *products* you build on top of those internals.

### 9.1 Current landscape

| Store | Type | Hybrid search | Key strength | Operational model | License |
|---|---|---|---|---|---|
| **pgvector / pgvecto.rs** (pgvecto.rs is now superseded by VectorChord) | Extension on PostgreSQL | BM25 via `pg_search` or application-side | Single-database simplicity; SQL joins across vectors and metadata; ACID | Self-hosted or managed Postgres (Supabase, Neon, RDS) | PostgreSQL / Apache 2.0 |
| **Qdrant** | Purpose-built vector DB | Native sparse vectors + dense; built-in RRF | Low P99 latency in Qdrant's own published benchmarks (vendor-reported); Rust performance; rich filtering | Self-hosted or Qdrant Cloud | Apache 2.0 |
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
| **Already have Postgres** | pgvector | — | Under ~5M vectors (rule of thumb), pgvector eliminates an entire service; `03` §4 and §2.6 cover the HNSW parameters, `03` §10 compares pgvector with a dedicated store |
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
Practitioner reports from 2024–2026 commonly show gains in the range of +5–15% recall from adding
a BM25 branch with RRF fusion, and another +3–8% from adding a cross-encoder reranker (reported
ranges, not a controlled study; your gain depends on your queries). The cost
of the BM25 branch is near-zero (it's a text index), and the reranker cost is proportional to
top-k, not corpus size.

If you are running dense-only retrieval and haven't tested hybrid, that is the single
highest-ROI change available to you before touching the parser or the embedding model.

---

## 10. Rerankers

> **In plain words.** A reranker takes the top 20–100 search results and re-sorts them with a slower, more careful model. It only sees a few passages, so it is cheap for the quality it adds.
>
> **Real-world example.** At Cohere's $2 per 1,000 searches, 1M queries a month cost ~$2,000; if a single generated answer costs ~$0.01, generation for the same 1M queries is ~$10,000.

Latencies are illustrative (they depend on passage length, hardware and network). Prices are
list prices at the time of writing; some vendors charge per search, others per token.

| Reranker | Type | Latency (100 passages) | Quality | Cost |
|---|---|---|---|---|
| **Cohere Rerank v3.5** | API (cross-encoder) | ~200ms | Strong on general-domain (vendor-reported); multilingual | $2/1K searches (a search = 1 query + up to 100 passages) |
| **Jina Reranker v2** | API (cross-encoder) | ~150ms | Strong; cost-effective | Per token (Jina API token pricing) |
| **Voyage Rerank 2** | API (cross-encoder) | ~180ms | Strong on code/technical | ~$0.05 per 1M tokens ≈ $1.50/1K searches at 100 passages × 300 tokens |
| **BGE-Reranker-v2.5-gemma2** | Open-weight | ~300ms (GPU) | Near-API quality; Gemma license terms (not Apache) | Self-hosted GPU |
| **cross-encoder/ms-marco-MiniLM-L-12-v2** | Open-weight | ~100ms (GPU) | Good baseline; fast | Self-hosted; CPU-viable |
| **ColBERT v2** | Late interaction | ~50ms | Different paradigm; per-token matching | Self-hosted |
| **FlashRank** | Open-weight (small) | ~50ms (CPU) | Fastest; lowest quality of the group | Self-hosted; CPU-only |

**Recommendation:** start with Cohere Rerank or Jina Reranker for quality; move to
BGE-Reranker-v2.5 if you need self-hosted at comparable quality. The reranker is applied to
top-k only (typically 20–100 passages), so even API pricing (about $0.002 per search at Cohere's
$2/1K) is usually small next to generation costs.

---

## 11. End-to-end RAG evaluation frameworks

> **In plain words.** These tools score your RAG system: did search find the right text, and did the answer stick to it. You need both scores, because a model can ignore correct text.
>
> **Real-world example.** A support bot finds the right refund article 95% of the time, yet 1 answer in 10 invents a refund period. Only a faithfulness metric catches that.

`08` and `09` will cover evaluation methodology and infrastructure in depth. This section is a
tool-selection aid.

| Framework | Approach | Key metrics | Integration | Best for |
|---|---|---|---|---|
| **RAGAS** | Reference-free LLM-as-judge; modular metrics | Faithfulness, answer relevancy, context precision, context recall, answer correctness | LangChain, LlamaIndex, standalone | Prototyping and rapid iteration; faithfulness and answer relevancy need no ground truth (context recall and answer correctness do); widely used |
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

Practitioner analyses from 2025–2026 report (numbers vary by study and are not from a single
controlled benchmark) that **generation failures account for roughly 28–42% of hallucinations** — cases where the correct context was retrieved but the model ignored
it, misread it, or confabulated beyond it. This means even a perfect retrieval system inherits a
hallucination floor from the generation model. Evaluation frameworks that measure only retrieval
metrics miss this entirely, which is why faithfulness (a generation metric) belongs in every eval
suite alongside recall (a retrieval metric).

---

## 12. Recommended stacks by use case

> **In plain words.** Four ready-made starting stacks: high accuracy, low cost, many languages, and code. Each lists the part most likely to be wrong for you.
>
> **Real-world example.** A 5-person startup takes the cost-effective stack: Docling, 512-token chunks, a $0.02/1M embedding, pgvector, a free CPU reranker, and RAGAS. Nearly all of the bill is LLM generation.

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
| **Embeddings** | Jina v3 (~$0.02/1M tokens) or Nomic Embed v2 (self-hosted, free; 512-token inputs) | Jina for API simplicity; Nomic for zero marginal cost |
| **Vector store** | pgvector (if you have Postgres) or Chroma (prototyping) | No additional infrastructure |
| **Reranker** | Jina Reranker v2 (token-priced API) or FlashRank (free, CPU) | Jina for quality; FlashRank for zero cost |
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
| **Reranker** | Cohere Rerank v3.5 (multilingual) | Few rerankers handle non-English well; Cohere is a strong option (test on your languages) |
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
   (20–100 passages per query). At roughly $1–2 per 1K searches on common APIs (or free self-hosted), it is among the cheapest components
   in the pipeline relative to its impact. The cost of *not* reranking is answered in
   irrelevant passages consuming your context window and your generation budget.

8. **Evaluating retrieval without evaluating generation.** Practitioner reports put roughly 28–42% of
   hallucinations on generation failures, not retrieval failures. An eval suite that measures only
   recall@k will miss up to about 40% of the problem. Always include faithfulness alongside retrieval metrics.

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

- **The open-weight embedding gap closed.** Qwen3-Embedding and GTE-Qwen2 match API models on
  benchmarks. The remaining API advantage is operational, not quality.

- **Generation hallucinates even with perfect retrieval.** Measure faithfulness, not just recall.
  The eval suite that only checks retrieval will miss roughly 28–42% of the problem (practitioner
  estimate).

- **Every number in this appendix is a snapshot.** The landscape moves quarterly. The mental
  models move slowly. Trust the models, verify the numbers.

---

## 15. How to use these benchmarks in an interview

> **In plain words.** Interviewers do not want you to recite leaderboard scores. They want to hear
> that you know scores are hints, that you would shortlist from them and then test on real data,
> and that you check license, cost and speed too.
>
> **Real-world example.** "Which PDF parser would you use?" → "Depends on the documents. For
> born-digital single-column text, PyMuPDF is 100 pages/sec and free. For tables and columns I'd
> shortlist Docling and Marker, check the license, and run both on our 50 worst pages before
> choosing."

### 15.1 Conceptual questions

**Q1. "A parser scores 76% on olmOCR-Bench and another 50%. Which do you pick?"**
*Sections: §3, §5.* Neither yet. Say what the score measures (a pass rate on small checks, on
someone else's pages), that many scores are self-reported, and that a high score can hide your
failure mode (§5: the higher-scoring parser interleaved a two-column page). Shortlist both, test
on your own worst documents, and add license and throughput to the decision.

**Q2. "Why might recursive chunking beat semantic chunking?"**
*Sections: §6.2.* Semantic splits follow a noisy similarity signal and create uneven chunks;
recursive splits are predictable and close to what embedding models were tuned on. Add that the
published numbers come from different studies, so you would confirm on your own eval set.

**Q3. "Does a higher MTEB score mean better retrieval for us?"**
*Sections: §8.* Not necessarily. MTEB averages many tasks and languages; the columns in public
tables often mix leaderboard versions. Check the retrieval sub-score, your language, max input
length and license, then run your own golden set.

**Q4. "Where should a team spend first to improve answers?"**
*Sections: §3.3, §9.3, §11.2, §13.* Check the parse output first (the parser is the ceiling),
then add hybrid search and a reranker (cheap, usually a clear win), and measure faithfulness, since
a share of hallucinations happen even with correct retrieval.

### 15.2 System design prompt

**"Design the ingestion and retrieval stack for 1M pages of mixed PDFs (some scanned) for an
internal search assistant."**

1. **Clarify:** file types, share of scans and tables, languages, license limits, budget, how often
   documents change.
2. **Shortlist from this appendix:** a tier-1 parser for simple born-digital pages, a tier-2 parser
   for complex ones, OCR or a VLM only for scans (§2, §5). Route per document.
3. **Test:** 100 hardest pages, known answers, measure per failure mode (columns, tables, headers).
4. **Chunk:** structure-aware if the parser emits headings, otherwise recursive at ~512 tokens.
5. **Embed and store:** a model that fits language, license and input length; pgvector if under a
   few million vectors and Postgres exists, else a dedicated store (§9.2).
6. **Retrieve:** hybrid BM25 + vectors with RRF, rerank the top 50 (§9.3, §10).
7. **Evaluate:** recall@k and faithfulness in CI (§11).

*What interviewers listen for:* per-document routing instead of one parser for everything; cost
arithmetic (for example, 1M pages × $0.011 ≈ $11,000 for an all-VLM parse vs ~$1,500 for OCR at
$1.50/1K pages); licenses; and a test on real documents before the decision.

### 15.3 Rapid-fire

| Question | Strong answer | Section |
|---|---|---|
| What does olmOCR-Bench measure? | % of small pass/fail checks passed on ~1,400 PDFs | §3.1 |
| Fastest open-source PDF text extractor? | PyMuPDF / pymupdf4llm, ~100+ pages/sec, but no layout model | §4.4 |
| Why worry about Marker's license? | GPL-3.0; closed-source commercial use needs a paid license | §4.2, §13 |
| Why is a VLM parser hard for incremental updates? | Output changes between runs, so chunk IDs change | §3.3 |
| Default chunk size? | ~512 tokens with ~10% overlap, then measure | §6.3, §7 |
| Is NV-Embed-v2 OK for a commercial product? | No, its weights are non-commercial (CC-BY-NC) | §8.1 |
| When is pgvector enough? | You run Postgres and have up to a few million vectors | §9.2 |
| Cheapest big quality win in retrieval? | Hybrid BM25 + vectors, then a reranker | §9.3 |
| What does a reranker cost per query at $2/1K? | $0.002 | §10 |
| Retrieval is great but answers still wrong? | Measure faithfulness; the generator may ignore the text | §11.2 |

### 15.4 Debugging prompt

**Symptoms:** after switching from PyMuPDF to a parser that scores ~26 points higher on a
leaderboard, answer accuracy on the finance bot *drops* from 81% to 74% on the golden set; failures
cluster on two-column annual reports. **Diagnosis:** compare the two parses of a failing page side
by side; the new parser interleaves the columns. The benchmark rewarded token presence, not your
reading order. **Fix:** route two-column documents to the parser that keeps order, add a reading-order
check to the ingestion gates (`02` §3.2), re-run the golden set.

### 15.5 Common mistakes

- Quoting a leaderboard number as a fact without saying who measured it.
- Comparing numbers from different benchmarks or different MTEB versions.
- Forgetting licenses (GPL/AGPL/non-commercial weights).
- Counting characters when you meant tokens in chunk sizes.
- Measuring only retrieval, not faithfulness.

---

## 16. Real-world cases — incidents with numbers

These are **composite scenarios** built from failure modes this chapter describes; numbers are
illustrative but internally consistent.

Quick index: leaderboard winner made answers worse → 16.1 · chunks far smaller than planned →
16.2 · license blocked launch → 16.3 · VLM parse broke incremental updates → 16.4.

### 16.1 The leaderboard winner that mixed up columns

- **Setup:** a finance Q&A bot over 8,000 pages of annual reports, 60% two-column.
- **Symptom:** after a parser upgrade chosen from the §3.2 table, golden-set accuracy fell from 81%
  to 74%.
- **Measurement/Diagnosis:** of the 200 golden questions, the new setup got 14 fewer right
  (162 → 148); all 14 came from two-column pages where left and right columns were interleaved.
- **Fix:** route two-column pages to the parser that kept reading order; add a reading-order spot
  check. Accuracy rose to 85% (170/200).
- **Lesson:** a leaderboard score is an average over someone else's pages; test on your worst ones.

### 16.2 The 512 that was characters, not tokens

- **Setup:** a support desk indexes 3,000 help articles with `RecursiveCharacterTextSplitter(chunk_size=512)`.
- **Symptom:** answers lack context; the bot often says "see the steps below" with no steps.
- **Measurement/Diagnosis:** average chunk length was ~120 tokens, not 512; the index held ~41,000
  chunks instead of the planned ~10,000. Recall@5 on the golden set: 0.62.
- **Fix:** rebuilt with `from_tiktoken_encoder(chunk_size=512, chunk_overlap=50)`: ~10,500 chunks,
  recall@5 up to 0.78.
- **Lesson:** check what unit your chunker counts in; the default is characters.

### 16.3 The license that blocked the launch

- **Setup:** a SaaS vendor builds a document-search feature on Marker (GPL-3.0) and NV-Embed-v2
  (CC-BY-NC) because both ranked high in the tables.
- **Symptom:** the legal review before launch rejects both.
- **Measurement/Diagnosis:** 6 weeks of work depended on two components that could not ship in a
  closed-source commercial product without a license.
- **Fix:** swapped to Docling (MIT) and GTE-Qwen2-7B (Apache 2.0); re-ran the golden set:
  accuracy 0.80 → 0.77, within the team's accepted 5-point margin. Launch slipped 2 weeks.
- **Lesson:** check the license column on day one, not in week six.

### 16.4 The VLM parser that re-embedded everything

- **Setup:** 50,000 pages, 1% change per night, parsed with a VLM for top accuracy. The nightly
  job re-parses every page and re-embeds chunks whose text hash changed (~2 chunks per page).
- **Symptom:** each nightly update re-embedded far more than 500 pages; embedding bills and index
  churn jumped.
- **Measurement/Diagnosis:** re-parsing the same unchanged page gave slightly different text about
  15% of the time (~7,500 pages), so chunk hashes changed (§3.3, `02` §9). Nightly re-embeds: ~15,000 chunks
  instead of ~1,000.
- **Fix:** parse a page only when the source file's hash changes and cache the parse output. Nightly
  re-embeds dropped to ~1,000 chunks.
- **Lesson:** a non-deterministic parser needs identity based on the source file, not the parsed text.
