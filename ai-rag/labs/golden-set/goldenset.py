"""Golden-set types: locators, answer spans, and hit rules.

The central design decision of this lab lives in this file, so it is worth stating
before the code:

    A golden record's ground truth is a **span of canonical text**, not a chunk ID.

Chunk IDs only exist relative to a chunking (02 §11.2). Label against them and
re-chunking silently invalidates every record you own; re-deriving the labels with
"whichever new chunk overlaps the old chunk" smuggles the old chunker's boundaries
into the new chunker's score. Spans survive re-chunking because they are defined
against the corpus, which is the thing that did not change.

Chunk IDs are still what exercise 2 needs to compute recall@k, so they are *derived*
at build time from (span, chunking, hit rule) and written into the built artifact —
regenerable, never hand-maintained.

One more layer sits above spans: the **locator**. Hand-writing `char_start: 48122`
is not something a human can do or review, and offsets shift whenever the document is
edited. So the human authors a short exact `quote`, the builder resolves it to
offsets, and a quote that no longer appears — or that appears twice — is a build
error rather than a wrong number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from chunker import Chunk
from corpus import Document


class LocatorError(ValueError):
    """A locator that does not resolve to exactly one place in its document."""


@dataclass(frozen=True)
class Locator:
    """Human-authored pointer at the answer text."""

    doc: str
    quote: str
    # exact=True labels the quote's own character range. Default False expands to the
    # enclosing paragraph block, so the author can anchor on a short distinctive
    # phrase and still get a span that carries the whole answer.
    exact: bool = False


@dataclass(frozen=True)
class Span:
    doc_id: str
    char_start: int
    char_end: int

    def __len__(self) -> int:
        return self.char_end - self.char_start


@dataclass(frozen=True)
class Question:
    """One hand-authored golden record, before resolution. Chunking-independent."""

    id: str
    query: str
    expected_answer: str
    locators: tuple[Locator, ...]
    hop: str = "single"  # "single" | "multi" — multi-hop needs the strict hit rule
    tags: tuple[str, ...] = field(default_factory=tuple)


def load_questions(path: Path) -> list[Question]:
    questions: list[Question] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        raw = json.loads(line)
        try:
            questions.append(
                Question(
                    id=raw["id"],
                    query=raw["query"],
                    expected_answer=raw["expected_answer"],
                    locators=tuple(
                        Locator(doc=loc["doc"], quote=loc["quote"], exact=loc.get("exact", False))
                        for loc in raw["locators"]
                    ),
                    hop=raw.get("hop", "single"),
                    tags=tuple(raw.get("tags", ())),
                )
            )
        except KeyError as exc:
            raise LocatorError(f"{path}:{lineno}: missing field {exc}") from exc
    return questions


LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+\.)\s")
TABLE_ROW_RE = re.compile(r"^\s*\|")


def _line_bounds(text: str, pos: int) -> tuple[int, int]:
    lo = text.rfind("\n", 0, pos) + 1
    hi = text.find("\n", pos)
    return lo, (len(text) if hi == -1 else hi + 1)


def enclosing_block(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand [start, end) to the smallest structural unit that contains it.

    The rule, stated once here so it can be restated next to every number derived
    from it — three cases, checked in this order:

    1. **Table row.** The quote sits in a `|`-delimited row: label the rows the quote
       touches, nothing else. Without this case a single anchor in a nine-row
       comparison table labels all nine rows as the answer, and two different
       questions end up with identical spans.
    2. **List item.** The quote sits in a `-`/`1.` item: label that item, from its
       marker to the next item at the same or shallower indent. Markdown lists have
       no blank lines between items, so the paragraph rule would swallow the entire
       list — which is exactly what it did on the first build of this set, giving
       fourteen records a 3,245-character span covering a dozen unrelated facts.
    3. **Paragraph.** Otherwise, the maximal run of lines with no blank line in it.

    All three are deterministic and structural. A sentence splitter would be neither,
    and on Markdown containing code fences, tables, and list items it would be wrong
    in ways that are invisible until someone reads a span by hand.
    """
    block_lo = text.rfind("\n\n", 0, start)
    block_lo = 0 if block_lo == -1 else block_lo + 2
    block_hi = text.find("\n\n", end)
    block_hi = len(text) if block_hi == -1 else block_hi + 1

    first_lo, _ = _line_bounds(text, start)
    _, last_hi = _line_bounds(text, max(start, end - 1))

    if TABLE_ROW_RE.match(text[first_lo:last_hi]):
        return first_lo, last_hi

    # Walk up to the list item that owns the anchor line, if any.
    item_lo, item_indent = None, 0
    pos = first_lo
    while pos >= block_lo:
        line_lo, line_hi = _line_bounds(text, pos)
        m = LIST_ITEM_RE.match(text[line_lo:line_hi])
        if m:
            item_lo, item_indent = line_lo, len(m.group(1))
            break
        if line_lo <= block_lo:
            break
        pos = line_lo - 1

    if item_lo is None:
        return block_lo, block_hi

    # ...and down to the next item at the same or shallower indent.
    pos = last_hi
    while pos < block_hi:
        line_lo, line_hi = _line_bounds(text, pos)
        m = LIST_ITEM_RE.match(text[line_lo:line_hi])
        if m and len(m.group(1)) <= item_indent:
            return item_lo, line_lo
        pos = line_hi
    return item_lo, block_hi


