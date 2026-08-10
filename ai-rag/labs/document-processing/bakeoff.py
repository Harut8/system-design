"""Run production tooling over the same fixtures and score what each one misses.

    uv venv .venv && uv pip install -r requirements-bakeoff.txt
    .venv/bin/python bakeoff.py                 # everything installed
    .venv/bin/python bakeoff.py --only pdf      # one section
    .venv/bin/python bakeoff.py --list          # adapters and their availability

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
| Table integrity | `metrics.md` | 4-row table. Splitting it mid-grid loses the header. |
| Code integrity | `service.py.txt` | 2 functions + 1 class. Splitting mid-function is wrong. |
| Notation | `notation.md` | `x²`, `Ⅻ`, `½` must survive the loader. |
| Duplication | `site/*.html`, `thread.eml` | 5 pages share chrome; the thread quotes itself 4 deep. |
| Tokenizer | all | `tiktoken` is ground truth for this lab's estimator. |

**A missing adapter is not a failure.** Anything not installed prints `not installed`
and is skipped, so a partial install still produces a comparison. Nothing here calls a
network or an API key.

**What this is not.** It is not a benchmark and it does not produce a ranking. Chunk
counts and split behaviour are facts about a library; whether they matter to *your*
retrieval is §15's lab 5, which needs a golden set this lab does not have. Read the
columns as "what would I have to know about this tool before trusting it", not as a
score.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import normalize as N
import parse as PA
import split as SP

ROOT = Path(__file__).parent / "corpus"

# Third-party chatter drowns the comparison table. These are the libraries' own advice
# messages ("chunk size is small", "fitz is deprecated"), not findings about the corpus.
warnings.filterwarnings("ignore")
logging.getLogger("llama_index").setLevel(logging.ERROR)
logging.getLogger("llama_index.core.node_parser").setLevel(logging.ERROR)


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


# --------------------------------------------------------------------------------
# Tokenizer ground truth
# --------------------------------------------------------------------------------


def section_tokenizer() -> None:
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


def _run_pdf(name: str, fn, data: bytes) -> PdfResult:
    t0 = time.perf_counter()
    try:
        text = fn(data)
    except Exception as exc:  # a library that raises is *better* than one that lies
        return PdfResult(name, "", (time.perf_counter() - t0) * 1000, error=f"{type(exc).__name__}: {exc}")
    return PdfResult(name, text, (time.perf_counter() - t0) * 1000)


def pdf_adapters() -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = [("this lab (tier 1, columns)", None)]
    if have("pypdf"):
        out.append(("pypdf", _pypdf))
    if have("pymupdf"):
        out.append(("pymupdf (text)", lambda d: _pymupdf(d, "text")))
        out.append(("pymupdf (blocks, as-is)", lambda d: _pymupdf(d, "blocks")))
        out.append(("pymupdf (sort=True)", _pymupdf_sorted))
        out.append(("pymupdf (per-column clip)", _pymupdf_clipped))
    if have("pdfminer"):
        out.append(("pdfminer.six", _pdfminer))
    return out


def section_pdf() -> None:
    """Reading order, empty text layers, and broken encodings across real parsers."""
    h1("PDF PARSERS — the same bytes through every tier-1 library you have")

    adapters = pdf_adapters()
    for module in ("pypdf", "fitz", "pdfminer"):
        if not have(module):
            skip(f"pdf: {module}", {"fitz": "pymupdf", "pdfminer": "pdfminer.six"}.get(module, module))

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
    for name, fn in adapters:
        verdicts = []
        for fixture in fixtures:
            data = (ROOT / fixture).read_bytes()
            if fn is None:
                doc = PM.extract(data)
                running = PM.detect_running_lines(doc)
                text = "\n".join(PM.page_text(doc, reading_order="columns", drop_lines=running))
                verdicts.append(_reading_order_verdict(text))
            else:
                r = _run_pdf(name, fn, data)
                verdicts.append(f"ERROR {r.error[:20]}" if r.error else _reading_order_verdict(r.text))
        short = [v.split("—")[0].strip()[:28] for v in verdicts]
        print(f"  {name:<32} {short[0]:<30} {short[1]}")

    print()
    print("  pdfminer.six recovers the columns in BOTH fixtures — its LAParams layout")
    print("  analysis groups text by position. pypdf and PyMuPDF's default text mode follow")
    print("  emission order and interleave. Note that `sort=True` does NOT rescue PyMuPDF:")
    print("  it sorts blocks, and PyMuPDF has already merged both columns into one block, so")
    print("  the interleaving is inside a block where block sorting cannot reach it. Only")
    print("  per-column clip rectangles fix it, and finding those rectangles is your job.")
    print()
    print("  first body line of page 1, row-band fixture:")
    for name, fn in adapters:
        data = (ROOT / "report_twocol_interleaved.pdf").read_bytes()
        if fn is None:
            doc = PM.extract(data)
            text = "\n".join(PM.page_text(doc, reading_order="columns",
                                          drop_lines=PM.detect_running_lines(doc)))
        else:
            r = _run_pdf(name, fn, data)
            if r.error:
                continue
            text = r.text
        line = next(
            (ln for ln in text.split("\n")
             if len(ln.strip()) > 20 and "Northwind" not in ln and "Page " not in ln),
            "",
        )
        print(f"    {name:<32} {line.strip()[:52]!r}")

    h2("Empty text layer (§3.2) — scan.pdf: does the library tell you?")
    print("  KNOWN ANSWER: two pages of image, zero text. Every parser should return ''.")
    print("  The question is whether that is reported as a condition or as success.\n")
    data = (ROOT / "scan.pdf").read_bytes()
    for name, fn in adapters:
        if fn is None:
            doc = PM.extract(data)
            text = "\n".join(PM.page_text(doc))
            gate = PA.gate(PA.parse_file(ROOT / "scan.pdf", ROOT))
            print(f"  {name:<28} chars={len(text.strip()):>4}  → routed to {gate.route!r}")
            continue
        r = _run_pdf(name, fn, data)
        status = f"ERROR {r.error}" if r.error else f"chars={len(r.text.strip()):>4}  → returned normally"
        print(f"  {r.name:<28} {status}")
    print("\n  Every library returns the empty string without complaint. That is correct")
    print("  behaviour and it is exactly the problem: the caller must add the gate.")

    h2("Broken encoding (§3.2) — subset_broken.pdf vs subset_ok.pdf")
    print("  KNOWN ANSWER: identical layout; one ships resolvable glyph names, one does not.\n")
    for fixture in ("subset_ok.pdf", "subset_broken.pdf"):
        data = (ROOT / fixture).read_bytes()
        print(f"  {fixture}")
        for name, fn in adapters:
            if fn is None:
                doc = PM.extract(data)
                text = "\n".join(PM.page_text(doc))
            else:
                r = _run_pdf(name, fn, data)
                if r.error:
                    print(f"    {name:<26} ERROR {r.error[:44]}")
                    continue
                text = r.text
            sanity = PA.script_sanity(text)
            ctrl = sum(1 for c in text if unicodedata.category(c) in ("Cc", "Co", "Cn")
                       and c not in "\n\r\t")
            print(f"    {name:<26} chars={len(text.strip()):>4} sanity={sanity:.2f} "
                  f"control_chars={ctrl:>3}")
    print("\n  No library flags the broken file. All of them return plausible-length text.")
    print("  This is the case the sanity gate exists for, and no parser will do it for you.")

    h2("Ligatures and hyphenation (§3.2, §4.1) — do they arrive repaired?")
    data = (ROOT / "report_twocol.pdf").read_bytes()
    for name, fn in adapters:
        if fn is None:
            doc = PM.extract(data)
            text = "\n".join(PM.page_text(doc))
        else:
            r = _run_pdf(name, fn, data)
            if r.error:
                continue
            text = r.text
        lig = sum(text.count(l) for l in N.LIGATURES)
        hyphen = "organi-" in text.replace("\n", "\n")
        canon = N.canonicalize(text)
        print(f"  {name:<28} ligatures={lig:>2} split-hyphen={str(hyphen):<5} "
              f"→ after normalize: ligatures={sum(canon.count(l) for l in N.LIGATURES)}, "
              f"repaired={'organizational' in canon}")
    print("\n  Parsers emit the ligature codepoint as-is — correctly, it is what the file says.")
    print("  Repair is the normalizer's job (§4.1) and it does not happen unless you do it.")


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


def splitter_adapters(max_tokens: int) -> dict[str, object]:
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

    return out


def _run_split(name: str, fn, text: str) -> SplitResult:
    t0 = time.perf_counter()
    try:
        chunks = [c for c in fn(text) if c and c.strip()]
    except Exception as exc:
        return SplitResult(name, error=f"{type(exc).__name__}: {exc}"[:70])
    return SplitResult(name, chunks, ms=(time.perf_counter() - t0) * 1000)


def section_splitters() -> None:
    """Chunk shape, and the two integrity checks with known answers."""
    h1("SPLITTERS — chunk shape across libraries, at a nominal 128-token budget")

    for module, pip in (
        ("langchain_text_splitters", "langchain-text-splitters"),
        ("llama_index.core", "llama-index-core"),
        ("semchunk", "semchunk"),
        ("chonkie", "chonkie"),
    ):
        if not have(module):
            skip(f"splitter: {module}", pip)

    adapters = splitter_adapters(128)
    tk = SP.DEFAULT_TOKENIZER

    for fixture in ("transcript.txt", "book.md", "notation.md"):
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
    for name, fn in splitter_adapters(64).items():
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

    for name, fn in splitter_adapters(96).items():
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


def section_notation() -> None:
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


def section_duplication() -> None:
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


SECTIONS = {
    "tokenizer": section_tokenizer,
    "pdf": section_pdf,
    "splitters": section_splitters,
    "notation": section_notation,
    "duplication": section_duplication,
}

ADAPTERS = [
    ("pypdf", "pypdf", "PDF tier 1, pure python — the most common default"),
    ("pymupdf", "pymupdf", "PDF tier 1, fastest; get_text('blocks') is layout-aware"),
    ("pdfminer", "pdfminer.six", "PDF tier 1 with LAParams layout analysis"),
    ("langchain_text_splitters", "langchain-text-splitters", "RecursiveCharacterTextSplitter et al"),
    ("llama_index.core", "llama-index-core", "SentenceSplitter, HierarchicalNodeParser"),
    ("semchunk", "semchunk", "small tokenizer-aware recursive splitter"),
    ("chonkie", "chonkie", "many chunkers behind one API"),
    ("unstructured", "unstructured", "element-based parsing for md/html/email"),
    ("bs4", "beautifulsoup4", "HTML main-content extraction"),
    ("tiktoken", "tiktoken", "real cl100k tokenizer — ground truth for §5.4"),
    ("datasketch", "datasketch", "reference MinHash"),
]


def main(argv: list[str]) -> int:
    if "--list" in argv:
        print("adapter status (install with: uv pip install -r requirements-bakeoff.txt)\n")
        for module, pip, what in ADAPTERS:
            print(f"  {'OK ' if have(module) else '-- '} {pip:<28} {what}")
        return 0
    if not ROOT.exists():
        print("corpus/ is missing — run `python3 make_fixtures.py` first", file=sys.stderr)
        return 1

    chosen = list(SECTIONS)
    if "--only" in argv:
        wanted = argv[argv.index("--only") + 1 :]
        chosen = [s for s in wanted if s in SECTIONS]
        if not chosen:
            print(f"available sections: {', '.join(SECTIONS)}", file=sys.stderr)
            return 1

    installed = sum(1 for m, _, _ in ADAPTERS if have(m))
    print(f"bake-off: {installed}/{len(ADAPTERS)} adapters available "
          f"(`--list` for details, `--only <section>` to narrow)")
    for name in chosen:
        SECTIONS[name]()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
