"""A deterministic, structure-aware Markdown chunker with content-addressed IDs.

Scope: this is the chunker the golden set is *resolved against*, not a claim about
the best chunking. It exists because a chunk ID has no meaning without a fixed
chunking (02 §11.2), so exercise 1 cannot produce chunk IDs until a chunker is
pinned. Chapter 02's lab exercises are where chunkings get compared; this one only
has to be deterministic, structure-aware, and honest about its own parameters.

Design, and the reason for each choice:

- **Structure-aware split at headings.** A section heading is a boundary the author
  already drew (02 §6.3, mental model 9). Splitting there is free and beats any
  fixed-size window that cuts mid-argument.
- **Heading path prepended to the embedded text.** The free version of contextual
  retrieval (02 §6.3): a chunk that says "the same applies here" is unretrievable
  alone; prefixed with `00-mental-models.md > 9. The cost model` it is not.
- **Zero overlap.** Overlap costs `1/(1-f)`, not `f` (02 §5.5) and it complicates
  span accounting, since one answer span then lands in two chunks by construction.
  For a labelling harness that is noise, not signal.
- **Never split a fenced code block.** A code block cut in half is the parsing
  failure class from 02 §3 reintroduced by the chunker itself.
- **Content-addressed IDs.** `sha256(doc_id, chunker_version, embed_text)`, exactly
  the scheme in 02 §9.1, including the `\\x00` separators that stop ("ab","c") and
  ("a","bc") from colliding. Position-addressed IDs would mean inserting a paragraph
  at the top of a chapter renames every chunk below it — and would invalidate every
  golden label that referenced them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from corpus import Document

# Any change to the splitting rules or the parameters below must bump this. It scopes
# the ID space, so old and new chunks can coexist during a migration instead of
# colliding (02 §9.1).
CHUNKER_VERSION = "md-struct-v1"

MAX_CHARS = 1800  # ~450 tokens of English prose at the ~4 chars/token rule of thumb
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A `---` rule carries no content, so it must not become a chunk of its own: an
# empty chunk is a vector that can be retrieved and cannot answer anything.
RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    heading_path: str
    start: int  # inclusive char offset into Document.text
    end: int  # exclusive
    text: str  # exactly Document.text[start:end] — the canonical slice
    embed_text: str  # heading path + text; what gets embedded, and what the ID hashes

    @property
    def n_chars(self) -> int:
        return self.end - self.start


def chunk_id(doc_id: str, chunker_version: str, canonical_text: str) -> str:
    """Content-addressed chunk identity — 02 §9.1, unmodified.

    The NUL separators are load-bearing: without them, changing where the doc_id ends
    and the text begins could produce the same digest for two different chunks.
    """
    h = hashlib.sha256()
    for part in (doc_id, chunker_version, canonical_text):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _line_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) char offsets of each line, end exclusive and including its \\n."""
    spans, pos = [], 0
    for line in text.splitlines(keepends=True):
        spans.append((pos, pos + len(line)))
        pos += len(line)
    return spans


def _blocks(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split [start, end) into paragraph blocks: runs of non-blank lines.

    A fenced code block counts as one indivisible block even if it contains blank
    lines. Blank lines between blocks belong to neither and are dropped from spans —
    which is why a chunk's text is a slice of the canonical text but the chunks of a
    section do not necessarily tile it without gaps.
    """
    out: list[tuple[int, int]] = []
    cur_start: int | None = None
    in_fence = False
    for ls, le in _line_spans(text):
        if ls < start or le > end:
            continue
        line = text[ls:le]
        if FENCE_RE.match(line):
            in_fence = not in_fence
            if cur_start is None:
                cur_start = ls
            if not in_fence:  # closing fence ends the block
                out.append((cur_start, le))
                cur_start = None
            continue
        if in_fence:
            continue
        if line.strip() and not RULE_RE.match(line):
            if cur_start is None:
                cur_start = ls
        elif cur_start is not None:
            out.append((cur_start, ls))
            cur_start = None
    if cur_start is not None:
        out.append((cur_start, end))
    return out


def _sections(doc: Document) -> list[tuple[str, int, int]]:
    """(heading_path, start, end) for each heading-delimited section of the document.

    Headings inside fenced code blocks are ignored — a `# comment` in a shell snippet
    is not a section boundary, and treating it as one is the kind of parser bug that
    puts a chunk boundary in the middle of an example.
    """
    text = doc.text
    stack: list[tuple[int, str]] = []
    marks: list[tuple[str, int]] = []  # (heading_path, body_start)
    in_fence = False

    for ls, le in _line_spans(text):
        line = text[ls:le]
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line.rstrip("\n"))
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        marks.append((" > ".join(t for _, t in stack), le))

    if not marks:  # a document with no headings is one section
        return [(doc.doc_id, 0, len(text))]

    # Preamble before the first heading, if any, gets the document's own name.
    sections: list[tuple[str, int, int]] = []
    first_body = marks[0][1]
    first_heading_start = text.rfind("\n", 0, first_body - 1) + 1
    if text[:first_heading_start].strip():
        sections.append((doc.doc_id, 0, first_heading_start))

    for i, (path, body_start) in enumerate(marks):
        # A section ends where the next heading line begins, not where its body does.
        if i + 1 < len(marks):
            nxt_body = marks[i + 1][1]
            end = text.rfind("\n", 0, nxt_body - 1) + 1
        else:
            end = len(text)
        sections.append((f"{doc.doc_id} > {path}", body_start, end))
    return sections


def _pack(blocks: list[tuple[int, int]], text: str, max_chars: int) -> list[tuple[int, int]]:
    """Greedily pack whole blocks up to max_chars; hard-split only oversized blocks."""
    packed: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None

    for bs, be in blocks:
        if be - bs > max_chars:
            if cur:
                packed.append(cur)
                cur = None
            # Oversized single block (a long code fence, a wide table): split at line
            # boundaries so a chunk never begins mid-line. Rare by construction; if it
            # is not rare on your corpus, max_chars is wrong for it.
            seg_start = bs
            for ls, le in _line_spans(text):
                if ls < bs or le > be:
                    continue
                if le - seg_start > max_chars and le > seg_start:
                    packed.append((seg_start, ls if ls > seg_start else le))
                    seg_start = ls if ls > seg_start else le
            if seg_start < be:
                packed.append((seg_start, be))
            continue
        if cur is None:
            cur = (bs, be)
        elif be - cur[0] <= max_chars:
            cur = (cur[0], be)
        else:
            packed.append(cur)
            cur = (bs, be)
    if cur:
        packed.append(cur)
    return packed


def chunk_document(doc: Document, max_chars: int = MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading_path, sec_start, sec_end in _sections(doc):
        blocks = _blocks(doc.text, sec_start, sec_end)
        for start, end in _pack(blocks, doc.text, max_chars):
            body = doc.text[start:end]
            if not body.strip():
                continue
            embed_text = f"{heading_path}\n\n{body.strip()}"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id(doc.doc_id, CHUNKER_VERSION, embed_text),
                    doc_id=doc.doc_id,
                    heading_path=heading_path,
                    start=start,
                    end=end,
                    text=body,
                    embed_text=embed_text,
                )
            )
    return chunks


def chunk_corpus(docs: dict[str, Document], max_chars: int = MAX_CHARS) -> list[Chunk]:
    out: list[Chunk] = []
    for doc_id in sorted(docs):
        out.extend(chunk_document(docs[doc_id], max_chars))
    return out
