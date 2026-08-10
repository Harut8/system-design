"""The golden set's own test suite — the regression gate, not a one-off script.

Run with pytest if you have it, or standalone with zero dependencies:

    python3 test_golden_set.py
    pytest -q                      # identical assertions

`43-testing-strategy.md`'s "evals are tests" argument cuts both ways: the eval is a
test suite, and the *labels the eval depends on* need one too. A golden set is data
that silently rots — a document gets edited, a quote moves, a chunker parameter
changes, and the recall number you print next week is computed against labels that no
longer point at the text they were written for. Nothing about that failure is visible
in the number. These tests are what make it visible.

Grouped by what they defend:

  * resolution   — every label still points at real text, unambiguously
  * derivation   — the committed artifact is exactly what the builder produces now
  * identity     — chunk IDs are content-addressed and survive edits elsewhere
  * scoring      — the hit rules mean what §11.2 says they mean
  * shape        — the set is big enough and varied enough to conclude anything from
"""

from __future__ import annotations

import json
from pathlib import Path

import build as build_mod
from chunker import CHUNKER_VERSION, Chunk, chunk_corpus, chunk_document, chunk_id
from corpus import Document, canonicalize, corpus_digest, load_corpus
from goldenset import (
    Locator,
    LocatorError,
    Span,
    any_overlap,
    chunks_for_span,
    enclosing_block,
    load_questions,
    resolve_locator,
    span_containment,
    union_coverage,
)

