"""Corpus loading and canonicalization for the golden-set lab.

One job: turn a set of files on disk into *canonical text* — the single string per
document that every downstream offset, span, and chunk ID is defined against.

Why this file exists at all, and why it is separate from the chunker: the golden set
labels answer spans as `(doc_id, char_start, char_end)` (02 §11.2). Character offsets
are only meaningful relative to a fixed string. If the canonicalization changes — a
different Unicode normal form, CRLF handling, a stripped BOM — every offset in every
golden record silently shifts, and the labels quietly stop pointing at the text they
were written against. So canonicalization is a *versioned* decision, recorded in the
built artifact, and the test suite fails loudly when it changes.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Bump when any rule in `canonicalize()` changes. This invalidates every offset in
# every built golden set, which is the point: the change becomes visible instead of
# silent (02 §9.1's chunker_version argument, applied one stage earlier).
CANONICAL_VERSION = "canon-v1"

# The lab corpus: the ai-rag chapters themselves. A real corpus you own, per 00 §16
# exercise 1. Sorted for determinism — `glob` order is filesystem-dependent.
CORPUS_GLOB = "*.md"


@dataclass(frozen=True)
class Document:
    doc_id: str  # repo-relative path; stable across machines, unlike an absolute path
    text: str  # canonical text — the string all offsets are defined against
    sha256: str  # digest of `text`, so staleness is detectable without re-reading

    def __len__(self) -> int:
        return len(self.text)


def canonicalize(raw: str) -> str:
    """The canonical form. Every rule here is a decision the golden set depends on.

    - NFC: the same logical character can arrive as one code point or as base +
      combining mark. Without normalization the same visible text hashes differently
      depending on which editor wrote it (01 §8, 02 §4.1).
    - CRLF -> LF: otherwise every offset in a file touched on Windows is shifted by
      the number of preceding lines.
    - BOM stripped: a leading U+FEFF shifts every offset in the document by one.
    - Trailing newline forced: makes the last block's end offset unambiguous.

    Deliberately *not* done: lowercasing, whitespace collapsing, punctuation
    stripping. Those belong to the lexical analyzer, not the canonical form — 02 §4.3
    is explicit that letting the analyzer's output become the stored text is a silent
    hybrid-search regression. The canonical text is what a human reads.
    """
    text = raw.lstrip("﻿")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    if not text.endswith("\n"):
        text += "\n"
    return text


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_corpus(root: Path, pattern: str = CORPUS_GLOB) -> dict[str, Document]:
    """Load every matching file under `root` as a canonical Document, keyed by doc_id."""
    docs: dict[str, Document] = {}
    for path in sorted(root.glob(pattern)):
        text = canonicalize(path.read_text(encoding="utf-8"))
        doc_id = path.name
        docs[doc_id] = Document(doc_id=doc_id, text=text, sha256=digest(text))
    if not docs:
        raise FileNotFoundError(f"no documents matched {pattern!r} under {root}")
    return docs


def corpus_digest(docs: dict[str, Document]) -> str:
    """One digest over the whole corpus: doc_id + content, in sorted order.

    Recorded in the built golden set so a test can answer "was this file built against
    the corpus as it exists now?" — the staleness check from 00 §3, applied to the
    labels instead of to the index.
    """
    h = hashlib.sha256()
    for doc_id in sorted(docs):
        h.update(doc_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(docs[doc_id].sha256.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
