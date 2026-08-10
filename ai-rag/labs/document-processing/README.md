# Lab: document processing (`02-chunking-and-document-processing.md`)

A production-shaped ingestion pipeline — acquire, parse, normalize, split, enrich,
identify, dedup — run over a corpus of **20 fixtures built to contain one known failure
each**: two-column PDFs, a scanned PDF with no text layer, a PDF whose font ships no
glyph map, tables, a spreadsheet, source code, an email thread, a book, and a document
full of `x²`, `Ⅻ`, `½`, `US` vs `us`.

Then the same fixtures through **the tooling you probably already run** — LangChain,
LlamaIndex, PyMuPDF, pypdf, pdfminer.six, unstructured, semchunk, chonkie — to see
what each one does with them and where each one quietly loses something.

**Status: rung 2 — implemented.** The code runs and 28 assertions pass. It produces
*chunks, counts and routing decisions*, not retrieval quality: no embedding model,
vector index or generator is involved. Chapter `02` §15's labs 1, 3 and 9 are executed
here; labs 2 and 4–8 need a golden set and a model, and are out of scope by design
(see §7).

```
python3 make_fixtures.py     # regenerate corpus/ (deterministic; already committed)
python3 run.py               # the whole report, ten acts, zero dependencies
python3 run.py --list        # act names
python3 run.py parse tables  # just those acts
python3 test_pipeline.py     # 28 assertions, zero dependencies

uv venv .venv && uv pip install -r requirements-bakeoff.txt
.venv/bin/python bakeoff.py            # the same fixtures through real libraries
.venv/bin/python bakeoff.py --list     # which adapters are installed
```

---

## Contents

