"""Run production tooling over the same fixtures and score what each one misses.

    uv venv .venv && uv pip install -r requirements-bakeoff.txt
    .venv/bin/python bakeoff.py                     # the default run: tier 1, every section
    .venv/bin/python bakeoff.py --list              # adapters, tiers, document classes
    .venv/bin/python bakeoff.py --help              # every filter

Most teams do not write a chunker; they configure LangChain's or LlamaIndex's and read
PDFs with PyMuPDF or pypdf. So the question that actually matters is not "what is the
best chunker" but **"what does the toolset I already run do to my documents, and where
does it quietly lose something."** This harness answers that on a corpus where the
right answer is known by construction, which is the only way the question is
answerable at all: on a real corpus you cannot tell a parser bug from a hard document.

Every check below is a **known-answer test**, not a preference:

| Check | Fixture | Known answer |
|---|---|---|
| Reading order | `report_twocol.pdf` | Left column is one topic, right is another. Interleaving is wrong. |
| Running heads | `report_twocol.pdf` | `Northwind…Confidential` on all 3 pages is chrome. |
| Empty text layer | `scan.pdf` | Extracts to `""`. Does the library say so, or return success? |
| Broken encoding | `subset_broken.pdf` | 129 chars of garbage. Does anything complain? |
| Ligature / hyphen | `report_twocol.pdf` | `classiﬁcation`, `organi-\\nzational` must be repaired. |
| Field association | `invoice.pdf` | Every label keeps its own value and every line its own amount. |
| Table integrity | `metrics.md` | 4-row table. Splitting it mid-grid loses the header. |
| Code integrity | `service.py.txt` | 2 functions + 1 class. Splitting mid-function is wrong. |
| Notation | `notation.md` | `x²`, `Ⅻ`, `½` must survive the loader. |
| Duplication | `site/*.html`, `thread.eml` | 5 pages share chrome; the thread quotes itself 4 deep. |
| Tokenizer | all | `tiktoken` is ground truth for this lab's estimator. |

**Answering it for one document class.** Appendix D §5 shortlists a parser per
constraint and then tells you to run that shortlist against your own documents. The
`probe` section is that sentence made executable — one class, end to end, through
whichever parsers and chunkers you name:

    bakeoff.py --only probe --doc invoice
    bakeoff.py --only probe --doc invoice --parser docling --chunker semchunk --show-chunks 3
    bakeoff.py --only pdf --tier 1 2            # add the layout models to the parser table

`--list` prints the document classes, the fixture standing in for each, and how many
known-answer checks it carries.

**Tier 2 is opt-in.** `--tier 2` adds Docling, Marker and `unstructured`'s `hi_res`
path. They download model weights on first use and cost tens of seconds per page on
CPU, so the default run is tier 1 and reproduces the numbers quoted in the README.

**A missing adapter is not a failure.** Anything not installed prints `not installed`
and is skipped, so a partial install still produces a comparison. Nothing here calls a
network or an API key, beyond the one-time model download `--tier 2` triggers.

**What this is not.** It is not a benchmark and it does not produce a ranking. Chunk
counts and split behaviour are facts about a library; whether they matter to *your*
retrieval is §15's lab 5, which needs a golden set this lab does not have. Read the
columns as "what would I have to know about this tool before trusting it", not as a
score.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import logging
import sys
import time
import unicodedata
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import normalize as N
import parse as PA
import split as SP

ROOT = Path(__file__).parent / "corpus"

# Third-party chatter drowns the comparison table. These are the libraries' own advice
# messages ("chunk size is small", "fitz is deprecated"), not findings about the corpus.
warnings.filterwarnings("ignore")
# `logging.disable` rather than per-logger levels: these libraries bind their handlers
# at import time, so redirecting stderr inside the call does not reach them and naming
# each logger only works until the next release renames one. Errors still surface —
# `_run_split` and `_run_pdf` catch and report exceptions themselves.
logging.disable(logging.WARNING)


def have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def h1(title: str) -> None:
    print(f"\n{'=' * 88}\n{title}\n{'=' * 88}")


def h2(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 84 - len(title)))


def skip(name: str, module: str) -> None:
    print(f"  {name:<34} not installed  (pip install {module})")


@dataclass
class Options:
    """Everything the CLI can vary, threaded through every section.

    The defaults reproduce the run whose output is quoted in the README, so `bakeoff.py`
    with no arguments stays the reproducible artifact and every flag is a deviation
    from it. `tiers` defaults to tier 1 alone because tier 2 downloads model weights
    and costs tens of seconds per page — opt in with `--tier 2`.
    """

    tiers: set[int] = field(default_factory=lambda: {1})
    parsers: list[str] = field(default_factory=list)
    chunkers: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    max_tokens: int = 128
    show_chunks: int = 0


# --------------------------------------------------------------------------------
# Tokenizer ground truth
# --------------------------------------------------------------------------------


def section_tokenizer(opts: Options) -> None:
    """How wrong is this lab's token estimator? (§5.4)"""
    h1("TOKENIZER — this lab's estimator vs real cl100k")
    if not have("tiktoken"):
        skip("tiktoken", "tiktoken")
        print("\n  Without this, every token count in run.py is an estimate with unknown error.")
        return

    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    ours = SP.DEFAULT_TOKENIZER

    print(f"  {'document':<22} {'chars':>7} {'cl100k':>8} {'estimate':>9} {'error':>8} {'c/tok':>7}")
    errors = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        parsed = PA.parse_file(path, ROOT)
        text = N.canonicalize(parsed.raw_text)
        if len(text) < 200:
            continue
        real = len(enc.encode(text, disallowed_special=()))
        est = ours.count(text)
        err = (est - real) / max(real, 1)
        errors.append(abs(err))
        print(f"  {parsed.doc_id:<22} {len(text):>7,} {real:>8,} {est:>9,} "
              f"{err:>+7.1%} {len(text) / max(real, 1):>7.2f}")

    print(f"\n  mean absolute error: {sum(errors) / len(errors):.1%} across {len(errors)} documents")
    print("  The estimator is close enough to demonstrate that chars/token varies by content")
    print("  type and nowhere near close enough to set a production chunk size. Use tiktoken.")


# --------------------------------------------------------------------------------
# PDF parsers
# --------------------------------------------------------------------------------


@dataclass
class PdfResult:
    name: str
    text: str
    ms: float
    note: str = ""
    error: str = ""


def _pypdf(data: bytes) -> str:
    import io

    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _pymupdf(data: bytes, mode: str = "text") -> str:
    """PyMuPDF, in two modes that behave very differently on a two-column page.

    `"text"` returns a plain string in the library's own reading order. `"blocks"`
    returns tuples of `(x0, y0, x1, y1, text, block_no, block_type)` — geometry the
    caller can use to detect columns, which is the whole reason to prefer it. Joining
    the tuples' text field in the order returned is the *naive* use of blocks mode and
    is what most code does; sorting them by (x-band, y) is the useful one.
    """
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    if mode != "blocks":
        return "\n".join(page.get_text(mode) for page in doc)
    out: list[str] = []
    for page in doc:
        for block in page.get_text("blocks"):
            out.append(str(block[4]).strip())
    return "\n".join(b for b in out if b)


def _pymupdf_sorted(data: bytes) -> str:
    """`get_text("text", sort=True)` — the one-flag fix, which does not work here.

    Worth running precisely because it is the obvious thing to reach for and it fails.
    `sort=True` orders *blocks* by position, but on a row-band-emitted two-column page
    PyMuPDF has already grouped left and right text into the **same block** — the
    interleaving is inside a block, where sorting blocks cannot reach it.
    """
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text("text", sort=True) for page in doc)


