"""Run the lab and print the report. `python3 run.py [act ...]`, zero dependencies.

Each "act" is one section of `02` made observable on real bytes. Run them all, or name
the ones you want:

    python3 run.py                    # everything
    python3 run.py parse normalize    # just those two
    python3 run.py --list             # the act list with one-line descriptions

Every number printed here is **rung 1 — measured** on *this* corpus (README §6), and
this corpus is 19 synthetic fixtures totalling about 75 KB. That is enough to
demonstrate a mechanism and nowhere near enough to choose a chunk size: §11.6's point
is that the answer depends on your corpus *and* your query distribution, and this lab
has neither. Quote nothing from here as a result about chunking in general.
"""

from __future__ import annotations

import sys
from pathlib import Path

import dedup
import identity
import normalize as N
import parse as PA
import pdfmini as PM
import pipeline as PL
import split as SP
import tables as TB

ROOT = Path(__file__).parent / "corpus"

# What each fixture is *for*. The lab is only useful if you know which failure each
# document is supposed to contain, so this table is the map between the corpus and §3.
CASES = [
    ("transcript.txt", "pure text", "No structure at all — recursive splitting is the floor"),
    ("handbook.md", "markdown", "Headings, code fence — the control: nothing is lost"),
    ("metrics.md", "markdown + table", "§3.4 three serializations, header repetition"),
    ("notation.md", "notation hazards", "§4.2 NFKC destroys x², ½, Ⅻ; case carries US vs us"),
    ("site/*.html", "html + chrome", "§3.5 nav/footer/cookie boilerplate outweighs the body"),
    ("report_clean.pdf", "PDF, 1 column", "Tier 1 works; paragraph breaks are still lost"),
    ("report_twocol.pdf", "PDF, 2 column", "§3.2 column interleaving, running head/foot, hyphens"),
    ("statement.pdf", "PDF table", "§3.2 table flattening; grid recovered by x-clustering"),
    ("scan.pdf", "PDF, image only", "§3.2 no text layer → 0 chunks, 0 errors → OCR route"),
    ("subset_broken.pdf", "PDF, no ToUnicode", "§3.2 mojibake at full length → review route"),
    ("subset_ok.pdf", "PDF, control", "Same layout, resolvable glyph names — the A/B partner"),
    ("revenue.csv", "spreadsheet", "§3.6 wrong tool → route to a query engine"),
    ("service.py.txt", "source code", "§3.7 AST split, enclosing context carried in"),
    ("thread.eml", "email thread", "§10.3 quoted replies duplicated across every message"),
    ("book.md", "book", "§7 parent/child, deep nesting, §12 cost model at scale"),
]

BAR = "=" * 86


def h1(title: str) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")


def h2(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 82 - len(title)))


# --------------------------------------------------------------------------------


def act_corpus() -> None:
    """The corpus, and the gate verdict on each document."""
    h1("ACT 1 — THE CORPUS: what each fixture is for, and does it survive the gate?")
    print(f"{'fixture':<20} {'class':<18} {'demonstrates'}")
    for name, klass, why in CASES:
        print(f"{name:<20} {klass:<18} {why}")

    h2("Gate verdicts (§3.2) — the check that turns silence into a routing decision")
    print(f"{'document':<22} {'pages':>5} {'chars/pg':>9} {'sanity':>7}  {'route':<7} reason")
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        parsed = PA.parse_file(path, ROOT)
        v = PA.gate(parsed)
        mark = " " if v.ok else "!"
        print(
            f"{mark}{parsed.doc_id:<21} {parsed.page_count:>5} {v.yield_per_page:>9.0f} "
            f"{v.sanity:>7.2f}  {v.route:<7} {v.reason}"
        )
    print(
        "\nThe two flagged documents produce ZERO chunks. Without these gates they would\n"
        "produce zero chunks *and no error* — the document silently ceases to exist."
    )


