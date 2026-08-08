# 02 — Chunking and document processing

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the pipeline as dataflow, the
> recall ceiling, the four failure classes — chunking is where failure class 1 is decided),
> [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) (especially §8 on
> context limits and silent truncation, and §9 on the chunk-that-means-nothing-alone problem — this
> chapter is the other half of §9 and assumes you've read it),
> [`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md)
> (every chunking A/B in §11 is a measurement claim and needs a confidence interval).
>
> **Feeds into:** [`03-indexing-and-vector-stores.md`](03-indexing-and-vector-stores.md) (chunk
> count *is* index size; §12's arithmetic is the input to index sizing),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (the lexical branch
> wants a different analyzer over the same chunk text — §4.4; near-duplicate suppression at merge
> time — §10.4), [`06-context-engineering.md`](06-context-engineering.md) (what you retrieve is not
> what you put in the prompt — §7 is where that split begins),
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§11's span-labeled golden set is a
> hard prerequisite for evaluating anything in this chapter),
> [`15-ingestion-pipelines-and-freshness.md`](15-ingestion-pipelines-and-freshness.md) (§9's chunk
> identity scheme is what makes incremental update possible at all),
> [`16-multi-tenancy-and-isolation.md`](16-multi-tenancy-and-isolation.md) (§8's metadata is where
> tenant and ACL keys get attached, once, at ingest).
>
> **THESIS:** the chunk is the unit of retrieval, and *choosing it is a second schema decision* —
> with the same migration cost as the embedding model decision in `01`, for the same reason: change
> it and you re-process the entire corpus. But it is also strictly *upstream* of that decision in a
> way people get backwards. Parsing sets the ceiling; chunking decides how much of the ceiling you
> can reach; the embedding model only decides how well you exploit what's left. **No embedding
> model recovers information the parser destroyed, and no chunking strategy recovers information
> that was never parsed out of the PDF.** The order of operations for improving retrieval is
> therefore the reverse of the order people try: parse, then chunk, then retrieve, then model.

---

## Contents

1. [Thesis, restated as an engineering claim](#1-thesis-restated-as-an-engineering-claim)
2. [The ingestion pipeline, stage by stage](#2-the-ingestion-pipeline-stage-by-stage)
3. [Parsing — the stage that decides your ceiling](#3-parsing--the-stage-that-decides-your-ceiling)
4. [Normalization, and why the two retrieval branches want different text](#4-normalization-and-why-the-two-retrieval-branches-want-different-text)
5. [What chunk size actually trades off](#5-what-chunk-size-actually-trades-off)
6. [Chunking strategies, ordered by how much structure they use](#6-chunking-strategies-ordered-by-how-much-structure-they-use)
7. [Decoupling the retrieval unit from the generation unit](#7-decoupling-the-retrieval-unit-from-the-generation-unit)
8. [Metadata — the part that makes filtering and citation possible](#8-metadata--the-part-that-makes-filtering-and-citation-possible)
9. [Chunk identity, idempotency, and incremental update](#9-chunk-identity-idempotency-and-incremental-update)
10. [Deduplication](#10-deduplication)
11. [Evaluating a chunking strategy](#11-evaluating-a-chunking-strategy)
12. [Cost model for the chunking layer](#12-cost-model-for-the-chunking-layer)
13. [Anti-patterns](#13-anti-patterns)
14. [Mental models — the compressed set](#14-mental-models--the-compressed-set)
15. [Lab exercises](#15-lab-exercises)

---

## 1. Thesis, restated as an engineering claim

`01` argued that the embedding model is a schema decision because its migration cost is O(corpus).
Chunking has exactly the same property and is almost never treated with the same seriousness. If
you change your chunk size from 512 to 1024 tokens, you must re-chunk every document, re-embed
every chunk, and rebuild the index — the identical operation you'd run for a model migration, at
the identical cost. The chunker is as load-bearing as the model, and it's usually a default value
someone never revisited.

But the framing that actually changes how you work is the *ordering* claim. Four things determine
whether a relevant passage can be retrieved at all, and they compose as a chain of ceilings:

```
    what the parser extracted            ← hard ceiling: information not extracted is gone
      ⊇ what survived normalization      ← you can only lose here, never gain
        ⊇ what a chunk boundary preserved as a coherent unit
          ⊇ what the embedding model could represent about that chunk
            ⊇ what the index actually returns at k
```

Each stage can only pass through or destroy what the previous stage produced. This makes the
first two stages disproportionately important and the last one disproportionately over-attended.
A team that swaps `text-embedding-3-small` for a frontier model while feeding it PDF text
extracted in the wrong column order has spent money to represent garbage more accurately.

Three consequences, which the rest of the chapter is in service of:

- **Parser quality is a retrieval-quality lever, and it's usually the cheapest one available.**
  §3 is the longest section in this chapter for that reason.
- **"Chunk size" is not one number to tune — it's the outcome of four constraints pushing from
  both directions** (§5), which is why the answer is corpus-dependent and must be measured, not
  looked up.
- **You cannot A/B two chunking strategies against a chunk-labeled golden set**, because the labels
  are defined in terms of one of the two chunkings. This is a real methodological trap that
  invalidates a lot of published chunking comparisons, and §11.2 is the fix.

---

## 2. The ingestion pipeline, stage by stage

Everything between "a file exists" and "a vector is in the index" is this pipeline. Naming its
stages separately matters because each has its own failure mode, its own cost, its own idempotency
requirement, and — critically — its own version number.

| Stage | Input | Output | Primary failure mode | Reversible? |
|---|---|---|---|---|
| **Acquire** | source system | bytes + source metadata | missed documents, stale snapshots | yes (re-fetch) |
| **Parse** | bytes | structured text + layout | information destroyed silently (§3) | yes, if you kept the bytes |
| **Normalize** | raw extracted text | canonical text | over-normalization destroys meaning (§4.2) | yes, if you kept the parse |
| **Split** | canonical text + structure | chunks + spans | boundaries that orphan meaning (§5, §6) | yes, if you kept the canonical text |
| **Enrich** | chunks | chunks + metadata + context (§7, `01` §9) | metadata that can't be filtered on (§8.2) | yes |
| **Embed** | chunk text | vectors | truncation, wrong `input_type` (`01` §3, §8) | yes, at token cost |
| **Index** | vectors + payload | queryable index | orphaned vectors after updates (§9.3) | rebuild |

**Keep the intermediate artifacts.** The single highest-leverage architectural decision in an
ingestion pipeline is persisting the output of each stage, not just the final vectors. It costs
object storage — the cheapest resource in the system — and it converts "we changed the chunker, so
re-download and re-parse 400k PDFs" into "we changed the chunker, so re-run stages 4–7 from the
cached parse." Parsing is frequently the most expensive stage (§12.1), and it is the one you least
often need to redo.

Version-stamp each stage independently: `parser_version`, `normalizer_version`, `chunker_version`,
`embedding_model_version`. `01` §12 makes the case for the last one. The argument is identical for
the first three, and the payoff is the same: you can answer "which chunks in this index were
produced by the old chunker?" with a query instead of a guess, which is the precondition for
shadow-indexing and incremental migration.

---

## 3. Parsing — the stage that decides your ceiling

### 3.1 The format taxonomy, ordered by how much structure survives

| Format | Structure actually available | What's typically lost | Difficulty |
|---|---|---|---|
| Markdown | headings, lists, code fences, tables | almost nothing | trivial |
| HTML | semantic tags, headings, real `<table>` structure | drowned in boilerplate (§3.5) | easy |
| DOCX / OOXML | heading levels, lists, tables, footnotes, revision marks | usually ignored by the parser you picked | easy, underexploited |
| Plain text | line breaks only | everything else was never there | trivial |
| Source code | full AST via a real parser | comment↔code association if split naively | easy with tree-sitter |
| Email / MIME | headers, thread structure, quoted-reply nesting | quoted replies duplicated across every message (§10) | medium |
| PDF (digital) | glyph positions and font runs — **not** paragraphs | reading order, tables, headers/footers (§3.2) | hard |
| PDF (scanned) | pixels | everything, until OCR | hard |
| Slides | text boxes with coordinates | reading order, speaker notes association | hard |
| Spreadsheets | cells, formulas, sheet names | the fact that this isn't prose at all (§3.6) | wrong tool |

The steep part of that table is PDFs, and PDFs are also the format most enterprise RAG corpora are
made of. That is not a coincidence about difficulty — it's a fact about which corpora are worth
building over.

### 3.2 PDF is a print format, not a document format

This is the fact that explains every PDF extraction bug you will ever hit. A PDF content stream
does not contain paragraphs. It contains operators that place glyph runs at coordinates on a page —
conceptually "draw these glyphs at x=72.0, y=650.3 in this font at this size." There is no
paragraph object, no reading-order declaration, no table object, and no guarantee that the drawing
order corresponds to the order a human reads in. Everything a text extractor gives you above the
glyph level — lines, paragraphs, columns, tables, reading order — is *reconstructed by heuristics*
from geometry.

Once you know that, the failure modes stop being surprising:

| Failure | Mechanism | What it looks like downstream |
|---|---|---|
| **Column interleaving** | a two-column page whose glyph runs are emitted row-band by row-band; the extractor joins across the gutter | sentences that alternate between two unrelated topics mid-clause — chunks that are semantically incoherent no matter where you cut them |
| **Header/footer injection** | running heads and page numbers are just more glyph runs at the top and bottom of every page | "Confidential — Page 7 of 92" spliced into the middle of a sentence at every page break |
| **Hyphenation** | line-break hyphens are literal characters in the stream | `"organi- zational"` tokenizes as two junk tokens; the term is unsearchable lexically and mis-embedded densely |
| **Ligatures** | `ﬁ`, `ﬂ`, `ﬃ` are single glyphs with single codepoints | `"classiﬁcation"` never matches a BM25 query for `classification` |
| **Missing `ToUnicode` CMap** | a subset-embedded font maps glyph IDs to shapes but the PDF omits the map back to Unicode | extraction returns plausible-length text that is total mojibake — and it will be embedded and indexed without complaint |
| **No text layer** | the page is an image of a document | extraction returns empty string; the document is silently absent from the index |
| **Table flattening** | cells are glyph runs at coordinates; the extractor emits them in scan order | row/column association gone — see §3.4 |

Two of these deserve pipeline-level responses rather than best-effort cleanup:

**Empty-text-layer detection is a data-loss check, not a nice-to-have.** A scanned PDF that
extracts to `""` produces zero chunks and therefore zero vectors, and *nothing anywhere in the
system reports an error*. The document simply doesn't exist as far as retrieval is concerned. This
is exactly the shape of `01` §8's silent-truncation argument, one stage earlier: assert on
extraction yield (characters per page, or characters per KB of source) and route anything below
threshold to OCR or to a human, loudly.

**Mojibake detection is cheaper than it sounds.** A missing `ToUnicode` map produces text with a
wildly non-linguistic character distribution. A crude but effective gate: compute the fraction of
extracted characters that are in the expected script's alphabet plus common punctuation, and flag
pages below a threshold. It will not catch everything, but it catches the catastrophic case, which
is the one that matters.

```python
# Extraction-yield and script-sanity gates. Run these at parse time, not at debug time.
import re, unicodedata

LATIN_OK = re.compile(r"[A-Za-z0-9\s.,;:!?'\"()\[\]{}%$€£/@#&*+=<>_\-–—…]")

def extraction_yield(text: str, page_count: int) -> float:
    """Characters of extracted text per page. Near-zero means no text layer."""
    return len(text) / max(page_count, 1)

def script_sanity(text: str, sample: int = 20_000) -> float:
    """Fraction of characters that look like the expected script. Low means mojibake."""
    s = text[:sample]
    if not s:
        return 0.0
    ok = sum(1 for ch in s if LATIN_OK.match(ch) or unicodedata.category(ch).startswith("L"))
    return ok / len(s)

def gate(doc_id: str, text: str, page_count: int) -> None:
    y, q = extraction_yield(text, page_count), script_sanity(text)
    # Thresholds are corpus-specific: calibrate them on documents you have *verified* by eye,
    # then treat a breach as a routing decision (→ OCR, → quarantine), never as a warning log
    # nobody reads.
    if y < 100:
        raise NeedsOCR(f"{doc_id}: {y:.0f} chars/page — probable scan or empty text layer")
    if q < 0.80:
        raise NeedsReview(f"{doc_id}: script sanity {q:.2f} — probable encoding failure")
```

The thresholds above are placeholders, deliberately. Calibrating them means eyeballing a sample of
your own documents and picking values that separate the ones you know are broken from the ones you
know are fine — that's lab 1 in §15. A threshold copied from a document like this one is a number
you invented.

### 3.3 The three tiers of PDF parsing, and what each buys

| Tier | Mechanism | Recovers | Cost shape | When it's right |
|---|---|---|---|---|
| **Geometric text extraction** (`pypdf`, `pdfminer.six`, `PyMuPDF`) | glyph positions → heuristic lines/blocks | text, roughly ordered | CPU-milliseconds per page, no API | born-digital, single-column, table-light corpora |
| **Layout models** (`unstructured` `hi_res`, Docling, Marker, Azure Document Intelligence, AWS Textract) | a detection model segments the page into title / paragraph / table / figure regions, then extracts per region | reading order, table structure, element types | CPU/GPU-seconds per page, or per-page API price | mixed corpora, multi-column, tables that matter |
| **VLM page understanding** (a vision model reading the rendered page) | the model reads the page as an image and emits structured text | everything a human reading the page would get, including chart labels and handwriting | LLM tokens per page — the most expensive tier by an order of magnitude | high-value, low-volume, visually complex documents |

The tier-2/tier-3 boundary has been collapsing since 2024, and the 2026 open-source landscape is
worth knowing by name because it changed what "self-hostable" means. **Docling** (IBM, now hosted
by the Linux Foundation) ships `Granite-Docling-258M`, an Apache-2.0 model small enough to parse a
page in a single pass on modest hardware, and emits a structured document model rather than a text
blob — which is what makes §6.3's structure-aware splitting possible downstream. **MinerU**,
**Marker**, **olmOCR 2**, and **DeepSeek-OCR** occupy the same band, trading throughput against
extraction fidelity. **olmOCR-Bench** is the current public leaderboard for this class of tool.

Two cautions, both of which are `01` §5's leaderboard argument transplanted:

- I have **not** verified the current olmOCR-Bench figures against the primary source, so no scores
  appear here. Look them up when you need them; they move faster than any document can track, and a
  parser score quoted from a blog post is a number you inherited rather than checked.
- The consistent finding across practitioner write-ups is that **no open-source parser extracts
  every table cleanly**. That makes the benchmark much less decision-relevant than one hour spent
  running two candidates over *your* five worst tables — a 10-K financial statement, a merged-header
  spec sheet, a clinical results grid. §15's lab 2 is that hour.

The tiering decision is per-corpus, sometimes per-document, and it should be driven by measurement:
parse a sample at two tiers, run the same golden set against both, and see whether the recall
difference justifies the cost difference (§15, lab 2). The reflex to reach straight for the most
capable parser is as unexamined as the reflex to reach for the biggest embedding model, and `01` §5
made that argument already.

A fourth option exists and is genuinely different in kind: **don't parse at all.** ColPali-style
page-patch retrieval (`01` §9.4) embeds rendered page images directly and retrieves pages via late
interaction, skipping text extraction entirely. For scanned, form-heavy, or chart-heavy corpora
that is not a hack — it is the removal of the lossiest stage in the pipeline. It costs the storage
of per-patch vectors and requires retrieval infrastructure that supports MaxSim, which is why `01`
§9.5 put it in the "generous budget" column.

`unstructured`'s strategy parameter is the clearest expression of this tiering in a single library
(`fast` for geometric extraction, `hi_res` for layout models, `ocr_only` for scans, `auto` to
choose per document). Whatever library you use, know which tier you're on — the common failure is
a team running the fast path on a scanned corpus and concluding that "RAG doesn't work on our
documents."

### 3.4 Tables — the hardest case, and the one with a concrete fix

A table's meaning lives in the *association* between a cell, its column header, and its row label.
Flatten it in scan order and you get `"Q1 2024 1,204 1,190 14 Q2 2024 1,318 1,275 43"` — a string
that will embed to something adjacent to "numbers about quarters" and answer no question at all.

Three serializations, with real differences:

| Serialization | Preserves | Loses | Token cost |
|---|---|---|---|
| HTML `<table>` | full structure including `rowspan`/`colspan`, nesting | nothing structural | highest — markup is tokens |
| Markdown pipe table | column alignment of a simple rectangular table | merged cells, nesting, multi-row headers | medium |
| Row-wise sentences (`"For Q2 2024, revenue was 1,318 and cost was 1,275."`) | the header↔cell association, explicitly, in prose the embedding model was trained on | the visual table as an object | highest per row, but each row is independently retrievable |

The non-obvious point is that **these are not mutually exclusive and shouldn't be treated as one
choice.** Row-wise sentences retrieve well, because each one is a self-contained factual statement
of exactly the kind dense models are trained on. HTML preserves what a generator needs to reason
across rows. The pattern that gets both: **index the row-wise serialization, return the full
table.** That is parent-document retrieval (§7.1) applied to tables, and it is one of the cleanest
wins available in a table-heavy corpus.

The other concrete fix, which costs nothing:

> **If a table is split across multiple chunks, repeat the header row in every chunk.**

A 200-row table chunked at 512 tokens produces one chunk that has column headers and a dozen that
are unlabeled number grids. Repeating the header — and, ideally, the table caption and the section
heading — makes every chunk independently interpretable. This is the same medicine as `01` §9's
contextual retrieval, applied deterministically and for free, because the context you're
prepending is already in the document.

### 3.5 HTML: the structure is good, the boilerplate is the problem

HTML is the one common format where table structure is reliable and headings are semantic. The
problem is a different one: navigation, footers, cookie banners, related-article rails, and legal
boilerplate can outweigh the article body, and every one of those chunks is retrievable garbage
that competes for top-k slots.

Two approaches, and you want both:

1. **Readability-style main-content extraction** — density heuristics that identify the content
   subtree and discard the chrome. Fast, works well on article-shaped pages, degrades on
   application-shaped pages.
2. **Corpus-level repeated-block detection** — if a normalized block of text appears on more than
   *X%* of pages from the same domain, it's chrome. This catches site-specific boilerplate that no
   generic extractor knows about, and it needs no per-site rules.

```python
from collections import Counter

def repeated_block_filter(pages: dict[str, list[str]], threshold: float = 0.30) -> set[str]:
    """Blocks appearing on >threshold of a source's pages are chrome, not content.

    pages: {page_id: [normalized_block_text, ...]} for a single source/domain.
    Returns the set of block texts to strip. Run per source — a footer that's boilerplate
    on one site is body text on another.
    """
    n = len(pages)
    counts = Counter(b for blocks in pages.values() for b in set(blocks))
    return {b for b, c in counts.items() if c / n > threshold and len(b) < 500}
```

The `len(b) < 500` guard matters: a genuinely repeated *long* passage is more likely to be a
syndicated article or a duplicated document than chrome, and that's §10's problem, handled
differently.

The same technique, applied per-document rather than per-domain, strips PDF running heads and
footers: a line appearing at the same vertical position on most pages of one document is a header.
That is a better fix than the alternative of hoping the layout model catches it.

### 3.6 Spreadsheets and the "wrong tool" answer

A spreadsheet is a relation. The right way to answer "what was total revenue in EMEA in Q3" over a
relation is a query, not a nearest-neighbor search over embedded rows. Vector retrieval over
tabular data will confidently return rows that are *lexically* similar to the question and will
never aggregate, never join, and never be exhaustive — the three things the question actually
needs.

So the correct architecture for tabular sources is usually to load them into a query engine and
route numeric/aggregate questions there — `../databases/21-in-process-olap-duckdb-chdb.md` is the
in-process option and `../databases/04-query-engine-internals.md` is the machinery underneath. The
routing decision itself belongs to `05-query-understanding.md`. What belongs *here* is the ingest
rule: **don't chunk a spreadsheet into prose because your pipeline only knows how to do that.**
Route it, or at minimum index a natural-language *description* of each table (its schema, its
grain, its date range) so a query about it can retrieve a pointer to the right table rather than a
scattering of its rows.

### 3.7 Code

Code has a free, exact parser, and using anything else is a choice to discard structure that is
already available. Split at AST boundaries — function and class definitions — using tree-sitter or
the language's own parser. Two rules make code chunks retrievable:

- **Carry the enclosing context into the chunk.** A method body without its class name, and a
  function without its module's imports, is `01` §9.1's problem in its purest form: `def
  process(self, batch):` tells you nothing about what is being processed. Prepend the file path,
  the enclosing class signature, and (for statically typed languages) the imports the chunk
  actually references.
- **Keep the docstring and the preceding comment block with the code they describe.** They are the
  natural-language half of a (code, docstring) pair, which is exactly the positive-pair shape many
  code-retrieval models were contrastively trained on (`01` §2.1). Splitting them apart discards
  the one part of the chunk that matches how a human phrases a query.

---

## 4. Normalization, and why the two retrieval branches want different text

### 4.1 The safe transforms

These are close to always-correct on prose corpora:

| Transform | Why | Note |
|---|---|---|
| Unicode NFC | canonical composition — `é` as one codepoint, not `e` + combining accent | do this before hashing anything (§9), or identical text produces different IDs |
| Strip zero-width chars (`U+200B`, `U+FEFF`) and soft hyphens (`U+00AD`) | invisible characters break lexical match and burn tokens | soft hyphens are epidemic in PDF and DOCX text |
| De-hyphenate across line breaks | `"organi-\nzational"` → `"organizational"` | only across a *line break*; never touch `"state-of-the-art"` |
| Expand ligatures (`ﬁ`→`fi`, `ﬂ`→`fl`) | otherwise the term is lexically unreachable | targeted replacement, not blanket NFKC — see §4.2 |
| Collapse runs of whitespace | layout artifacts become tokens otherwise | preserve paragraph breaks — they're your best chunk boundaries |
| Normalize quotes and dashes to a consistent form | `'` vs `’` splits lexical matches | pick one and apply it on both the index and query sides |

The last note generalizes: **any normalization applied at ingest must be applied identically to
queries at search time.** A normalizer that runs in the ingestion service and not in the query
service is a bug that manifests as unexplained lexical recall loss, and it is invisible to every
test that doesn't compare the two code paths. Put the normalizer in one shared, versioned module
and call it from both.

### 4.2 The transforms that destroy meaning

**NFKC is lossy and people apply it reflexively.** Compatibility normalization maps `ﬁ` → `fi`,
which you want. It also maps `²` → `2`, `½` → `1⁄2`, `Ⅻ` → `XII`, and full-width forms to ASCII.
In a chemistry, mathematics, legal, or CJK corpus those are semantic changes, not cleanups —
`x²` and `x2` are different expressions. Use NFC plus an explicit, audited list of compatibility
replacements you actually want, rather than NFKC's whole table.

**Lowercasing and stopword removal are a lexical-era habit that harms dense retrieval.** Embedding
models were trained on natural text, with case and function words present; case carries entity
signal (`US` vs `us`, `Polish` vs `polish`), and stopwords carry relational structure ("revenue
*before* tax" vs "revenue *after* tax"). Stripping them changes the input distribution away from
what the model saw in training, for no benefit.

Which produces the point that trips up hybrid pipelines:

### 4.3 The canonical chunk is one thing; the analyzed forms are per-branch

The dense branch and the lexical branch want *different* transformations of the same chunk. BM25
genuinely benefits from lowercasing, stemming or lemmatization, and stopword handling — that's what
its scoring model assumes. Dense retrieval wants the text as written.

The architecture that survives this: **store one canonical chunk text, and derive per-branch
analyzed forms from it at index time.** Never let the lexical analyzer's output become the text
you embed, and never let the embedding-side text become what you store as canonical for citation.
Three distinct artifacts:

```
canonical_text     → stored, cited, shown to users, hashed for identity (§9)
      ├── embed_text    = canonical_text (+ optional prepended context, §7 / 01 §9.2)
      └── lexical_text  = analyzer(canonical_text)   # lowercase, fold, stem — BM25's business
```

`04-retrieval-hybrid-and-reranking.md` owns the analyzer choice. What this chapter owns is the
insistence that the two branches read from a shared canonical form rather than from each other's
output — because the alternative failure is subtle, silent, and looks like "hybrid search doesn't
help much on our corpus."

### 4.4 Keep the offsets

Every transformation should preserve a mapping back to character offsets in the pre-normalization
text, or at minimum the pipeline should retain the canonical text and record each chunk's
`(char_start, char_end)` into *it*. This is what makes citation with highlighting possible
(`06-context-engineering.md`), what makes span-level golden sets possible (§11.2), and what makes
"show me exactly what the model was quoting" answerable during an incident
(`18-failure-modes-and-incident-walkthrough.md`). Retrofitting offsets after the fact means
re-running ingestion; recording them costs two integers per chunk.

---

## 5. What chunk size actually trades off

Chunk size is not a tuning knob with a monotone quality curve. It is squeezed by four constraints,
two pushing down and two pushing up, and the optimum is where they balance *for your corpus and
your query distribution*. That's why the answer is a measurement (§11) and not a number in a blog
post.

### 5.1 The constraints pushing chunks smaller

**C1 — The embedding model's context limit is a hard ceiling.** `01` §8 covers this fully: exceed
it and most vendors silently truncate, producing a vector for the first part of the chunk that is
stored and ranked exactly like a complete one. This is a hard bound, not a tradeoff.

**C2 — Representational dilution.** Most embedding models pool token representations into one
vector, and pooling is averaging. Average over a chunk covering one topic and you get a vector that
points at that topic. Average over a chunk covering six topics and you get a vector that points at
their centroid — a location that is *near* all six and *specific to* none. The practical effect is
that large chunks drift toward the corpus mean and become less discriminative: they get retrieved
moderately often for many queries and strongly for none. This is a mechanical consequence of
pooling, and it's the reason "just make the chunks big and let the LLM sort it out" degrades
retrieval even when it improves the generator's raw material.

### 5.2 The constraints pushing chunks larger

**C3 — A chunk must contain enough to be judged relevant.** This is `01` §9.1's argument, and it is
the dominant constraint on most corpora. A chunk stripped of the entity, date, and subject its
sentences refer to cannot be matched by a query that names them. Shrinking chunks makes this
strictly worse — more chunks, each with less of its own context.

**C4 — Fixed per-chunk overhead is real and it's mostly index size.** Every chunk costs one vector
(dimension × bytes per component — `01` §7 and §13), plus its payload, plus its ID, plus its
metadata, plus a node in the graph index (`../databases/11-hnsw-vector-search-internals.md`).
Halving chunk size roughly doubles all of it. §12.2 does the arithmetic.

### 5.3 The interaction nobody accounts for: chunk size changes what `k` means

Retrieving `k=10` chunks of 256 tokens gives the generator 2,560 tokens of retrieved context.
Retrieving `k=10` chunks of 1,024 tokens gives it 10,240. These are not comparable configurations,
and yet "recall@10 improved when I increased chunk size" is a conclusion people draw constantly.
Of course it did — you quadrupled the context budget.

This has two consequences. The measurement consequence is §11.2's: compare at a fixed *token
budget*, not a fixed `k`. The design consequence is that **chunk size and `k` are one joint
decision, bounded by the generation context budget** (`06-context-engineering.md`) and by the
reranker's per-candidate cost (`04`). Fixing one and tuning the other is fine; reporting a result
that changed both and attributing it to one is not.

### 5.4 Count tokens, not characters

A splitter that counts characters produces chunks whose *token* counts vary enormously by content
type. English prose runs roughly 4 characters per token under `cl100k_base`; code, JSON, and
identifier-dense text run much lower; CJK text lower still. A character-based splitter set to
"2,000 characters" therefore yields ~500 tokens on prose and can yield well over a thousand on a
JSON-heavy document — which is how a corpus with mixed content types ends up hitting C1's ceiling
on some documents and nowhere near it on others, with no single setting that's right for both.

Count in the tokenizer of the embedding model you're actually using (`tiktoken` for OpenAI's
`cl100k_base`, the model's own tokenizer otherwise — `01` §8.2). The cost is one tokenizer call
per candidate boundary, which is negligible next to the embedding call that follows.

### 5.5 Overlap: what it buys, and the cost that isn't obvious

Overlap exists so that a passage straddling a boundary appears intact in at least one chunk. It
works. It has two costs, one arithmetic and one behavioral.

**The arithmetic cost is superlinear in the overlap fraction, and people quote it linearly.** With
chunk size `C` and overlap `O`, the stride is `S = C - O`, and a document of `N` tokens produces
about `N/S` chunks, each of `C` tokens. Total embedded tokens:

```
tokens_embedded ≈ N × C / (C - O) = N / (1 - f)      where f = O/C
```

```python
def overlap_inflation(chunk_tokens: int, overlap_tokens: int) -> float:
    """Multiplier on both embedding cost and index size, from overlap alone."""
    stride = chunk_tokens - overlap_tokens
    if stride <= 0:
        raise ValueError("overlap must be strictly less than chunk size")
    return chunk_tokens / stride

for f in (0.10, 0.15, 0.20, 0.25, 0.50):
    print(f"{f:.0%} overlap → {1/(1-f):.3f}x chunks, embed cost, and index bytes")
# 10% overlap → 1.111x ...
# 20% overlap → 1.250x ...
# 50% overlap → 2.000x ...
```

20% overlap costs 25% more, not 20% more, and it costs it on *both* the one-time embedding bill and
the recurring storage bill. At 50% it doubles your index. That's a real budget line, and it's
usually set by a default nobody priced.

**The behavioral cost is that overlap inflates recall@k while deflating the distinct information in
the top-k.** Two adjacent chunks sharing 20% of their text embed to nearby vectors and tend to be
retrieved together. Your top-10 now contains, say, 7 distinct passages plus 3 near-duplicates of
passages already present — the metric says 10, the generator sees 7. Recall@k improves (the target
span is present in more chunks, so it's easier to hit) while the effective context diversity drops.
The mitigation is near-duplicate suppression at merge time (§10.4), not less overlap — but you have
to know the effect exists, or you'll read the recall improvement as free.

**And there is published evidence that overlap can cost you quality outright, not just money.** In
Chroma's evaluation (the full table is in §6.4), with `text-embedding-3-large`, recursive splitting
at 400 tokens scored **89.5 recall / 17.7 Precision_Ω with zero overlap** versus **88.1 / 13.9 with
200 tokens of overlap** — worse on both axes, while costing 1.25× the tokens and bytes. The
degradation is sharpest at large chunks: 800/400 was the weakest recursive configuration in their
table on every metric.

The mechanism is the behavioral cost above, made visible by a token-level metric: overlapping chunks
put duplicate tokens in the retrieved set, and Chroma's IoU denominator counts *all* retrieved
tokens while counting each relevant token once — which is exactly the "the metric says 10, the
generator sees 7" effect, priced.

**But this reverses for small chunks with a weaker model.** In the same report's
`all-MiniLM-L6-v2` table, 250-token chunks scored **82.4 recall with 125 overlap** versus **77.1
with zero** — the authors conclude that "for smaller context, overlapping chunks are necessary for
high recall." So overlap is not simply good or bad: it buys boundary insurance that matters when
chunks are small relative to the answer, and it buys redundancy you pay for twice when they aren't.
That is a corpus-and-model-dependent tradeoff, which is to say it's lab 3 and lab 5 in §15, not a
default.

**The structural alternative is usually better than the arithmetic one.** Overlap is a blunt
instrument for a problem that structure often solves exactly: if you split at paragraph and section
boundaries (§6.3) rather than at arbitrary token offsets, far fewer passages straddle a boundary in
the first place. Overlap is the fallback for corpora without usable structure, not the default.

---

## 6. Chunking strategies, ordered by how much structure they use

The ordering below is deliberate: each strategy uses strictly more information about the document
than the one before it. That, and not sophistication, is the axis that predicts whether it will
help you — a strategy can only exploit structure your parser actually recovered (§1's ceiling
chain).

### 6.1 Fixed-size splitting

Cut every `C` tokens with `O` overlap, ignoring all structure. Its virtues are real: it is exactly
predictable in cost and chunk count, perfectly deterministic, and trivially parallel. Its vice is
that it cuts mid-sentence and mid-table with no awareness of doing so.

It is the right choice for genuinely unstructured text (transcripts without speaker turns, OCR
output with no recovered layout) and the wrong choice everywhere structure survived parsing. It is
also the correct *baseline*: every strategy below should be measured against fixed-size splitting
at matched token budget, and a surprising number of them fail to beat it (§6.4).

### 6.2 Recursive character/token splitting

The workhorse default, and worth understanding rather than importing. Given an ordered list of
separators — conventionally `["\n\n", "\n", " ", ""]`, i.e. paragraph, line, word, character — it
tries to split on the most semantically meaningful separator that yields pieces under the size
limit, and falls back down the list only for pieces that are still too large.

```python
def recursive_split(text: str, separators: list[str], max_tokens: int, count) -> list[str]:
    """What RecursiveCharacterTextSplitter is actually doing, in 20 lines.

    count: a callable returning the token count of a string, under the *embedding
    model's* tokenizer (§5.4). Note this sketch omits overlap — see §5.5 for why
    structure-aware boundaries are usually the better answer than overlap anyway.
    """
    sep, rest = separators[0], separators[1:]
    pieces = text.split(sep) if sep else list(text)
    out: list[str] = []
    buf = ""
    for piece in pieces:
        candidate = f"{buf}{sep}{piece}" if buf else piece
        if count(candidate) <= max_tokens:
            buf = candidate                      # keep accreting into the current chunk
            continue
        if buf:
            out.append(buf)                      # flush what we had
            buf = ""
        if count(piece) <= max_tokens:
            buf = piece
        elif rest:
            out.extend(recursive_split(piece, rest, max_tokens, count))
        else:
            raise ValueError("indivisible piece exceeds max_tokens")
    if buf:
        out.append(buf)
    return out
```

Two things follow from reading the code that don't follow from reading the docs:

- **It is greedy, not balanced.** It packs each chunk as full as it can before starting the next,
  so the final chunk of a document is frequently a 40-token orphan. Those orphans embed poorly
  (little content, high dilution relative to their length) and clutter the index. A post-pass that
  merges any chunk below a minimum size into its neighbour is worth the ten lines.
- **The separator list is the entire strategy.** The default list encodes an assumption that
  paragraphs are delimited by blank lines — true of Markdown and clean prose, false of PDF-extracted
  text where paragraph breaks may be single newlines or nothing at all, and false of code. Supply a
  separator list that matches your actual format. That single change is usually worth more than
  switching to a fancier strategy.

There is direct published evidence for that last claim, and it's worth quoting because it comes
from people trying to be *fair to the baseline* rather than to beat it. Chroma's chunking
evaluation (Smith & Troynikov, 2024) reports that they could not use the library default at all:

> "We found that it was necessary to alter some defaults to achieve fair results. By default, the
> `RecursiveCharacterTextSplitter` uses the following separators: `["\n\n", "\n", " ", ""]`. We
> found this would commonly result in very short chunks, which performed poorly... Therefore we use
> `["\n\n", "\n", ".", "?", "!", " ", ""]` as the set of separators."

Adding sentence terminators to the list — a one-line change — was a precondition for the default
splitter performing competitively at all. Note what it fixes: without `.`/`?`/`!`, a document whose
paragraphs exceed the chunk size falls straight through to splitting on `" "`, i.e. mid-sentence at
an arbitrary word. The sentence tier is the missing rung on the ladder. If you take one
configuration change from this chapter, take this one.

Library defaults deserve a specific warning. LangChain's base text splitter has historically
defaulted to a chunk size in the low thousands of *characters* with a small overlap, and
LlamaIndex's sentence splitter to roughly a thousand *tokens* — different units, different
magnitudes, both chosen as generic middles rather than for your corpus. Check the defaults in the
version you have pinned; treat any splitter parameter you didn't set deliberately as unset.

### 6.3 Structure-aware splitting

Use the document's own hierarchy as the boundary set: Markdown headings, HTML section elements,
DOCX heading levels, PDF layout elements from a tier-2 parser (§3.3), AST nodes for code (§3.7).
Split at the deepest heading level whose sections fit under the size limit, and split *within* a
section only when a section alone exceeds it.

This is the highest-value strategy per unit of effort on any corpus that has structure, for a
reason worth stating explicitly: **a section boundary is a semantic boundary the author already
drew for you.** Semantic chunking (§6.4) spends an embedding pass trying to infer boundaries the
author annotated in the source.

It also hands you the cheapest possible version of `01` §9's contextualization, at zero marginal
cost: **prepend the heading path to each chunk's embedded text.**

```python
def contextualize(chunk_text: str, heading_path: list[str], doc_title: str) -> str:
    """Deterministic, free, no LLM. The document already told you this."""
    trail = " > ".join([doc_title, *heading_path])
    return f"{trail}\n\n{chunk_text}"

# "ACME 10-K 2023 > Item 7. MD&A > Liquidity and Capital Resources
#
#  The company's revenue grew by 3% over the previous quarter."
```

Compare that to `01` §9.2's contextual retrieval: same effect on the failing example, no LLM call,
fully deterministic, and therefore no chunk-ID churn on reprocessing (§9.1). It doesn't recover
facts stated elsewhere in the document body — an LLM-generated context can pull in "the previous
quarter's revenue was $314 million," and this cannot — so it isn't a strict replacement. It is the
thing to do *first*, before paying for the LLM version, so that the LLM version's measured
improvement is measured against a fair baseline rather than against a straw man.

### 6.4 Semantic chunking, and an honest verdict

**Mechanism:** embed each sentence, compute the similarity between consecutive sentence embeddings,
and place a boundary where the similarity drops sharply — the intuition being that a topic shift
shows up as a discontinuity in embedding space. The threshold is set by a percentile of the observed
distance distribution, or by standard deviations from its mean, or by an interquartile rule.

```python
import numpy as np

def semantic_breakpoints(sentence_vecs: np.ndarray, percentile: float = 95.0) -> np.ndarray:
    """Indices *before which* to cut. sentence_vecs: (n_sentences, dim)."""
    v = sentence_vecs / np.linalg.norm(sentence_vecs, axis=1, keepdims=True)  # 01 §2.2
    adjacent_sim = np.sum(v[:-1] * v[1:], axis=1)      # cosine between sentence i and i+1
    distance = 1.0 - adjacent_sim
    threshold = np.percentile(distance, percentile)
    return np.flatnonzero(distance > threshold) + 1
```

**The honest verdict, with the numbers.**

Chroma's technical report (Smith & Troynikov, *Evaluating Chunking Strategies for Retrieval*, 2024)
is the reference public study, and its headline result for semantic chunking is not encouraging.
Reproduced from their table for `text-embedding-3-large` at n=5 retrieved chunks — their metrics are
token-level, and §11.4 explains why that matters more than the recall column:

| Chunking | Size (mean) | Overlap | Recall | Precision | Precision_Ω | IoU |
|---|---|---|---|---|---|---|
| Recursive | 800 (~661) | 400 | 85.4 | 1.5 | 6.7 | 1.5 |
| Recursive | 400 (~312) | 200 | 88.1 | 3.3 | 13.9 | 3.3 |
| Recursive | 400 (~276) | 0 | 89.5 | 3.6 | 17.7 | 3.6 |
| Recursive | 200 (~137) | 0 | 88.1 | 7.0 | 29.9 | 6.9 |
| **Kamradt (semantic, default)** | N/A (~660) | 0 | **83.6** | **1.5** | **7.4** | **1.5** |
| ★ KamradtModified | 300 (~397) | 0 | 87.1 | 2.1 | 10.5 | 2.1 |
| ★ ClusterSemantic | 400 (~182) | 0 | 91.3 | 4.5 | 20.7 | 4.5 |
| ★ ClusterSemantic | 200 (~103) | 0 | 87.3 | **8.0** | **34.0** | **8.0** |
| ★ LLM (GPT-4o) | N/A (~240) | 0 | **91.9** | 3.9 | 19.9 | 3.9 |

*(★ = strategies Chroma proposed. Sizes in cl100k tokens. Standard deviations are large — 25–40 —
and are in the source; they are omitted here only for width, not because they're small. This is
Chroma's measurement on Chroma's synthetic evaluation set, not mine and not yours.)*

**The default semantic chunker came last on recall.** 83.6, below every recursive configuration in
the table including the naïve 800/400 one. The reason is visible in the size column: Kamradt's
threshold is a *percentile* of the observed distance distribution, so it is relative to the corpus
and produced ~660-token chunks here — the algorithm has no chunk-size control at all, which is
precisely what Chroma's KamradtModified variant adds via binary search over the threshold. The
lesson isn't "semantic chunking is bad"; it's that **the published semantic chunker's headline
mechanism is coupled to a parameter it doesn't let you set**, and that a strategy which can't
guarantee chunks fit your embedding model's context window (§5.1's C1) has a correctness problem
before it has a quality problem.

The variants that *did* win are worth reading carefully, because both wins are qualified:

- `ClusterSemanticChunker` took the best recall among non-LLM methods (91.3) and by far the best
  token efficiency at small sizes (8.0 IoU at ~103 tokens, more than 5× the naïve 800/400 config).
  It works by maximizing within-chunk similarity up to a user-specified maximum length — i.e. it
  fixes exactly the size-control defect above. It is also, explicitly, *embedding-model-aware*.
- `LLMChunker` took the best recall overall (91.9), and Chroma's own limitations section notes the
  cost they did not measure: runtime "can vary from almost instantaneous to tens of minutes in the
  case of the `LLMChunker`." That is §6.5's argument, conceded by the authors of the winning number.

Two further points the table doesn't say out loud but does support:

**It has an ingest cost that comparisons routinely omit** — an embedding call per *sentence*, on top
of the embedding call per chunk. On a corpus averaging ~20 sentences per chunk that's an order of
magnitude more ingest-time embedding calls, and a quality comparison that ignores it is comparing at
unequal cost.

**Semantic chunking couples your chunk boundaries to your embedding model** — which the
`ClusterSemanticChunker`'s design makes explicit rather than accidental. Change the model and the
boundaries move, so the corpus re-chunks, so every chunk ID changes (§9.1), so `01` §12's "just
re-embed" migration becomes "re-chunk, re-embed, and invalidate every stored chunk reference." Two
schema decisions that were independent are now welded together. That can still be worth it. It is a
cost that belongs in the decision, and it is the reason `ClusterSemanticChunker`'s strong numbers
don't make it a free upgrade.

The reasonable posture: treat semantic chunking as a hypothesis to test against structure-aware
splitting (§6.3) on corpora that *lack* usable structure, where the author-drawn boundaries it's
trying to infer genuinely aren't available. On a corpus of Markdown or well-parsed HTML you're
paying an embedding pass to guess at headings you already have. And if you do test it, test a
size-controlled variant — the uncontrolled percentile version is the one that came last.

### 6.5 LLM-based chunking

Hand a document (or a window of it) to an LLM and ask it to emit boundaries or to rewrite the
document into self-contained propositions. It is the most capable strategy and the least
deployable, for reasons that are all operational rather than qualitative:

- **Cost scales with corpus tokens**, at generation prices rather than embedding prices — two to
  three orders of magnitude more per token than embedding (`01` §13, `11-token-accounting-and-cost.md`).
- **It is non-deterministic**, so reprocessing the same unchanged document can yield different
  boundaries, which churns chunk IDs and forces spurious re-embeds and index writes (§9.1). Pinning
  temperature to 0 reduces but does not eliminate this.
- **It can fabricate.** A proposition-extraction pass that rewrites text can introduce statements
  the source doesn't support, and those statements then get retrieved and cited as if they were
  source text. That is a faithfulness failure injected at *ingest*, where no output-side guardrail
  (`17-safety-guardrails-and-prompt-injection.md`) will catch it, and where the citation will point
  at a real document that doesn't actually say it.

The last point deserves the weight. Every other strategy in this section can only lose information.
This one can *add* information, and added information at ingest time is indistinguishable from
source information at retrieval time. If you use it, keep the original span alongside the rewritten
proposition and cite the original.

Where it earns its cost: small, high-value, structurally hostile corpora — the same profile that
justifies tier-3 parsing in §3.3.

### 6.6 Decision table

| Strategy | Structure used | Determinism | Ingest cost | Chunk-ID stability | Best fit |
|---|---|---|---|---|---|
| Fixed-size | none | total | free | stable if content-addressed (§9.1) | truly unstructured text; the mandatory baseline |
| Recursive | separators you supply | total | free | stable | the sane default; tune the separator list to the format |
| Structure-aware | headings, layout, AST | total | free (parser already paid) | stable | any corpus with real structure — start here |
| Semantic | inferred from embeddings | total, but coupled to model version | +1 embedding pass per sentence | **unstable across model changes** | unstructured corpora, as a tested hypothesis |
| LLM-based | inferred by a generator | none (mitigable, not removable) | generation-priced | **unstable run to run** | small, high-value, structurally hostile corpora |

---

## 7. Decoupling the retrieval unit from the generation unit

### 7.1 The core move

§5's constraints conflict because they're assumed to apply to one object. C2 (dilution) wants small
chunks so vectors stay discriminative. C3 (context) wants large chunks so passages are
interpretable. They only conflict if **the thing you search over and the thing you hand the
generator must be the same thing.** They don't.

- **Embed and index a small, precise unit** — a sentence, a paragraph, a table row. C2 is satisfied.
- **Return a larger unit that contains it** — the enclosing section, the surrounding window, the
  whole table. C3 is satisfied.

This is *parent-document retrieval* (also "small-to-big"), and it is the single most useful pattern
in this chapter. It costs one extra store lookup per result and a `parent_id` on every chunk.

### 7.2 The family

| Pattern | What's embedded | What's returned | Index size vs baseline | Extra ingest cost |
|---|---|---|---|---|
| **Parent document** | child chunk (small) | parent chunk/section | grows with child count | none |
| **Sentence window** | one sentence | that sentence ± *n* neighbours | large — one vector per sentence | none |
| **Auto-merging / hierarchical** | leaves of a chunk tree | a parent, once enough of its children are retrieved | grows with leaf count | none |
| **Summary indexing** | an LLM summary of the chunk | the original chunk | same as baseline | one generation per chunk |
| **Hypothetical-question indexing** | LLM-generated questions the chunk answers | the original chunk | ×(number of questions per chunk) | one generation per chunk |

Auto-merging deserves a note because its trigger rule is the interesting part: retrieve leaves,
then if *m* of a parent's *n* children appear in the result set, replace them with the parent. It
adapts the returned granularity to how concentrated the evidence is — a query answered by one
sentence gets one leaf, a query whose evidence is spread across a section gets the section. That
adaptivity is the thing fixed-size chunking fundamentally cannot do.

Hypothetical-question indexing is worth recognizing for what it is structurally: it attacks the
query/document asymmetry from `01` §3 **from the document side.** Rather than asking the embedding
model to bridge the gap between a question-shaped query and a statement-shaped passage, it stores a
question-shaped surrogate so that the query-side and document-side texts have the same shape. It is
the same insight as asymmetric `input_type`, paid for with generation tokens instead of a vendor
parameter — which is a good reason to make sure you've done the free version first.

### 7.3 The four-axis taxonomy — where every fix in this chapter and in `01` §9 sits

`01` §9 presented contextual retrieval, late chunking, and late interaction as three fixes for one
problem. Adding this chapter's patterns makes the underlying structure visible: each fix modifies a
*different stage* of the pipeline, which is why they compose rather than compete.

| Axis | What it changes | Techniques |
|---|---|---|
| **What text gets embedded** | the input to the embedding call | heading-path prefix (§6.3), contextual retrieval (`01` §9.2), summary/hypothetical-question indexing (§7.2) |
| **How the vector is computed** | the embedding mechanism itself | late chunking (`01` §9.3) |
| **How similarity is computed** | the scoring function at query time | late interaction / ColBERT, ColPali (`01` §9.4) |
| **What gets returned** | the retrieval→generation handoff | parent document, sentence window, auto-merging (§7.1–7.2) |

Read across the rows and the composability is obvious: you can prepend heading paths *and* return
parent documents *and* rerank — these touch different stages and stack. Read down and the cost
structure separates cleanly: axis 1 costs ingest tokens, axis 2 costs a long-context model, axis 3
costs storage and query compute, axis 4 costs almost nothing. **Axis 4 is nearly free and is the
one most often skipped**, which makes it the first thing to reach for.

### 7.4 What it costs you

Parent-document retrieval isn't free of consequences, just of tokens:

- **The dedup problem moves.** Ten retrieved children can map to three parents. You must dedup
  parents before assembling the prompt, or you'll send the same section three times and pay for it
  in context budget (`06`).
- **`k` becomes ambiguous.** "Top 10" now means ten children, which might be three parents' worth of
  text or ten. Budget in tokens after parent expansion, not in `k` before it — the same discipline
  §5.3 and §11.2 demand.
- **The parent store is a second store.** It can be a blob store or a row store; it does not need to
  be the vector database, and it usually shouldn't be.
- **Reranking applies to children, relevance applies to parents.** A cross-encoder scores the child
  it was given; whether the *parent* is worth its context cost is a different question. `04` owns
  this and it's a genuine open seam.

---

## 8. Metadata — the part that makes filtering and citation possible

### 8.1 What every chunk needs

| Field | Why | Consumer |
|---|---|---|
| `chunk_id` | identity, upsert, dedup | §9 |
| `doc_id`, `source_uri` | provenance, citation | `06` |
| `char_start`, `char_end` | exact span for highlighting and span-level eval | §4.4, §11.2 |
| `parent_id` | the §7 pattern | §7 |
| `tenant_id` | isolation, and it must be a *filter*, not a hope | `16` |
| ACL / visibility keys | authorization at query time | `17` |
| `page` / `section_path` | citation a human can verify | `06` |
| `created_at` / `modified_at` | recency filters, staleness SLOs | `15` |
| `parser_version`, `chunker_version`, `embedding_model_version` | migration, shadow indexing, incident forensics | §2, `01` §12 |
| `content_hash` | change detection, exact dedup | §9.2, §10.1 |

The version fields are the ones teams omit and then wish they had during an incident. They cost a
few bytes per chunk and they are the difference between "some chunks in this index are stale" and
`WHERE chunker_version < 7`.

### 8.2 The mistake: metadata in the embedded text instead of the payload

Prepending `"created_at: 2024-03-14 | tenant: acme | source: confluence"` to the chunk before
embedding is a common pattern and it is mostly wrong. Dense embedding models do not represent dates
in a way that makes `"2024-03-14"` closer to `"last March"` than to `"2019-11-02"` in any reliable,
orderable sense; you get a small amount of noise added to every vector in the corpus, uniformly,
which is dilution (§5.1's C2) in exchange for nothing. Worse, it *feels* like it works, because
retrieval still returns results.

The rule that actually holds:

- **Structured, enumerable, or ordered attributes → payload, and query them with a filter.** Dates,
  tenant IDs, document types, statuses, numeric ranges. Filtered vector search is what this is for
  (`03-indexing-and-vector-stores.md` covers pre-filter vs post-filter, which is a real performance
  decision with a real recall trap).
- **Natural-language context a human would need to interpret the chunk → embedded text.** Document
  title, section heading path, table caption, the entity the pronouns refer to. This is §6.3 and
  `01` §9.2 — and the test for whether something belongs here is simple: *would a human reading only
  this chunk need it to know what the chunk is about?* A date sometimes passes that test ("Q2 2023"
  in a filing chunk genuinely disambiguates), a tenant ID never does.

### 8.3 The cardinality budget

Every metadata field you can filter on is an index the vector store must maintain, and every
high-cardinality field you attach is a cost. This is the same cardinality problem as metric labels,
and the analysis in `../sre-observability/18-cardinality-and-cost.md` transfers directly. Decide
what you will filter on before ingest, because adding a filterable field to an existing index means
touching every vector — the migration cost, again, of a decision that looked like a config change.

---

## 9. Chunk identity, idempotency, and incremental update

A corpus is not a snapshot; documents change. Everything in this section is about making the update
path cost proportional to *what changed*, not to corpus size. `15-ingestion-pipelines-and-freshness.md`
owns the pipeline; this section owns the identity scheme that makes it possible.

### 9.1 Content-addressed vs position-addressed IDs

There are two natural ways to name a chunk, and the choice determines your update cost.

**Position-addressed:** `hash(doc_id, chunker_version, ordinal)`. Simple, and it has a nasty
property: insert a paragraph at the top of a document and every subsequent chunk's ordinal shifts
by one, so every chunk ID in the document changes, so the entire document is re-embedded and
re-indexed. A one-word edit costs a full document reprocess.

**Content-addressed:** `hash(doc_id, chunker_version, normalized_chunk_text)`. Insert a paragraph at
the top and only the chunks whose *text* changed get new IDs — typically the one you edited and its
immediate neighbours if the boundary moved. The rest keep their IDs, their vectors, and their index
entries.

```python
import hashlib

def chunk_id(doc_id: str, chunker_version: str, canonical_text: str) -> str:
    """Content-addressed chunk identity.

    - doc_id scopes the ID so identical boilerplate in two documents stays distinct
      (§10 handles cross-document duplication as a separate, deliberate decision).
    - chunker_version makes a chunker change produce a disjoint ID space, so old and
      new chunks coexist during a shadow migration instead of colliding.
    - The b"\\x00" separators prevent concatenation ambiguity: without them,
      ("ab", "c") and ("a", "bc") would hash identically.
    - canonical_text must be the *normalized* text (§4.1) — NFC first, or the same
      logical content produces different IDs depending on its source encoding.
    """
    h = hashlib.sha256()
    for part in (doc_id, chunker_version, canonical_text):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
```

Content-addressing is what makes the whole update path cheap, and its one requirement is a
deterministic chunker — which is precisely what §6.4 and §6.5 warned you that semantic and
LLM-based chunking give up. That is the concrete, operational reason determinism appears in every
decision table in this chapter: it isn't purity, it's the difference between re-embedding one
paragraph and re-embedding a document.

### 9.2 The update algorithm

```python
def reindex_document(doc_id: str, new_chunks: list[Chunk], store) -> dict:
    """Diff-based document update. Cost is proportional to the change, not the document."""
    new_ids = {c.id: c for c in new_chunks}
    old_ids = set(store.chunk_ids_for_doc(doc_id))     # requires a doc_id index — §8.1

    to_add    = [new_ids[i] for i in new_ids.keys() - old_ids]
    to_delete = old_ids - new_ids.keys()
    unchanged = old_ids & new_ids.keys()               # no embed call, no index write

    store.upsert(embed_all(to_add))                    # the only tokens you spend
    store.delete(to_delete)                            # see §9.3 — this is not free either
    return {"added": len(to_add), "deleted": len(to_delete), "unchanged": len(unchanged)}
```

Two properties worth naming, because both are load-bearing:

**Idempotency.** Running this twice on an unchanged document is a no-op: `to_add` and `to_delete`
are both empty. That is what lets you re-run ingestion after a partial failure without duplicating
anything, and it is the property that makes the pipeline safe to retry — which it will be, because
every ingestion pipeline eventually crashes mid-document.

**Deletion is not optional and it is the step people skip.** A chunk removed from a document but
left in the index is a vector that will be retrieved and cited as current, with a source URI that
no longer contains it. That is worse than missing data, because it's *confidently wrong data with a
citation*. The failure mode is documents that shrink — a section deleted from a wiki page whose
chunks live on in the index for months.

### 9.3 What deletion costs in the index

Vector indexes vary enormously here and the ingestion design depends on which you have. Graph-based
indexes typically implement delete as a tombstone — the node stays in the graph so connectivity
survives, and it's filtered from results — with space reclaimed only on a periodic compaction or
rebuild. That means a high-churn corpus accumulates tombstones, which degrade search performance
and inflate storage until compaction runs.
`../databases/11-hnsw-vector-search-internals.md` has the graph mechanics and
`03-indexing-and-vector-stores.md` will have the per-store specifics. What belongs here is the
consequence for chunking: **a chunking strategy with unstable IDs generates delete+insert churn on
every reprocess**, and on a tombstoning index that churn is a slow-motion performance regression
that will not be obvious from any ingestion metric. It shows up as query latency creeping up over
months.

---

## 10. Deduplication

### 10.1 Exact duplicates

`content_hash` over the normalized text (§4.1 — normalize *before* hashing, always). Cheap,
exact, and it catches the enormous amount of literal duplication in real corpora: the same PDF
attached to forty tickets, a wiki page copied into a new space, the same press release on three
domains.

Whether to dedup exact duplicates *across* documents is a real decision, not an obvious one.
Collapsing them shrinks the index and stops one fact from occupying multiple top-k slots. But each
copy may have distinct provenance that matters — different tenant (`16`), different ACL (`17`),
different recency. The pattern that keeps both: **store the vector once, keep a list of source
references on it**, and apply tenant/ACL filtering over that list at query time. If your store
can't express that, dedup within a tenant and accept cross-tenant duplication, which is the safe
direction to be wrong in.

### 10.2 Near-duplicates

Exact hashing misses the common case: a document revised, a boilerplate section with one changed
date, a page syndicated with a different footer. Shingling plus MinHash gives you an estimate of
Jaccard similarity between chunks at a cost that permits corpus-scale comparison via LSH bucketing.

```python
import hashlib, random

_MERSENNE = (1 << 61) - 1

def shingles(text: str, k: int = 5) -> set[str]:
    """Overlapping k-word sequences. Word-level shingles are more robust to
    whitespace and punctuation noise than character-level ones on prose."""
    toks = text.split()
    return {" ".join(toks[i:i + k]) for i in range(max(1, len(toks) - k + 1))}

def minhash(sh: set[str], num_perm: int = 128, seed: int = 0) -> list[int]:
    rng = random.Random(seed)
    params = [(rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE))
              for _ in range(num_perm)]
    sig = [_MERSENNE] * num_perm
    for s in sh:
        hv = int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
        for i, (a, b) in enumerate(params):
            candidate = (a * hv + b) % _MERSENNE
            if candidate < sig[i]:
                sig[i] = candidate
    return sig

def estimated_jaccard(sig_a: list[int], sig_b: list[int]) -> float:
    """Expected value equals the true Jaccard similarity; standard error ~1/sqrt(num_perm)."""
    return sum(x == y for x, y in zip(sig_a, sig_b)) / len(sig_a)
```

The `~1/sqrt(num_perm)` standard error is the number that sets your parameter: 128 permutations
gives roughly ±0.09, which is fine for a 0.9 threshold and useless for distinguishing 0.85 from
0.90. If you need a tighter threshold, pay for more permutations — and if you're comparing every
pair, you need LSH banding rather than the pairwise loop this sketch implies.

### 10.3 The duplication you should *not* remove

Not all repetition is noise:

- **Legitimate boilerplate with legitimate queries.** A licence header in 400 source files is
  duplicated, and "what licence is this file under?" is a real question about each of them.
- **Overlap-induced duplication** (§5.5) is duplication you created on purpose. Removing it at
  ingest defeats the point of the overlap.
- **Quoted email replies** are duplicated across every message in a thread. Collapsing them loses
  the thread structure; keeping them means the same paragraph occupies ten chunks. The usual answer
  is to strip quoted blocks at parse time (§3.1) and index the thread structure as metadata, rather
  than to deduplicate after the fact.

### 10.4 Retrieval-time suppression is the more important half

Ingest-time dedup shrinks the index. **Retrieval-time near-duplicate suppression protects the
context budget**, and that's usually worth more. If your top-10 contains four variants of the same
paragraph, the generator has six distinct pieces of evidence and you paid for ten. Diversity-aware
selection — MMR and its relatives — is `04-retrieval-hybrid-and-reranking.md`'s subject, and it is
the correct place to solve the problem, because it can weigh diversity against relevance with the
query in hand. Ingest-time dedup cannot; it has to decide without knowing what will be asked.

Do both, for different reasons: exact dedup at ingest because it's free and shrinks the index,
near-duplicate suppression at merge time because that's where the budget is actually spent.

---

## 11. Evaluating a chunking strategy

### 11.1 You cannot argue about chunking without a golden set

Every claim in §5 and §6 is corpus-dependent. "512 with 15% overlap" is not an answer; it is
someone else's measurement on someone else's corpus. This is P0 in the README's project ladder and
it is genuinely first — not for tidiness, but because without it every subsequent decision in this
chapter is a guess with a confident tone. `08-evaluation-methodology.md` owns the metrics; what
follows is the part specific to chunking, and it is more subtle than it looks.

### 11.2 The trap: chunk-level labels can't compare chunkings

The standard golden-set format is `(query, [relevant_chunk_ids])`. It is unusable for evaluating
chunking, and the reason is worth stating slowly, because the trap is well-camouflaged:

> Chunk IDs only exist *relative to a chunking*. If you labeled relevance against chunks produced
> by chunker A, those labels do not name anything in chunker B's output. Re-chunking invalidates
> your labels. Any comparison you run after re-chunking is either measuring against stale labels or
> against labels you re-derived using a heuristic — and if that heuristic is "the chunk that
> overlaps the old chunk," you have smuggled chunker A's boundaries into chunker B's score.

**The fix: label spans in the source document, not chunks.**

```python
# Golden set entry — chunking-independent by construction.
{
    "query_id": "q_0147",
    "query": "What was ACME's revenue growth in Q2 2023?",
    "answer_spans": [
        {"doc_id": "acme_10k_2023", "char_start": 48122, "char_end": 48310},
        {"doc_id": "acme_pr_q2",    "char_start":  1044, "char_end":  1190},
    ],
}
```

Offsets are into the **canonical normalized text** (§4.4), which is why §4.4 insisted on keeping it
and on keeping per-chunk offsets. Now any chunking is scorable: a chunk is a hit if its
`[char_start, char_end)` range overlaps an answer span under a stated rule. The rule matters and
must be stated with every number:

| Hit rule | Definition | When it's the right one |
|---|---|---|
| **Any overlap** | chunk ∩ span ≠ ∅ | lenient; a chunk containing one sentence of a three-sentence answer counts |
| **Span containment** | span ⊆ chunk | strict; the chunk alone must carry the whole answer — the right rule when there's no parent expansion (§7) |
| **Coverage ≥ τ** | \|chunk ∩ span\| / \|span\| ≥ τ | tunable middle; state τ |
| **Union coverage ≥ τ** | coverage of the span by the *union* of all retrieved chunks ≥ τ | the honest rule when the answer is legitimately assembled from several chunks |

Union coverage is the one that most closely matches what the generator actually needs, and it is
the one that correctly rewards a chunking that splits an answer into two adjacent retrievable
pieces both of which get retrieved. Pick one, write it down next to every number, and never compare
figures computed under different rules — this is `01` §16's "state your exact hit definition,"
made concrete for chunking.

**This is not a novel idea and you should steal the established version of it.** Chroma's chunking
evaluation arrives at the same conclusion and names the metric: they score at the *token* level
rather than the document level, and define a token-wise **Intersection over Union** between the
tokens of the relevant excerpts (`t_e`) and the tokens of all retrieved chunks (`t_r`):

```
IoU(q) = |t_e ∩ t_r| / |t_e ∪ t_r|

Precision(q)   = |t_e ∩ t_r| / |t_r|      # what fraction of retrieved tokens were relevant
Recall(q)      = |t_e ∩ t_r| / |t_e|      # what fraction of relevant tokens were retrieved
Precision_Ω(q) = precision when *all* chunks containing excerpt tokens are retrieved
                 — i.e. the ceiling this chunking allows, isolated from retriever error
```

Their framing is worth adopting wholesale: **think of chunks as bounding boxes and relevant
excerpts as ground-truth boxes**, exactly as in object detection. It makes the whole problem legible
— a chunking that returns a 660-token chunk to answer a 40-token question has poor localization,
which is a real defect that document-level recall is structurally blind to.

Two details in their definition are load-bearing and easy to get wrong when reimplementing:

- **The numerator counts each relevant token once; the denominator counts every retrieved token.**
  That's what makes overlapping chunkings (§5.5) score honestly instead of double-counting — the same
  reason §11.3's scorer uses a character-level `set` rather than summing intersection lengths.
- **Precision_Ω isolates the chunking from the retriever.** It answers "if retrieval were perfect,
  how good could this chunking be?" — the chunking's own ceiling, which is the quantity you actually
  want when the thing under test is the chunker and not the index. It is the direct analogue of
  `00`'s oracle-context harness, one stage earlier in the pipeline.

### 11.3 The second trap: compare at fixed token budget, not fixed k

§5.3 established that `k` means different things at different chunk sizes. So the metric to compare
chunkings on is not recall@10 — it's **recall at a fixed retrieved-token budget**: retrieve chunks
in rank order until the budget is exhausted, then score.

```python
def recall_at_budget(ranked_chunks, answer_spans, budget_tokens: int, count) -> float:
    """Span-level recall at a fixed context budget — comparable across chunk sizes.

    ranked_chunks: retriever output, best first, each with .doc_id/.start/.end/.text
    answer_spans:  the golden set's labeled spans for this query
    Returns union-coverage-weighted recall: the fraction of labeled answer
    characters covered by the chunks that fit in the budget.
    """
    used, selected = 0, []
    for c in ranked_chunks:
        t = count(c.text)
        if used + t > budget_tokens:
            break                       # strict budget; do not skip-and-continue, that
        used += t                       # silently changes the ranking being evaluated
        selected.append(c)

    total = sum(s.end - s.start for s in answer_spans)
    covered = 0
    for s in answer_spans:
        marks = set()
        for c in selected:
            if c.doc_id == s.doc_id:
                lo, hi = max(c.start, s.start), min(c.end, s.end)
                if lo < hi:
                    marks.update(range(lo, hi))     # set union handles overlapping chunks
        covered += len(marks)
    return covered / total if total else 0.0
```

The `marks` set is doing real work: with overlapping chunks (§5.5) a naive sum of intersection
lengths double-counts, and a chunking with 50% overlap would score above 1.0. Deduplicating at the
character level is what makes overlap-heavy and overlap-free configurations comparable at all.

### 11.4 Recall barely discriminates between chunkings — token efficiency does

This is the most useful thing in Chroma's results and it is not their headline. Look at the spread
across all thirteen configurations in §6.4's table:

| Metric | Worst | Best | Spread |
|---|---|---|---|
| Recall | 83.6 | 91.9 | **8.3 points** — a ~10% relative range |
| Precision / IoU | 1.4 | 8.0 | **5.7×** |
| Precision_Ω | 4.7 | 34.0 | **7.2×** |

Every strategy in the table retrieves the relevant tokens *somewhere* in its top-5 the large
majority of the time. What separates them by most of an order of magnitude is **how much irrelevant
text they drag along** to do it. Recall is close to saturated at n=5 on this evaluation; token
efficiency is not remotely saturated.

Three consequences, and they redirect real effort:

1. **If you evaluate chunking on recall@k alone, you will conclude that chunking barely matters** —
   which is exactly the conclusion a great many teams have reached, and it is an artifact of the
   metric, not a finding about chunking. An 8-point recall spread looks like noise next to a 5.7×
   precision spread that the metric never showed you.
2. **The thing chunking actually controls is the cost side of the quality/cost tradeoff.** Wasted
   retrieved tokens are context budget spent (`06-context-engineering.md`), reranker candidates paid
   for (`04`), generation tokens billed (`11-token-accounting-and-cost.md`), and — per the
   long-context position-bias literature — distraction for the generator. A chunking that holds
   recall flat while tripling IoU is a large win that recall@k reports as zero.
3. **It reframes §5's chunk-size question.** Small chunks in the table don't win on recall; they win
   on precision and IoU, dramatically (Recursive 200/0 at 7.0 precision versus 800/400 at 1.5). That
   is the clean empirical statement of §5.1's C2 dilution argument — and it is also the argument for
   §7's decoupling, which lets you take the small chunk's localization *and* hand the generator the
   surrounding context, instead of choosing.

So: **report IoU or an equivalent token-efficiency measure alongside recall, always.** If you only
have room for one number in a comparison, it should probably not be recall.

### 11.5 Report the whole picture

A chunking change moves at least five numbers, and reporting one is how a strategy that costs 2×
the index for +1pp recall gets adopted:

| Report | Why |
|---|---|
| recall@budget, with hit rule and budget stated | the quality claim |
| **token-level IoU or precision** | §11.4 — the axis that actually separates chunkings, and the one recall hides |
| bootstrap CI on the *delta* vs baseline | `../python-mastery/31-measurement-methodology.md` — a delta without an interval is a coin flip with a decimal point |
| chunk count and total index bytes | §12.2 — the recurring cost |
| ingest cost (parse + embed + any LLM calls) | §12 — the one-time cost |
| p50/p95 chunk token length | reveals the orphan-chunk problem (§6.2) that averages hide, and drives IoU directly |

The standard deviations in §6.4's table are a warning about the CI row: they run 25–40 against
means of 84–92, i.e. per-query variance dwarfs the between-strategy differences. Any chunking
comparison reported without an interval on the *delta* is not interpretable, and this is the
concrete corpus-level reason why.

And the baseline must be a *fair* one: recursive splitting with a separator list matched to your
format (§6.2 — including sentence terminators, which Chroma found was necessary for fairness) and
heading-path prefixes if you have headings (§6.3), not the library default. A large share of
published wins for sophisticated chunking are wins against an unconfigured baseline.

### 11.6 Query-set composition decides the answer

One more confound, and it changes conclusions rather than just widening intervals. Chunk size
interacts with query type:

- **Narrow factoid queries** ("what was the Q2 revenue figure") favour small, precise chunks —
  the answer is one sentence and dilution (C2) is the binding constraint.
- **Synthesis queries** ("how did the company explain the margin decline") favour large chunks or
  parent expansion — the answer spans paragraphs and context (C3) is binding.

A golden set that is 90% factoids will tell you small chunks win. That conclusion is about your
golden set, not your corpus. Stratify the query set by type, report per-stratum, and check whether
the strata disagree — if they do, that's an argument for §7's decoupling or for query-type routing
(`05-query-understanding.md`), not for picking whichever chunk size wins on aggregate.

---

## 12. Cost model for the chunking layer

### 12.1 Ingest cost

```
parse_cost   = pages × per_page_cost(tier)          # §3.3: ~free (tier 1)
                                                    #       → per-page API price (tier 2)
                                                    #       → generation tokens/page (tier 3)
embed_cost   = corpus_tokens × 1/(1 - f) × price_per_token     # f = overlap fraction, §5.5
context_cost = chunks × generated_context_tokens × generation_price   # 01 §9.2, if used
              (+ sentence_embed_cost if semantic chunking — §6.4)
```

The shape worth internalizing: **on most corpora, tier-2/3 parsing and LLM contextualization each
dominate embedding cost, often by a lot.** Embedding is the cheap stage. Teams optimize it because
it's the stage with an obvious per-token price attached, and leave the expensive stages
unexamined because their costs are CPU-seconds and API calls that land on a different line item.

A worked example, with every input labeled as an assumption so the arithmetic is checkable:

> **Assumptions:** 100M-token corpus; 512-token chunks; 15% overlap; embedding at $0.02 per
> million tokens (`text-embedding-3-small`'s list price as of `01` §4 — verify before quoting).
>
> - Overlap inflation: `1/(1-0.15)` = **1.176×**
> - Tokens embedded: 100M × 1.176 = **117.6M** → **$2.35**
> - Stride: 512 − 77 = 435 tokens → chunks ≈ 100M / 435 ≈ **229,900**
>
> Now add contextual retrieval (`01` §9.2) at ~100 generated tokens per chunk: 229,900 × 100 =
> **23M generated tokens**, at generation prices. Even at cheap-model rates that is one to two
> orders of magnitude above the $2.35 embedding bill. `01` §9.2's $1.02/M-document-tokens figure —
> Anthropic's, on Anthropic's assumptions, with prompt caching — implies ~$102 here. Roughly 40×
> the embedding cost, for the same corpus.

That ratio is the argument for doing the free version first (§6.3's heading-path prefix) and
measuring what's left before paying for the LLM version.

### 12.2 Storage cost — chunk size is the dominant lever

Storage is the recurring bill, and chunk size controls it almost linearly:

> **Assumptions:** same 100M-token corpus, 15% overlap, 1536-dimensional float32 vectors
> (4 bytes/component = 6,144 bytes/vector, before index overhead and payload).
>
> | Chunk size | Stride | Chunks | Vector bytes |
> |---|---|---|---|
> | 256 | 218 | ~459,800 | ~2.82 GB |
> | 512 | 435 | ~229,900 | ~1.41 GB |
> | 1024 | 870 | ~114,900 | ~0.71 GB |
>
> Halving chunk size doubles the index. Add HNSW graph overhead and payload on top of all three
> (`../databases/11-hnsw-vector-search-internals.md`, `03-indexing-and-vector-stores.md`).

Three levers compose multiplicatively on that number, and they're worth seeing together:
chunk size (this table, ~2× per halving), MRL dimension truncation (`01` §6), and quantization
(`01` §7 — int8 for 4×, binary for 32×). A 512-token chunking at 1024 dimensions with int8
quantization is ~0.235 GB against the 1.41 GB baseline. **The chunking decision is not separable
from the representation decisions in `01`; they multiply.**

Sentence-window indexing (§7.2) is the pattern to price carefully here: one vector per *sentence*
against one per 512-token chunk is roughly a 20–25× increase in vector count on typical prose. It
often retrieves better. It is not free, and the storage table above is where that shows up.

### 12.3 Reindex cost — the chunker is a schema decision

Changing the chunker costs: re-chunk (free) + re-embed every chunk (§12.1's `embed_cost`, in full)
+ rebuild the index + invalidate every stored `chunk_id` reference anywhere in the system —
citations in saved conversations, eval result rows keyed by chunk, feedback labels, caches.

That is `01` §1's migration argument verbatim, and the last term is the one that bites: chunk IDs
leak into places that aren't the index. Storing citations as `(doc_id, char_start, char_end)`
rather than `chunk_id` makes them survive a re-chunk, which is one more reason §4.4's offsets earn
their two integers.

---

## 13. Anti-patterns

**1. Tuning the embedding model before fixing the parser.**
*Why it's tempting:* the model has a leaderboard and a version number; the parser is a library
someone added in week one and nobody has looked at since.
*What it costs:* money spent representing corrupted text more precisely (§1's ceiling chain), and a
conclusion — "RAG doesn't work on our documents" — that is true only of your parse.
*Instead:* extract 20 documents, read the extracted text with your own eyes, and fix what you see
before touching anything downstream. It is the highest-yield hour available (§15, lab 1).

**2. Accepting the library's default chunk size.**
*Why it's tempting:* it's a working default, and chunk size feels like a detail.
*What it costs:* a schema decision (§12.3) made by a library author who had never seen your corpus,
frequently in the wrong *unit* — characters where you assumed tokens (§5.4).
*Instead:* set it explicitly, in tokens, under your embedding model's tokenizer, and measure at
least three values against a span-labeled golden set (§11).

**3. Chunk-level golden sets used to compare chunking strategies.**
*Why it's tempting:* it's the format every tutorial and every eval library shows.
*What it costs:* a comparison that is structurally incapable of being fair, because the labels are
defined in terms of one of the two things being compared (§11.2).
*Instead:* label character spans in the canonical source text. It costs slightly more to produce
once and is valid forever, across every chunker you will ever try.

**4. Comparing chunk sizes at fixed `k`, on recall alone.**
*Why it's tempting:* recall@10 is *the* standard retrieval metric, and holding `k` fixed *feels*
like the controlled comparison.
*What it costs:* two compounding errors. Fixed `k` means bigger chunks silently get a bigger context
budget, so of course they "win" (§5.3). And recall alone is nearly saturated across chunking
strategies — in Chroma's table it spans 8.3 points while precision spans 5.7× (§11.4) — so the axis
where chunking actually differs is the one you didn't measure. Together they produce the widespread
and false conclusion that chunking doesn't matter much.
*Instead:* fix the retrieved-token budget, not `k` (§11.3), and report token-level IoU next to
recall, with the budget and hit rule stated beside both.

**5. Overlap as the fix for bad boundaries.**
*Why it's tempting:* it's one parameter and it reliably nudges recall up.
*What it costs:* `1/(1-f)` on both embedding bill and storage bill forever (§5.5), plus top-k slots
consumed by near-duplicates of passages already retrieved.
*Instead:* split on structure the author already provided (§6.3); use overlap as the fallback for
corpora that genuinely lack it, and price it before setting it.

**6. Stuffing filterable metadata into the embedded text.**
*Why it's tempting:* "more context is better," and it's one line of string formatting.
*What it costs:* uniform noise added to every vector in the corpus, in exchange for a matching
behaviour dense models don't reliably provide for dates and IDs (§8.2) — and it hides the fact that
what you needed was a filter.
*Instead:* structured attributes to the payload and filter on them; natural-language context (title,
heading path, caption) into the embedded text.

**7. Position-addressed chunk IDs.**
*Why it's tempting:* `f"{doc_id}:{i}"` is the obvious thing to write.
*What it costs:* a one-word edit near the top of a document re-embeds and re-writes the entire
document, and on a tombstoning index that churn compounds into a latency regression nobody traces
back to the ID scheme (§9.1, §9.3).
*Instead:* content-addressed IDs over normalized text, with `chunker_version` in the hash.

**8. Skipping deletes on document update.**
*Why it's tempting:* upsert works, deletes are extra code, and nothing visibly breaks.
*What it costs:* removed content that stays retrievable and gets cited as current, with a source URI
that no longer contains it — confidently wrong output *with a citation*, which is the worst failure
shape available (§9.2).
*Instead:* diff old and new chunk-ID sets per document and issue the deletes; assert the invariant
that `chunk_ids_for_doc(d)` equals the current chunking of `d`.

**9. LLM-based chunking that rewrites text, with citations pointing at the rewrite.**
*Why it's tempting:* proposition-style chunks retrieve beautifully.
*What it costs:* fabricated content injected at ingest, downstream of every guardrail, cited to a
real document that doesn't say it (§6.5). No output-side check catches this.
*Instead:* keep the original span next to any generated text, cite the original, and treat generated
text as a retrieval surrogate — never as the source of record.

**10. Treating parse, chunk, and embed as one irreversible step.**
*Why it's tempting:* a single pipeline that goes from file to vector is simpler to write and looks
cleaner.
*What it costs:* every chunking experiment re-runs the most expensive stage (§12.1), which makes
experiments slow, which means you run fewer of them, which means the chunking is whatever you
guessed first.
*Instead:* persist each stage's output to object storage and version each stage independently
(§2). It is the cheapest architectural decision in the pipeline and it's what makes §15's labs
affordable to run more than once.

---

## 14. Mental models — the compressed set

1. **Parsing sets the ceiling; chunking decides how much of it you reach; the model only decides
   how well you exploit what's left.** Improvement effort should follow that order, and it usually
   follows the reverse (§1).
2. **PDF is a print format. Everything above the glyph level is reconstructed by heuristics** —
   which is why reading order, tables, and headers fail the way they do, and why "the extractor is
   buggy" is usually "the information was never in the file as structure" (§3.2).
3. **A scanned PDF that extracts to the empty string produces zero vectors and zero errors.**
   Extraction-yield assertions are a data-loss check, the same argument as `01` §8's
   silent-truncation case, one stage earlier (§3.2).
4. **Repeat the header row in every chunk of a split table.** Free, deterministic, and it converts
   unlabeled number grids into interpretable chunks (§3.4).
5. **The dense and lexical branches want different text; keep one canonical form and derive both
   from it.** Letting the analyzer's output become the embedded text is a silent hybrid-search
   regression (§4.3).
6. **Chunk size is squeezed by four constraints, two from each side** — the model's context limit
   and pooling dilution pushing down, interpretability and per-chunk overhead pushing up. There is
   no universal answer, only your corpus's (§5).
7. **Chunk size and `k` are one decision.** Changing chunk size at fixed `k` changes the context
   budget, so any result attributed to chunk size alone is confounded (§5.3, §11.3).
8. **Overlap costs `1/(1-f)`, not `f`** — 20% overlap is 25% more chunks, tokens, and bytes, forever
   (§5.5).
9. **A section heading is a boundary the author already drew.** Structure-aware splitting plus a
   prepended heading path is the free version of contextual retrieval and belongs in the baseline,
   not in the comparison set (§6.3).
10. **Determinism is not aesthetic — it's what makes content-addressed IDs work**, and
    content-addressed IDs are what make document updates cost proportional to the edit rather than
    to the document (§6.4, §6.5, §9.1).
11. **The retrieval unit and the generation unit don't have to be the same object**, and separating
    them dissolves the C2/C3 conflict for roughly the cost of a `parent_id` column (§7).
12. **Chunk-level relevance labels cannot compare two chunkings, because chunk IDs only exist
    relative to a chunking.** Label spans in the canonical text; state the hit rule with every
    number (§11.2). Token-level IoU is the established form of this — chunks as bounding boxes,
    relevant excerpts as ground truth.
13. **Recall barely separates chunking strategies; token efficiency separates them by an order of
    magnitude.** In Chroma's table the recall spread is 8.3 points and the precision spread is 5.7×.
    Evaluate on recall alone and you'll conclude chunking doesn't matter — which is a fact about the
    metric, not about chunking (§11.4).
14. **A chunker that can't control its own chunk size has a correctness problem before it has a
    quality problem** — it cannot guarantee chunks fit the embedding model's context window. That,
    not the semantic idea, is why the default semantic chunker came last on recall (§6.4).

---

## 15. Lab exercises

Every lab produces an artifact and a number. Every number produced here is **rung 1 — measured**
(README §6): quote it with its corpus, its size, its hit rule, and its token budget, every time, or
don't quote it. This document stays **rung 3 — studied** until these have been run against a real
corpus.

**Lab 1 — Read your own extracted text.**
*Goal:* find out what your parser is actually doing before optimizing anything downstream of it.
*Steps:* take 20 documents representative of your corpus (include the ugliest ones, not the
cleanest). Extract text with your current parser. Read all 20 extractions by eye. Tally, per
document, which of §3.2's failure modes are present. Then calibrate the §3.2 gate thresholds
(`extraction_yield`, `script_sanity`) so they separate the documents you judged broken from the ones
you judged fine.
*Artifact:* a failure-mode tally table plus two calibrated thresholds with a one-line justification
each.
*Success criterion:* you can state which failure modes affect what fraction of your sample, and your
gates flag the broken documents without flagging the good ones.
*Time:* ~1–2 hours.
*Unblocks:* everything else in this chapter. Do this one first.

**Lab 2 — Parser tier A/B.**
*Goal:* measure whether a better parser is worth its cost on your corpus, rather than assuming
either way.
*Steps:* parse the same 200-document sample at tier 1 (geometric) and tier 2 (layout model) per
§3.3. Hold chunking, embedding, and retrieval identical. Run your golden set (lab 4) against both
and compare recall@budget. Record wall-clock parse time and any per-page API cost for each tier.
*Artifact:* a two-row table — recall@budget, parse cost, parse latency — plus a stated verdict.
*Success criterion:* a defensible tier decision with the recall delta and the cost delta both
written down, including "tier 1 was sufficient" as a valid and valuable outcome.
*Time:* ~3–4 hours.
*Unblocks:* the whole ingestion cost model (§12.1).

**Lab 3 — Measure the overlap inflation on your real corpus.**
*Goal:* replace §5.5's formula with your own numbers, and confirm the formula holds.
*Steps:* chunk your corpus at a fixed size with overlap fractions of 0%, 10%, 20%, and 30%. Record
chunk count and total embedded tokens at each. Compare the measured ratios against the predicted
`1/(1-f)`.
*Artifact:* a four-row table of measured vs predicted inflation, plus the dollar cost of each
configuration at your embedding model's price.
*Success criterion:* measured ratios within a few percent of predicted (deviation comes from
document-boundary effects on short documents — explain any large gap).
*Time:* ~1 hour.
*Unblocks:* §12's cost model, and P0's budget.

**Lab 4 — Build a span-labeled golden set.**
*Goal:* produce the artifact that makes every other comparison in this chapter valid.
*Steps:* pick 50 real queries. For each, find the answer in the source documents and record
`(doc_id, char_start, char_end)` against the **canonical normalized text** — not against a chunking.
Write the loader and the `recall_at_budget` scorer from §11.3. Pick and document your hit rule
(§11.2) and your token budget. Add a token-level IoU scorer alongside recall (§11.4) — it is ten
extra lines and it is the metric that will actually separate your candidates. Stratify the queries
as factoid vs synthesis (§11.6).
*Artifact:* a versioned golden-set file, a scorer, and a written statement of hit rule + budget.
*Success criterion:* the same golden set scores two structurally different chunkings without
modification — that's the property that makes it worth building this way.
*Time:* ~1 day, dominated by labeling.
*Unblocks:* labs 5–8, all of `08-evaluation-methodology.md`, and P0.

**Lab 5 — The chunk-size sweep, done correctly.**
*Goal:* find your corpus's chunk size, with the confounds from §5.3 and §11.3 actually controlled.
*Steps:* chunk at 256 / 512 / 1024 tokens with overlap held fixed. Score each with
`recall_at_budget` **and IoU** at a *fixed token budget* (§11.3), not fixed `k`. Report per-stratum
(§11.6). Bootstrap a confidence interval on each delta versus the 512 baseline
(`../python-mastery/31-measurement-methodology.md`).
*Artifact:* a table of recall@budget **and IoU** with CIs, chunk counts, and index bytes per
configuration, split by query stratum.
*Success criterion:* a chunk size chosen for a stated reason, with the interval reported — and if
the recall intervals overlap, check whether IoU separates them before concluding "no difference"
(§11.4 predicts it usually will). If neither separates them, the honest conclusion is "no measurable
difference, pick the cheaper one."
*Time:* ~half a day given lab 4.
*Unblocks:* `03-indexing-and-vector-stores.md` (chunk count is the index sizing input).

**Lab 6 — Heading-path prefix versus LLM contextualization.**
*Goal:* find out how much of `01` §9.2's gain the free version (§6.3) already captures on your
corpus.
*Steps:* three configurations at matched chunk size and budget — (a) raw chunks, (b) chunks with
document title + heading path prepended, (c) chunks with LLM-generated context prepended per `01`
§9.2. Score all three. Record actual dollar cost of (c).
*Artifact:* a three-row recall table plus the measured cost of (c), with the (b)→(c) delta
isolated.
*Success criterion:* a defensible answer to "is the LLM contextualization worth it *given* we
already do heading paths?" — the comparison almost nobody runs, because (b) is usually missing from
the baseline.
*Time:* ~3 hours plus LLM API cost.
*Unblocks:* the §12.1 cost decision, and `08`.

**Lab 7 — Parent-document retrieval.**
*Goal:* measure the §7 decoupling, which §12.2 says is nearly free.
*Steps:* index small children (say 256 tokens) with `parent_id` pointing at their enclosing section.
At query time, retrieve children, map to parents, dedup parents, and fill the same token budget as
your baseline. Compare against a flat chunking at the budget-equivalent size. Count how often
distinct children mapped to the same parent.
*Artifact:* a recall@budget comparison plus the child→parent collapse ratio.
*Success criterion:* a measured verdict, including the operational cost — extra store lookups per
query and the p95 latency they add.
*Time:* ~4 hours.
*Unblocks:* `06-context-engineering.md`, `04-retrieval-hybrid-and-reranking.md`.

**Lab 8 — Near-duplicate census.**
*Goal:* find out how much of your top-k is redundant, which is the number that decides whether §10.4
is worth building.
*Steps:* run §10.2's MinHash over your chunks. Report the corpus-level near-duplicate rate at a
stated Jaccard threshold. Then, for your golden-set queries, measure the mean number of *distinct*
passages in the top-10 after collapsing near-duplicates — the gap between that and 10 is context
budget you're currently wasting.
*Artifact:* corpus near-dup rate plus a "distinct passages in top-10" figure with its threshold.
*Success criterion:* a number that makes the MMR decision in `04` an evidence-based one instead of a
default.
*Time:* ~3 hours.
*Unblocks:* `04-retrieval-hybrid-and-reranking.md`.

**Lab 9 — Incremental update rehearsal.**
*Goal:* prove §9's diff path works and is actually proportional to the edit.
*Steps:* implement content-addressed IDs (§9.1) and the diff-based update (§9.2). Take a document,
edit one paragraph near the *top*, reprocess, and count added/deleted/unchanged chunks. Repeat with
position-addressed IDs to see the difference. Then reprocess an unchanged document twice and assert
zero writes. Finally, delete a section and verify its vectors are gone from the index.
*Artifact:* a three-scenario table (edit / no-op / delete) × two ID schemes, with add/delete/unchanged
counts.
*Success criterion:* content-addressed edit touches a handful of chunks where position-addressed
touches the whole document; the no-op case writes nothing; the deleted section is unretrievable.
*Time:* ~3 hours.
*Unblocks:* `15-ingestion-pipelines-and-freshness.md`.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — how PDF content streams work,
what recursive splitting does, what MinHash estimates, how content-addressed IDs behave under edits
— are verifiable from primary sources and from the code in this chapter, and the arithmetic in §5.5
and §12 is derivable rather than measured (every input to it is labeled as an assumption, and every
output is checkable with a calculator).

The measured figures in §5.5, §6.4 and §11.4 are **someone else's rung 1** — Chroma's *Evaluating
Chunking Strategies for Retrieval* (Smith & Troynikov, 2024), read from the report itself and
reproduced with its conditions attached: their synthetic multi-domain evaluation set, `n=5`
retrieved chunks, `text-embedding-3-large` (and a second table for `all-MiniLM-L6-v2`), cl100k
token sizes, standard deviations of 25–40. Quote them that way or not at all. Their conclusions
differ between the two embedding models, which is itself the argument for measuring on your own
corpus rather than adopting their ranking. Anthropic's contextual-retrieval figures carried over
from `01` §9.2 have the same status and the same caveat.

Deliberately **not** in this document: any olmOCR-Bench or parser leaderboard score (§3.3), because
I did not verify those against primary sources — the tool names are the durable part, the scores
are not. And no claim anywhere that strategy X beats strategy Y *on your corpus*, because that
measurement is §15's job, not this document's.

That restraint matters more here than in most chapters: chunking has the highest ratio of confident
published numbers to reproducible ones in this whole track, largely because of §11.2's labeling
trap — most comparisons are scored against labels defined by one of the strategies being compared.

The labs in §15 are what convert this to **rung 1 — measured**, and their outputs must always travel
with their corpus, their size, their hit rule, and their token budget attached — the same discipline
`../python-mastery/31-measurement-methodology.md` applies to every timing claim in this repo, and
the same one `01` §16 applies to every recall claim. A recall number without those four attributes
is not a number worth keeping.