def _pymupdf_clipped(data: bytes) -> str:
    """Per-column clip rectangles — the approach that actually recovers the columns.

    Extract each column separately by restricting `get_text` to a rectangle, so the
    grouping happens *within* a column and cannot cross the gutter. This is the
    twenty lines a caller has to write on top of any tier-1 library, and the fact that
    they are yours to write is the practical content of the tier-1/tier-2 boundary in
    §3.3 — a layout model finds the column rectangles for you.

    The midpoint split here is hardcoded, which is fine for a two-column fixture and
    wrong for anything else. Finding the gutter is `pdfmini.find_gutter()`'s job.
    """
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    out: list[str] = []
    for page in doc:
        width, height = page.rect.width, page.rect.height
        midpoint = width / 2
        for rect in (
            pymupdf.Rect(0, 0, midpoint, height),
            pymupdf.Rect(midpoint, 0, width, height),
        ):
            text = page.get_text("text", clip=rect).strip()
            if text:
                out.append(text)
    return "\n".join(out)


def _pdfminer(data: bytes) -> str:
    import io

    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(data))


# --------------------------------------------------------------------------------
# Tier 2 — layout models (appendix D §2.1)
# --------------------------------------------------------------------------------
# Everything above reconstructs text from glyph coordinates with hand-written
# heuristics. Everything below runs a detection model over a rendered page, gets back
# regions typed as paragraph / table / figure, and extracts within each region. That is
# the whole tier boundary, and §5.1's per-column clip rectangles are the twenty lines
# you write to fake it.
#
# Three practical differences the table below will show, none of which is accuracy:
#
#   * **Cost.** Tier 1 is milliseconds; these are tens of seconds per page on CPU.
#     Appendix D quotes 0.5–5 pages/sec on GPU; this lab measures CPU, so read the
#     `ms` column as an upper bound, not as the number you would run in production.
#   * **Determinism.** Still deterministic — these are detection models, not VLMs — so
#     §9's content-addressed IDs survive. Tier 3 is where that stops being true.
#   * **First-run weight.** Each downloads model weights on first use (hundreds of MB)
#     and none of them says so before it starts. That is why they are opt-in behind
#     `--tier 2` rather than part of the default run.
#
# Each is wrapped so an import failure, a missing weight file or an unsupported device
# degrades to a reported error rather than aborting the comparison.

_TIER2_CACHE: dict[tuple[str, str], str] = {}
_MODELS: dict[str, Any] = {}


def _docling(data: bytes) -> str:
    """Docling → Markdown, via the `DoclingDocument` model (appendix D §4.1).

    The accelerator is pinned to CPU deliberately. Left on `AUTO` this raises on Apple
    Silicon — the layout model asks for a float64 tensor and MPS has no float64 — which
    is a good example of appendix D §3.3's point 4: the throughput number in a
    benchmark table assumes a device you may not have.

    `export_to_markdown()` is a *lossy view* of the real output. The reason to run
    Docling is `res.document`, a typed tree of headings, tables and lists that §6.3's
    structure-aware splitting can walk directly. Comparing its Markdown against
    pypdf's plain text — which is what this harness does — measures the one thing
    Docling is not for. Read the tier-2 rows as "did the layout model recover the
    structure", and remember the structure survives in the object, not in this string.
    """
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    if "docling" not in _MODELS:
        options = PdfPipelineOptions()
        options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
        _MODELS["docling"] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
    converter = _MODELS["docling"]
    result = converter.convert(DocumentStream(name="doc.pdf", stream=io.BytesIO(data)))
    return result.document.export_to_markdown()


def _marker(data: bytes) -> str:
    """Marker v2 → Markdown (appendix D §4.2). GPL-3.0; check that before you ship it.

    `create_model_dict()` loads several hundred MB of weights, so the converter is
    built once and cached for the process. Marker accepts a `BytesIO`, which is worth
    noting because the other two want a path or a temp directory.
    """
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    if "marker" not in _MODELS:
        _MODELS["marker"] = PdfConverter(artifact_dict=create_model_dict())
    rendered = _MODELS["marker"](io.BytesIO(data))
    out = text_from_rendered(rendered)
    return out[0] if isinstance(out, tuple) else str(out)


# MinerU (appendix D §2.2) has no adapter here, and the reason is worth more than the
# row would have been: **it cannot share a virtualenv with Marker.** `marker-pdf` 2.0
# requires `transformers>=5.12.1,<6`; `mineru` 3.4.4 requires `transformers<5.0.0`.
# Both are listed side by side in appendix D §2.2 as tier-2 options, both resolve
# cleanly on their own, and installing the second silently breaks the first — the
# failure surfaces as an `ImportError` from deep inside a model file, not from pip.
#
# The general point survives the specific versions: tier-2 parsers pin heavy ML stacks
# and a shortlist of two can be un-installable as a pair. Price that before you plan a
# bake-off, and if you need both, run them in separate environments behind a subprocess
# boundary rather than trying to resolve one.


def _unstructured(data: bytes, strategy: str) -> str:
    """`unstructured` with an explicit `strategy` — its per-document tier selector.

    This is the one library that spans the boundary: `fast` is tier 1 (it calls
    pdfminer underneath), `hi_res` runs a layout model, `ocr_only` goes to Tesseract.
    Running `fast` and `hi_res` side by side prices the tier-2 upgrade within a single
    API, which is appendix D §4.3's argument for reaching for it first.
    """
    from unstructured.partition.pdf import partition_pdf

    elements = partition_pdf(file=io.BytesIO(data), strategy=strategy)
    return "\n".join(str(e) for e in elements)


@dataclass(frozen=True)
class PdfAdapter:
    """One way of turning PDF bytes into text, with the tier it belongs to.

    `fn is None` marks this lab's own path, which is called differently (it needs the
    `pdfmini` document, not just the text) and is always present.
    """

    name: str
    tier: int
    fn: object = None
    module: str = ""


def _run_pdf(name: str, fn, data: bytes) -> PdfResult:
    """Run one adapter, memoized on (adapter, bytes).

    The cache is not an optimization detail — it is what makes tier 2 usable here. Each
    section re-parses the same handful of fixtures, so a full run asks each adapter for
    `report_twocol_interleaved.pdf` six times. At thirty seconds a call that is three
    minutes of recomputing an identical answer per adapter.
    """
    key = (name, hashlib.sha256(data).hexdigest())
    if key in _TIER2_CACHE:
        return PdfResult(name, _TIER2_CACHE[key], 0.0, note="cached")
    t0 = time.perf_counter()
    try:
        text = fn(data)
    except Exception as exc:  # a library that raises is *better* than one that lies
        return PdfResult(name, "", (time.perf_counter() - t0) * 1000, error=f"{type(exc).__name__}: {exc}")
    _TIER2_CACHE[key] = text
    return PdfResult(name, text, (time.perf_counter() - t0) * 1000)


def pdf_adapters(tiers: set[int] | None = None, name_filter: list[str] | None = None) -> list[PdfAdapter]:
    """Every installed PDF adapter matching the requested tiers and name substrings."""
    out: list[PdfAdapter] = [PdfAdapter("this lab (tier 1, columns)", 1)]
    if have("pypdf"):
        out.append(PdfAdapter("pypdf", 1, _pypdf, "pypdf"))
    if have("pymupdf"):
        out.append(PdfAdapter("pymupdf (text)", 1, lambda d: _pymupdf(d, "text"), "pymupdf"))
        out.append(PdfAdapter("pymupdf (blocks, as-is)", 1, lambda d: _pymupdf(d, "blocks"), "pymupdf"))
        out.append(PdfAdapter("pymupdf (sort=True)", 1, _pymupdf_sorted, "pymupdf"))
        out.append(PdfAdapter("pymupdf (per-column clip)", 1, _pymupdf_clipped, "pymupdf"))
    if have("pdfminer"):
        out.append(PdfAdapter("pdfminer.six", 1, _pdfminer, "pdfminer"))
    if have("unstructured"):
        out.append(PdfAdapter("unstructured (fast)", 1, lambda d: _unstructured(d, "fast"), "unstructured"))
        # `auto` is tier 2 because that is where it can end up: it tries the cheap path
        # and escalates. Worth its own row precisely because the escalation is invisible
        # — the same call costs 3 ms or 10 s depending on the document.
        out.append(PdfAdapter("unstructured (auto)", 2, lambda d: _unstructured(d, "auto"), "unstructured"))
        out.append(PdfAdapter("unstructured (hi_res)", 2, lambda d: _unstructured(d, "hi_res"), "unstructured"))
    if have("docling"):
        out.append(PdfAdapter("docling", 2, _docling, "docling"))
    if have("marker"):
        out.append(PdfAdapter("marker v2", 2, _marker, "marker"))

    if tiers is not None:
        out = [a for a in out if a.tier in tiers]
    if name_filter:
        needles = [n.lower() for n in name_filter]
        out = [a for a in out if any(n in a.name.lower() for n in needles)]
    return out