def act_parse() -> None:
    """Reading order, running heads, ligatures, and the encoding cliff."""
    h1("ACT 2 — PARSING: the stage that decides the ceiling (§3)")

    h2("Same bytes, two reading orders (§3.2) — report_twocol.pdf, page 1")
    doc = PM.extract((ROOT / "report_twocol.pdf").read_bytes())
    running = PM.detect_running_lines(doc)
    naive = PM.page_text(doc, reading_order="naive")[0]
    clean = PM.page_text(doc, reading_order="columns", drop_lines=running)[0]

    print("NAIVE — sort every run by (-y, x). Correct on one column, catastrophic on two:")
    for line in naive.split("\n")[:5]:
        print(f"   | {line}")
    print("\nCOLUMN-AWARE + running lines stripped:")
    for line in clean.split("\n")[:5]:
        print(f"   | {line}")
    print(f"\nRunning lines detected and removed: {sorted(running)}")
    print(
        "The naive text is not merely untidy. Every sentence alternates between two\n"
        "unrelated topics, so NO chunk boundary and NO embedding model can repair it.\n"
        "That is §1's ceiling chain: the damage happened upstream of everything else."
    )

    h2("The encoding cliff (§3.2) — identical page, one shipped without a glyph map")
    for name in ("subset_ok.pdf", "subset_broken.pdf"):
        parsed = PA.parse_file(ROOT / name, ROOT)
        v = PA.gate(parsed)
        preview = parsed.raw_text.split("\n")[0][:52]
        print(f"  {name:<20} sanity={v.sanity:.2f} route={v.route:<7} len={len(parsed.raw_text):>4}")
        print(f"  {'':<20} first line: {preview!r}")
    print(
        "\nSame length, same layout, same rendered page. Only the map back to Unicode\n"
        "differs — and nothing but the sanity gate notices."
    )

    h2("Repairs the normalizer makes on PDF text (§4.1)")
    parsed = PA.parse_file(ROOT / "report_twocol.pdf", ROOT)
    canon = N.canonicalize(parsed.raw_text)
    print(f"  hyphenated line break  'organi-\\nzational' → 'organizational': {'organizational' in canon}")
    print(f"  ligature ﬁ expanded to 'fi':                                   {'ﬁ' not in canon}")
    print(f"  ligatures remaining in canonical text:                         "
          f"{sum(canon.count(l) for l in N.LIGATURES)}")


def act_normalize() -> None:
    """What NFKC and lowercasing would destroy on this corpus."""
    h1("ACT 3 — NORMALIZATION: the transforms that destroy meaning (§4.2)")
    raw = (ROOT / "notation.md").read_text(encoding="utf-8")

    h2("Invisible characters present in the source")
    census = N.invisible_census(raw)
    for name, count in census.items():
        print(f"  {count:>3}  {name}")
    print("  Each one breaks exact lexical match and is undetectable in a rendered view.")

    h2("NFKC damage report — every character NFKC would rewrite")
    print(f"  {'char':<4} {'codepoint':<10} {'becomes':<8} {'n':>2}  verdict")
    for d in N.nfkc_damage(raw):
        print(f"  {d.char:<4} {d.codepoint:<10} {d.becomes!r:<8} {d.count:>2}  {d.verdict}")
    wanted = sum(1 for d in N.nfkc_damage(raw) if d.verdict == "wanted")
    total = len(N.nfkc_damage(raw))
    print(
        f"\n  {wanted} of {total} distinct mappings are ones you want (ligatures).\n"
        f"  The other {total - wanted} change what the text means. That ratio is the\n"
        f"  argument for NFC plus an audited replacement list, never NFKC's whole table."
    )

    h2("Case collisions — what lowercasing merges (§4.2, §4.3)")
    collisions = N.case_collisions(raw)
    significant = [c for c in collisions if c.significant]
    for c in significant:
        forms = " / ".join(f"{f}×{n}" for f, n in zip(c.forms, c.counts))
        print(f"  {c.folded:<10} {forms:<22} {c.reason}")
    print(
        f"\n  {len(significant)} significant of {len(collisions)} case variants "
        f"({len(collisions) - len(significant)} are sentence-initial capitals, suppressed).\n"
        "  BM25 wants this folding. The dense branch must not have it — which is exactly\n"
        "  why §4.3 keeps one canonical form and derives both branches from it."
    )

    h2("The three artifacts (§4.3)")
    sample = "The US filing deadline differs from the one us contractors were given."
    print(f"  canonical : {N.canonicalize(sample).strip()}")
    print(f"  embed     : (canonical + heading path — see act 5)")
    print(f"  lexical   : {' '.join(N.analyze(sample))}")
    print("              ^ 'US' and 'us' are now the same token. Correct here, fatal upstream.")


