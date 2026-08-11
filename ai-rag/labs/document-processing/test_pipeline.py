"""The regression gate. `python3 test_pipeline.py`, or `pytest -q`. Zero dependencies.

Every assertion here encodes something the lab claims, so that a claim cannot rot
silently. They fall into four groups:

1. **Invariants that must hold for the architecture to mean anything** — a chunk's
   text is exactly its span, payload fields never reach the embedded text, the corpus
   is deterministic, reprocessing is idempotent.
2. **Known answers about the fixtures** — `scan.pdf` routes to OCR, `subset_broken.pdf`
   routes to review, the two-column PDF interleaves under naive reading order.
3. **Bugs already found once**, pinned so they cannot come back. Each of those tests
   names the bug in its docstring; they are the most valuable tests in the file
   because every one of them passed by accident before it passed on purpose.
4. **Arithmetic from the chapter** — overlap inflation is `1/(1-f)`, content-addressed
   IDs are stable under an unrelated edit.

The tests deliberately do NOT assert retrieval quality. Nothing in this lab measures
it, and a test that asserted a chunking was "better" would be asserting a claim the
lab has no evidence for (§11.6).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import dedup
import identity
import make_fixtures
import normalize as N
import parse as PA
import pdfmini as PM
import pipeline as PL
import split as SP
import tables as TB

ROOT = Path(__file__).parent / "corpus"


# --------------------------------------------------------------------------------
# 1. Architectural invariants
# --------------------------------------------------------------------------------


def test_chunk_text_is_exactly_its_span() -> None:
    """§4.4's promise. If this fails, every citation and every span label is wrong."""
    for strategy in ("fixed", "recursive", "structural", "parent_child"):
        config = PL.Config(strategy=strategy)
        for result in PL.process_corpus(ROOT, config):
            if result.canonical is None:
                continue
            for chunk in result.chunks:
                expected = result.canonical.text[chunk.span.start : chunk.span.end]
                assert chunk.text == expected, (
                    f"{strategy}/{result.doc_id}: chunk text is not its span slice"
                )


def test_spans_are_within_bounds_and_ordered() -> None:
    for result in PL.process_corpus(ROOT, PL.Config(strategy="structural")):
        if result.canonical is None:
            continue
        length = len(result.canonical.text)
        for chunk in result.chunks:
            assert 0 <= chunk.span.start < chunk.span.end <= length, (
                f"{result.doc_id}: span {chunk.span} outside [0, {length})"
            )


def test_payload_never_leaks_into_embedded_text() -> None:
    """§8.2. Enforced in `pipeline.assert_no_leakage`; asserted here so it stays on."""
    for result in PL.process_corpus(ROOT, PL.Config(strategy="structural")):
        for record in result.records:
            PL.assert_no_leakage(record)
            assert record.tenant_id not in record.embed_text
            assert record.chunk_id not in record.embed_text


def test_canonical_text_is_normalized() -> None:
    """No ligatures, no invisible characters, no CRLF anywhere in canonical text."""
    for result in PL.process_corpus(ROOT, PL.Config(strategy="structural")):
        if result.canonical is None:
            continue
        text = result.canonical.text
        assert "\r" not in text, f"{result.doc_id}: CR survived canonicalization"
        for ligature in N.LIGATURES:
            assert ligature not in text, f"{result.doc_id}: ligature {ligature!r} survived"
        for invisible in N.INVISIBLE:
            assert invisible not in text, f"{result.doc_id}: invisible char survived"


def test_fixture_corpus_is_deterministic() -> None:
    """§9.1 requires a deterministic chunker; that requires a deterministic corpus."""
    before = {p: p.read_bytes() for p in sorted(ROOT.rglob("*")) if p.is_file()}
    make_fixtures.write_all()
    after = {p: p.read_bytes() for p in sorted(ROOT.rglob("*")) if p.is_file()}
    assert before.keys() == after.keys(), "regenerating changed the file set"
    for path, data in before.items():
        assert after[path] == data, f"{path.name} is not byte-identical across runs"


def test_chunking_is_deterministic() -> None:
    a = PL.process_corpus(ROOT, PL.Config(strategy="structural"))
    b = PL.process_corpus(ROOT, PL.Config(strategy="structural"))
    ids_a = [r.chunk_id for result in a for r in result.records]
    ids_b = [r.chunk_id for result in b for r in result.records]
    assert ids_a == ids_b, "the same corpus produced different chunk IDs"