def section_pdf(opts: Options) -> None:
    """Reading order, empty text layers, and broken encodings across real parsers."""
    tiers = "+".join(str(t) for t in sorted(opts.tiers))
    h1(f"PDF PARSERS — the same bytes through every tier-{tiers} library you have")

    adapters = pdf_adapters(opts.tiers, opts.parsers)
    if not adapters:
        print("  no adapter matches the current --tier/--parser filters")
        return
    for module in ("pypdf", "pymupdf", "pdfminer", "docling", "marker", "unstructured"):
        if not have(module):
            skip(f"pdf: {module}", {"pdfminer": "pdfminer.six", "marker": "marker-pdf"}.get(module, module))
    if 2 in opts.tiers:
        print("  tier 2 runs a layout model per page: expect tens of seconds per document")
        print("  on CPU, and a model download on first use. Results are memoized per run.")

    h2("Reading order on a two-column page (§3.2)")
    print("  KNOWN ANSWER: the left column is about PDF reading order; the right is about")
    print("  margin requirements. A parser that interleaves them produces sentences that")
    print("  alternate between two subjects, and no downstream stage can repair it.")
    print()
    print("  Two fixtures, IDENTICAL rendered pages and identical text, differing only in")
    print("  the order their Tj operators appear in the content stream:")
    print("    report_twocol.pdf             — whole left column emitted, then the right")
    print("    report_twocol_interleaved.pdf — emitted row-band by row-band across the")
    print("                                    gutter, which is what real producers do")
    print()
    print("  A parser that passes the first and fails the second is ordering by EMISSION,")
    print("  not by position — and it will fail on most real two-column documents.\n")

    import pdfmini as PM

    fixtures = ["report_twocol.pdf", "report_twocol_interleaved.pdf"]
    print(f"  {'parser':<32} {'column-ordered emission':<30} {'row-band emission'}")
    for adapter in adapters:
        verdicts = []
        for fixture in fixtures:
            data = (ROOT / fixture).read_bytes()
            if adapter.fn is None:
                doc = PM.extract(data)
                running = PM.detect_running_lines(doc)
                text = "\n".join(PM.page_text(doc, reading_order="columns", drop_lines=running))
                verdicts.append(_reading_order_verdict(text))
            else:
                r = _run_pdf(adapter.name, adapter.fn, data)
                verdicts.append(f"ERROR {r.error[:20]}" if r.error else _reading_order_verdict(r.text))
        short = [v.split("—")[0].strip()[:28] for v in verdicts]
        label = f"{adapter.name} [t{adapter.tier}]"
        print(f"  {label:<32} {short[0]:<30} {short[1]}")

    print()
    print("  pdfminer.six recovers the columns in BOTH fixtures — its LAParams layout")
    print("  analysis groups text by position. pypdf and PyMuPDF's default text mode follow")
    print("  emission order and interleave. Note that `sort=True` does NOT rescue PyMuPDF:")
    print("  it sorts blocks, and PyMuPDF has already merged both columns into one block, so")
    print("  the interleaving is inside a block where block sorting cannot reach it. Only")
    print("  per-column clip rectangles fix it, and finding those rectangles is your job.")
    print()
    print("  first body line of page 1, row-band fixture:")
    for adapter in adapters:
        data = (ROOT / "report_twocol_interleaved.pdf").read_bytes()
        if adapter.fn is None:
            doc = PM.extract(data)
            text = "\n".join(PM.page_text(doc, reading_order="columns",
                                          drop_lines=PM.detect_running_lines(doc)))
        else:
            r = _run_pdf(adapter.name, adapter.fn, data)
            if r.error:
                continue
            text = r.text
        line = next(
            (ln for ln in text.split("\n")
             if len(ln.strip()) > 20 and "Northwind" not in ln and "Page " not in ln),
            "",
        )
        print(f"    {adapter.name:<32} {line.strip()[:52]!r}")

    h2("Empty text layer (§3.2) — scan.pdf: does the library tell you?")
    print("  KNOWN ANSWER: two pages of image, zero text. Every parser should return ''.")
    print("  The question is whether that is reported as a condition or as success.\n")
    data = (ROOT / "scan.pdf").read_bytes()
    for adapter in adapters:
        if adapter.fn is None:
            doc = PM.extract(data)
            text = "\n".join(PM.page_text(doc))
            gate = PA.gate(PA.parse_file(ROOT / "scan.pdf", ROOT))
            print(f"  {adapter.name:<28} chars={len(text.strip()):>4}  → routed to {gate.route!r}")
            continue
        r = _run_pdf(adapter.name, adapter.fn, data)
        status = f"ERROR {r.error}" if r.error else f"chars={len(r.text.strip()):>4}  → returned normally"
        print(f"  {r.name:<28} {status}")
    print("\n  Every library returns the empty string without complaint. That is correct")
    print("  behaviour and it is exactly the problem: the caller must add the gate.")

    h2("Broken encoding (§3.2) — subset_broken.pdf vs subset_ok.pdf")
    print("  KNOWN ANSWER: identical layout; one ships resolvable glyph names, one does not.\n")
    for fixture in ("subset_ok.pdf", "subset_broken.pdf"):
        data = (ROOT / fixture).read_bytes()
        print(f"  {fixture}")
        for adapter in adapters:
            name = adapter.name
            if adapter.fn is None:
                doc = PM.extract(data)
                text = "\n".join(PM.page_text(doc))
            else:
                r = _run_pdf(name, adapter.fn, data)
                if r.error:
                    print(f"    {name:<26} ERROR {r.error[:44]}")
                    continue
                text = r.text
            sanity = PA.script_sanity(text)
            leak = PA.glyph_leakage(text)
            ctrl = sum(1 for c in text if unicodedata.category(c) in ("Cc", "Co", "Cn")
                       and c not in "\n\r\t")
            caught = "CAUGHT" if (sanity < 0.80 or leak > 0.20) else "MISSED"
            print(f"    {name:<26} chars={len(text.strip()):>4} sanity={sanity:.2f} "
                  f"ctrl={ctrl:>3} glyph-leak={leak:>4.0%}  gate: {caught}")
    print("\n  Read the CAUGHT/MISSED column as 'would this pipeline have noticed', not as")
    print("  a score for the parser. Three things are in it:")
    print()
    print("  1. NO library raises or warns on the broken file. Every one returns text of")
    print("     plausible length, and they fail in three DIFFERENT shapes:")
    print()
    print("       control characters   this lab, pymupdf   → script_sanity sees it (0.17)")
    print("       /g1/g2/g3 names      pypdf               → sanity 1.00, clean ASCII")
    print("       (cid:6) markers      pdfminer.six        → sanity 1.00, clean ASCII")
    print()
    print("     A gate calibrated against one parser's failure shape misses the other two.")
    print("     That is why glyph_leakage() runs alongside script_sanity() in gate(), and")
    print("     it is a concrete reason `parser_version` belongs on every chunk: change")
    print("     the parser and you change what your gates are able to see.")
    print()
    print("  2. pypdf is flagged on subset_ok.pdf too, and that is CORRECT rather than a")
    print("     false positive. pypdf does not resolve `uniXXXX` glyph names either, so it")
    print("     returns 3,117 characters of /uni0043/uni006C… for the *good* file. The")
    print("     control is only a control for parsers that can read the font at all.")
    print()
    print("  3. pymupdf(sort=True) returns 163 characters where every other mode returns")
    print("     394 — it silently drops most of the page. Clean sanity, clean leak score,")
    print("     and 60% of the document gone. No single gate catches every failure shape;")
    print("     an extraction-yield check against a *sibling parser* is what catches this.")

    h2("Ligatures and hyphenation (§3.2, §4.1) — do they arrive repairable?")
    print("  The fixture breaks `organi-` / `zational` across two lines of the left column.")
    print("  De-hyphenation (§4.1) fires on `(\\w)-\\n(\\w)`, so it needs the continuation to")
    print("  still be the NEXT LINE after the parse.\n")
    data = (ROOT / "report_twocol.pdf").read_bytes()
    for adapter in adapters:
        if adapter.fn is None:
            doc = PM.extract(data)
            text = "\n".join(
                PM.page_text(doc, reading_order="columns",
                             drop_lines=PM.detect_running_lines(doc))
            )
        else:
            r = _run_pdf(adapter.name, adapter.fn, data)
            if r.error:
                continue
            text = r.text
        lig = sum(text.count(l) for l in N.LIGATURES)
        canon = N.canonicalize(text)
        print(f"  {adapter.name:<32} ligatures={lig:>2} → after normalize: "
              f"ligatures={sum(canon.count(l) for l in N.LIGATURES):>2}, "
              f"hyphen repaired={str('organizational' in canon):<5}")

    # The compounding failure, isolated. This is the same extractor and the same
    # normalizer; only the reading order differs.
    doc = PM.extract(data)
    naive = "\n".join(PM.page_text(doc, reading_order="naive"))
    columns = "\n".join(
        PM.page_text(doc, reading_order="columns", drop_lines=PM.detect_running_lines(doc))
    )
    print()
    print("  Same extractor, same normalizer, two reading orders:")
    for label, text in (("naive", naive), ("columns", columns)):
        line = next((l for l in text.split("\n") if "organi" in l), "")
        print(f"    {label:<8} hyphen line: {line[:52]!r}")
        print(f"    {label:<8} de-hyphenation fires: {'organizational' in N.canonicalize(text)}")

    print()
    print("  This is the compounding failure worth taking away. Under naive reading order")
    print("  `The organi-` is joined with the RIGHT column's text on the same line, so the")
    print("  continuation `zational` is no longer the next line and the de-hyphenation rule")
    print("  can never match. A parser defect silently disabled a normalizer rule two")
    print("  stages downstream, and the term stays unsearchable — §1's ceiling chain with")
    print("  a second-order effect. Fixing the normalizer would not have helped.")
    print()
    print("  Note also that every parser emits the ligature codepoint as-is. That is")
    print("  CORRECT — it is what the file says. Expansion is the normalizer's job (§4.1)")
    print("  and it does not happen unless you do it.")