HERE = Path(__file__).resolve().parent
DOCS = load_corpus(build_mod.CORPUS_ROOT)
CHUNKS = chunk_corpus(DOCS)
QUESTIONS = load_questions(HERE / "questions.jsonl")
RECORDS = [
    json.loads(line)
    for line in (HERE / "golden-set.v1.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
MANIFEST = json.loads((HERE / "golden-set.v1.manifest.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- resolution


def test_every_locator_resolves_to_exactly_one_span():
    """A quote that vanished or went ambiguous is a build error, never a dropped record.

    This is the test that fires when someone edits a chapter. It should fail loudly
    and be fixed by re-anchoring the quote — not by deleting the question, which would
    silently shrink the set and make every trend line incomparable to last week's.
    """
    for q in QUESTIONS:
        for loc in q.locators:
            span = resolve_locator(DOCS, loc)
            assert span.char_start < span.char_end, f"{q.id}: empty span"
            assert loc.quote in DOCS[loc.doc].text[span.char_start : span.char_end], (
                f"{q.id}: expanded span no longer contains its own anchor"
            )


def test_missing_quote_is_an_error():
    try:
        resolve_locator(DOCS, Locator(doc="README.md", quote="no such text anywhere ∎"))
    except LocatorError:
        return
    raise AssertionError("a missing quote must raise, not resolve to a guess")


def test_ambiguous_quote_is_an_error():
    """Two occurrences means the scorer would pick one, and that choice would decide
    the recall number. Better to refuse and make the author lengthen the quote."""
    try:
        resolve_locator(DOCS, Locator(doc="README.md", quote="the"))
    except LocatorError as exc:
        assert "ambiguous" in str(exc)
        return
    raise AssertionError("an ambiguous quote must raise")


def test_spans_are_in_bounds_and_substantial():
    for r in RECORDS:
        for s in r["answer_spans"]:
            doc = DOCS[s["doc_id"]]
            assert 0 <= s["char_start"] < s["char_end"] <= len(doc)
            text = doc.text[s["char_start"] : s["char_end"]].strip()
            # A 5-character span is a mis-anchored label, not a tight one.
            assert len(text) >= 30, f"{r['query_id']}: span too short to be an answer"


def test_block_expansion_stops_at_list_and_table_boundaries():
    """The regression this file exists for: the first build of this set expanded a
    list-item anchor to the whole list, giving fourteen records a 3,245-char span."""
    text = canonicalize("Intro paragraph.\n\n1. First item here.\n2. Second item here.\n3. Third.\n")
    lo, hi = enclosing_block(text, text.index("Second"), text.index("Second") + 6)
    assert text[lo:hi].strip() == "2. Second item here."

    table = canonicalize("| a | b |\n|---|---|\n| row one | x |\n| row two | y |\n")
    lo, hi = enclosing_block(table, table.index("row two"), table.index("row two") + 7)
    assert table[lo:hi].strip() == "| row two | y |"


# --------------------------------------------------------------------------- derivation


def test_committed_artifact_matches_a_fresh_build():
    """The golden set is derived, not maintained. If this fails, run `python3 build.py`.

    It fires on two different causes and both matter: the corpus changed under the
    labels (00 §3's staleness, applied to derived data), or someone hand-edited the
    generated file, which would make it unreproducible.
    """
    records, manifest = build_mod.build()
    body, _ = build_mod.serialize(records, manifest)
    assert body == (HERE / "golden-set.v1.jsonl").read_text(encoding="utf-8"), (
        "golden-set.v1.jsonl is stale — regenerate with `python3 build.py`"
    )


def test_build_is_deterministic():
    a, _ = build_mod.serialize(*build_mod.build())
    b, _ = build_mod.serialize(*build_mod.build())
    assert a == b


def test_manifest_pins_the_corpus_and_the_chunking():
    assert MANIFEST["corpus_digest"] == corpus_digest(DOCS), "corpus changed since the last build"
    assert MANIFEST["chunker_version"] == CHUNKER_VERSION
    # Every number this set ever produces must be reported with this rule (02 §11.2).
    assert MANIFEST["build_hit_rule"] in {"any_overlap", "span_containment"}


def test_answer_bearing_chunk_ids_exist_in_the_current_index():
    live = {c.chunk_id for c in CHUNKS}
    for r in RECORDS:
        assert r["answer_bearing_chunk_ids"], f"{r['query_id']}: no chunk ids"
        for cid in r["answer_bearing_chunk_ids"]:
            assert cid in live, f"{r['query_id']}: chunk id {cid[:12]} not in the current chunking"


def test_labels_are_fully_covered_by_their_chunks():
    """Union coverage of 1.0 per span: no labelled character falls in a gap between
    chunks. A gap would silently cap recall at less than 1 for reasons that have
    nothing to do with the retriever."""
    for r in RECORDS:
        for s, recorded in zip(r["answer_spans"], r["span_union_coverage"]):
            span = Span(s["doc_id"], s["char_start"], s["char_end"])
            hits = chunks_for_span(CHUNKS, span, MANIFEST["build_hit_rule"])
            assert round(union_coverage(hits, span), 4) == recorded == 1.0


# ----------------------------------------------------------------------------- identity


def test_chunk_ids_are_stable_under_an_edit_elsewhere_in_the_document():
    """The property that makes derived chunk IDs usable at all (02 §9.1).

    Insert a paragraph at the top of a document and re-chunk. Content-addressed IDs
    for untouched sections are unchanged, so labels pointing at them survive. Under a
    position-addressed scheme every ID below the insertion would change instead, and
    the whole golden set would need relabelling after a one-paragraph edit.
    """
    original = canonicalize(
        "# Title\n\n## A\n\nAlpha paragraph about storage engines.\n\n"
        "## B\n\nBeta paragraph about retrieval.\n"
    )
    edited = original.replace("## A\n\n", "## A\n\nA newly inserted opening paragraph.\n\n")

    before = chunk_document(Document("d.md", original, "x"))
    after = chunk_document(Document("d.md", edited, "y"))

    unchanged = {c.chunk_id for c in before} & {c.chunk_id for c in after}
    beta_before = next(c for c in before if "Beta paragraph" in c.text)
    assert beta_before.chunk_id in unchanged, "an untouched section must keep its ID"
    assert beta_before.start != next(c for c in after if "Beta paragraph" in c.text).start, (
        "the test is vacuous unless the edit actually shifted the section's offsets"
    )


def test_chunk_id_is_scoped_by_doc_and_chunker_version():
    """Both scoping arguments are load-bearing: identical boilerplate in two documents
    must not collide, and a chunker change must produce a disjoint ID space so old and
    new chunks can coexist during a migration."""
    text = "identical boilerplate"
    assert chunk_id("a.md", "v1", text) != chunk_id("b.md", "v1", text)
    assert chunk_id("a.md", "v1", text) != chunk_id("a.md", "v2", text)
    # NUL separators: ("ab","c") and ("a","bc") must not hash the same.
    assert chunk_id("ab", "v1", "c") != chunk_id("a", "v1", "bc")


def test_chunk_text_is_exactly_the_canonical_slice():
    """Offsets are the contract between labels and chunks. If a chunker rewrites text
    (strips, reflows, prepends) without keeping this identity, every span comparison
    is off by an unknown amount."""
    for c in CHUNKS:
        assert DOCS[c.doc_id].text[c.start : c.end] == c.text


# ------------------------------------------------------------------------------ scoring


def _chunk(start: int, end: int, doc: str = "d.md") -> Chunk:
    return Chunk(f"id{start}", doc, "h", start, end, "x", "x")


def test_hit_rules_differ_where_they_should():
    span = Span("d.md", 100, 200)
    partial, whole = _chunk(150, 300), _chunk(50, 300)
    assert any_overlap(partial, span) and not span_containment(partial, span)
    assert any_overlap(whole, span) and span_containment(whole, span)
    assert not any_overlap(_chunk(150, 300, doc="other.md"), span)


def test_union_coverage_never_exceeds_one_with_overlapping_chunks():
    """The character `set` in union_coverage is doing real work: summing intersection
    lengths would score a 50%-overlap chunking above 1.0 (02 §11.3)."""
    span = Span("d.md", 0, 100)
    overlapping = [_chunk(0, 60), _chunk(40, 100), _chunk(20, 80)]
    assert union_coverage(overlapping, span) == 1.0


# -------------------------------------------------------------------------------- shape


def test_set_is_large_enough_to_conclude_anything():
    assert len(RECORDS) >= 50, "00 §16 exercise 1 asks for 50; below that, per-query noise dominates"


def test_ids_and_queries_are_unique():
    ids = [r["query_id"] for r in RECORDS]
    queries = [r["query"] for r in RECORDS]
    assert len(set(ids)) == len(ids)
    assert len(set(queries)) == len(queries)


def test_every_record_has_a_gradeable_expected_answer():
    for r in RECORDS:
        assert len(r["expected_answer"].strip()) >= 20, f"{r['query_id']}: answer too thin to grade"


def test_multi_hop_records_really_need_more_than_one_span():
    """The hop field decides which recall definition applies (00 §6). A record marked
    multi with one span would report a hit under the lenient rule and hide a synthesis
    failure."""
    for r in RECORDS:
        if r["hop"] == "multi":
            assert len(r["answer_spans"]) >= 2, f"{r['query_id']}: multi-hop with one span"
        else:
            assert len(r["answer_spans"]) == 1


def test_no_single_document_dominates_the_labels():
    """A golden set that is 90% one document (or one question shape) measures that
    document, not the corpus — 02 §11.5's "90% factoids" warning, made mechanical."""
    counts: dict[str, int] = {}
    for r in RECORDS:
        for s in r["answer_spans"]:
            counts[s["doc_id"]] = counts.get(s["doc_id"], 0) + 1
    total = sum(counts.values())
    worst = max(counts.values()) / total
    assert worst <= 0.6, f"one document holds {worst:.0%} of the labels"
    assert len(counts) == MANIFEST["n_docs"], "every corpus document should carry some labels"


if __name__ == "__main__":  # zero-dependency runner, so the gate works before pytest exists
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