def test_reindexing_is_idempotent() -> None:
    """§9.2. Running ingestion twice on an unchanged corpus must write nothing."""
    results = PL.process_corpus(ROOT, PL.Config(strategy="structural"))
    store = PL.index_corpus(results)
    first = store.embed_calls
    for result in results:
        if result.quarantined:
            continue
        outcome = identity.reindex_document(result.doc_id, result.chunks, store)
        assert outcome.added == 0 and outcome.updated == 0 and outcome.deleted == 0, (
            f"{result.doc_id}: re-running ingestion was not a no-op ({outcome})"
        )
    assert store.embed_calls == first, "a no-op reprocess spent embedding calls"


# --------------------------------------------------------------------------------
# 2. Known answers about the fixtures
# --------------------------------------------------------------------------------


def test_scanned_pdf_routes_to_ocr() -> None:
    """§3.2. Zero chunks is correct; zero chunks *and silence* is the bug."""
    parsed = PA.parse_file(ROOT / "scan.pdf", ROOT)
    verdict = PA.gate(parsed)
    assert verdict.route == PA.ROUTE_OCR, f"expected OCR route, got {verdict.route}"
    # Not exactly 0.0: joining two empty pages yields the newline between them. That
    # one character is why the gate compares against a *threshold* rather than testing
    # for the empty string — on a real scan you get a few characters of noise, not none.
    assert verdict.yield_per_page < 1.0, f"expected near-zero yield, got {verdict.yield_per_page}"
    assert not parsed.raw_text.strip()
    result = PL.process(ROOT / "scan.pdf", ROOT)
    assert result.quarantined, "scanned PDF was not quarantined"
    assert result.metrics.chunks == 0, "a quarantined document produced chunks"


def test_broken_encoding_routes_to_review() -> None:
    """§3.2. The control file with the same layout must pass, or the gate is useless."""
    broken = PA.gate(PA.parse_file(ROOT / "subset_broken.pdf", ROOT))
    control = PA.gate(PA.parse_file(ROOT / "subset_ok.pdf", ROOT))
    assert broken.route == PA.ROUTE_REVIEW, f"expected review, got {broken.route}"
    assert control.route == PA.ROUTE_INDEX, f"control must index, got {control.route}"
    assert broken.sanity < 0.5 < control.sanity


def test_glyph_name_leakage_is_detected() -> None:
    """Found by the bake-off: pypdf renders unmapped glyphs as literal `/g1/g2` text.

    That is clean ASCII, so `script_sanity` scores 1.00 and the mojibake gate misses
    it entirely. A second detector is required, and it must stay wired into `gate()`.
    """
    pypdf_style = "/g1/g2/g3/g4/g5/g6/g7/g8/g9/g10/g11/g12/g13/g14/g15"
    assert PA.script_sanity(pypdf_style) > 0.9, "premise changed: this text looks clean"
    assert PA.glyph_leakage(pypdf_style) > 0.5, "glyph-name leakage not detected"

    uni_style = "/uni0043/uni006C/uni0069/uni006E/uni0069/uni0063/uni0061/uni006C"
    assert PA.glyph_leakage(uni_style) > 0.5, "uniXXXX form not detected"

    # pdfminer.six spells the same failure differently: `(cid:6)` rather than `/g6`.
    # Found by running the broken fixture through all three parsers — a detector that
    # knows only pypdf's spelling passes pdfminer's output straight into the index.
    cid_style = "(cid:6) (cid:16)!(cid:7) (cid:25)\"#$ (cid:4)!(cid:3) (cid:17)%(cid:6)"
    assert PA.script_sanity(cid_style) > 0.9, "premise changed: this text looks clean"
    assert PA.glyph_leakage(cid_style) > 0.5, "pdfminer (cid:N) form not detected"

    assert PA.glyph_leakage("ordinary prose with a / slash in it") < 0.05
    assert PA.glyph_leakage("see figure 3 (cid is not a word here)") < 0.05