# Phrases known to belong to one column or the other, in document order. Checking
# *order* rather than per-line adjacency is what makes this test valid: a parser that
# emits one run per line never puts two columns on one line, so a line-based check
# passes trivially and tells you nothing.
_LEFT_PHRASES = [
    "Reading order is not stored",
    "content stream places glyph",
    "reconstructed from the geometry",
    "Two extractors will",
]
_RIGHT_PHRASES = [
    "Margin requirements for the retail",
    "following the regulator",
    "figure is fourteen percent",
    "positions held longer",
]


def _reading_order_verdict(text: str) -> str:
    """Did the parser interleave the columns? Judged by position, not by line.

    The left column must be read contiguously and then the right, or vice versa. If
    any right-column phrase appears *between* two left-column phrases, the parser has
    read row-band by row-band across the gutter — §3.2's column interleaving.
    """
    flat = " ".join(text.split())
    left = [flat.find(p) for p in _LEFT_PHRASES]
    right = [flat.find(p) for p in _RIGHT_PHRASES]
    if any(p < 0 for p in left) or any(p < 0 for p in right):
        missing = sum(1 for p in left + right if p < 0)
        return f"inconclusive — {missing}/8 marker phrases not found (text altered or lost)"

    crossings = sum(1 for r in right if min(left) < r < max(left))
    crossings += sum(1 for l in left if min(right) < l < max(right))
    if crossings:
        return f"INTERLEAVED — {crossings} cross-gutter transition(s)"
    return "columns kept separate"


# --------------------------------------------------------------------------------
# Splitters
# --------------------------------------------------------------------------------


@dataclass
class SplitResult:
    name: str
    chunks: list[str] = field(default_factory=list)
    error: str = ""
    ms: float = 0.0


