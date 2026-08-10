"""Stage 6 — chunk identity, idempotency and incremental update (`02` §9).

A corpus is not a snapshot; documents change. Everything here exists to make the
update path cost proportional to **what changed**, not to corpus size.

The choice that decides that cost is how a chunk is named, and there are only two
options:

- **Position-addressed** — `hash(doc_id, chunker_version, ordinal)`. Simple, and it
  has a nasty property: insert a paragraph at the top of a document and every
  subsequent ordinal shifts, so every chunk ID changes, so the entire document is
  re-embedded and re-indexed. A one-word edit costs a full document reprocess.
- **Content-addressed** — `hash(doc_id, chunker_version, canonical_text)`. Insert a
  paragraph at the top and only the chunks whose *text* changed get new IDs.

`rehearse_edit()` at the bottom of this file runs §15's lab 9 and prints the
difference. On this corpus the ratio is large enough that it stops being an argument.

Content-addressing has exactly one requirement: **a deterministic chunker**. That is
the concrete, operational reason determinism appears in every decision table in `02`,
and why semantic (§6.4) and LLM-based (§6.5) chunking give something real up. It is
not purity; it is the difference between re-embedding one paragraph and re-embedding
a document.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from split import Chunk

# Scopes the ID space. A chunker change produces a disjoint set of IDs, so old and new
# chunks coexist during a shadow migration instead of colliding (§9.1).
from split import CHUNKER_VERSION  # noqa: F401  (re-exported for the manifest)


def chunk_id(doc_id: str, chunker_version: str, canonical_text: str) -> str:
    """Content-addressed chunk identity (§9.1).

    - `doc_id` scopes the ID so identical boilerplate in two documents stays distinct;
      cross-document duplication is §10's separate, deliberate decision.
    - `chunker_version` makes a chunker change produce a disjoint ID space.
    - The `b"\\x00"` separators prevent concatenation ambiguity: without them,
      `("ab", "c")` and `("a", "bc")` hash identically.
    - `canonical_text` must be the *normalized* text (§4.1) — NFC first, or the same
      logical content produces different IDs depending on its source encoding.
    """
    h = hashlib.sha256()
    for part in (doc_id, chunker_version, canonical_text):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def position_id(doc_id: str, chunker_version: str, ordinal: int) -> str:
    """Position-addressed identity. Implemented only so the lab can measure its cost."""
    h = hashlib.sha256()
    for part in (doc_id, chunker_version, str(ordinal)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def assign_ids(chunks: list[Chunk], chunker_version: str, scheme: str = "content") -> list[str]:
    """Name every chunk under the chosen scheme.

    Note that the content-addressed hash covers `embed_text`, not `text`. That is
    deliberate and it is a real decision: the heading path is part of what gets
    embedded (§6.3), so a change to a section *title* must produce new IDs for every
    chunk beneath it — because those chunks' vectors genuinely did change. Hashing
    `text` alone would leave stale vectors in the index carrying the old heading, with
    nothing anywhere reporting a mismatch.
    """
    if scheme == "content":
        return [chunk_id(c.doc_id, chunker_version, c.embed_text) for c in chunks]
    if scheme == "position":
        return [position_id(c.doc_id, chunker_version, i) for i, c in enumerate(chunks)]
    raise ValueError(f"unknown id scheme {scheme!r}")


# --------------------------------------------------------------------------------
# A store, and the diff-based update (§9.2)
# --------------------------------------------------------------------------------


@dataclass
class UpdateResult:
    added: int
    updated: int  # same ID, different text — only possible under position addressing
    deleted: int
    unchanged: int
    embed_calls: int  # the only line that costs money

    @property
    def churn(self) -> int:
        return self.added + self.updated + self.deleted

    def __str__(self) -> str:
        return (
            f"+{self.added} ~{self.updated} -{self.deleted} ={self.unchanged} "
            f"({self.embed_calls} embed calls)"
        )


@dataclass
class Store:
    """An in-memory stand-in for the vector store, with the one index §9.2 requires.

    `by_doc` is not incidental: the diff needs "every chunk ID currently indexed for
    this document", and a vector store that cannot answer that cheaply forces you into
    either a full-corpus scan or a delete-everything-and-reinsert update, which is the
    churn §9.3 warns about on a tombstoning index.
    """

    vectors: dict[str, str] = field(default_factory=dict)  # id -> embed_text
    by_doc: dict[str, set[str]] = field(default_factory=dict)
    embed_calls: int = 0

    def chunk_ids_for_doc(self, doc_id: str) -> set[str]:
        return set(self.by_doc.get(doc_id, set()))

    def upsert(self, items: list[tuple[str, str]], doc_id: str) -> None:
        for cid, text in items:
            self.vectors[cid] = text
            self.embed_calls += 1  # a real embed call happens exactly here
            self.by_doc.setdefault(doc_id, set()).add(cid)

    def delete(self, ids: set[str], doc_id: str) -> None:
        for cid in ids:
            self.vectors.pop(cid, None)
            self.by_doc.get(doc_id, set()).discard(cid)


def reindex_document(
    doc_id: str, chunks: list[Chunk], store: Store, *, scheme: str = "content"
) -> UpdateResult:
    """Diff-based document update. Cost is proportional to the change (§9.2).

    Two properties are load-bearing:

    **Idempotency.** Running this twice on an unchanged document is a no-op — both
    `to_add` and `to_delete` are empty and `embed_calls` does not move. That is what
    lets ingestion be retried after a partial failure without duplicating anything,
    and every ingestion pipeline eventually crashes mid-document.

    **Deletion is not optional and it is the step people skip.** A chunk removed from
    a document but left in the index is a vector that will be retrieved and cited as
    current, with a source URI that no longer contains it. That is worse than missing
    data: it is confidently wrong data *with a citation*. The failure mode is
    documents that shrink — a section deleted from a wiki page whose chunks live on
    for months.
    """
    ids = assign_ids(chunks, CHUNKER_VERSION, scheme)
    new = {cid: c.embed_text for cid, c in zip(ids, chunks)}
    old = store.chunk_ids_for_doc(doc_id)

    to_add = new.keys() - old
    to_delete = old - new.keys()
    both = old & new.keys()

    # An ID present before and after does NOT mean the content is unchanged — and the
    # difference between those two statements is the whole case against position
    # addressing. A content-addressed ID *is* a digest of the text, so ID equality
    # implies text equality and this comparison always finds zero. A position-addressed
    # ID is a digest of an ordinal and says nothing about content, so the text at
    # position 3 can change completely while keeping its name.
    #
    # A pipeline that skips this comparison (the obvious implementation, and the one
    # in §9.2's sketch) therefore *silently keeps the stale vector* under position
    # addressing: the edited paragraph is never re-embedded, and the index answers
    # queries with the pre-edit text under a chunk ID that looks current. That is a
    # worse failure than the re-embedding cost it appears to avoid, and it is invisible
    # to any metric that counts adds and deletes.
    to_update = {cid for cid in both if store.vectors.get(cid) != new[cid]}
    unchanged = both - to_update

    before = store.embed_calls
    store.upsert([(cid, new[cid]) for cid in sorted(to_add | to_update)], doc_id)
    store.delete(to_delete, doc_id)

    return UpdateResult(
        len(to_add), len(to_update), len(to_delete), len(unchanged),
        store.embed_calls - before,
    )


# --------------------------------------------------------------------------------
# Lab 9 — the incremental update rehearsal (§15)
# --------------------------------------------------------------------------------


@dataclass
class RehearsalRow:
    scenario: str
    scheme: str
    result: UpdateResult
    total_chunks: int

    @property
    def fraction_reprocessed(self) -> float:
        return self.result.added / max(self.total_chunks, 1)


def rehearse_edit(
    build_chunks,
    original: str,
    edited: str,
    doc_id: str,
) -> list[RehearsalRow]:
    """Run §15's lab 9: edit / no-op, under both ID schemes.

    `build_chunks(text) -> list[Chunk]` must be deterministic — which is the whole
    point of the exercise, and the reason the callable is a parameter rather than a
    hardcoded chunker.

    The expected shape of the result, from §9.1: a content-addressed edit near the top
    of a document touches a handful of chunks where a position-addressed one touches
    every chunk below the edit. The no-op case must write nothing under content
    addressing and — this is the part people do not expect — also nothing under
    position addressing, because an unchanged document produces identical ordinals.
    Position addressing is not broken by *reprocessing*; it is broken by *editing*.
    """
    rows: list[RehearsalRow] = []
    for scheme in ("content", "position"):
        store = Store()
        base = build_chunks(original)
        reindex_document(doc_id, base, store, scheme=scheme)

        after_edit = build_chunks(edited)
        rows.append(
            RehearsalRow(
                "edit first paragraph",
                scheme,
                reindex_document(doc_id, after_edit, store, scheme=scheme),
                len(after_edit),
            )
        )
        rows.append(
            RehearsalRow(
                "reprocess unchanged",
                scheme,
                reindex_document(doc_id, build_chunks(edited), store, scheme=scheme),
                len(after_edit),
            )
        )

        # Delete a section and prove its vectors are gone, not merely unreferenced.
        shortened = "\n\n".join(edited.split("\n\n")[:-2]) + "\n"
        rows.append(
            RehearsalRow(
                "delete trailing section",
                scheme,
                reindex_document(doc_id, build_chunks(shortened), store, scheme=scheme),
                len(build_chunks(shortened)),
            )
        )
    return rows