def test_naive_reading_order_interleaves_columns() -> None:
    """§3.2. The failure must actually reproduce, or the fix proves nothing."""
    doc = PM.extract((ROOT / "report_twocol.pdf").read_bytes())
    naive = PM.page_text(doc, reading_order="naive")[0]
    assert any(
        "Reading order is not stored in the" in line and "Margin requirements" in line
        for line in naive.split("\n")
    ), "naive reading order failed to interleave — fixture no longer demonstrates §3.2"

    clean = PM.page_text(doc, reading_order="columns", drop_lines=PM.detect_running_lines(doc))[0]
    assert not any(
        "Reading order is not stored in the" in line and "Margin requirements" in line
        for line in clean.split("\n")
    ), "column-aware reading order still interleaves"


def test_running_head_and_footer_are_both_detected() -> None:
    """Bug found once: exact matching caught the header and missed the page-numbered
    footer, leaving `Page 3 of 3` spliced into the body. Digit masking is the fix."""
    doc = PM.extract((ROOT / "report_twocol.pdf").read_bytes())
    running = PM.detect_running_lines(doc)
    assert any("Confidential" in r or "Conﬁdential" in r for r in running), "header missed"
    assert any("Page # of #" in r for r in running), "page-numbered footer missed"


def test_hyphenation_and_ligatures_are_repaired() -> None:
    parsed = PA.parse_file(ROOT / "report_twocol.pdf", ROOT)
    canonical = N.canonicalize(parsed.raw_text)
    assert "organizational" in canonical, "line-break hyphen not repaired"
    # The fixture writes `ﬁxed` and `ﬁrst` with the U+FB01 ligature; both must come
    # back as ASCII or a BM25 query for either term cannot match this document.
    assert "ﬁ" not in canonical, "ligature codepoint survived normalization"
    assert "fixed" in canonical and "first" in canonical, "ligature not expanded to ASCII"
    assert "state-of-the-art" == N.canonicalize("state-of-the-art").strip(), (
        "de-hyphenation damaged a real compound"
    )
    assert N.canonicalize("organi-\nzational").strip() == "organizational"


def test_spreadsheet_is_routed_not_prosified() -> None:
    """§3.6. A CSV must yield a table description, not one chunk per row of prose."""
    parsed = PA.parse_file(ROOT / "revenue.csv", ROOT)
    assert parsed.notes["route"] == "query_engine"
    kinds = {e.kind for e in parsed.elements}
    assert PA.DESCRIPTION in kinds, "no natural-language table description was emitted"


def test_quoted_replies_are_stripped_at_parse_time() -> None:
    """§10.3. Stripping at parse time, not deduplicating after the fact."""
    parsed = PA.parse_file(ROOT / "thread.eml", ROOT)
    assert parsed.notes["messages"] == 4
    assert parsed.notes["duplication_ratio"] > 0.5, "quoted text was not the bulk of the file"
    assert ">" not in parsed.raw_text, "quote markers survived into the parsed text"


# --------------------------------------------------------------------------------
# 3. Bugs found once, pinned
# --------------------------------------------------------------------------------


def test_recovered_pdf_table_reaches_the_index() -> None:
    """Bug found once: the PDF table element carried its grid in `meta` but had empty
    `text`, so `build_canonical` dropped it and the recovered grid never became chunks.
    Grid recovery worked perfectly and the result was discarded one stage later."""
    result = PL.process(ROOT / "statement.pdf", ROOT, PL.Config(strategy="structural"))
    rows = [c for c in result.chunks if c.kind == "table_row"]
    assert len(rows) == 6, f"expected 6 table rows from statement.pdf, got {len(rows)}"
    assert any("revenue was 1,204" in c.embed_text for c in rows)


def test_table_rows_cite_their_own_characters() -> None:
    """Bug found once: every row pointed at the whole table's span, so citation
    highlighted the entire table and `content_hash` collapsed six rows into one."""
    result = PL.process(ROOT / "metrics.md", ROOT, PL.Config(strategy="structural"))
    rows = [c for c in result.chunks if c.kind == "table_row"]
    assert len(rows) == 4
    spans = {(c.span.start, c.span.end) for c in rows}
    assert len(spans) == len(rows), "table rows share a span"
    for chunk in rows:
        assert chunk.text.strip().startswith("|"), "row span does not cover a table row"
    assert len({dedup.content_hash(c.text) for c in rows}) == len(rows)