def splitter_adapters(max_tokens: int, name_filter: list[str] | None = None) -> dict[str, object]:
    """Every splitter available, normalized to `f(text) -> list[str]`."""
    out: dict[str, object] = {}

    def lab(text: str) -> list[str]:
        canonical = N.build_canonical("x", [text])
        return [c.text for c in SP.recursive(canonical, max_tokens=max_tokens)]

    out["this lab: recursive"] = lab

    if have("langchain_text_splitters"):
        from langchain_text_splitters import (
            MarkdownHeaderTextSplitter,
            RecursiveCharacterTextSplitter,
        )

        # Character-based, the library default unit. ~4 chars/token on prose.
        out["langchain: recursive(chars)"] = lambda t: RecursiveCharacterTextSplitter(
            chunk_size=max_tokens * 4, chunk_overlap=0
        ).split_text(t)

        if have("tiktoken"):
            out["langchain: recursive(tiktoken)"] = lambda t: (
                RecursiveCharacterTextSplitter.from_tiktoken_encoder(
                    encoding_name="cl100k_base", chunk_size=max_tokens, chunk_overlap=0
                ).split_text(t)
            )

        # Chroma's fix from §6.2: add sentence terminators to the separator list.
        out["langchain: +sentence seps"] = lambda t: RecursiveCharacterTextSplitter(
            chunk_size=max_tokens * 4,
            chunk_overlap=0,
            separators=["\n\n", "\n", ".", "?", "!", " ", ""],
        ).split_text(t)

        def md_headers(text: str) -> list[str]:
            splitter = MarkdownHeaderTextSplitter(
                headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")]
            )
            return [d.page_content for d in splitter.split_text(text)]

        out["langchain: markdown headers"] = md_headers

    if have("llama_index.core"):
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.core.schema import Document as LIDocument

        def li_sentence(text: str) -> list[str]:
            splitter = SentenceSplitter(chunk_size=max_tokens, chunk_overlap=0)
            return [n.get_content() for n in splitter.get_nodes_from_documents([LIDocument(text=text)])]

        out["llamaindex: SentenceSplitter"] = li_sentence

        def li_hierarchical(text: str) -> list[str]:
            from llama_index.core.node_parser import HierarchicalNodeParser

            parser = HierarchicalNodeParser.from_defaults(
                chunk_sizes=[max_tokens * 4, max_tokens, max_tokens // 2]
            )
            return [n.get_content() for n in parser.get_nodes_from_documents([LIDocument(text=text)])]

        out["llamaindex: Hierarchical"] = li_hierarchical

    if have("semchunk") and have("tiktoken"):
        from semchunk.semchunk import chunkerify
        import tiktoken

        chunker = chunkerify(tiktoken.get_encoding("cl100k_base"), max_tokens)
        out["semchunk"] = lambda t: list(chunker(t))

    if have("chonkie"):
        try:
            from chonkie import RecursiveChunker

            # The library default is `tokenizer="character"`, so `chunk_size=128`
            # means 128 CHARACTERS — roughly an eighth of what the same number means
            # to every other splitter here. Both are shown because the gap between
            # them is §5.4 and §13's anti-pattern 2 happening in a shipped default,
            # not in a cautionary tale: same parameter name, same value, ~5x the
            # chunk count. Read your library's unit before you read its benchmarks.
            default = RecursiveChunker(chunk_size=max_tokens)
            out["chonkie: Recursive (default=chars)"] = lambda t: [c.text for c in default.chunk(t)]

            tokenwise = RecursiveChunker(tokenizer="cl100k_base", chunk_size=max_tokens)
            out["chonkie: Recursive (cl100k)"] = lambda t: [c.text for c in tokenwise.chunk(t)]
        except Exception:
            pass

    if name_filter:
        needles = [n.lower() for n in name_filter]
        out = {k: v for k, v in out.items() if any(n in k.lower() for n in needles)}
    return out


def _run_split(name: str, fn, text: str) -> SplitResult:
    """Run one splitter, capturing whatever it decides to print at us.

    Several libraries emit advice on every call — LlamaIndex's `TokenTextSplitter`
    uses a bare `print()` for "metadata length is close to chunk size", which no
    logging configuration can suppress. At ten adapters x three fixtures that buries
    the table it is printed next to. Captured, not silenced: exceptions are still
    caught and reported in the `error` field below.
    """
    sink = io.StringIO()
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            chunks = [c for c in fn(text) if c and c.strip()]
    except Exception as exc:
        return SplitResult(name, error=f"{type(exc).__name__}: {exc}"[:70])
    return SplitResult(name, chunks, ms=(time.perf_counter() - t0) * 1000)


def section_splitters(opts: Options) -> None:
    """Chunk shape, and the two integrity checks with known answers."""
    h1(f"SPLITTERS — chunk shape across libraries, at a nominal {opts.max_tokens}-token budget")

    for module, pip in (
        ("langchain_text_splitters", "langchain-text-splitters"),
        ("llama_index.core", "llama-index-core"),
        ("semchunk", "semchunk"),
        ("chonkie", "chonkie"),
    ):
        if not have(module):
            skip(f"splitter: {module}", pip)

    adapters = splitter_adapters(opts.max_tokens, opts.chunkers)
    if not adapters:
        print("  no splitter matches the current --chunker filter")
        return
    tk = SP.DEFAULT_TOKENIZER

    for fixture in _pick(("transcript.txt", "book.md", "notation.md"), opts.docs):
        text = N.canonicalize((ROOT / fixture).read_text(encoding="utf-8"))
        h2(f"{fixture} — {len(text):,} chars")
        print(f"  {'splitter':<32} {'n':>4} {'p50':>5} {'max':>5} {'orph':>5} {'ms':>7}")
        for name, fn in adapters.items():
            r = _run_split(name, fn, text)
            if r.error:
                print(f"  {name:<32} ERROR {r.error}")
                continue
            counts = sorted(tk.count(c) for c in r.chunks)
            p50 = counts[len(counts) // 2] if counts else 0
            orphans = sum(1 for c in counts if c < 24)
            print(f"  {name:<32} {len(r.chunks):>4} {p50:>5} {counts[-1] if counts else 0:>5} "
                  f"{orphans:>5} {r.ms:>7.1f}")

    print("\n  Note what the two structure-aware rows are NOT doing: LangChain's")
    print("  MarkdownHeaderTextSplitter and LlamaIndex's HierarchicalNodeParser do not")
    print("  enforce a size budget at all. Their 'max' column runs well past the nominal")
    print("  128, which on a real corpus means chunks the embedding model will silently")
    print("  truncate (§5.1's C1). Structure-aware splitting still needs a size fallback.")

    h2("KNOWN ANSWER 1 — does the splitter cut through a table? (metrics.md, §3.4)")
    print("  The 4-row table must stay whole, or the rows lose their column headers.")
    print("  Read this WITH the size column above: a splitter that never splits anything")
    print("  passes this test trivially and fails the context limit instead.\n")
    text = N.canonicalize((ROOT / "metrics.md").read_text(encoding="utf-8"))
    header_line = "| Region | Q1 revenue | Q2 revenue | Q1 margin | Q2 margin | Headcount |"
    for name, fn in splitter_adapters(64, opts.chunkers).items():
        r = _run_split(name, fn, text)
        if r.error:
            continue
        touching = [c for c in r.chunks if "| EMEA |" in c or "| LATAM |" in c]
        intact = any(header_line in c and "| LATAM |" in c for c in r.chunks)
        headerless = sum(1 for c in touching if header_line not in c)
        verdict = "table intact" if intact else f"SPLIT — {headerless} row chunk(s) with no header"
        print(f"  {name:<32} {verdict}")
    print("\n  This is why §3.4 says repeat the header row in every piece of a split table.")
    print("  No general-purpose text splitter does it, because none of them know it is a table.")

    h2("KNOWN ANSWER 2 — does the splitter cut through a function? (service.py.txt, §3.7)")
    print("  Two module-level functions and one class. A chunk boundary inside a def is wrong.\n")
    code = (ROOT / "service.py.txt").read_text(encoding="utf-8")
    import ast

    tree = ast.parse(code)
    defs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))]

    def broken_count(chunks: list[str]) -> int:
        broken = 0
        for chunk in chunks:
            try:
                ast.parse(chunk)
            except SyntaxError:
                broken += 1
        return broken

    for name, fn in splitter_adapters(96, opts.chunkers).items():
        r = _run_split(name, fn, code)
        if r.error:
            continue
        print(f"  {name:<36} {len(r.chunks):>3} chunks, {broken_count(r.chunks):>2} do not parse")

    # The lab's own AST path, measured rather than asserted. Every other row above is a
    # general-purpose text splitter applied to source code; this one uses the language's
    # own parser, which is free and exact (§3.7).
    parsed = PA.parse_file(ROOT / "service.py.txt", ROOT)
    ast_chunks = [e.text for e in parsed.elements if e.kind == PA.CODE]
    print(f"  {'this lab: parse_code (AST)':<36} {len(ast_chunks):>3} chunks, "
          f"{broken_count(ast_chunks):>2} do not parse")

    print(f"\n  The file defines {len(defs)} symbols: {', '.join(defs)}")
    print("  'Does not parse' is a proxy, not proof — but a chunk that is not valid Python")
    print("  has been cut somewhere a reader would not cut it. Only the AST path scores")
    print("  zero, and it does so by construction rather than by tuning.")

    if have("langchain_text_splitters"):
        h2("BONUS — LangChain's language-aware splitter on the same file")
        from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter.from_language(
            Language.PYTHON, chunk_size=96 * 4, chunk_overlap=0
        )
        chunks = splitter.split_text(code)
        broken = 0
        for chunk in chunks:
            try:
                ast.parse(chunk)
            except SyntaxError:
                broken += 1
        print(f"  from_language(PYTHON)            {len(chunks):>3} chunks, {broken:>2} do not parse")
        print("  Better than the generic separators — it splits on `\\nclass `/`\\ndef ` — but it")
        print("  is still separator matching, not parsing, so it has no notion of nesting.")


# --------------------------------------------------------------------------------
# Loaders and notation
# --------------------------------------------------------------------------------


def section_notation(opts: Options) -> None:
    """Does the toolchain preserve x², Ⅻ, ½ — or quietly fold them? (§4.2)"""
    h1("NOTATION SURVIVAL — what the loaders do to mathematics, numerals and case")

    raw = (ROOT / "notation.md").read_text(encoding="utf-8")
    # (label, probe, must_survive). The ligature is the one character here that SHOULD
    # be rewritten — `ﬁ` → `fi` is the compatibility mapping you want (§4.1), and a
    # pipeline that preserves it leaves the term lexically unreachable. Marking it
    # separately is the difference between a report and a scoreboard that punishes the
    # correct answer.
    probes = [
        ("superscript ² (x²)", "²", True),
        ("fraction ½", "½", True),
        ("Roman numeral Ⅻ", "Ⅻ", True),
        ("micro sign µ", "µ", True),
        ("full-width Ａ", "Ａ", True),
        ("US (vs us)", "US", True),
        ("ligature ﬁ", "ﬁ", False),
    ]

    variants: dict[str, str] = {"source file": raw, "this lab: canonicalize": N.canonicalize(raw)}
    variants["NFKC (the reflex)"] = unicodedata.normalize("NFKC", raw)
    variants["NFKC + lower (the habit)"] = unicodedata.normalize("NFKC", raw).lower()

    if have("unstructured"):
        try:
            from unstructured.partition.md import partition_md

            variants["unstructured: partition_md"] = "\n".join(
                str(e) for e in partition_md(text=raw)
            )
        except Exception as exc:
            print(f"  unstructured partition_md failed: {type(exc).__name__}: {exc}"[:88])
    else:
        skip("loader: unstructured", "unstructured")

    print(f"  {'variant':<28} " + " ".join(f"{label.split()[0][:8]:>9}" for label, _, _ in probes))
    for label, text in variants.items():
        marks = []
        for _, probe, must_survive in probes:
            present = probe in text
            if must_survive:
                marks.append(f"{'ok' if present else 'LOST':>9}")
            else:
                marks.append(f"{'present' if present else 'expanded':>9}")
        print(f"  {label:<28} " + " ".join(marks))

    print("\n  The last column is the one that SHOULD change: expanding ﬁ to fi is the")
    print("  compatibility mapping you want (§4.1), and leaving it makes the term")
    print("  unreachable by any BM25 query for 'classification'.")
    print()
    print("  Every other column must survive. NFKC alone destroys the superscript, the")
    print("  fraction, the Roman numeral and the full-width form — while fixing the")
    print("  ligature. Adding .lower() then merges US into us. Both are one-line")
    print("  'cleanups' that a reviewer would wave through.")