def act_tables() -> None:
    """The three serializations, and the free fix."""
    h1("ACT 4 — TABLES: the hardest case, and the one with a concrete fix (§3.4)")

    parsed = PA.parse_file(ROOT / "statement.pdf", ROOT)
    element = next(e for e in parsed.elements if e.kind == PA.TABLE)
    grid = element.meta["table"]
    table = TB.Table(grid.header, grid.rows, "Table 3. Quarterly results, thousands of USD.",
                     "Northwind consolidated results")

    print("Recovered from positioned glyph runs by x-clustering — there was no table object.")
    print(f"Shape: {table.shape[0]} rows x {table.shape[1]} columns\n")

    h2("1. Scan order — what naive extraction gives you")
    print("  " + TB.to_scan_order(table)[:150] + " ...")
    print("  → embeds to something adjacent to 'numbers about quarters'; answers nothing.")

    h2("2. Markdown — column alignment preserved")
    for line in TB.to_markdown(table).split("\n")[:4]:
        print("  " + line)
    print("  ...")

    h2("3. Row-wise sentences — the header↔cell association stated in prose")
    for sentence in TB.to_row_sentences(table)[:2]:
        print("  " + sentence)
    print("  → each row is independently retrievable, in the shape dense models are trained on.")

    h2("The pattern that gets both: index the rows, return the table (§7.1)")
    tk = SP.DEFAULT_TOKENIZER
    print(f"  index unit  : row sentence, ~{tk.count(TB.to_row_sentences(table)[0])} tokens")
    print(f"  return unit : full table,   ~{tk.count(TB.to_markdown(table))} tokens")

    h2("Header repetition on a split table (§3.4) — free, deterministic")
    for part in TB.split_with_headers(table, 3):
        first = TB.to_markdown(part).split("\n")[0]
        print(f"  part: {first}   ({len(part.rows)} rows, header repeated)")
    print("  Without this, one chunk has column headers and the rest are unlabeled grids.")


def act_chunk() -> None:
    """Strategy comparison across document classes."""
    h1("ACT 5 — CHUNKING: four strategies across document classes (§6)")
    tk = SP.DEFAULT_TOKENIZER

    print(f"{'document':<20} {'strategy':<14} {'n':>4} {'p50':>4} {'p95':>4} {'max':>4} "
          f"{'orph':>4} {'over':>4}")
    for name in ("transcript.txt", "handbook.md", "metrics.md", "service.py.txt",
                 "report_twocol.pdf", "book.md"):
        for strategy in ("fixed", "recursive", "structural", "parent_child"):
            config = PL.Config(strategy=strategy, max_tokens=256, child_tokens=64)
            result = PL.process(ROOT / name, ROOT, config)
            m = result.metrics
            print(f"{name if strategy == 'fixed' else '':<20} {strategy:<14} {m.chunks:>4} "
                  f"{m.p50:>4} {m.p95:>4} {m.max_tokens_seen:>4} {m.orphans:>4} {m.oversize:>4}")
        print()

    print("orph = chunks under min_tokens (the greedy splitter's orphan, §6.2)")
    print("over = chunks above max_tokens. Nonzero here only for atomic code elements:")
    print("       a whole class that cannot be split without becoming §3.7's problem.")
    print("       That is a real, unresolved conflict with C1 (§5.1), not a bug — and it")
    print("       is the kind of thing a chunker must report rather than hide.")

    h2("Heading-path prefixing — the free contextualization (§6.3)")
    result = PL.process(ROOT / "book.md", ROOT, PL.Config(strategy="structural"))
    deep = max(result.chunks, key=lambda c: len(c.heading_path))
    print(f"  raw chunk  : {deep.text[:70].strip()!r}")
    print(f"  embed text : {deep.embed_text[:110].strip()!r}")
    print("  Same effect as an LLM-written context on this example. No API call, fully")
    print("  deterministic, and therefore no chunk-ID churn on reprocessing (§9.1).")

    h2("Parent/child collapse ratio (§7.4)")
    pc = PL.process(ROOT / "book.md", ROOT, PL.Config(strategy="parent_child", child_tokens=64))
    children = [c for c in pc.chunks if c.parent_span]
    parents = {(c.parent_span.start, c.parent_span.end) for c in children}
    print(f"  {len(children)} children map to {len(parents)} distinct parents "
          f"({len(children) / max(len(parents), 1):.1f}x collapse)")
    print("  'Top 10' therefore means ten children but far fewer parents' worth of text.")
    print("  Budget in tokens AFTER parent expansion, never in k before it (§5.3, §7.4).")