def test_structural_chunks_respect_the_budget_after_contextualization() -> None:
    """Bug found once: the size check ran on the raw slice, then the heading path was
    prepended, so chunks exceeded max_tokens by the length of their own breadcrumb.
    Over the model's context limit is silent truncation, not untidiness (§5.1's C1)."""
    config = PL.Config(strategy="structural", max_tokens=256)
    for result in PL.process_corpus(ROOT, config):
        for chunk in result.chunks:
            if chunk.kind in (PA.CODE, PA.TABLE):
                continue  # atomic elements are allowed over budget, and are reported
            assert chunk.tokens() <= config.max_tokens, (
                f"{result.doc_id}: {chunk.kind} chunk is {chunk.tokens()} tokens "
                f"> {config.max_tokens} after contextualization"
            )


def test_two_column_table_is_not_reported_as_a_grid() -> None:
    """Bug found once: x-clustering reported a confident 2-column 'table' for every
    two-column *text* page. Geometry cannot tell those apart — hence >=3 columns and
    short cells before `confidence == "high"`."""
    parsed = PA.parse_file(ROOT / "report_twocol.pdf", ROOT)
    assert parsed.notes["tables_recovered"] == 0, "two-column prose recovered as a table"
    assert parsed.warnings, "the ambiguity should be reported, not silently dropped"

    statement = PA.parse_file(ROOT / "statement.pdf", ROOT)
    assert statement.notes["tables_recovered"] == 1, "a real table was not recovered"


def test_case_collision_report_suppresses_orthographic_noise() -> None:
    """Bug found once: the report returned every sentence-initial capital and buried
    the eight collisions that matter under `The`/`the`."""
    raw = (ROOT / "notation.md").read_text(encoding="utf-8")
    collisions = N.case_collisions(raw)
    significant = {c.folded for c in collisions if c.significant}
    for expected in ("us", "polish", "it", "who", "sap", "march", "apple", "ai"):
        assert expected in significant, f"{expected!r} should be a significant collision"
    for noise in ("the", "this", "every"):
        assert noise not in significant, f"{noise!r} is orthographic noise, not signal"


def test_percentile_is_correct_for_small_samples() -> None:
    """Bug found once: index arithmetic returned a p95 *below* p50 for n=2, which read
    as a chunker bug in the report and was a bug in the statistic."""
    assert PL._percentile([10, 20], 0.95) == 20
    assert PL._percentile([10, 20], 0.50) == 10
    assert PL._percentile([5], 0.95) == 5
    assert PL._percentile([], 0.95) == 0
    values = list(range(1, 101))
    assert PL._percentile(values, 0.50) == 50
    assert PL._percentile(values, 0.95) == 95


# --------------------------------------------------------------------------------
# 4. Arithmetic from the chapter
# --------------------------------------------------------------------------------


def test_overlap_inflation_matches_the_formula() -> None:
    """§5.5. Measured chunk counts must track `1/(1-f)` within document-boundary error."""
    base = PL.process(ROOT / "book.md", ROOT, PL.Config(strategy="fixed", max_tokens=256))
    for overlap in (26, 51, 128):
        config = PL.Config(strategy="fixed", max_tokens=256, overlap=overlap)
        measured = PL.process(ROOT / "book.md", ROOT, config).metrics.chunks / base.metrics.chunks
        predicted = SP.overlap_inflation(256, overlap)
        # The direction is the real check. `N/S` chunks assumes the document divides
        # evenly; the final window of a single document is short, so measured must sit
        # at or below predicted. Measured *above* predicted would mean the splitter is
        # emitting windows the arithmetic cannot account for.
        assert measured <= predicted + 0.01, (
            f"overlap {overlap}: measured {measured:.3f} exceeds predicted {predicted:.3f}"
        )
        # The magnitude is loose on purpose: one 26 KB document divided into ~23
        # windows pays the boundary rounding once against a small denominator, so the
        # gap reaches ~9% at f=0.5 here and would shrink toward zero over a real corpus.
        assert abs(measured - predicted) < 0.15, (
            f"overlap {overlap}: measured {measured:.3f} vs predicted {predicted:.3f}"
        )
    assert abs(SP.overlap_inflation(256, 51) - 1.25) < 0.01, "20% overlap is not 1.25x"
    assert abs(SP.overlap_inflation(512, 256) - 2.0) < 1e-9, "50% overlap is not 2x"