# --------------------------------------------------------------------------------
# HTML and duplication
# --------------------------------------------------------------------------------


def section_duplication(opts: Options) -> None:
    """Chrome removal and near-duplicate detection, against known answers."""
    h1("DUPLICATION — chrome extraction and near-duplicate detection")

    h2("HTML main-content extraction (§3.5) — site/*.html")
    print("  KNOWN ANSWER: every page carries the same nav, cookie banner, related-articles")
    print("  rail and footer. Only the <article> body differs.\n")

    page = (ROOT / "site/backpressure.html").read_text(encoding="utf-8")
    parsed = PA.parse_file(ROOT / "site/backpressure.html", ROOT)
    body_only = "\n".join(e.text for e in parsed.elements if not e.meta.get("chrome"))
    print(f"  {'extractor':<32} {'chars kept':>11} {'chrome left':>12}")
    print(f"  {'this lab: tag/class filter':<32} {len(body_only):>11} "
          f"{'yes' if 'Copyright 2024' in body_only else 'no':>12}")

    if have("bs4"):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page, "lxml" if have("lxml") else "html.parser")
        all_text = soup.get_text(" ", strip=True)
        print(f"  {'bs4: get_text() (no filtering)':<32} {len(all_text):>11} "
              f"{'yes' if 'Copyright 2024' in all_text else 'no':>12}")
        for tag in soup(["nav", "footer", "aside", "script", "style"]):
            tag.decompose()
        stripped = soup.get_text(" ", strip=True)
        print(f"  {'bs4: after decompose(nav…)':<32} {len(stripped):>11} "
              f"{'yes' if 'Copyright 2024' in stripped else 'no':>12}")
    else:
        skip("html: beautifulsoup4", "beautifulsoup4 lxml")

    if have("unstructured"):
        try:
            from unstructured.partition.html import partition_html

            text = "\n".join(str(e) for e in partition_html(text=page))
            print(f"  {'unstructured: partition_html':<32} {len(text):>11} "
                  f"{'yes' if 'Copyright 2024' in text else 'no':>12}")
        except Exception as exc:
            print(f"  unstructured partition_html failed: {type(exc).__name__}"[:88])

    print("\n  The cookie banner sits in a plain <div class='cookie-banner'>, so tag-based")
    print("  stripping misses it. That is §3.5's argument for running corpus-level")
    print("  repeated-block detection as well — it needs no per-site rules.")

    h2("MinHash — this lab's implementation vs datasketch")
    if not have("datasketch"):
        skip("dedup: datasketch", "datasketch")
    else:
        from datasketch import MinHash

        import dedup

        a = (ROOT / "site/backpressure.html").read_text(encoding="utf-8")
        b = (ROOT / "site/deletes.html").read_text(encoding="utf-8")
        pairs = [("chrome-heavy page A vs B", a, b), ("A vs itself", a, a)]
        for label, x, y in pairs:
            sx, sy = dedup.shingles(x), dedup.shingles(y)
            true = len(sx & sy) / max(len(sx | sy), 1)
            ours = dedup.estimated_jaccard(dedup.minhash(sx), dedup.minhash(sy))
            mx, my = MinHash(num_perm=128), MinHash(num_perm=128)
            for s in sx:
                mx.update(s.encode())
            for s in sy:
                my.update(s.encode())
            print(f"  {label:<28} true={true:.3f}  this lab={ours:.3f}  "
                  f"datasketch={mx.jaccard(my):.3f}")
        print(f"\n  Both estimators sit within the ±{dedup.signature_error(128):.3f} standard error")
        print("  that 128 permutations buys. That bound is the reason a 0.80 threshold is")
        print("  defensible and distinguishing 0.85 from 0.90 is not.")

    h2("Quoted-reply duplication (§10.3) — thread.eml")
    print("  KNOWN ANSWER: 4 messages; each quotes everything before it, so message 1's")
    print("  text appears 4 times in the file.\n")
    raw = (ROOT / "thread.eml").read_text(encoding="utf-8")
    parsed = PA.parse_file(ROOT / "thread.eml", ROOT)
    print(f"  raw file                         {len(raw):>6} chars")
    print(f"  this lab: quoted blocks stripped {len(parsed.raw_text):>6} chars "
          f"({parsed.notes['duplication_ratio']:.0%} was quotation)")
    if have("unstructured"):
        try:
            from unstructured.partition.email import partition_email

            elements = partition_email(text=raw)
            text = "\n".join(str(e) for e in elements)
            print(f"  unstructured: partition_email    {len(text):>6} chars "
                  f"({len(elements)} elements)")
        except Exception as exc:
            print(f"  unstructured partition_email     failed: {type(exc).__name__}"[:88])
    print("\n  A splitter run over the raw file indexes the same paragraph up to four times.")
    print("  §10.3's point is that this is a PARSING fix, not a deduplication one: strip the")
    print("  quoted blocks and keep the thread structure as metadata.")


# --------------------------------------------------------------------------------
# Document classes — appendix D §5's shortlist, made runnable
# --------------------------------------------------------------------------------
# Appendix D's decision matrix shortlists a parser per constraint and tells you to
# "run your shortlist against your own documents in labs/document-processing". This is
# the table that makes that sentence executable: each row names the fixture that stands
# in for a document class, the tool appendix D would shortlist, and — where the class
# has a crisp known answer — the checks that say whether the shortlisted tool actually
# delivered it on this corpus.
#
# The checks import their expected values from `make_fixtures`, so the fixture and the
# assertion about it cannot drift apart. Editing a line item in the generator changes
# what the check expects, by construction.

import make_fixtures as MF  # noqa: E402  — fixture data doubles as the known answers


@dataclass(frozen=True)
class DocClass:
    """One document class: what stands in for it, what to reach for, what to verify."""

    name: str
    fixtures: tuple[str, ...]
    shortlist: str  # appendix D §5 / README §6.1 first choice
    chunk_with: str
    bites: str  # the failure this class is prone to
    checks: tuple[tuple[str, Callable[[str], bool]], ...] = ()


def _flat(text: str) -> str:
    return " ".join(text.split())


def _blocks_contiguous(text: str, block: list[str], other: list[str]) -> bool:
    """Did `block` survive as one run, or did `other` get spliced into it?

    Same positional test as `_reading_order_verdict`, for the same reason: a per-line
    adjacency check passes trivially on any parser that emits one run per line.
    """
    flat = _flat(text)
    positions = [flat.find(line) for line in block]
    if any(p < 0 for p in positions) or positions != sorted(positions):
        return False
    intruders = [p for p in (flat.find(line) for line in other) if p >= 0]
    return not any(min(positions) < p < max(positions) for p in intruders)


def _invoice_field(text: str) -> bool:
    """`Invoice Number` must yield exactly `NW-2024-0731`, not that plus its neighbour.

    The value is read as "everything up to the first line break after the label",
    skipping any blank lines between the two. Both halves of that matter, and the
    second was a bug here first: a parser that emits the label and the value as
    separate elements — which is what a layout model does, and what you *want* — has
    a blank line between them, and a naive `line.split(":")` scores it as a failure
    for being correct. The failure this check is looking for is the opposite one: the
    value running on into the neighbouring column's label.
    """
    index = text.find("Invoice Number")
    if index < 0:
        return False
    after = text[index + len("Invoice Number"):].lstrip(": \t\r\n")
    return after.split("\n", 1)[0].strip() == "NW-2024-0731"


