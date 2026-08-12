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
5. [The bake-off: results](#5-the-bake-off-results)
6. [Which tool, for which case](#6-which-tool-for-which-case)
7. [Build log — the bugs, and what each one taught](#7-build-log--the-bugs-and-what-each-one-taught)
8. [What this deliberately is not](#8-what-this-deliberately-is-not)
9. [Where to go next](#9-where-to-go-next)

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
| `invoice.pdf` | PDF form + line items | Side-by-side label/value pairs, two addresses of different companies, a 5-row line-item grid. Every token survives extraction and only the *associations* break. |
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

## 5. The bake-off: results

`bakeoff.py` runs the same fixtures through whatever is installed. Every check is a
**known-answer test** — the fixture was built to contain the defect, so a library that
misses it is wrong rather than unlucky. §5.1–§5.11 are the default run: all 13 adapters
present, **tier 1 only**. §5.12 and §5.13 add the layout models behind `--tier 2`.

```
bakeoff.py                                  the default run — tier 1, every section
bakeoff.py --list                           adapters, tiers, and document classes
bakeoff.py --only pdf --tier 1 2            add Docling and Marker to the parser tables
bakeoff.py --only probe --doc invoice       one document class, end to end
bakeoff.py --only probe --doc invoice \
           --parser docling --chunker semchunk --show-chunks 3
bakeoff.py --only splitters --max-tokens 512 --chunker langchain
```

`--only probe` is the mode to reach for when the question is "how does my stack do on
*this kind of document*" rather than "what do these libraries do in general". It takes a
document class from §6.1, runs parse → gate → normalize → chunk across every parser and
chunker you name, and prints that class's known answers next to the result. `--list`
shows the classes and how many checks each carries.

### 5.1 PDF reading order

Two fixtures, **identical rendered pages and identical text**, differing only in the
order their `Tj` operators appear in the content stream. `report_twocol.pdf` writes the
whole left column then the right; `report_twocol_interleaved.pdf` writes row-band by
row-band across the gutter, which is what LaTeX and word processors actually emit.

| parser | column-ordered emission | row-band emission |
|---|---|---|
| this lab (gutter detection) | separate | separate |
| pypdf | separate | **INTERLEAVED** |
| pymupdf (`text`) | separate | **INTERLEAVED** |
| pymupdf (`blocks`, as-is) | separate | **INTERLEAVED** |
| pymupdf (`sort=True`) | **INTERLEAVED** | **INTERLEAVED** |
| pymupdf (per-column clip) | separate | separate |
| pdfminer.six | separate | separate |

The first body line of page 1 under row-band emission, as pypdf sees it:

```
'Reading order is not stored in the Margin requiremen…'
 └── left column ──────────────────┘ └── right column ──
```

**What this means.** A parser that passes column 1 and fails column 2 is ordering by
*emission*, not by position. Most real two-column PDFs emit row-band by row-band, so
that parser will interleave on your corpus even though it looked fine on your test file.

- **pdfminer.six is the only default that gets both right.** Its `LAParams` layout
  analysis groups text by position rather than by stream order.
- **`sort=True` does not rescue PyMuPDF** and makes the easy fixture *worse*. It sorts
  *blocks*; PyMuPDF has already merged both columns into a single block, so the
  interleaving lives inside a block where block sorting cannot reach.
- **Per-column clip rectangles work.** Finding those rectangles is your job — that is
  the practical content of the tier-1/tier-2 boundary in §3.3.

### 5.2 Broken encodings — the finding that changed the pipeline

`subset_broken.pdf` ships a font with no usable glyph map. `subset_ok.pdf` is the same
page with resolvable names.

| parser | chars | script sanity | control chars | glyph leak | gate |
|---|---|---|---|---|---|
| this lab | 394 | **0.17** | 327 | 0% | CAUGHT |
| pymupdf (`text`) | 394 | **0.17** | 327 | 0% | CAUGHT |
| pymupdf (`blocks`) | 392 | **0.17** | 326 | 0% | CAUGHT |
| pymupdf (`sort=True`) | **163** | 1.00 | 0 | 0% | **MISSED** |
| pypdf | 1379 | **1.00** | 0 | 100% | CAUGHT (by leak) |
| pdfminer.six | 3078 | **1.00** | 1 | 87% | CAUGHT (by leak) |

**No library raises, warns, or returns an error.** All of them return text of plausible
length. And they fail in three *different shapes*:

```
control characters   this lab, pymupdf     script_sanity sees it (0.17)
/g1/g2/g3 names      pypdf                 sanity 1.00 — clean printable ASCII
(cid:6) markers      pdfminer.six          sanity 1.00 — clean printable ASCII
```

**So a mojibake gate calibrated against one parser's failure shape misses the other
two.** That is why `parse.glyph_leakage()` exists alongside `script_sanity()` and why
both run in `gate()`. It is also the most concrete argument in this lab for putting
`parser_version` on every chunk: change the parser and you change *what your gates are
able to see*.

Two more things in that table:

- **pypdf is flagged on `subset_ok.pdf` as well, and that is correct.** pypdf does not
  resolve `uniXXXX` glyph names either, so it returns 3,117 characters of
  `/uni0043/uni006C…` for the *good* file. The control is only a control for parsers
  that can read the font at all.
- **`sort=True` returns 163 characters where every other mode returns 394** — it
  silently drops 60% of the page, with clean sanity and a clean leak score. No single
  gate catches every shape; an extraction-yield comparison against a *sibling parser*
  is what catches this one.

### 5.3 Empty text layers

Every library returns `""` for `scan.pdf` and **returns normally**. That is correct
behaviour — there genuinely is no text — and it is exactly the problem: zero chunks,
zero vectors, zero errors, and the document silently ceases to exist. The gate is yours
to add; no parser will do it for you.

### 5.4 The compounding failure: a parse bug disabling a normalizer rule

The fixture breaks `organi-` / `zational` across two lines. De-hyphenation (§4.1) fires
on `(\w)-\n(\w)`, so it needs the continuation to still be the **next line** after
parsing.

```
naive     hyphen line: 'The organi- Counterparty exposure is measured'
naive     de-hyphenation fires: False
columns   hyphen line: 'The organi-'
columns   de-hyphenation fires: True
```

Same extractor, same normalizer, two reading orders. Under naive ordering `The organi-`
is joined with the *right column's* text on the same line, so `zational` is no longer
the next line and the repair rule can never match. **A parser defect silently disabled a
normalizer rule two stages downstream**, and the term stays unsearchable in both
branches. Fixing the normalizer would not have helped. This is §1's ceiling chain with a
second-order effect, and it is the single best argument in the lab for fixing parsing
first.

`pymupdf (sort=True)` shows the same `repaired=False`, for the same reason.

Separately: every parser emits the ligature codepoint `ﬁ` as-is. That is **correct** —
it is what the file says. Expansion is the normalizer's job and does not happen unless
you do it.

### 5.5 Splitters — chunk shape at a nominal 128-token budget

`book.md`, 26,848 characters:

| splitter | n | p50 | max | orphans | ms |
|---|---|---|---|---|---|
| this lab: recursive | 61 | 86 | 128 | 0 | 18.5 |
| langchain: `recursive(chars)` | 75 | 74 | 122 | 0 | 0.1 |
| langchain: `recursive(tiktoken)` | 61 | 85 | 125 | 0 | 5.8 |
| langchain: `+sentence seps` | 75 | 74 | 122 | 0 | 0.1 |
| langchain: `MarkdownHeaderTextSplitter` | 37 | 151 | **280** | 0 | 0.8 |
| llamaindex: `SentenceSplitter` | 52 | 115 | 126 | 0 | 6.2 |
| llamaindex: `HierarchicalNodeParser` | 242 | 55 | **491** | 5 | 27.8 |
| semchunk | 61 | 85 | 126 | 0 | 5.1 |
| chonkie: `Recursive` (default) | 317 | 18 | 35 | **286** | 0.9 |
| chonkie: `Recursive` (cl100k) | 48 | 121 | 130 | 0 | 1.4 |

Two rows deserve attention.

**The two structure-aware splitters do not enforce a size budget at all.** Their max
chunk runs to 280 and 491 tokens against a 128 setting. On a real corpus that means
chunks the embedding provider silently truncates (§5.1's C1). Structure-aware splitting
is the right default *and* it still needs a size fallback — which is what this lab's
`structural()` does by recursing into any element over budget.

**`chonkie.RecursiveChunker` defaults to `tokenizer="character"`.** `chunk_size=128`
therefore means 128 *characters*: **317 chunks with 286 orphans**, against **48 chunks
with none** when the same number is interpreted as cl100k tokens. A **6.6× difference
in chunk count, and a 286-to-0 difference in orphans, from one undeclared default** —
§13's anti-pattern 2 living in a shipped library rather than in a cautionary tale.
Check your splitter's unit before you read anyone's benchmarks, including this table.

### 5.6 Tables — does the splitter cut through one?

`metrics.md` holds a 4-row table that must survive intact or its rows lose their column
headers.

| splitter | verdict |
|---|---|
| this lab: recursive | SPLIT — 1 row chunk with no header |
| langchain: `recursive(chars)` | SPLIT — 1 row chunk with no header |
| langchain: `recursive(tiktoken)` | SPLIT — 2 row chunks with no header |
| langchain: `+sentence seps` | SPLIT — 1 row chunk with no header |
| langchain: `MarkdownHeaderTextSplitter` | table intact¹ |
| llamaindex: `SentenceSplitter` | SPLIT — 1 row chunk with no header |
| llamaindex: `HierarchicalNodeParser` | table intact¹ |
| semchunk | SPLIT — 1 row chunk with no header |
| chonkie (both configs) | SPLIT — 2 row chunks with no header |

¹ *by not enforcing a size budget — see §5.5. A splitter that never splits anything
passes this test trivially and fails the context limit instead.*

**No general-purpose text splitter repeats the header row**, because none of them knows
it is a table. That is the free fix from §3.4, and it requires a parser that emits a
table *element* — which is why this lab's `parse.py` returns typed elements rather than
a string.

### 5.7 Code — does the splitter cut through a function?

`service.py.txt`: two module-level functions and a class. Chunks that no longer parse as
Python:

| splitter | chunks | do not parse |
|---|---|---|
| this lab: recursive | 8 | 6 |
| langchain: `recursive(chars)` | 8 | 6 |
| langchain: `recursive(tiktoken)` | 7 | 4 |
| langchain: `from_language(PYTHON)` | 10 | 7 |
| llamaindex: `SentenceSplitter` | 6 | 3 |
| llamaindex: `HierarchicalNodeParser` | 23 | 16 |
| semchunk | 7 | 5 |
| chonkie: `Recursive` (cl100k) | 5 | 3 |
| **this lab: `parse_code` (AST)** | **5** | **0** |

"Does not parse" is a proxy, not proof — but a chunk that is not valid Python has been
cut where no reader would cut it. LangChain's `from_language(PYTHON)` is better than
generic separators (it splits on `\nclass ` and `\ndef `) but it is still separator
matching, not parsing, so it has no notion of nesting and scores worse here than the
plain tiktoken splitter. **Code has a free, exact parser; using anything else is a
choice to discard structure you already have.**

### 5.8 Notation survival

| variant | x² | ½ | Ⅻ | µ | Ａ | US | ﬁ |
|---|---|---|---|---|---|---|---|
| source file | ok | ok | ok | ok | ok | ok | present |
| this lab: `canonicalize` | ok | ok | ok | ok | ok | ok | **expanded** |
| `NFKC` (the reflex) | **LOST** | **LOST** | **LOST** | **LOST** | **LOST** | ok | expanded |
| `NFKC` + `.lower()` (the habit) | **LOST** | **LOST** | **LOST** | **LOST** | **LOST** | **LOST** | expanded |
| unstructured: `partition_md` | ok | ok | ok | ok | ok | ok | present |

The last column is the one that *should* change: expanding `ﬁ` → `fi` is the
compatibility mapping you want, and leaving it makes the term unreachable by any BM25
query for `classification`. Every other column must survive. NFKC destroys five of them
in one line, and `.lower()` then merges `US` into `us` — both are one-line "cleanups" a
reviewer waves through.

### 5.9 HTML chrome and duplication

| extractor | chars kept | chrome left |
|---|---|---|
| this lab: tag/class filter | 492 | no |
| bs4: `get_text()` | 995 | **yes** |
| bs4: after `decompose(nav, footer, aside…)` | 701 | no |
| unstructured: `partition_html` | 901 | **yes** |

Chrome is 34–38% of each page. The cookie banner sits in a plain
`<div class="cookie-banner">`, so tag-based stripping alone misses it — which is §3.5's
argument for running corpus-level repeated-block detection *as well*. It needs no
per-site rules and it found 6 blocks appearing on >30% of the five pages.

MinHash agreement, this lab vs `datasketch` at 128 permutations:

```
chrome-heavy page A vs B    true=0.336   this lab=0.367   datasketch=0.305
A vs itself                 true=1.000   this lab=1.000   datasketch=1.000
```

Both sit inside the ±0.088 standard error that 128 permutations buys — which is exactly
why a 0.80 threshold is defensible and distinguishing 0.85 from 0.90 is not.

`thread.eml`: 3,826 raw characters → 813 after stripping quoted replies at parse time.
**66% of the file was quotation.** A splitter run over the raw file indexes the same
paragraph up to four times.

### 5.10 Tokenizer

This lab's dependency-free estimator against real cl100k, per document:

| document | cl100k | estimate | error | chars/token |
|---|---|---|---|---|
| `book.md` | 5,783 | 5,757 | −0.4% | 4.64 |
| `transcript.txt` | 375 | 367 | −2.1% | 4.85 |
| `site/*.html` | ~145 | ~150 | +2–5% | 4.9–5.1 |
| `statement.pdf` | 150 | 130 | −13.3% | 2.37 |
| `revenue.csv` | 318 | 267 | **−16.0%** | 2.20 |
| `invoice.pdf` | 335 | 275 | **−17.9%** | 2.95 |
| `service.py.txt` | 433 | 538 | **+24.2%** | 4.61 |

Mean absolute error 9.3% across 20 documents. The mean hides the shape: prose lands
within a few percent, and the outliers are exactly the identifier-dense and
delimiter-dense content §5.4 of the chapter is about — `revenue.csv` and `invoice.pdf`
are almost entirely numbers, dates and reference codes, which tokenize at roughly half
the characters-per-token of English prose. **chars/token ranges from 2.20 to 5.05 across
this corpus** — a character-based splitter set to one number produces token counts
differing by more than 2× across these files.

### 5.11 Parse speed

Median of 20 warm runs on the 3-page `report_twocol.pdf`:

| parser | ms/page | note |
|---|---|---|
| this lab | 0.43 | no font decoding, uncompressed streams |
| pymupdf | 0.53 | C library |
| pypdf | 0.68 | pure Python |
| pdfminer.six | **2.23** | ~4× slower, and the only one that gets columns right |

**Treat these as ordering, not as magnitudes.** The fixture is a small uncompressed
synthetic PDF with one base-14 font. Real PDFs with embedded subset fonts, compressed
streams and images shift these numbers a lot — but the ranking (PyMuPDF fastest,
pdfminer slowest because it is doing more) holds in every published comparison I have
seen and is explained by what each one actually does.

The tradeoff is the point: **pdfminer.six costs ~4× the CPU and is the only default that
recovers columns.** On a single-column corpus that is 4× for nothing; on a multi-column
corpus it is 4× for the difference between usable and unusable text.

### 5.12 Tier 2 — what a layout model buys, and what it costs

`bakeoff.py --tier 2` adds Docling, Marker v2 and `unstructured`'s `hi_res` and `auto`
strategies to every table above. Appendix D §2.1 puts them one tier up from everything
in §5.1–§5.11: instead of reconstructing text from glyph coordinates with heuristics, a
detection model segments the page into regions and text is extracted per region.

Reading order first, since that is where tier 1 fails hardest:

| parser | tier | column-ordered emission | row-band emission |
|---|---|---|---|
| pdfminer.six | 1 | kept separate | kept separate |
| pymupdf (per-column clip) | 1 | kept separate | kept separate |
| pypdf, pymupdf (text/blocks) | 1 | kept separate | **INTERLEAVED** |
| unstructured (`fast`) | 1 | *returns nothing at all* | *returns nothing at all* |
| unstructured (`auto`) | 2 | kept separate | kept separate |
| unstructured (`hi_res`) | 2 | kept separate | kept separate |
| **Docling** | 2 | kept separate | kept separate |
| **Marker v2** | 2 | kept separate | **INTERLEAVED** |

Four things in that table are worth more than the tier boundary it was built to show.

**1. Marker v2 fails the row-band fixture.** Appendix D §3.2 has Marker as the
open-source accuracy leader at ~76% on olmOCR-Bench, ahead of Docling at ~50%, and here
it interleaves the columns that Docling recovers. That is not a contradiction, it is
what a single-score benchmark cannot tell you: edit similarity over a whole page barely
moves when the *order* of two correct columns is wrong, because every token is still
present. Appendix D §3.3 lists exactly this as the first thing benchmarks miss. **A
parser can be 26 points better on the leaderboard and worse on your failure mode.**

**2. `unstructured`'s cheapest tier returns nothing, silently.** `strategy="fast"`
produces **zero elements on every PDF in this corpus** — not an error, not a warning, an
empty list. `auto` and `hi_res` both work on the same bytes. The saving grace is that
`auto` is what appendix D §4.3 recommends and `auto` detects the empty result and
escalates, which is the per-document tier selection working as advertised. But a
pipeline pinned to `fast` for cost reasons indexes an empty corpus and reports success.
*Caveat:* these are minimal synthetic PDFs, and `fast` may want font metadata they do
not carry — treat this as "verify your strategy on your own files", not as a general
claim about the library.

**3. Docling is the only parser that read the invoice.** See §5.13.

**4. Tier 2 does not run on hope.** Docling raises on Apple Silicon unless its
accelerator is pinned to CPU — the layout model requests a float64 tensor and MPS has no
float64. Marker's fallback path wants a `llama-server` binary and raises `SpawnError`
without it. `unstructured`'s `hi_res` needs four packages the base install does not pull
plus two Homebrew formulae. And **MinerU cannot be installed next to Marker at all**:
`marker-pdf` 2.0 requires `transformers>=5.12.1`, `mineru` 3.4.4 requires
`transformers<5.0.0`, and installing the second breaks the first with an `ImportError`
from inside a model file rather than an error from pip. Two rows of appendix D §2.2's
shortlist are un-installable as a pair.

**Speed, measured warm.** Median of 3 runs on the 3-page `report_twocol.pdf`, after a
discarded warm-up call, CPU only (Apple Silicon, no CUDA):

| parser | tier | ms/page (warm) | vs pymupdf |
|---|---|---|---|
| pymupdf | 1 | 0.53 | 1× |
| pdfminer.six | 1 | 2.23 | 4× |
| marker v2 | 2 | 59.7 | 113× |
| docling | 2 | 205 | 387× |
| unstructured (`auto`) | 2 | 1,195 | 2,254× |
| unstructured (`hi_res`) | 2 | 1,504 | 2,838× |

Read these as ordering, and read the *cold* number separately: the first call to Docling
in a fresh process took **97 seconds**, and to Marker **34 seconds**, because that is
when the model weights load. In a batch job the warm number is what you pay; in a
request path or a Lambda, the cold one is. Appendix D quotes 0.5–5 pages/sec for this
tier on GPU — nothing here contradicts that, it just is not the device most people
develop on.

**Where tier 2 is actively more dangerous.** On `subset_broken.pdf` — the PDF whose font
ships no usable `ToUnicode` CMap:

| parser | chars | script sanity | glyph leak | would the gate fire? |
|---|---|---|---|---|
| this lab, pymupdf | 394 | 0.17 | 0% | **CAUGHT** (control characters) |
| pypdf | 1,379 | 1.00 | 100% | **CAUGHT** (`/gN` names leak) |
| pdfminer.six | 3,078 | 1.00 | 87% | **CAUGHT** (`(cid:N)` leaks) |
| unstructured (all three) | 0 | 0.00 | 0% | **CAUGHT** (empty) |
| **Docling** | **992** | **1.00** | **0%** | **MISSED** |

Docling returns 992 characters of clean, well-formed, plausible text from a document
whose text layer is unreadable — because it does not need the text layer. It renders the
page and reads the pixels. Every signal §5.2 built its gate from is gone: the sanity
score is perfect, nothing leaks, the length is reasonable. **The tier that fixes your
reading-order problem removes your ability to detect the encoding problem**, and it does
so silently. If you move to tier 2, the extraction-yield and script-sanity gates in
`parse.gate()` stop being sufficient and you need a different check — agreement with a
sibling tier-1 parser is the cheapest one that still works.

The same mechanism shows up benignly on `scan.pdf`: every tier-1 parser returns `""`,
and Docling returns `<!-- image -->\n\n<!-- image -->`. That is *better* reporting — it
says "there were two images and no text" — and it is 30 characters, so a naive
`len(text) > 0` yield gate now passes a document with no readable content in it.

Ligatures move too. Every tier-1 parser hands over `classiﬁcation` with the ligature
codepoint intact, which is correct — it is what the file says — and §4.1's normalizer
expands it. Docling and `unstructured` return **zero** ligature codepoints: they expand
during extraction. The result is the same here, but the stage that did it changed, and
`parser_version` on the chunk is what lets you tell which.

### 5.13 The invoice — where tier 2 earns its cost

`bakeoff.py --only probe --doc invoice --tier 1 2` runs one document class end to end.
The invoice is the class where the payload is *association* rather than text: labels
next to values, line items next to amounts, and two addresses belonging to two different
companies. Every parser below extracts every token. The question is only what stayed
attached to what.

| parser | tier | invoice no. | bill-to intact | ship-to intact | line item → amount | totals |
|---|---|---|---|---|---|---|
| this lab (columns) | 1 | ✗ | ✗ | ✗ | ok | ok |
| pypdf | 1 | ✗ | ✗ | ✗ | ok | ok |
| pymupdf (text) | 1 | ok | ✗ | ✗ | ok | ok |
| pymupdf (per-column clip) | 1 | ok | ok | ok | **✗** | ok |
| pdfminer.six | 1 | ok | ok | ok | **✗** | ok |
| unstructured (`fast`) | 1 | ✗ | ✗ | ✗ | ✗ | ✗ |
| unstructured (`auto`) | 2 | ok | ✗ | ok | ✗ | ok |
| unstructured (`hi_res`) | 2 | ✗ | ✗ | ok | ✗ | ok |
| marker v2 | 2 | ✗ | ✗ | ✗ | ok | ok |
| **Docling** | 2 | **ok** | **ok** | **ok** | **ok** | **ok** |

**The trade-off in rows 4 and 5 is the finding.** `pdfminer.six` and per-column clipping
are §5.1's answer to column interleaving, and they are the two tier-1 rows that recover
the two address blocks — then they are the *only* rows that break the line items. The
same column-splitting that separates bill-to from ship-to also cuts the line-item grid
down its middle, so `Ingestion connector license` and `5,400.00` end up in different
columns. **The tier-1 fix for one half of the page is the tier-1 bug for the other
half**, because a single global reading-order rule cannot be right for a page that is
two-column at the top and tabular in the middle. That is precisely what a layout model
is for: it segments regions and applies a different rule inside each.

Docling passes all five, and the reason is visible in its output — it returns the line
items as an actual Markdown table:

```
|   # | Description                         |   Qty |   Unit Price |   Amount |
|-----|-------------------------------------|-------|--------------|----------|
|   1 | Ingestion connector license, annual |    12 |       450.00 | 5,400.00 |
|   2 | Document parser add-on, tier 2      |     4 |     1,250.00 | 5,000.00 |
```

No tier-1 parser in this lab produces a grid, because none of them has the concept.
`export_to_markdown()` is a lossy view of what Docling actually returns — the real output
is a `DoclingDocument`, a typed tree whose table cells are addressable — which is why
appendix D §4.1 calls structure-aware splitting trivial against it. The comparison above
is therefore *unfair to Docling in the direction that matters*: it scores the string,
and the reason to run Docling is the object.

**None of this says buy tier 2.** It says: on a born-digital single-column corpus tier 2
buys nothing measurable here and costs 100–2,800× the CPU per page; on an invoice it is
the difference between five known answers and two; and on a font-broken document it takes
away a gate you were relying on. Route by document class (§6.1), and stamp
`parser_version` on every chunk so you can tell which of these you were running.

---

## 6. Which tool, for which case

The bake-off produces facts about libraries. This section turns them into defaults.
Everything here is a **starting point to measure from**, not a ranking — §11.6 is
explicit that the answer depends on your corpus *and* your query distribution.

### 6.1 By document class

| Your corpus is mostly… | Parse with | Chunk with | The thing that will bite you |
|---|---|---|---|
| **Markdown / MDX / rST** | native — no library needed | `MarkdownHeaderTextSplitter` **plus a size fallback** | the header splitter has no size cap (§5.5); a long section becomes one 500-token chunk |
| **HTML** (docs sites, wikis, KBs) | `unstructured.partition_html` or bs4, **plus corpus-level repeated-block detection** | header-aware, then recursive within a section | chrome is 30–40% of bytes and tag rules miss `<div class="cookie-banner">` (§5.9) |
| **Born-digital PDF, single column** | PyMuPDF (fastest) or pypdf | recursive **with sentence separators** | paragraph breaks are lost in extraction, so `\n\n` never fires — add `.`/`?`/`!` to the separator list |
| **PDF, multi-column** (papers, reports, filings) | **pdfminer.six**, or PyMuPDF + per-column clip rects | structure-aware if your parser gives you elements | pypdf and PyMuPDF default text mode interleave columns (§5.1); `sort=True` does not fix it |
| **Scanned PDF / images** | OCR or a VLM — out of scope here | n/a until text exists | every library returns `""` and returns *normally* (§5.3) |
| **PDF with tables** (financial, spec sheets, clinical) | tier 2 — Docling, `unstructured` `hi_res`, or a cloud API | index row-wise sentences, return the full table (§3.4) | no text splitter keeps a table intact or repeats headers (§5.6) |
| **Invoices, forms, statements** | tier 2 or a cloud prebuilt model — no tier-1 option passes (§5.13) | don't chunk: one record per document, index the fields, return the page | every token survives and only the *associations* break, so no yield or sanity gate fires (§5.13) |
| **Spreadsheets / CSV** | load into DuckDB or Postgres | **do not chunk rows into prose** — index a table *description* | vector search never aggregates, never joins, never exhausts (§3.6) |
| **Source code** | the language's own AST (`ast`, tree-sitter) | AST boundaries + enclosing context (file path, class signature, imports) | `from_language()` is separator matching, not parsing — 7/10 chunks unparseable (§5.7) |
| **Email / tickets / chat** | strip quoted replies **at parse time**; keep thread as metadata | one chunk per message, thread id in payload | 66% duplication if you skip it (§5.9) |
| **Books, manuals, long structured docs** | native | **parent/child** — small children, section parents | budget tokens *after* parent expansion, never `k` before it (§7.4) |
| **Maths / chemistry / legal / CJK** | anything, but normalize with **NFC only** | anything | one `NFKC` call destroys `x²`, `½`, `Ⅻ`, `µ`, full-width forms (§5.8) |
| **Transcripts, OCR output, unstructured text** | plain | recursive with sentence separators | this is the one place overlap genuinely earns its cost — there are no author-drawn boundaries to use instead (§5.5 of the chapter) |
| **Mixed corpus** (the real case) | route by media type; one parser per class | pick per class, **stamp `parser_version` and `chunker_version` on every chunk** | a single global chunk size is wrong for prose *and* code at once — chars/token spans 2.2–5.1 (§5.10) |

### 6.2 By tool — when to reach for it, and what to watch

**PDF parsing**

- **PyMuPDF** — reach for it by default on volume. Fastest (0.53 ms/page here), and
  `get_text("blocks")` hands you coordinates so you can do your own layout work.
  *Watch:* default text mode follows emission order and interleaves columns;
  `sort=True` is a trap that makes it worse; on a broken font it can silently drop 60%
  of a page. Import as `pymupdf`, not the deprecated `fitz`.
- **pypdf** — reach for it when you want zero binary dependencies and a permissive
  install. *Watch:* interleaves columns, and it does not resolve `uniXXXX` or `/gN`
  glyph names — it emits them as literal text, so a font-subset PDF comes out as clean
  ASCII gibberish that no script-based gate catches.
- **pdfminer.six** — reach for it when your corpus is **multi-column** and you do not
  want to write layout code. The only default here that gets both reading-order
  fixtures right. *Watch:* ~4× slower; emits `(cid:N)` for unmapped glyphs, which is
  the same clean-ASCII trap in a different spelling.
- **Docling** — tier 2, and the one to try first: MIT, widest format support, and the
  only parser here that read the invoice correctly (§5.13) and returned its line items as
  a real table. Reach for it when **structure is the payload** — tables, forms, anything
  where association matters more than prose. *Watch:* pin the accelerator to CPU on Apple
  Silicon or it raises on a float64 tensor MPS cannot make; 205 ms/page warm and ~97 s to
  load weights cold; and it **defeats your encoding gate** — it renders the page, so a
  font-broken PDF comes back as 992 characters of clean, confident, unverifiable text
  (§5.12). Its real output is a `DoclingDocument` tree; `export_to_markdown()` throws away
  the reason you ran it.
- **Marker v2** — tier 2, GPL-3.0, and the appendix D accuracy leader (~76% olmOCR-Bench
  vs Docling's ~50%). On this corpus it **interleaves the row-band two-column fixture that
  Docling recovers** (§5.12) — a reminder that a whole-page edit-similarity score is
  nearly blind to reading order. Reach for it on PDF-dominant corpora where you have
  measured it on your own layouts and the licence is acceptable. *Watch:* its fallback
  path wants a `llama-server` binary and raises `SpawnError` without one.
- **`unstructured`** — the per-document tier selector, and the easiest way to implement
  §3.3's routing: `fast` / `hi_res` / `ocr_only` / `auto` behind one call. *Watch:*
  `strategy="fast"` returned **zero elements on every PDF in this corpus** without
  raising; `auto` caught that and escalated, which is the argument for `auto`. `hi_res`
  needs `pi_heif`, `pdf2image`, `unstructured-inference` and `unstructured_pytesseract`
  plus poppler and tesseract from Homebrew, none of which the base install pulls, and it
  is the slowest option measured here at ~1.5 s/page.
- **MinerU** — appendix D §2.2 shortlists it beside Marker, and **the two cannot share a
  virtualenv**: `marker-pdf` 2.0 needs `transformers>=5.12.1`, `mineru` 3.4.4 needs
  `<5.0.0`. Installing the second breaks the first with an `ImportError` from inside a
  model file. No adapter here for that reason; run it in its own environment behind a
  subprocess boundary if you need both.

**Chunking**

- **LangChain `text-splitters`** — the ubiquitous default, and fine, *if* you configure
  two things: use `from_tiktoken_encoder` so `chunk_size` means tokens, and add `.`
  `?` `!` to the separator list. Chroma's evaluation could not use the stock default at
  all. `MarkdownHeaderTextSplitter` is genuinely good for structure; pair it with a
  size fallback.
- **LlamaIndex `node_parser`** — reach for it when you want §7's patterns off the
  shelf: `HierarchicalNodeParser` + `AutoMergingRetriever`, `SentenceWindowNodeParser`,
  parent/child. This is the best reason to use LlamaIndex for ingestion specifically.
  *Watch:* `HierarchicalNodeParser` returns all levels, so chunk counts and max sizes
  look alarming until you realise you are seeing parents and leaves together.
- **semchunk** — reach for it when you want *only* good tokenizer-aware recursive
  splitting with no framework. Matched this lab's output almost exactly (61 chunks,
  p50 85) at a fraction of the code.
- **chonkie** — reach for it to try many strategies quickly behind one API.
  *Watch, seriously:* `tokenizer="character"` is the default, so `chunk_size` is
  characters until you say otherwise — a ~50× difference in chunk count here.
- **Write your own** — worth it exactly when you have structure a general splitter
  cannot see: tables that need header repetition, code that needs an AST, documents
  where the heading path should be prepended. That is `split.py`, and it is ~400 lines.

**Measurement**

- **tiktoken** — always, for counting. A rule-of-thumb estimator is 8.8% off on
  average and 24% off on code (§5.10); against a hard context limit that is a
  truncation.
- **datasketch** — MinHash + LSH at corpus scale. The hand-rolled version in
  `dedup.py` agrees with it inside the ±0.088 error that 128 permutations buys, and
  exists to make the mechanism readable, not to compete.

### 6.3 What to run first on your own corpus

In this order, because each step's answer changes the next:

1. **`bakeoff.py --only pdf`, but on your files.** Swap `ROOT` for a directory of your
   twenty ugliest documents. You are looking for which of §3.2's failure modes are
   present and at what rate — that is §15's lab 1, and it is the highest-yield hour
   available.
2. **Calibrate the gates.** `min_yield` and `min_sanity` in `parse.gate()` are
   placeholders. Set them so they flag the documents you judged broken by eye and only
   those. A threshold copied from a document is a number you invented.
3. **`bakeoff.py --only tokenizer`** to learn your corpus's chars/token by content
   type, then set chunk size in *tokens* under your embedding model's tokenizer.
4. **`bakeoff.py --only splitters`** to see what your current splitter does to your
   tables and your code. If you ship a mixed corpus, the answer is usually "route by
   type" rather than "tune one number".
5. **Only then** build the golden set ([`../golden-set/`](../golden-set/)) and start
   comparing chunkings on quality. Everything before this point is measurable without a
   model; nothing after it is.

### 6.4 The defaults I would ship

If someone handed me a mixed enterprise corpus tomorrow and wanted a starting
configuration rather than a research project:

```
route by media type, never one parser for everything
  PDF          pdfminer.six if multi-column is common, else PyMuPDF
               + extraction-yield gate + script-sanity gate + glyph-leak gate
               + tier 2 (Docling) for the table-heavy subset only
  HTML         unstructured or bs4, + corpus repeated-block detection per domain
  Markdown     native, header-aware
  code         AST, never a text splitter
  spreadsheets route to a query engine; index a description
  email        strip quotes at parse time

normalize   NFC + an audited ligature/invisible list. Never NFKC. Never lowercase
            on the dense branch; lowercase freely on the lexical branch.

chunk       structure-aware with a recursive fallback, 256–512 tokens measured in
            the embedding model's tokenizer, zero overlap where structure exists,
            heading path prepended to embed_text, table headers repeated.

identify    content-addressed IDs over embed_text, with parser/normalizer/chunker
            versions on every chunk, and a content comparison in the update diff.

then        measure, and change one thing at a time against a span-labelled
            golden set. Everything above is a prior, not a result.
```

## 7. Build log — the bugs, and what each one taught

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

**10. The mojibake gate is parser-dependent** — found by the bake-off, and the finding
that changed the pipeline rather than a test. `script_sanity` catches the corruption
only when the parser falls back to raw bytes and produces control characters. pypdf
emits the glyph *names* as literal ASCII instead, scoring 1.00. Hence
`parse.glyph_leakage()`. See §5.2.

**11. …and one detector was not enough either.** The first `glyph_leakage()` knew
pypdf's spelling (`/g1`, `/uni0043`) and missed pdfminer.six's, which writes `(cid:6)`
for the same condition — also clean ASCII, also sanity 1.00, also straight into the
index. Three parsers, three spellings of "I could not map this glyph", and none of them
an error. *A gate is only as good as the failure shapes it has actually seen; every
parser you add is a new shape to check.*

**12. A parser bug can disable a normalizer rule two stages downstream.** De-hyphenation
matches `(\w)-\n(\w)`, so it needs the continuation to still be the next line. Under
naive reading order `The organi-` gets joined with the *right column's* text, so
`zational` is no longer the next line and the rule can never fire — the term stays
unsearchable and no amount of work on the normalizer helps. Found by noticing a
`repaired=False` in a column I had added for a different reason. *This is the clearest
thing in the lab about why §1's ordering claim is an ordering claim.*

---

## 8. What this deliberately is not

**Not a benchmark, and it produces no ranking.** Chunk counts and split behaviour are
facts about a library. Whether they matter to *your* retrieval is §15's lab 5, which
needs a golden set and an embedding model. Read the bake-off as "what would I have to
know about this tool before trusting it", not as a score.

**No embedding model, no index, no generator.** Everything measured here is arithmetic
or a routing decision. That is a deliberate scope choice, backed by §11.4: recall
barely separates chunking strategies while token efficiency separates them by an order
of magnitude, and token efficiency does not need a model.

**The pipeline is tier 1 only; the bake-off is not.** Everything in `parse.py`,
`pdfmini.py` and the rest of the lab is stdlib geometric extraction, and that is the
point — it shows precisely what tier 1 cannot recover. `bakeoff.py --tier 2` runs Docling,
Marker and `unstructured` `hi_res` against the same known answers (§5.12, §5.13) so the
baseline has something to be measured against, but nothing in the pipeline itself calls a
model. **No VLM at any point** — tier 3 is out of scope here, and §5.12's warning about
losing the encoding gate gets worse at that tier, where output also stops being
deterministic and §9's content-addressed IDs stop working.

**No semantic or LLM-based chunking in the core pipeline.** Both need a model call per
document, both give up the determinism that makes content-addressed IDs work, and §6.4's
published evidence has the default semantic chunker coming *last* on recall. `bakeoff.py`
will run LangChain's `SemanticChunker` if you install `sentence-transformers` — the
honest framing is "a hypothesis to test against `structural`", not an upgrade.

**A ~75 KB synthetic corpus.** Enough to demonstrate a mechanism, nowhere near enough
to choose a parameter. Every number here travels with its corpus or it should not
travel.

---

## 9. Where to go next

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