1. [What this is](#1-what-this-is)
2. [The corpus, and what each fixture is for](#2-the-corpus-and-what-each-fixture-is-for)
3. [The pipeline](#3-the-pipeline)
4. [What the run actually shows](#4-what-the-run-actually-shows)
5. [The bake-off: what production tooling misses](#5-the-bake-off-what-production-tooling-misses)
6. [Build log — the bugs, and what each one taught](#6-build-log--the-bugs-and-what-each-one-taught)
7. [What this deliberately is not](#7-what-this-deliberately-is-not)
8. [Where to go next](#8-where-to-go-next)

---

## 1. What this is

Chapter `02`'s thesis is an ordering claim: **parsing sets the ceiling, chunking
decides how much of it you reach, and the embedding model only decides how well you
exploit what is left.** That is easy to agree with and hard to feel, because in a real
pipeline every stage is somebody else's library and the failures are silent.

So this lab makes the failures happen on real bytes, in a place where the right answer
is known:

- `pdfmini.py` **writes** the PDFs and **extracts** them from scratch. There is no
  `pypdf` in the core pipeline. Writing both halves is what makes "a PDF contains glyph
  runs at coordinates, not paragraphs" stop being a sentence you nod at.
- Every fixture is built to contain a specific defect, so a gate that fails to fire is
  a bug in the gate rather than an ambiguity about the document. On a real corpus you
  cannot tell those apart, which is why real corpora are bad teaching material and
  excellent production material.
- Everything is deterministic — no timestamps, no unseeded randomness, sorted iteration
  — because §9's content-addressed identity scheme is meaningless over a corpus that
  drifts between runs.

| File | Role | Chapter |
|---|---|---|
| `pdfmini.py` | PDF writer + from-scratch tier-1 extractor, naive vs column-aware reading order | §3.2, §3.3 |
| `make_fixtures.py` | Generates all 20 fixtures deterministically | — |
| `parse.py` | Per-format parsers emitting typed elements; extraction-yield, script-sanity and glyph-leakage gates | §3 |
| `normalize.py` | Canonical text + offsets; NFKC and case-folding damage reports | §4 |
| `tables.py` | Grid recovery from glyph runs; three serializations; header repetition | §3.4 |
| `split.py` | fixed / recursive / structural / parent-child; token estimator | §5, §6, §7 |
| `identity.py` | Content vs position addressing; diff-based update | §9 |
| `dedup.py` | Exact hash, MinHash + LSH, repeated-block chrome detection | §10, §3.5 |
| `pipeline.py` | Stage wiring, the `ChunkRecord` schema, version stamps, cost metrics | §2, §8, §12 |
| `run.py` | The report — ten acts | all |
| `bakeoff.py` | The same fixtures through real libraries | §3.3, §6.2 |
| `test_pipeline.py` | 28 assertions | — |

---

## 2. The corpus, and what each fixture is for

| Fixture | Class | The failure it contains |
|---|---|---|
| `transcript.txt` | pure text | No structure at all. Recursive splitting is the floor, not a choice. |
| `handbook.md` | markdown | The control: nothing is lost. Failures here are never parsing failures. |
| `metrics.md` | markdown + table | A 4-row table that every general-purpose splitter cuts through. |
| `notation.md` | notation hazards | `x²`, `½`, `Ⅻ`, `µ`, full-width forms, `US`/`us`, `Polish`/`polish`. |
| `site/*.html` × 5 | html + chrome | Nav, cookie banner, related rail and footer — 34–38% of each page. |
| `report_clean.pdf` | PDF, 1 column | Tier 1 works. Paragraph breaks are still lost. |
| `report_twocol.pdf` | PDF, 2 column | Column interleaving, running head *and* page-numbered footer, `organi-\nzational`, ligatures. |
| `report_twocol_interleaved.pdf` | PDF, 2 column | Same page, emitted row-band by row-band. The version real producers make. |
| `statement.pdf` | PDF table | A 6×5 grid drawn as positioned glyph runs. No table object exists. |
| `scan.pdf` | PDF, image only | Extracts to `""`. Zero chunks, zero errors. |
| `subset_broken.pdf` | PDF, no ToUnicode | 129 characters of plausible-length garbage. |
| `subset_ok.pdf` | PDF, control | Byte-identical layout, resolvable glyph names. The A/B partner. |
| `revenue.csv` | spreadsheet | A relation. Vector search is the wrong tool. |
| `service.py.txt` | source code | Two functions and a class; text splitters cut through all of them. |
| `thread.eml` | email thread | 66% of the file is quoted repetition. |
| `book.md` | book | Four parts, ten chapters, thirty sections, footnotes, tables. |

The prose is written for the lab; the **bytes** are real. `report_twocol.pdf` is a
valid PDF a viewer renders, and its text is destroyed by naive extraction for exactly
the reason real PDFs' text is destroyed. `book.md`'s body is composed from a fixed
sentence pool by a seeded generator — structurally realistic, semantically thin, which
costs nothing because nothing here measures answer quality.

---

## 3. The pipeline

```
Acquire → Parse → Normalize → Split → Enrich → (Embed) → (Index)
```

Three commitments are implemented rather than described.

**Parsers emit typed elements, not strings.** A splitter handed `str` can only split on
characters. A splitter handed `[Element(kind="heading", level=2), Element(kind="table",
…)]` can split on boundaries the author drew, refuse to cut a table or a function in
half, and repeat a table header into every piece. **The chunking strategies available
downstream are decided by what the parser chose to emit** — §1's ceiling chain
expressed as an API.

**One canonical form; both branches derived from it** (§4.3):

```
canonical_text     → stored, cited, shown to users, hashed for identity
      ├── embed_text    = canonical_text + heading path
      └── lexical_text  = analyze(canonical_text)   # lowercase, fold — BM25's business
```

`normalize.analyze()` is never stored. It cannot become the thing you embed, which is
the silent hybrid-search regression §4.3 warns about.

**Every chunk is a span, never a string.** `chunk.text == canonical[span.start:span.end]`
is asserted for all four strategies across the whole corpus. That is what makes
citation-with-highlighting possible, and it is what lets the sibling
[`golden-set`](../golden-set/) lab score any of these chunkings against the same span
labels without re-labelling.

`ChunkRecord` is §8.1's metadata table as a schema, with `assert_no_leakage()`
enforcing §8.2: `tenant_id`, `acl`, `created_at` and `chunk_id` are payload and must
never appear in `embed_text`. Prepending them *feels* like it works, because retrieval
still returns results.

---

## 4. What the run actually shows

Numbers below are from this corpus — 20 synthetic fixtures, ~75 KB. They demonstrate
mechanisms. They do not choose a chunk size for you; §11.6 is explicit that the answer
depends on your corpus *and* your query distribution, and this lab has neither.

**Gates (§3.2).** Two documents are quarantined and 18 index:

```
scan.pdf           0 chars/page          → ocr
subset_broken.pdf  script sanity 0.17    → review
subset_ok.pdf      script sanity 1.00    → index      ← same layout, same length
```

Without the gates both broken files produce zero chunks and **no error anywhere**.

**Reading order (§3.2).** Same bytes, two policies:

```
NAIVE   | Reading order is not stored in the Margin requirements for the retail
        | file. A content stream places glyph portfolio were revised in February
COLUMNS | Reading order is not stored in the
        | file. A content stream places glyph
```

Every sentence in the naive version alternates between two unrelated topics. No chunk
boundary and no embedding model repairs that.

**Normalization (§4.2)** — the report on `notation.md`:

- 19 distinct characters would be rewritten by NFKC. **3 of those are ones you want**
  (the ligatures). The other 16 change what the text means: `²`→`2`, `½`→`1⁄2`,
  `Ⅻ`→`XII`, `Ａ`→`A`.
- 10 significant case collisions out of 19 case variants: `US`/`us`, `IT`/`it`,
  `WHO`/`who`, `SAP`/`sap`, `AI`/`ai`, `Polish`/`polish`, `March`/`march`,
  `Apple`/`apple`. Sentence-initial capitals are suppressed as orthographic noise.

**Chunk shape (§6).** Chunk counts at `max_tokens=256`, `child_tokens=64`:

| Document | fixed | recursive | structural | parent_child |
|---|---|---|---|---|
| `transcript.txt` | 2 | 2 | 2 | 9 |
| `handbook.md` | 2 | 2 | 9 | 9 |
| `book.md` | 23 | 25 | 69 | 150 |
| `service.py.txt` | 2 | 3 | 6 | 6 |

`parent_child` on `book.md`: **150 children collapse to 45 distinct parents (3.3×)**.
"Top 10" therefore means ten children but roughly three parents' worth of text —
budget in tokens *after* expansion, never in `k` before it (§5.3, §7.4).

**Overlap inflation (§5.5, lab 3)** — measured against `1/(1-f)`:

| overlap | f | chunks | measured | predicted | delta |
|---|---|---|---|---|---|
| 0 | 0.00 | 23 | 1.000 | 1.000 | +0.000 |
| 26 | 0.10 | 25 | 1.087 | 1.113 | −0.026 |
| 51 | 0.20 | 28 | 1.217 | 1.249 | −0.032 |
| 128 | 0.50 | 44 | 1.913 | 2.000 | −0.087 |

Measured tracks predicted, consistently **below** it, and the gap widens with `f`.
That is the document-boundary effect the chapter mentions: the last window of a
document is short, so a 26,848-character book chunked into only 23 windows pays that
rounding once against a small denominator. Run the same sweep over a corpus of a
thousand documents and the gap shrinks toward zero — which is worth knowing before you
read a 9% deviation as a broken formula. The direction of the error is the check:
measured should never exceed predicted.

**20% overlap costs ~25% more**, on the embedding bill and on the storage bill, forever.

**Characters per token (§5.4).** Under cl100k, across this corpus: `revenue.csv` 2.20,
`statement.pdf` 2.37, `service.py.txt` 4.61, prose 4.6–5.0. A character-based splitter
set to one number produces token counts that differ by more than 2× across these files.

**Identity (§9, lab 9)** — one sentence edited at the top of `handbook.md`:

| scenario | scheme | added | updated | deleted | unchanged | embeds |
|---|---|---|---|---|---|---|
| edit first paragraph | content | 1 | 0 | 1 | 8 | 1 |
| reprocess unchanged | content | 0 | 0 | 0 | 9 | 0 |
| edit first paragraph | position | 0 | **1** | 0 | 8 | 1 |
| reprocess unchanged | position | 0 | 0 | 0 | 9 | 0 |

The `updated` column is the finding, and it is sharper than the chapter's framing. A
position-addressed ID is a digest of an *ordinal*, so it does not change when the text
does. A pipeline that diffs on IDs alone — the obvious implementation, and the one in
§9.2's sketch — therefore sees **no change at all** under position addressing and skips
the re-embed, leaving stale vectors in the index under current-looking IDs. The cost of
position addressing is not the churn; it is that the churn is *invisible* unless you
compare content you were trying to avoid comparing.

---

## 5. The bake-off: what production tooling misses

`bakeoff.py` runs the same fixtures through whatever is installed. Every check is a
known-answer test. Findings from the current run:

**PDF reading order.** Two fixtures, identical rendered pages, differing only in the
order of their `Tj` operators:

| parser | column-ordered emission | row-band emission |
|---|---|---|
| this lab (gutter detection) | separate | separate |
| pypdf | separate | **INTERLEAVED** |
| pymupdf (text) | separate | **INTERLEAVED** |
| pymupdf (blocks, as-is) | separate | **INTERLEAVED** |
| pymupdf (`sort=True`) | **INTERLEAVED** | **INTERLEAVED** |
| pymupdf (per-column clip) | separate | separate |
| pdfminer.six | separate | separate |

Three things worth keeping:

- **pdfminer.six recovers columns in both.** Its `LAParams` layout analysis groups by
  position. pypdf and PyMuPDF's default text mode follow *emission order*.
- **`sort=True` does not rescue PyMuPDF** — and makes the easy fixture worse. It sorts
  *blocks*, and PyMuPDF has already merged both columns into one block, so the
  interleaving lives inside a block where block sorting cannot reach it.
- **Per-column clip rectangles work**, and finding those rectangles is your job. That
  is the practical content of the tier-1/tier-2 boundary in §3.3.

**Broken encodings — the most important finding here.** `subset_broken.pdf` has a font
with no usable glyph map:

| parser | chars | script sanity | control chars |
|---|---|---|---|
| this lab | 394 | **0.17** | 327 |
| pymupdf | 394 | **0.17** | 327 |
| pypdf | 1379 | **1.00** | 0 |

pypdf renders unmapped glyphs as their literal names — `/g1/g2/g3…` — which is clean
printable ASCII. **A mojibake gate calibrated on PyMuPDF output scores that file 1.00
and waves it straight into the index.** So `script_sanity` is parser-dependent, and
`parse.glyph_leakage()` exists because of this run. "We have a mojibake check" is not
the same as "we would catch this", and that is one more reason `parser_version` belongs
on every chunk.

**Empty text layers.** Every library returns `""` for `scan.pdf` without complaint.
That is *correct* behaviour and exactly the problem: the gate is yours to add.

**Splitters, at a nominal 128-token budget.** None of the general-purpose splitters
keeps a table intact, and none repeats a header row — none of them knows it is a table.
Two rows do report "table intact" (`MarkdownHeaderTextSplitter`, `HierarchicalNodeParser`)
and both do it by **not enforcing a size budget at all**: their max chunk runs to 366
and 491 tokens against a 128 setting. Structure-aware splitting still needs a size
fallback, or you trade a split table for silent truncation (§5.1's C1).

**Units.** `chonkie.RecursiveChunker` defaults to `tokenizer="character"`, so
`chunk_size=128` means 128 *characters*: 21 chunks where the same number in cl100k
tokens gives 4. That is §13's anti-pattern 2 in a shipped default, not a cautionary
tale — check your library's unit before you read its benchmarks.

**Code.** Chunks that no longer parse as Python, from `service.py.txt`:

```
langchain: recursive(tiktoken)      7 chunks,  4 do not parse
langchain: from_language(PYTHON)   10 chunks,  7 do not parse
llamaindex: SentenceSplitter        6 chunks,  3 do not parse
semchunk                            7 chunks,  5 do not parse
this lab: parse_code (AST)          5 chunks,  0 do not parse
```

LangChain's language-aware splitter is better than generic separators — it splits on
`\nclass ` and `\ndef ` — but it is still separator matching, not parsing, so it has no
notion of nesting. Code has a free, exact parser; using anything else is a choice.

**Tokenizer.** This lab's dependency-free estimator runs **8.8% mean absolute error**
against real cl100k across the corpus — but the mean hides the shape. Prose lands
within a few percent (`book.md` −0.4%, `handbook.md` +3.9%); the outliers are
`service.py.txt` at **+24%** and `revenue.csv` at **−16%**, i.e. exactly the
identifier-dense and delimiter-dense content §5.4 says behaves differently. Enough to
demonstrate that chars-per-token is a property of content type; **not** enough to set a
chunk size against a hard context limit, where a 24% underestimate is a truncation.

---

## 6. Build log — the bugs, and what each one taught

Every one of these passed by accident before it passed on purpose. Each is now pinned
by a named test.

**1. The recovered PDF table never reached the index.** `parse_pdf` put the grid in
`meta` and left the element's `text` empty. Empty text normalizes to nothing, so
`build_canonical` dropped the element and the table vanished — grid recovery working
perfectly, result discarded one stage later. Found by counting chunks from
`statement.pdf` and getting 1 instead of 7. *An element with no text is not an element.*

**2. Structural chunks silently exceeded their budget.** The size check ran on the raw
slice; the heading path was prepended afterwards. Every chunk in a deep section
overshot by the length of its own breadcrumb — 257, 258, 278 tokens against a 256
limit. That is not untidiness: `max_tokens` exists to respect the model's context
limit, and exceeding it means silent truncation. *Check the budget against the text you
actually embed.*

**3. Every two-column text page was reported as a two-column table.** x-clustering
cannot distinguish a two-column layout from a two-column table — they are the same
picture. Now `confidence == "high"` requires ≥3 columns *and* short cells, and the
ambiguous case is a warning rather than a wrong grid presented as a right one.

**4. Table rows all cited the whole table.** Every row's span pointed at the table, so
citation highlighted the entire grid for a one-row question and `content_hash` was
identical across rows — 41 chunks reported as duplicates that were nothing of the kind.
Rows now carry their own spans.

**5. The running-footer filter missed the footer.** Exact matching caught
`Northwind…Confidential` and missed `Page 1 of 3`, because a page-numbered footer is
never byte-identical across pages. One `re.sub(r"\d+", "#", …)` fixed it. *The chrome
you fail to strip is spliced into the body at every page break.*

**6. The case-collision report was 21 rows of `The`/`the`.** Counting case variants
finds every sentence-initial capital and buries the eight that matter. Significance now
requires an internal capital (acronym) or a capitalized form appearing mid-sentence
(proper noun), with headings excluded as Title Case.

**7. The token estimator was 65% wrong.** It charged 4 characters per token per
pre-token — the "4 chars/token" rule of thumb, misapplied. That rule describes whole
prose; a common word plus its leading space is *one* BPE token whether it is 3
characters or 9. Recalibrated against `tiktoken` to ~8.8%. *A rule of thumb applied at
the wrong granularity is not an approximation, it is a different quantity.*

**8. `p95` came out below `p50`.** Index arithmetic on a 2-element list. It read as a
chunker bug in the report and was a bug in the statistic.

**9. The two-column fixture was too easy** — found by the bake-off. pypdf, PyMuPDF and
pdfminer all recovered the columns, because the fixture emitted the whole left column
before the right and all three preserve emission order. The test was passing for a
reason unrelated to layout analysis. `report_twocol_interleaved.pdf` emits row-band by
row-band, as real producers do, and three of the four parsers then fail.

**10. The mojibake gate is parser-dependent** — also found by the bake-off, and the
finding that changed the pipeline rather than a test. See §5.

---

## 7. What this deliberately is not

**Not a benchmark, and it produces no ranking.** Chunk counts and split behaviour are
facts about a library. Whether they matter to *your* retrieval is §15's lab 5, which
needs a golden set and an embedding model. Read the bake-off as "what would I have to
know about this tool before trusting it", not as a score.

**No embedding model, no index, no generator.** Everything measured here is arithmetic
or a routing decision. That is a deliberate scope choice, backed by §11.4: recall
barely separates chunking strategies while token efficiency separates them by an order
of magnitude, and token efficiency does not need a model.

**Tier 1 only.** No layout model, no VLM. The lab's job is to show precisely what tier
1 cannot recover so a tier-2 comparison (lab 2) has an honest baseline to beat.

**No semantic or LLM-based chunking in the core pipeline.** Both need a model call per
document, both give up the determinism that makes content-addressed IDs work, and §6.4's
published evidence has the default semantic chunker coming *last* on recall. `bakeoff.py`
will run LangChain's `SemanticChunker` if you install `sentence-transformers` — the
honest framing is "a hypothesis to test against `structural`", not an upgrade.

**A ~75 KB synthetic corpus.** Enough to demonstrate a mechanism, nowhere near enough
to choose a parameter. Every number here travels with its corpus or it should not
travel.

---

## 8. Where to go next

**To use this on your own documents**, the smallest useful step is lab 1: point
`parse.py` at twenty of your ugliest files, read the extracted text *by eye*, and
calibrate `min_yield` and `min_sanity` so they separate the files you judged broken from
the ones you judged fine. The thresholds here are placeholders and a threshold copied
from a document is a number you invented.

**To make any chunking comparison valid**, you need the sibling lab:
[`../golden-set/`](../golden-set/) builds span-labelled ground truth, which is the
artifact §11.2 says every chunking comparison requires and most published ones lack.
This lab's chunks carry `(char_start, char_end)` into canonical text precisely so that
its labels apply to all four strategies without re-labelling. Wiring the two together
is labs 4 and 5, and it is what turns this from rung 2 into rung 1.

**To read the real thing**, the repositories worth your time:
`chroma-core/chunking_evaluation` (the code behind §6.4's table, and the token-level
IoU metric), LlamaIndex's `node_parser` package (every §7 pattern as production code),
and Docling's `HybridChunker` (§6.3 done against a structured document model rather
than a text blob).

---

## Rung ledger

This lab is **rung 2 — implemented**: the code runs, 28 assertions pass, and the bugs
in §6 are recorded rather than smoothed over.

Numbers it emits are **rung 1 — measured on this corpus**, and the corpus is 20
synthetic fixtures. Quote them with that attached or not at all. The overlap-inflation
table and the cost model in act 10 are derivable arithmetic, checkable with a
calculator; the parser comparison in §5 is a measurement of specific library versions
on specific bytes, and both the versions and the bytes are in this directory.

Nothing here is **rung 1 about retrieval quality**, because nothing here retrieves.
Chapter `02` stays rung 3 — studied — until labs 2 and 4–8 run against a real corpus.
