"""Stage 4 — splitting. The four strategies of `02` §6, ordered by structure used.

The ordering in this file is the chapter's and it is not about sophistication. Each
strategy uses strictly more information about the document than the one before it, and
that — not cleverness — is what predicts whether it will help you, because **a strategy
can only exploit structure the parser actually recovered** (§1's ceiling chain).

    fixed        → nothing.        The mandatory baseline (§6.1).
    recursive    → separators.     The sane default; the separator list *is* the
                                   strategy (§6.2).
    structural   → the parse tree. Highest value per unit of effort (§6.3).
    parent_child → the parse tree, twice. Dissolves the C2/C3 conflict (§7.1).

Semantic (§6.4) and LLM-based (§6.5) chunking are deliberately **not** implemented
here, and their absence is a position rather than an omission. Both require a model
call per document, which would make this lab depend on an API key and a network; both
give up determinism, which is what makes §9's content-addressed IDs work; and the
published evidence in §6.4 has the default semantic chunker coming *last* on recall.
`bakeoff.py` runs LangChain's `SemanticChunker` if you install it, which is the honest
way to look at it: as a hypothesis to test against `structural`, not as an upgrade.

Everything here splits on **offsets into the canonical text**, never on strings. A
chunk is a `Span`, and its text is a slice. That is what keeps §4.4's promise — every
chunk can point back at exactly the characters it came from — and it is what lets the
sibling `golden-set` lab score any of these chunkings against the same span labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from normalize import CanonicalDoc, Span
from parse import CODE, HEADING, TABLE, Element
from tables import Table, split_with_headers, to_markdown, to_row_sentences

CHUNKER_VERSION = "split-v1"

# §6.2, and the single most valuable configuration change in this chapter. Chroma's
# evaluation could not use the library default at all: without sentence terminators, a
# document whose paragraphs exceed the chunk size falls straight through to splitting
# on " ", i.e. mid-sentence at an arbitrary word. The sentence tier is the missing rung.
SEPARATORS = ["\n\n", "\n", ". ", "? ", "! ", "; ", " ", ""]

# What the library ships. Kept so the lab can A/B them rather than assert the
# difference (§15, lab 5's spirit applied to the separator list).
LIBRARY_DEFAULT_SEPARATORS = ["\n\n", "\n", " ", ""]


# --------------------------------------------------------------------------------
# Tokenization (§5.4) — count tokens, not characters
# --------------------------------------------------------------------------------

# A cl100k-style pre-tokenizer. Python's `re` has no \p{L}, so this approximates the
# published pattern with Unicode-aware \w. Pre-tokens are then charged a token cost by
# script, because BPE merges a common Latin word (plus its leading space) into a single
# token, splits digit runs every 1-3 characters, and barely merges CJK at all.
_PRETOKEN = re.compile(
    r"'(?:[sdmt]|ll|ve|re)|[^\r\n\w]?\w+|[^\s\w]+|\s+", re.UNICODE
)
_CJK = re.compile(r"[　-鿿豈-﫿＀-￯]")


# Characters per estimated token, by pre-token class. These are **calibrated against
# real cl100k**, not guessed: `bakeoff.py`'s tokenizer section sweeps them against
# `tiktoken` over this corpus. The first version of this class used 4/2/1 — the
# "4 characters per token" rule of thumb — and over-counted by **65% on average**,
# because that rule describes *whole prose* and not individual pre-tokens: a common
# word plus its leading space is one BPE token whether it is 3 characters or 9.
#
# At 11/2/3 the mean absolute error over this corpus is ~8.8%, with the worst cases
# +24% on Python source and -16% on CSV. Those outliers are not noise to be tuned
# away — they are §5.4's actual point, that characters-per-token is a property of
# content type, and no single constant describes prose and code at once.
_ALPHA_CHARS_PER_TOKEN = 11
_DIGIT_CHARS_PER_TOKEN = 2  # BPE splits long digit runs into 1-3 digit groups
_PUNCT_CHARS_PER_TOKEN = 3


class Tokenizer:
    """Token spans over a string. Swap this for `tiktoken` and nothing else changes.

    **This is an estimator, not a tokenizer, and the lab says so everywhere it prints
    a token count.** It exists so the lab runs with no dependencies at all. Its error
    against cl100k is measured rather than asserted — run:

        .venv/bin/python bakeoff.py --only tokenizer

    and you get a per-document error table. On this corpus it is ~8.8% mean — but the
    mean hides the shape: prose lands within a few percent, while Python source runs
    +24% and CSV runs -16%. That is enough to demonstrate that characters-per-token is
    a property of content type, and **not** enough to set a production chunk size: a
    24% underestimate against a context limit is a truncation waiting to happen
    (§5.1's C1). Use `tiktoken`, or the tokenizer of the model you actually embed with.
    """

    name = "heuristic-cl100k-approx"

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Character spans, one per estimated token. Exact and monotonic by design.

        Returning spans rather than a count is what lets `fixed()` cut on a token
        boundary and still report exact character offsets (§4.4). A tokenizer that only
        returns a count forces the splitter back onto characters, which is the very
        thing §5.4 says not to do.
        """
        out: list[tuple[int, int]] = []
        for match in _PRETOKEN.finditer(text):
            start, end = match.span()
            piece = match.group()
            if piece.isspace():
                if "\n" in piece:
                    out.append((start, end))  # newlines are tokens; space runs mostly aren't
                continue
            if _CJK.search(piece):
                # CJK is roughly one token per character, and often more than one for
                # rarer glyphs. This is where a Latin-calibrated estimator is worst.
                out.extend((start + i, start + i + 1) for i in range(len(piece)))
                continue
            body = piece.strip()
            if body.isdigit():
                step = _DIGIT_CHARS_PER_TOKEN
            elif body.isalpha() or body.isalnum():
                step = _ALPHA_CHARS_PER_TOKEN
            else:
                step = _PUNCT_CHARS_PER_TOKEN
            i = start
            while i < end:
                out.append((i, min(i + step, end)))
                i += step
        return out

    def count(self, text: str) -> int:
        return len(self.spans(text))


DEFAULT_TOKENIZER = Tokenizer()


# --------------------------------------------------------------------------------
# The chunk
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One unit of retrieval, defined as a span of the document's canonical text.

    Three text fields, and keeping them distinct is §4.3's whole argument:

    - `text` is the canonical slice — stored, cited, shown to a user, hashed for
      identity. It is exactly `canonical_text[span.start:span.end]`, always.
    - `embed_text` is what goes to the embedding model: the canonical text plus any
      prepended context (heading path, table caption, enclosing class signature).
    - the lexical form is *derived at index time* by `normalize.analyze()` and
      deliberately not stored here, so it can never be mistaken for the canonical text.
    """

    doc_id: str
    span: Span
    text: str
    embed_text: str
    kind: str = "paragraph"
    heading_path: tuple[str, ...] = ()
    page: int | None = None
    parent_span: Span | None = None
    strategy: str = ""
    meta: dict = field(default_factory=dict, compare=False)

    def tokens(self, tokenizer: Tokenizer = DEFAULT_TOKENIZER) -> int:
        return tokenizer.count(self.embed_text)


def contextualize(text: str, heading_path: tuple[str, ...], doc_title: str = "") -> str:
    """Prepend the heading path. Deterministic, free, no LLM (§6.3).

    The document already told you this. Compare to `01` §9.2's contextual retrieval:
    same effect on the failing example, no generation call, fully deterministic — and
    therefore no chunk-ID churn on reprocessing (§9.1).

    It is not a strict replacement: an LLM-generated context can pull in a fact stated
    elsewhere in the document body ("the previous quarter's revenue was $314 million")
    and this cannot. It is the thing to do *first*, so that the LLM version's measured
    improvement is measured against a fair baseline rather than a straw man.
    """
    trail = [p for p in (doc_title, *heading_path) if p]
    return f"{' > '.join(trail)}\n\n{text}" if trail else text


# --------------------------------------------------------------------------------
# §6.1 — Fixed-size
# --------------------------------------------------------------------------------


def fixed(
    doc: CanonicalDoc,
    *,
    size: int = 256,
    overlap: int = 0,
    tokenizer: Tokenizer = DEFAULT_TOKENIZER,
) -> list[Chunk]:
    """Cut every `size` tokens with `overlap`, ignoring all structure.

    Its virtues are real: exactly predictable cost and chunk count, perfectly
    deterministic, trivially parallel. Its vice is that it cuts mid-sentence and
    mid-table with no awareness of doing so.

    This is the **mandatory baseline** (§6.1). Every strategy below should be measured
    against it at matched token budget, and a surprising number of published strategies
    fail to beat it.
    """
    if overlap >= size:
        raise ValueError("overlap must be strictly less than size (§5.5: stride > 0)")
    spans = tokenizer.spans(doc.text)
    stride = size - overlap
    out: list[Chunk] = []
    for i in range(0, len(spans), stride):
        window = spans[i : i + size]
        if not window:
            break
        span = Span(window[0][0], window[-1][1])
        text = doc.text[span.start : span.end]
        out.append(Chunk(doc.doc_id, span, text, text, "fixed", strategy="fixed"))
        if i + size >= len(spans):
            break
    return out


def overlap_inflation(chunk_tokens: int, overlap_tokens: int) -> float:
    """Multiplier on both embedding cost and index size, from overlap alone (§5.5).

    `1/(1-f)`, not `f`. 20% overlap costs 25% more, on the one-time embedding bill and
    on the recurring storage bill, forever. At 50% it doubles the index.
    """
    stride = chunk_tokens - overlap_tokens
    if stride <= 0:
        raise ValueError("overlap must be strictly less than chunk size")
    return chunk_tokens / stride


# --------------------------------------------------------------------------------
# §6.2 — Recursive
# --------------------------------------------------------------------------------


def recursive(
    doc: CanonicalDoc,
    *,
    max_tokens: int = 256,
    separators: list[str] | None = None,
    min_tokens: int = 24,
    tokenizer: Tokenizer = DEFAULT_TOKENIZER,
) -> list[Chunk]:
    """Split on the most meaningful separator that yields pieces under the limit.

    Two things follow from the implementation that do not follow from the docs:

    - **It is greedy, not balanced.** It packs each chunk as full as it can before
      starting the next, so the final chunk of a document is frequently a 40-token
      orphan. Those orphans embed poorly and clutter the index. `min_tokens` runs the
      post-pass that merges them, which §6.2 says is worth the ten lines — this lab
      measures how many it catches (`orphan_rate` in `report.py`).
    - **The separator list is the entire strategy.** The default encodes an assumption
      that paragraphs are delimited by blank lines: true of Markdown, false of
      PDF-extracted text where paragraph breaks may be single newlines or nothing at
      all, and false of code. Pass `LIBRARY_DEFAULT_SEPARATORS` to see the difference.
    """
    seps = separators if separators is not None else SEPARATORS
    offsets = _recursive_offsets(doc.text, 0, len(doc.text), seps, max_tokens, tokenizer)
    offsets = _merge_orphans(doc.text, offsets, min_tokens, max_tokens, tokenizer)
    return [
        Chunk(
            doc.doc_id,
            Span(s, e),
            doc.text[s:e],
            doc.text[s:e],
            "recursive",
            strategy="recursive",
        )
        for s, e in offsets
        if doc.text[s:e].strip()
    ]


def _recursive_offsets(
    text: str, lo: int, hi: int, seps: list[str], max_tokens: int, tokenizer: Tokenizer
) -> list[tuple[int, int]]:
    if lo >= hi:
        return []
    if tokenizer.count(text[lo:hi]) <= max_tokens:
        return [(lo, hi)]
    if not seps:
        return [(lo, hi)]  # indivisible; the caller decides whether that is an error

    sep, rest = seps[0], seps[1:]
    if sep == "":
        # Character-level fallback. Reached only by a single token longer than the
        # budget — a base64 blob, a minified line. Chunking it is meaningless, which
        # is a signal the *parser* should have excluded it.
        step = max(1, (hi - lo) // max(1, tokenizer.count(text[lo:hi]) // max_tokens + 1)) or 1
        return [(i, min(i + step, hi)) for i in range(lo, hi, step)]

    pieces: list[tuple[int, int]] = []
    cursor = lo
    while cursor < hi:
        found = text.find(sep, cursor, hi)
        if found == -1:
            pieces.append((cursor, hi))
            break
        # The separator stays with the piece that precedes it, so concatenating every
        # chunk's span reproduces the document exactly. Dropping separators makes the
        # spans non-contiguous and quietly breaks offset-based citation.
        pieces.append((cursor, found + len(sep)))
        cursor = found + len(sep)

    out: list[tuple[int, int]] = []
    buf_start: int | None = None
    buf_end = lo
    for start, end in pieces:
        candidate_start = buf_start if buf_start is not None else start
        if tokenizer.count(text[candidate_start:end]) <= max_tokens:
            buf_start, buf_end = candidate_start, end
            continue
        if buf_start is not None:
            out.append((buf_start, buf_end))
            buf_start = None
        if tokenizer.count(text[start:end]) <= max_tokens:
            buf_start, buf_end = start, end
        else:
            out.extend(_recursive_offsets(text, start, end, rest, max_tokens, tokenizer))
    if buf_start is not None:
        out.append((buf_start, buf_end))
    return out


def _merge_orphans(
    text: str,
    offsets: list[tuple[int, int]],
    min_tokens: int,
    max_tokens: int,
    tokenizer: Tokenizer,
) -> list[tuple[int, int]]:
    """Fold undersized chunks into a neighbour, without exceeding the size budget.

    Merging forward first keeps a heading-like fragment attached to what it introduces,
    which is usually what the reader means; merging backward is the fallback for the
    final chunk of a document, which is the orphan the greedy loop reliably produces.
    """
    if min_tokens <= 0 or not offsets:
        return offsets
    out = list(offsets)
    i = 0
    while i < len(out):
        s, e = out[i]
        if tokenizer.count(text[s:e]) >= min_tokens or len(out) == 1:
            i += 1
            continue
        if i + 1 < len(out) and tokenizer.count(text[s : out[i + 1][1]]) <= max_tokens:
            out[i : i + 2] = [(s, out[i + 1][1])]
        elif i > 0 and tokenizer.count(text[out[i - 1][0] : e]) <= max_tokens:
            out[i - 1 : i + 1] = [(out[i - 1][0], e)]
            i -= 1
        else:
            i += 1
    return out


# --------------------------------------------------------------------------------
# §6.3 — Structure-aware
# --------------------------------------------------------------------------------


def structural(
    doc: CanonicalDoc,
    elements: list[Element],
    *,
    max_tokens: int = 256,
    min_tokens: int = 24,
    doc_title: str = "",
    table_rows_per_chunk: int = 8,
    tokenizer: Tokenizer = DEFAULT_TOKENIZER,
) -> list[Chunk]:
    """Use the document's own hierarchy as the boundary set.

    The highest-value strategy per unit of effort on any corpus that has structure, for
    a reason worth stating explicitly: **a section boundary is a semantic boundary the
    author already drew for you.** Semantic chunking (§6.4) spends an embedding pass
    per sentence trying to infer boundaries the author annotated in the source.

    Three behaviours here that the flat strategies structurally cannot have:

    1. **Heading paths are prepended to `embed_text`** — §6.3's free contextualization.
    2. **Atomic elements are never split internally.** A code block cut in half is the
       §3 parsing failure reintroduced by the chunker; a table cut between its header
       and its rows is §3.4's unlabeled number grid.
    3. **Oversized tables are split by row, with the header repeated in every piece**
       (§3.4, mental model 4) — free, deterministic, and it converts a dozen unlabeled
       number grids into a dozen interpretable chunks.
    """
    if len(elements) != len(doc.element_spans):
        raise ValueError(
            f"element/span mismatch ({len(elements)} vs {len(doc.element_spans)}) — "
            "build_canonical() drops empty elements; filter them the same way"
        )

    out: list[Chunk] = []
    heading_path: list[str] = []
    buffer: list[tuple[Element, Span]] = []

    def budget() -> int:
        """The size limit for the *sliced* text, after paying for the heading prefix.

        This is a correctness fix, not a refinement, and the first version of this
        function got it wrong in the way that is easiest to get wrong: it measured the
        canonical slice against `max_tokens` and *then* prepended the heading path,
        so every chunk in a deeply-nested section exceeded the budget by the length of
        its own breadcrumb. On this corpus that produced chunks of 257, 258 and 278
        tokens against a 256 limit.

        Silently over-budget chunks are not a cosmetic problem. `max_tokens` exists to
        respect the embedding model's context limit (§5.1's C1), which is a **hard
        bound**: exceed it and most vendors truncate without an error, storing a vector
        for the first part of the chunk that ranks exactly like a complete one (`01`
        §8). The budget must therefore be checked against the text you actually embed.
        """
        prefix = contextualize("", tuple(heading_path), doc_title)
        return max(16, max_tokens - tokenizer.count(prefix))

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        span = Span(buffer[0][1].start, buffer[-1][1].end)
        text = doc.text[span.start : span.end]
        path = tuple(heading_path)
        out.append(
            Chunk(
                doc.doc_id,
                span,
                text,
                contextualize(text, path, doc_title),
                "section",
                path,
                page=buffer[0][0].page,
                strategy="structural",
            )
        )
        buffer = []

    for element, span in zip(elements, doc.element_spans):
        if element.kind == HEADING:
            flush()
            level = max(1, element.level)
            del heading_path[level - 1 :]
            heading_path.append(element.text)
            continue

        if element.kind == TABLE and element.meta.get("table") is not None:
            flush()
            out.extend(
                _table_chunks(
                    doc, element, span, tuple(heading_path), doc_title,
                    max_tokens, table_rows_per_chunk, tokenizer,
                )
            )
            continue

        text = doc.text[span.start : span.end]
        if element.atomic:
            flush()
            context = element.meta.get("context", "")
            embed = contextualize(
                f"{context}\n{text}" if context else text, tuple(heading_path), doc_title
            )
            out.append(
                Chunk(
                    doc.doc_id, span, text, embed, element.kind, tuple(heading_path),
                    page=element.page, strategy="structural",
                    meta={"symbol": element.meta.get("symbol", "")},
                )
            )
            continue

        candidate = Span(buffer[0][1].start if buffer else span.start, span.end)
        if buffer and tokenizer.count(doc.text[candidate.start : candidate.end]) > budget():
            flush()
        buffer.append((element, span))

        # A single element over budget still has to be split, and recursive splitting
        # is the right fallback: it is the strategy that uses the next-most structure.
        if tokenizer.count(doc.text[span.start : span.end]) > budget():
            buffer = []
            for s, e in _recursive_offsets(
                doc.text, span.start, span.end, SEPARATORS, budget(), tokenizer
            ):
                piece = doc.text[s:e]
                if piece.strip():
                    out.append(
                        Chunk(
                            doc.doc_id, Span(s, e), piece,
                            contextualize(piece, tuple(heading_path), doc_title),
                            "paragraph", tuple(heading_path), page=element.page,
                            strategy="structural",
                        )
                    )

    flush()
    return _merge_small_chunks(out, min_tokens, max_tokens, doc, tokenizer)


def _table_chunks(
    doc: CanonicalDoc,
    element: Element,
    span: Span,
    heading_path: tuple[str, ...],
    doc_title: str,
    max_tokens: int,
    rows_per_chunk: int,
    tokenizer: Tokenizer,
) -> list[Chunk]:
    """Index row-wise sentences, carry the full table for the generator (§3.4, §7.1).

    This is parent-document retrieval applied to a table, and §3.4 calls it one of the
    cleanest wins available on a table-heavy corpus. Each row sentence is a
    self-contained factual statement — the shape dense retrievers are trained on — and
    `meta["parent_table"]` holds the Markdown the generator should actually receive.
    """
    table: Table = element.meta["table"]
    context = " > ".join([p for p in (doc_title, *heading_path) if p])
    full = Table(table.header, table.rows, table.caption, context)
    parent_markdown = to_markdown(full)

    # Derive the split size from the token budget rather than taking a row count on
    # faith: a table with five short columns and one with twenty wide ones do not fit
    # the same number of rows, and `rows_per_chunk` is the cap, not the target. This is
    # §5.4's "count tokens, not units" applied one level up from characters.
    if full.rows:
        sample = to_markdown(Table(full.header, full.rows[:1], full.caption, context))
        per_row = max(1, tokenizer.count(sample) - tokenizer.count(to_markdown(
            Table(full.header, [], full.caption, context))))
        fits = max(1, (max_tokens - tokenizer.count(context)) // per_row)
        rows_per_chunk = max(1, min(rows_per_chunk, fits))

    row_spans = _row_spans(doc, span, len(full.rows))

    out: list[Chunk] = []
    row_index = 0
    for part in split_with_headers(full, rows_per_chunk):
        for sentence in to_row_sentences(part):
            # Each row cites its OWN characters, not the whole table's. Pointing every
            # row at the table span looks harmless and breaks two things at once:
            # citation highlights the entire table for a question about one row, and
            # `content_hash(chunk.text)` becomes identical across every row, so exact
            # dedup collapses a six-row table into one chunk. On this corpus that
            # showed up as 41 "duplicate" chunks that were nothing of the kind.
            row_span = row_spans[row_index] if row_index < len(row_spans) else span
            out.append(
                Chunk(
                    doc.doc_id,
                    row_span,
                    doc.text[row_span.start : row_span.end],
                    sentence,
                    "table_row",
                    heading_path,
                    page=element.page,
                    parent_span=span,  # the parent is still the whole table (§7.1)
                    strategy="structural",
                    meta={"parent_table": parent_markdown, "header": tuple(part.header)},
                )
            )
            row_index += 1
    if not out:
        text = doc.text[span.start : span.end]
        out.append(
            Chunk(
                doc.doc_id, span, text, contextualize(text, heading_path, doc_title),
                TABLE, heading_path, page=element.page, strategy="structural",
            )
        )
    return out


def _row_spans(doc: CanonicalDoc, table_span: Span, n_rows: int) -> list[Span]:
    """Locate each data row's own character range inside the table's canonical text.

    A serialized table is header line, separator line, then one line per row, so the
    data rows are the lines after the separator. When that shape does not hold — a
    table whose canonical text was reflowed, or a serialization this function does not
    recognise — it returns an empty list and the caller falls back to the table span.
    Guessing a row boundary would produce citations that point at the wrong number,
    which is worse than pointing at the whole table.
    """
    lines: list[Span] = []
    cursor = table_span.start
    for line in doc.text[table_span.start : table_span.end].split("\n"):
        lines.append(Span(cursor, cursor + len(line)))
        cursor += len(line) + 1

    data = [
        s
        for s in lines
        if doc.text[s.start : s.end].strip().startswith("|")
        and not re.fullmatch(r"\|[\s:|-]+\|", doc.text[s.start : s.end].strip())
    ]
    # Drop the header line; what remains should be exactly the data rows.
    return data[1:] if len(data) - 1 == n_rows else []


def _merge_small_chunks(
    chunks: list[Chunk], min_tokens: int, max_tokens: int, doc: CanonicalDoc, tokenizer: Tokenizer
) -> list[Chunk]:
    """Merge undersized *section* chunks. Atomic and table-row chunks are left alone.

    A 12-token table row is not an orphan — it is the unit, and it is small on purpose.
    Applying a blanket minimum size would undo §3.4's entire point, which is the kind
    of interaction between two individually reasonable rules that only shows up when
    both are implemented.
    """
    if min_tokens <= 0:
        return chunks
    out: list[Chunk] = []
    for chunk in chunks:
        if (
            out
            and chunk.kind == "section"
            and out[-1].kind == "section"
            and out[-1].heading_path == chunk.heading_path
            and tokenizer.count(chunk.embed_text) < min_tokens
            and tokenizer.count(doc.text[out[-1].span.start : chunk.span.end]) <= max_tokens
        ):
            previous = out.pop()
            span = Span(previous.span.start, chunk.span.end)
            text = doc.text[span.start : span.end]
            out.append(
                Chunk(
                    doc.doc_id, span, text,
                    contextualize(text, chunk.heading_path),
                    "section", chunk.heading_path, page=previous.page, strategy="structural",
                )
            )
            continue
        out.append(chunk)
    return out


# --------------------------------------------------------------------------------
# §7.1 — Parent / child decoupling
# --------------------------------------------------------------------------------


def parent_child(
    doc: CanonicalDoc,
    elements: list[Element],
    *,
    child_tokens: int = 64,
    parent_tokens: int = 512,
    doc_title: str = "",
    tokenizer: Tokenizer = DEFAULT_TOKENIZER,
) -> list[Chunk]:
    """Embed a small, precise unit; return the larger unit that contains it.

    §5's constraints only conflict if the thing you search over and the thing you hand
    the generator must be the same object. They don't:

    - **Embed and index small** — C2 (pooling dilution) is satisfied.
    - **Return the enclosing section** — C3 (interpretability) is satisfied.

    This is the single most useful pattern in the chapter and it costs one extra store
    lookup per result plus a `parent_id` on every chunk. §7.3's taxonomy puts it on the
    axis that costs almost nothing and is most often skipped.

    Two consequences implemented here rather than described: `parent_span` is what the
    retriever dedups on (ten children routinely map to three parents — §7.4), and the
    token budget must be counted *after* parent expansion, never as `k` before it.
    """
    parents = structural(
        doc, elements, max_tokens=parent_tokens, min_tokens=0,
        doc_title=doc_title, tokenizer=tokenizer,
    )
    out: list[Chunk] = []
    for parent in parents:
        if parent.kind in ("table_row", CODE):
            # Already the right shape: a row sentence or a function is its own child,
            # and its parent is recorded in meta / parent_span.
            out.append(parent)
            continue
        for start, end in _recursive_offsets(
            doc.text, parent.span.start, parent.span.end, SEPARATORS, child_tokens, tokenizer
        ):
            child_text = doc.text[start:end]
            if not child_text.strip():
                continue
            out.append(
                Chunk(
                    doc.doc_id,
                    Span(start, end),
                    child_text,
                    contextualize(child_text, parent.heading_path, doc_title),
                    "child",
                    parent.heading_path,
                    page=parent.page,
                    parent_span=parent.span,
                    strategy="parent_child",
                    meta={"parent_text": parent.text},
                )
            )
    return out


def collapse_to_parents(chunks: list[Chunk]) -> list[Chunk]:
    """Map retrieved children to distinct parents, preserving rank order (§7.4).

    "Top 10" now means ten children, which might be three parents' worth of text or
    ten. Dedup before assembling the prompt or you send the same section three times
    and pay for it in context budget.
    """
    seen: set[tuple[int, int]] = set()
    out: list[Chunk] = []
    for chunk in chunks:
        span = chunk.parent_span or chunk.span
        key = (span.start, span.end)
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
    return out