def resolve_locator(docs: dict[str, Document], loc: Locator) -> Span:
    """Locator -> Span, or a loud failure. Never a guess.

    Two failure modes, both build errors on purpose:
      - zero matches: the quote was mistyped, or the document changed under the label.
        Silently dropping the record would quietly shrink the golden set; silently
        fuzzy-matching would relabel it against text nobody chose.
      - multiple matches: the label is ambiguous, and which occurrence the scorer
        picks would decide the recall number. Fix by lengthening the quote.
    """
    doc = docs.get(loc.doc)
    if doc is None:
        raise LocatorError(f"unknown doc {loc.doc!r} (have: {sorted(docs)})")

    hits = [m.start() for m in re.finditer(re.escape(loc.quote), doc.text)]
    if not hits:
        raise LocatorError(f"{loc.doc}: quote not found: {loc.quote[:70]!r}")
    if len(hits) > 1:
        raise LocatorError(
            f"{loc.doc}: quote is ambiguous ({len(hits)} occurrences), lengthen it: "
            f"{loc.quote[:70]!r}"
        )

    start = hits[0]
    end = start + len(loc.quote)
    if not loc.exact:
        start, end = enclosing_block(doc.text, start, end)
    return Span(doc_id=loc.doc, char_start=start, char_end=end)


# ---------------------------------------------------------------------------
# Hit rules — 02 §11.2's table, implemented. Every recall number computed with
# this module must name which of these produced it.
# ---------------------------------------------------------------------------


def any_overlap(chunk: Chunk, span: Span) -> bool:
    """Lenient: one sentence of a three-sentence answer counts as a hit."""
    return chunk.doc_id == span.doc_id and chunk.start < span.char_end and span.char_start < chunk.end


def span_containment(chunk: Chunk, span: Span) -> bool:
    """Strict: the chunk alone must carry the whole span. The right rule when there
    is no parent expansion at generation time (02 §7)."""
    return chunk.doc_id == span.doc_id and chunk.start <= span.char_start and chunk.end >= span.char_end


def coverage(chunk: Chunk, span: Span) -> float:
    """Fraction of the span's characters this one chunk covers."""
    if chunk.doc_id != span.doc_id or len(span) == 0:
        return 0.0
    lo, hi = max(chunk.start, span.char_start), min(chunk.end, span.char_end)
    return max(0, hi - lo) / len(span)


def union_coverage(chunks: list[Chunk], span: Span) -> float:
    """Fraction of the span covered by the *union* of several chunks.

    The honest rule when an answer is legitimately assembled from adjacent chunks.
    The character `set` is doing real work: with overlapping chunkings, summing
    intersection lengths double-counts and can score above 1.0 (02 §11.3).
    """
    if len(span) == 0:
        return 0.0
    marks: set[int] = set()
    for c in chunks:
        if c.doc_id != span.doc_id:
            continue
        lo, hi = max(c.start, span.char_start), min(c.end, span.char_end)
        if lo < hi:
            marks.update(range(lo, hi))
    return len(marks) / len(span)


HIT_RULES = {
    "any_overlap": any_overlap,
    "span_containment": span_containment,
}


def chunks_for_span(chunks: list[Chunk], span: Span, rule: str = "any_overlap") -> list[Chunk]:
    try:
        predicate = HIT_RULES[rule]
    except KeyError:
        raise ValueError(f"unknown hit rule {rule!r}; have {sorted(HIT_RULES)}") from None
    return [c for c in chunks if predicate(c, span)]