def act_tokens() -> None:
    """§5.4 — count tokens, not characters."""
    h1("ACT 6 — CHARACTERS PER TOKEN BY CONTENT TYPE (§5.4)")
    tk = SP.DEFAULT_TOKENIZER
    print("A character-based splitter set to one size yields wildly different token")
    print("counts across these documents. That is why the unit matters.\n")
    print(f"{'document':<22} {'class':<18} {'chars':>7} {'tokens':>7} {'chars/token':>12}")
    for name, klass, _ in CASES:
        if "*" in name:
            continue
        parsed = PA.parse_file(ROOT / name, ROOT)
        text = N.canonicalize(parsed.raw_text)
        if not text.strip():
            continue
        n = tk.count(text)
        print(f"{name:<22} {klass:<18} {len(text):>7,} {n:>7,} {len(text) / max(n, 1):>12.2f}")
    print(f"\nTokenizer: {tk.name} — an ESTIMATOR, not cl100k. Run bakeoff.py with tiktoken")
    print("installed to measure this approximation's error against the real thing.")


def act_overlap() -> None:
    """§15 lab 3 — measured overlap inflation vs the 1/(1-f) prediction."""
    h1("ACT 7 — OVERLAP INFLATION: measured against the formula (§5.5, lab 3)")
    result_base = PL.process(ROOT / "book.md", ROOT, PL.Config(strategy="fixed", max_tokens=256))
    base = result_base.metrics.chunks
    base_tokens = result_base.metrics.tokens_total

    print(f"Corpus: book.md, fixed 256-token chunks, baseline = {base} chunks\n")
    print(f"{'overlap':>8} {'f':>6} {'chunks':>7} {'measured':>9} {'predicted':>10} {'delta':>7}")
    for overlap in (0, 26, 51, 77, 128):
        config = PL.Config(strategy="fixed", max_tokens=256, overlap=overlap)
        m = PL.process(ROOT / "book.md", ROOT, config).metrics
        f = overlap / 256
        measured = m.chunks / base
        predicted = SP.overlap_inflation(256, overlap)
        print(f"{overlap:>8} {f:>6.2f} {m.chunks:>7} {measured:>9.3f} {predicted:>10.3f} "
              f"{measured - predicted:>+7.3f}")
    print(f"\nBaseline embedded tokens: {base_tokens:,}")
    print("20% overlap costs 25% more, not 20% — on the one-time embedding bill AND on")
    print("the recurring storage bill, forever. Deviations from the prediction come from")
    print("document-boundary effects: the last window of a document is short.")