def test_content_addressed_ids_survive_an_unrelated_edit() -> None:
    """§9.1. Editing the top of a document must not rename the chunks below it."""
    original = (ROOT / "handbook.md").read_text(encoding="utf-8")
    edited = original.replace(
        "The ingestion service turns source documents into indexed chunks.",
        "The ingestion service turns source documents into indexed chunks, idempotently.",
    )
    assert edited != original, "edit anchor no longer matches the fixture"

    def build(text: str) -> list[SP.Chunk]:
        parsed = PA.parse_markdown("handbook.md", "corpus://handbook.md", text.encode("utf-8"))
        elements = [e for e in parsed.elements if N.canonicalize(e.text).strip()]
        canonical = N.build_canonical("handbook.md", [e.text for e in elements])
        return SP.structural(canonical, elements, max_tokens=256, doc_title="handbook.md")

    before = set(identity.assign_ids(build(original), SP.CHUNKER_VERSION, "content"))
    after = set(identity.assign_ids(build(edited), SP.CHUNKER_VERSION, "content"))
    changed = len(before - after)
    assert changed <= 2, f"{changed} chunks changed identity for a one-sentence edit"
    assert len(before & after) >= 7, "most chunks should keep their IDs"


def test_position_addressed_edit_is_invisible_without_a_content_check() -> None:
    """§9.1's real cost. Position IDs do not change when the text does, so a diff on
    IDs alone reports no work and leaves stale vectors in the index."""
    original = (ROOT / "handbook.md").read_text(encoding="utf-8")
    edited = original.replace("indexed chunks.", "indexed chunks, idempotently.")

    def build(text: str) -> list[SP.Chunk]:
        parsed = PA.parse_markdown("handbook.md", "corpus://handbook.md", text.encode("utf-8"))
        elements = [e for e in parsed.elements if N.canonicalize(e.text).strip()]
        canonical = N.build_canonical("handbook.md", [e.text for e in elements])
        return SP.structural(canonical, elements, max_tokens=256, doc_title="handbook.md")

    store = identity.Store()
    identity.reindex_document("handbook.md", build(original), store, scheme="position")
    outcome = identity.reindex_document("handbook.md", build(edited), store, scheme="position")
    assert outcome.added == 0 and outcome.deleted == 0, "premise changed: IDs did shift"
    assert outcome.updated >= 1, (
        "the edit produced no 'updated' rows — the content comparison is missing and "
        "stale vectors would survive the update"
    )


def test_minhash_estimates_jaccard() -> None:
    a = dedup.shingles("the quick brown fox jumps over the lazy dog " * 6)
    b = dedup.shingles("the quick brown fox leaps over the lazy dog " * 6)
    true = len(a & b) / len(a | b)
    estimate = dedup.estimated_jaccard(dedup.minhash(a), dedup.minhash(b))
    assert abs(estimate - true) < 3 * dedup.signature_error(128)
    assert dedup.estimated_jaccard(dedup.minhash(a), dedup.minhash(a)) == 1.0


def test_table_serializations_preserve_the_header_association() -> None:
    """§3.4. Row sentences must name the column; scan order must not."""
    table = TB.Table(["Period", "Revenue"], [["Q1 2024", "1,644"], ["Q2 2024", "1,702"]])
    sentences = TB.to_row_sentences(table)
    assert len(sentences) == 2
    assert "revenue was 1,644" in sentences[0] and "Q1 2024" in sentences[0]
    assert "revenue" not in TB.to_scan_order(table).lower().split("q1")[1]

    parts = TB.split_with_headers(TB.Table(["A", "B"], [[str(i), "x"] for i in range(10)]), 3)
    assert len(parts) == 4
    assert all(part.header == ["A", "B"] for part in parts), "header not repeated in every part"


def test_gate_thresholds_separate_good_from_broken() -> None:
    """Lab 1's success criterion: the gates flag the broken documents and only those."""
    expected_bad = {"scan.pdf", "subset_broken.pdf"}
    flagged = {
        r.doc_id for r in PL.process_corpus(ROOT, PL.Config(strategy="structural"))
        if r.quarantined
    }
    assert flagged == expected_bad, f"gates flagged {flagged}, expected {expected_bad}"


def test_run_and_make_fixtures_execute_cleanly() -> None:
    """The lab's own entry points must run. A README nobody can execute is a document."""
    for script in ("make_fixtures.py", "run.py"):
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / script)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"{script} exited {proc.returncode}:\n{proc.stderr[-800:]}"


# --------------------------------------------------------------------------------


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures.append((name, str(exc)))
            print(f"  FAIL  {name}\n          {exc}")
        except Exception as exc:  # noqa: BLE001 - a crash is a failure too
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