def _invoice_line_items(text: str) -> bool:
    """Every description must still sit next to its own amount.

    "Next to" is bounded by the description's own length plus room for the quantity and
    unit price. A parser that reads column-wise puts all five descriptions together and
    all five amounts together, which fails this by hundreds of characters while losing
    no tokens at all — the failure mode no yield or sanity gate in this lab can see.
    """
    flat = _flat(text)
    for desc, qty, unit in MF.INVOICE_ITEMS:
        d = flat.find(desc)
        a = flat.find(f"{qty * unit:,.2f}")
        if d < 0 or a < d or a - d > len(desc) + 40:
            return False
    return True


def _invoice_totals(text: str) -> bool:
    flat = _flat(text)
    return all(
        f"{v:,.2f}" in flat
        for v in (MF.INVOICE_SUBTOTAL, MF.INVOICE_TAX, MF.INVOICE_FREIGHT, MF.INVOICE_TOTAL)
    )


DOC_CLASSES: dict[str, DocClass] = {
    "invoice": DocClass(
        "invoice", ("invoice.pdf",),
        shortlist="tier 2 (Docling / Marker) or a cloud prebuilt model (appendix D §2.3)",
        chunk_with="do not chunk — one record per document; index fields, return the page",
        bites="every token survives and only the associations are lost; no gate sees it",
        checks=(
            ("label→value  (Invoice Number is NW-2024-0731 and nothing else)", _invoice_field),
            ("bill-to block intact  (no ship-to line spliced in)",
             lambda t: _blocks_contiguous(t, MF.INVOICE_BILL_TO, MF.INVOICE_SHIP_TO)),
            ("ship-to block intact  (no bill-to line spliced in)",
             lambda t: _blocks_contiguous(t, MF.INVOICE_SHIP_TO, MF.INVOICE_BILL_TO)),
            ("line item → its own amount  (all 5)", _invoice_line_items),
            ("all four totals present", _invoice_totals),
        ),
    ),
    "multicolumn": DocClass(
        "multicolumn", ("report_twocol.pdf", "report_twocol_interleaved.pdf"),
        shortlist="pdfminer.six (tier 1) or a layout model (tier 2)",
        chunk_with="structure-aware if the parser returns elements, else recursive",
        bites="pypdf and PyMuPDF interleave columns; sort=True does not fix it (§5.1)",
        checks=(("columns kept separate",
                 lambda t: _reading_order_verdict(t) == "columns kept separate"),),
    ),
    "financial-table": DocClass(
        "financial-table", ("statement.pdf", "metrics.md"),
        shortlist="tier 2 — table structure is the deciding metric (appendix D §3.3)",
        chunk_with="row-wise sentences for the index, full table for the answer (§3.4)",
        bites="edit-similarity benchmarks score a destroyed grid as a pass",
    ),
    "scanned": DocClass(
        "scanned", ("scan.pdf",),
        shortlist="OCR or a VLM — every parser here returns '' and returns normally",
        chunk_with="nothing until text exists",
        bites="success and emptiness are indistinguishable without a yield gate (§5.3)",
        checks=(("extracts to empty", lambda t: len(t.strip()) == 0),),
    ),
    "broken-font": DocClass(
        "broken-font", ("subset_broken.pdf", "subset_ok.pdf"),
        shortlist="any — the point is the gate, not the parser",
        chunk_with="n/a — quarantine before chunking",
        bites="three parsers fail in three different shapes; one gate catches one (§5.2)",
    ),
    "html": DocClass(
        "html", ("site/backpressure.html", "site/deletes.html"),
        shortlist="unstructured.partition_html or bs4, plus repeated-block detection",
        chunk_with="header-aware, then recursive within a section",
        bites="chrome is 30–40% of bytes and tag rules miss <div class='cookie-banner'>",
    ),
    "email": DocClass(
        "email", ("thread.eml",),
        shortlist="strip quoted replies at parse time; keep the thread as metadata",
        chunk_with="one chunk per message, thread id in the payload",
        bites="66% duplication if you skip it (§5.9)",
    ),
    "code": DocClass(
        "code", ("service.py.txt",),
        shortlist="the language's own AST (`ast`, tree-sitter)",
        chunk_with="AST boundaries plus enclosing context",
        bites="from_language() is separator matching, not parsing (§5.7)",
    ),
    "notation": DocClass(
        "notation", ("notation.md",),
        shortlist="anything, but normalize with NFC only",
        chunk_with="anything",
        bites="one NFKC call destroys x², ½, Ⅻ, µ and full-width forms (§5.8)",
    ),
    "spreadsheet": DocClass(
        "spreadsheet", ("revenue.csv",),
        shortlist="load into DuckDB or Postgres",
        chunk_with="do not chunk rows into prose — index a table description",
        bites="vector search never aggregates, never joins, never exhausts (§3.6)",
    ),
    "longform": DocClass(
        "longform", ("book.md", "handbook.md"),
        shortlist="native — no library needed",
        chunk_with="parent/child: small children, section parents",
        bites="budget tokens after parent expansion, never k before it (§7.4)",
    ),
    "transcript": DocClass(
        "transcript", ("transcript.txt",),
        shortlist="plain",
        chunk_with="recursive with sentence separators",
        bites="the one place overlap earns its cost — no author-drawn boundaries exist",
    ),
}


def _pick(candidates, filters: list[str]):
    """Filter by case-insensitive substring; no filter means everything."""
    if not filters:
        return list(candidates)
    needles = [f.lower() for f in filters]
    return [c for c in candidates if any(n in str(c).lower() for n in needles)]