def act_dedup() -> None:
    """§10 and §3.5 — duplication, measured."""
    h1("ACT 8 — DUPLICATION: how much of the corpus is a copy of itself (§10, lab 8)")

    h2("Quoted email replies (§10.3) — stripped at parse time, not deduplicated later")
    parsed = PA.parse_file(ROOT / "thread.eml", ROOT)
    notes = parsed.notes
    print(f"  {notes['messages']} messages, {notes['quoted_chars_stripped']:,} characters of "
          f"quoted repetition removed")
    print(f"  duplication ratio: {notes['duplication_ratio']:.1%} of the thread's text was quotation")
    print("  Collapsing it after the fact would lose the thread structure too, so §10.3")
    print("  says strip at parse time and keep the thread as metadata. That is what runs here.")

    h2("Site chrome (§3.5) — two detectors, and why you want both")
    pages: dict[str, list[str]] = {}
    tag_flagged = 0
    total_blocks = 0
    for path in sorted((ROOT / "site").glob("*.html")):
        parsed = PA.parse_file(path, ROOT)
        pages[parsed.doc_id] = [e.text for e in parsed.elements]
        tag_flagged += sum(1 for e in parsed.elements if e.meta.get("chrome"))
        total_blocks += len(parsed.elements)
    repeated = dedup.repeated_block_filter(pages, threshold=0.30)
    print(f"  tag/class heuristic flagged      : {tag_flagged}/{total_blocks} blocks")
    print(f"  corpus repeated-block detection  : {len(repeated)} distinct blocks on >30% of pages")
    for block in sorted(repeated)[:3]:
        print(f"      {block[:70]!r}")
    print("  The tag heuristic misses site-specific boilerplate in an ordinary <div>;")
    print("  the corpus heuristic needs more than one page from a domain. Run both.")

    h2("Near-duplicate census across all chunks (lab 8)")
    results = PL.process_corpus(ROOT, PL.Config(strategy="structural"))
    texts = {
        r.chunk_id: r.text
        for result in results
        for r in result.records
    }
    report = dedup.duplication_report(texts, threshold=0.80)
    print(f"  {report.total} chunks | exact-duplicate groups: {report.exact_groups} "
          f"({report.exact_redundant} redundant)")
    print(f"  near-duplicate pairs at Jaccard >= 0.80: {report.near_pairs} "
          f"({report.near_redundant} redundant)")
    print(f"  corpus redundancy: {report.redundancy:.1%}")
    print(f"  MinHash standard error at 128 permutations: +/-{dedup.signature_error(128):.3f}")
    print("  That error is why a 0.80 threshold is defensible and 0.85 vs 0.90 is not.")


def act_identity() -> None:
    """§15 lab 9 — the incremental update rehearsal."""
    h1("ACT 9 — IDENTITY AND INCREMENTAL UPDATE (§9, lab 9)")

    original = (ROOT / "handbook.md").read_text(encoding="utf-8")
    # Edit one sentence in the FIRST paragraph — the worst case for position addressing.
    edited = original.replace(
        "The ingestion service turns source documents into indexed chunks.",
        "The ingestion service turns source documents into indexed chunks, idempotently.",
    )
    assert edited != original, "fixture text changed; update the edit anchor"

    def build(text: str) -> list[SP.Chunk]:
        parsed = PA.parse_markdown("handbook.md", "corpus://handbook.md", text.encode("utf-8"))
        elements = [e for e in parsed.elements if N.canonicalize(e.text).strip()]
        canonical = N.build_canonical("handbook.md", [e.text for e in elements])
        return SP.structural(canonical, elements, max_tokens=256, doc_title="handbook.md")

    print(f"Document: handbook.md, {len(build(original))} chunks, structural/256")
    print("Edit: eight words appended to the first sentence of the first paragraph.\n")
    print(f"{'scenario':<26} {'scheme':<10} {'added':>6} {'updated':>8} {'deleted':>8} "
          f"{'unchanged':>10} {'embeds':>7}")
    for row in identity.rehearse_edit(build, original, edited, "handbook.md"):
        r = row.result
        print(f"{row.scenario:<26} {row.scheme:<10} {r.added:>6} {r.updated:>8} "
              f"{r.deleted:>8} {r.unchanged:>10} {r.embed_calls:>7}")

    print("\nRead the two 'edit' rows against each other. Content addressing re-embeds only")
    print("the chunks whose text changed. Position addressing keeps every ID — the ordinals")
    print("are stable — but the TEXT behind those IDs shifted, so every chunk from the edit")
    print("point onward must be re-embedded, and they show up as 'updated'.")
    print()
    print("That 'updated' column is the trap. A pipeline that diffs on IDs alone (the")
    print("obvious implementation) sees no change under position addressing and skips the")
    print("re-embed entirely — leaving stale vectors in the index under current-looking")
    print("IDs. The cost of position addressing is not the churn; it is that the churn is")
    print("invisible unless you compare content you were trying not to have to compare.")
    print()
    print("Both no-op rows are zero. Reprocessing an unchanged document is free under")
    print("either scheme: position addressing is not broken by reprocessing, only by")
    print("EDITING (§9.1).")


def act_cost() -> None:
    """§12 — the cost model, with every input labeled as an assumption."""
    h1("ACT 10 — COST MODEL (§12)")
    results = PL.process_corpus(ROOT, PL.Config(strategy="structural"))
    chunks = sum(r.metrics.chunks for r in results)
    tokens = sum(r.metrics.tokens_total for r in results)
    parse_ms = sum(r.metrics.parse_ms for r in results)
    split_ms = sum(r.metrics.split_ms for r in results)

    print("Measured on this corpus:")
    print(f"  documents        : {len(results)} ({sum(1 for r in results if r.quarantined)} quarantined)")
    print(f"  chunks           : {chunks}")
    print(f"  embedded tokens  : {tokens:,} (estimator, not cl100k)")
    print(f"  parse wall time  : {parse_ms:7.1f} ms")
    print(f"  split wall time  : {split_ms:7.1f} ms  ({split_ms / max(parse_ms, 0.01):.2f}x parse)")
    print(f"  index bytes      : {PL.estimate_index_bytes(chunks):,} at 1536-dim float32")

    print("\nScaled to a 100M-token corpus. ASSUMPTIONS, all of them checkable:")
    print("  - 512-token chunks, 15% overlap")
    print("  - embedding at $0.02 per million tokens (verify before quoting — 01 §4)")
    print("  - 1536-dim float32 vectors = 6,144 bytes each, before graph overhead\n")
    inflation = SP.overlap_inflation(512, 77)
    embedded = 100_000_000 * inflation
    n_chunks = int(100_000_000 / (512 - 77))
    print(f"  overlap inflation      : {inflation:.3f}x")
    print(f"  tokens embedded        : {embedded / 1e6:,.1f}M  ->  ${embedded / 1e6 * 0.02:,.2f}")
    print(f"  chunks                 : {n_chunks:,}")
    print(f"  vector bytes           : {n_chunks * 6144 / 1e9:.2f} GB")
    print(f"\n  Now add contextual retrieval at ~100 generated tokens per chunk:")
    print(f"  generated tokens       : {n_chunks * 100 / 1e6:,.1f}M, at GENERATION prices.")
    print(f"  Even at cheap-model rates that is one to two orders of magnitude above the")
    print(f"  ${embedded / 1e6 * 0.02:,.2f} embedding bill — which is the argument for doing the free")
    print(f"  version (heading paths, §6.3) first and measuring what is left.")

    h2("Chunk size is the dominant storage lever (§12.2)")
    print(f"  {'chunk size':>10} {'stride':>7} {'chunks':>12} {'vector bytes':>14}")
    for size in (256, 512, 1024):
        stride = int(size * 0.85)
        n = int(100_000_000 / stride)
        print(f"  {size:>10} {stride:>7} {n:>12,} {n * 6144 / 1e9:>13.2f} GB")
    print("  Halving chunk size doubles the index. This multiplies with MRL truncation")
    print("  and quantization from `01` — the decisions are not separable.")


ACTS = {
    "corpus": act_corpus,
    "parse": act_parse,
    "normalize": act_normalize,
    "tables": act_tables,
    "chunk": act_chunk,
    "tokens": act_tokens,
    "overlap": act_overlap,
    "dedup": act_dedup,
    "identity": act_identity,
    "cost": act_cost,
}


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for name, fn in ACTS.items():
            print(f"  {name:<10} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    if not ROOT.exists():
        print("corpus/ is missing — run `python3 make_fixtures.py` first", file=sys.stderr)
        return 1

    chosen = [a for a in argv if not a.startswith("-")] or list(ACTS)
    unknown = [a for a in chosen if a not in ACTS]
    if unknown:
        print(f"unknown act(s): {', '.join(unknown)}\navailable: {', '.join(ACTS)}", file=sys.stderr)
        return 1
    for name in chosen:
        ACTS[name]()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