def section_probe(opts: Options) -> None:
    """One document class, end to end, through the parsers and chunkers you named.

    This is the section to reach for when the question is "how does my stack do on
    *this kind of document*" rather than "what do these libraries do in general" — it
    runs parse → gate → normalize → chunk on one class and prints that class's known
    answers next to the result. `--doc invoice --parser docling --chunker semchunk` is
    the shape appendix D §5 asks you to run against your own corpus.
    """
    classes = [c for name, c in DOC_CLASSES.items() if not opts.docs or _pick([name], opts.docs)]
    if not classes:
        print(f"  no document class matches {opts.docs!r}")
        print(f"  available: {', '.join(DOC_CLASSES)}")
        return

    adapters = pdf_adapters(opts.tiers, opts.parsers)
    chunkers = splitter_adapters(opts.max_tokens, opts.chunkers)
    tk = SP.DEFAULT_TOKENIZER
    import pdfmini as PM

    for cls in classes:
        h1(f"PROBE — {cls.name}")
        print(f"  fixtures      {', '.join(cls.fixtures)}")
        print(f"  parse with    {cls.shortlist}")
        print(f"  chunk with    {cls.chunk_with}")
        print(f"  what bites    {cls.bites}")

        for fixture in cls.fixtures:
            path = ROOT / fixture
            if not path.exists():
                print(f"\n  {fixture}: missing — run `python3 make_fixtures.py`")
                continue
            data = path.read_bytes()
            h2(f"{fixture} — parse")

            texts: dict[str, str] = {}
            if path.suffix == ".pdf":
                for adapter in adapters:
                    if adapter.fn is None:
                        doc = PM.extract(data)
                        text = "\n".join(PM.page_text(
                            doc, reading_order="columns",
                            drop_lines=PM.detect_running_lines(doc)))
                        ms = 0.0
                    else:
                        r = _run_pdf(adapter.name, adapter.fn, data)
                        if r.error:
                            print(f"  {adapter.name:<30} ERROR {r.error[:44]}")
                            continue
                        text, ms = r.text, r.ms
                    texts[adapter.name] = text
                    print(f"  {adapter.name:<30} [t{adapter.tier}] {len(text):>6,} chars {ms:>8.0f} ms")
            else:
                # Non-PDF classes have exactly one parser in this lab: its own router.
                parsed = PA.parse_file(path, ROOT)
                texts["this lab: parse_file"] = parsed.raw_text
                print(f"  {'this lab: parse_file':<30} [t1] {len(parsed.raw_text):>6,} chars"
                      f"   route={PA.gate(parsed).route!r}")
                if opts.parsers:
                    print("  (--parser only applies to PDF fixtures)")

            if cls.checks and texts:
                h2(f"{fixture} — known answers")
                width = max(len(label) for label, _ in cls.checks)
                print(f"  {'parser':<30} " + " ".join(f"{i + 1:>3}" for i in range(len(cls.checks))))
                for name, text in texts.items():
                    marks = []
                    for _, check in cls.checks:
                        try:
                            marks.append(" ok" if check(text) else "  X")
                        except Exception:
                            marks.append("err")
                    print(f"  {name:<30} " + " ".join(f"{m:>3}" for m in marks))
                print()
                for i, (label, _) in enumerate(cls.checks):
                    print(f"    {i + 1}. {label:<{width}}")

            if not chunkers or not texts:
                continue
            h2(f"{fixture} — chunk at {opts.max_tokens} tokens")
            print(f"  {'parser × chunker':<52} {'n':>4} {'p50':>5} {'max':>5} {'orph':>5}")
            for parser_name, text in texts.items():
                canonical = N.canonicalize(text)
                if not canonical.strip():
                    print(f"  {parser_name + ' × (any)':<52} nothing to chunk")
                    continue
                for chunker_name, fn in chunkers.items():
                    r = _run_split(chunker_name, fn, canonical)
                    label = f"{parser_name} × {chunker_name}"
                    if r.error:
                        print(f"  {label:<52} ERROR {r.error}")
                        continue
                    counts = sorted(tk.count(c) for c in r.chunks)
                    p50 = counts[len(counts) // 2] if counts else 0
                    orphans = sum(1 for c in counts if c < 24)
                    print(f"  {label:<52} {len(r.chunks):>4} {p50:>5} "
                          f"{counts[-1] if counts else 0:>5} {orphans:>5}")
                    for chunk in r.chunks[: opts.show_chunks]:
                        print(f"      | {_flat(chunk)[:96]}")


SECTIONS = {
    "tokenizer": section_tokenizer,
    "pdf": section_pdf,
    "splitters": section_splitters,
    "notation": section_notation,
    "duplication": section_duplication,
}

# `probe` is deliberately outside SECTIONS: it is the one section that answers a
# question about *your* document class rather than about the libraries in general, so
# a bare `bakeoff.py` should not run it. Ask for it by name.
SECTIONS_OPTIONAL = {"probe": section_probe}

ADAPTERS = [
    ("pypdf", 1, "pypdf", "PDF tier 1, pure python — the most common default"),
    ("pymupdf", 1, "pymupdf", "PDF tier 1, fastest; get_text('blocks') is layout-aware"),
    ("pdfminer", 1, "pdfminer.six", "PDF tier 1 with LAParams layout analysis"),
    ("unstructured", 1, "unstructured", "element-based parsing; `fast`=t1, `hi_res`=t2"),
    ("docling", 2, "docling", "layout model → DoclingDocument; MIT (appendix D §4.1)"),
    ("marker", 2, "marker-pdf", "layout model → Markdown; GPL-3.0 (appendix D §4.2)"),
    ("langchain_text_splitters", 0, "langchain-text-splitters", "RecursiveCharacterTextSplitter et al"),
    ("llama_index.core", 0, "llama-index-core", "SentenceSplitter, HierarchicalNodeParser"),
    ("semchunk", 0, "semchunk", "small tokenizer-aware recursive splitter"),
    ("chonkie", 0, "chonkie", "many chunkers behind one API"),
    ("bs4", 0, "beautifulsoup4", "HTML main-content extraction"),
    ("tiktoken", 0, "tiktoken", "real cl100k tokenizer — ground truth for §5.4"),
    ("datasketch", 0, "datasketch", "reference MinHash"),
]

USAGE = """\
examples:
  bakeoff.py                                    the default run (tier 1, every section)
  bakeoff.py --list                             adapters, tiers, and document classes
  bakeoff.py --only pdf --tier 1 2              add the layout models to the parser table
  bakeoff.py --only probe --doc invoice         one class, end to end, every parser
  bakeoff.py --only probe --doc invoice \\
             --parser docling --chunker semchunk --show-chunks 3
  bakeoff.py --only splitters --max-tokens 512 --chunker langchain
"""


def _print_list() -> None:
    print("adapters (install with: uv pip install -r requirements-bakeoff.txt)\n")
    for module, tier, pip, what in ADAPTERS:
        mark = "OK " if have(module) else "-- "
        label = f"tier {tier}" if tier else "      "
        print(f"  {mark} {label}  {pip:<26} {what}")

    print("\nPDF parser adapters matching --parser / --tier:\n")
    for adapter in pdf_adapters({1, 2}):
        print(f"  tier {adapter.tier}  {adapter.name}")

    print("\nsplitter adapters matching --chunker:\n")
    for name in splitter_adapters(128):
        print(f"          {name}")

    print("\ndocument classes matching --doc (appendix D §5, README §6.1):\n")
    for name, cls in DOC_CLASSES.items():
        checks = f"{len(cls.checks)} known-answer check(s)" if cls.checks else "no checks yet"
        print(f"  {name:<17} {', '.join(cls.fixtures)}")
        print(f"  {'':<17} shortlist: {cls.shortlist}")
        print(f"  {'':<17} {checks}")

    print(f"\nsections: {', '.join(SECTIONS)}, {', '.join(SECTIONS_OPTIONAL)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bakeoff.py",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list", action="store_true",
                   help="show adapters, tiers, splitters and document classes, then exit")
    p.add_argument("--only", nargs="+", metavar="SECTION", default=None,
                   help=f"sections to run: {', '.join(SECTIONS)}, {', '.join(SECTIONS_OPTIONAL)}")
    p.add_argument("--tier", nargs="+", type=int, choices=(1, 2), default=[1], metavar="N",
                   help="parser tiers to include (default: 1). Tier 2 runs layout models: "
                        "model download on first use, tens of seconds per page on CPU")
    p.add_argument("--parser", nargs="+", default=[], metavar="SUBSTR",
                   help="only PDF parsers whose name contains one of these (e.g. docling pymupdf)")
    p.add_argument("--chunker", nargs="+", default=[], metavar="SUBSTR",
                   help="only splitters whose name contains one of these (e.g. semchunk langchain)")
    p.add_argument("--doc", nargs="+", default=[], metavar="NAME",
                   help="restrict to document classes or fixtures matching these substrings")
    p.add_argument("--max-tokens", type=int, default=128, metavar="N",
                   help="chunk budget for the splitter comparison and probe (default: 128)")
    p.add_argument("--show-chunks", type=int, default=0, metavar="N",
                   help="with --only probe, print the first N chunks of each combination")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        _print_list()
        return 0
    if not ROOT.exists():
        print("corpus/ is missing — run `python3 make_fixtures.py` first", file=sys.stderr)
        return 1

    known = {**SECTIONS, **SECTIONS_OPTIONAL}
    chosen = list(SECTIONS) if args.only is None else [s for s in args.only if s in known]
    if not chosen:
        print(f"available sections: {', '.join(known)}", file=sys.stderr)
        return 1

    opts = Options(
        tiers=set(args.tier),
        parsers=args.parser,
        chunkers=args.chunker,
        docs=args.doc,
        max_tokens=args.max_tokens,
        show_chunks=args.show_chunks,
    )

    installed = sum(1 for m, _, _, _ in ADAPTERS if have(m))
    print(f"bake-off: {installed}/{len(ADAPTERS)} adapters available, "
          f"tier {'+'.join(str(t) for t in sorted(opts.tiers))} "
          f"(`--list` for details, `--help` for filters)")
    for name in chosen:
        known[name](opts)
    print()
    return 0


# MinerU and `unstructured`'s hi_res path spawn worker processes, and a spawned worker
# re-imports this module. Without this guard the workers would re-run the bake-off.
if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
