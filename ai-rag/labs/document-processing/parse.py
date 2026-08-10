"""Stage 2 — parsing. The stage that decides the ceiling (`02` §3).

Every parser here emits the same thing: an ordered list of **typed elements** with
provenance, not a text blob. That interface choice is the single most consequential
one in this file, and it is worth stating why before the code.

A splitter that receives `str` can only split on characters. A splitter that receives
`[Element(kind="heading", level=2), Element(kind="table", ...), ...]` can split on the
boundaries the author drew (§6.3), refuse to cut a table or a code block in half
(§3.4, §3.7), and repeat a table header into every piece of a split table. **The
chunking strategies available downstream are determined by what the parser chose to
emit**, which is §1's ceiling chain expressed as an API.

The registry maps a media type to a parser and a *tier* (§3.3). Everything here is
tier 1 — geometric extraction, stdlib only, no layout model and no VLM. That is
deliberate: the lab's job is to show precisely what tier 1 cannot recover, so that a
tier-2 comparison (§15's lab 2) has an honest baseline to beat.

**Gates run at parse time, not at debug time** (§3.2). A scanned PDF that extracts to
`""` produces zero chunks, zero vectors and zero errors; the document simply ceases
to exist as far as retrieval is concerned. `gate()` turns that silence into a routing
decision.
"""

from __future__ import annotations

import ast
import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pdfmini as P
from tables import Table, recover_grid, to_markdown

# Bump on any change to extraction behaviour. Stamped onto every chunk (§2, §8.1) so
# "which chunks came from the old parser?" is a query rather than a guess.
PARSER_VERSION = "tier1-v1"

# Element kinds. Kept small on purpose: every kind is one the splitter treats
# differently, and a taxonomy richer than the splitter's behaviour is decoration.
HEADING = "heading"
PARAGRAPH = "paragraph"
LIST_ITEM = "list_item"
CODE = "code"
TABLE = "table"
QUOTE = "quote"
FIGURE = "figure"
DESCRIPTION = "description"  # a generated natural-language stand-in (§3.6)


@dataclass(frozen=True)
class Element:
    """One parsed unit, with everything the splitter needs to decide about it."""

    kind: str
    text: str
    level: int = 0  # heading depth; list nesting
    page: int | None = None
    meta: dict = field(default_factory=dict, compare=False)

    @property
    def atomic(self) -> bool:
        """True when splitting *inside* this element destroys it.

        A code block cut in half is the §3 parsing failure reintroduced by the
        chunker; a table cut between its header and its rows is §3.4's unlabeled
        number grid. The splitter honours this flag rather than rediscovering it.
        """
        return self.kind in (CODE, TABLE)


@dataclass
class ParsedDoc:
    doc_id: str
    source_uri: str
    media_type: str
    elements: list[Element]
    page_count: int
    raw_text: str  # pre-normalization, for the gates
    parser: str = PARSER_VERSION
    tier: int = 1
    warnings: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    @property
    def text_len(self) -> int:
        return len(self.raw_text)


# --------------------------------------------------------------------------------
# Gates (§3.2) — data-loss checks, not nice-to-haves
# --------------------------------------------------------------------------------

LATIN_OK = re.compile(r"[A-Za-z0-9\s.,;:!?'\"()\[\]{}%$€£/@#&*+=<>_\-–—…]")

ROUTE_INDEX = "index"
ROUTE_OCR = "ocr"
ROUTE_REVIEW = "review"


@dataclass(frozen=True)
class GateVerdict:
    route: str
    yield_per_page: float
    sanity: float
    reason: str

    @property
    def ok(self) -> bool:
        return self.route == ROUTE_INDEX


def extraction_yield(text: str, page_count: int) -> float:
    """Characters of extracted text per page. Near-zero means no text layer."""
    return len(text) / max(page_count, 1)


def script_sanity(text: str, sample: int = 20_000) -> float:
    """Fraction of characters that look like the expected script. Low means mojibake.

    This deviates from the chapter's sketch in one place, and the deviation was forced
    by building it. The chapter counts a character as fine if it is a letter by
    Unicode category. A subset font with no `/ToUnicode` map decodes to C0 control
    characters, which are category `Cc` and correctly fail — but the same failure with
    high byte codes decodes to `À`, `Ê`, `Ø`, which are category `Lu` and would
    **pass** the gate while being just as unreadable. So control, private-use and
    unassigned categories are rejected explicitly rather than by omission.

    It still will not catch everything. It catches the catastrophic case, which is the
    one that silently indexes a document nobody can ever retrieve.
    """
    s = text[:sample]
    if not s:
        return 0.0
    ok = 0
    for ch in s:
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Co", "Cs", "Cn") and ch not in "\n\r\t":
            continue
        if LATIN_OK.match(ch) or category.startswith("L"):
            ok += 1
    return ok / len(s)


# Glyph names leaking into extracted text: `/uni0041`, `/g17`, `/cid42`, `/C0_3`.
# Not mojibake — perfectly good ASCII — which is exactly why a script-based gate
# cannot see it.
_GLYPH_NAME_LEAK = re.compile(r"/(?:uni[0-9A-Fa-f]{4}|g\d+|cid\d+|[A-Z]\d+_\d+)")


def glyph_leakage(text: str, sample: int = 20_000) -> float:
    """Fraction of characters that are un-decoded glyph names (§3.2).

    **This check exists because the bake-off found that `script_sanity` is
    parser-dependent.** Run `subset_broken.pdf` — a PDF whose font ships no usable
    glyph map — through two libraries and you get two different shapes of failure:

    - PyMuPDF and this lab's extractor fall back to the raw byte, producing C0 control
      characters. `script_sanity` scores 0.17 and the gate fires.
    - pypdf emits the glyph *names* as literal text: `/g1/g2/g3...`. That is clean
      printable ASCII. `script_sanity` scores **1.00**, zero control characters, and
      the document sails into the index as 1,379 characters of nonsense.

    So a gate calibrated against one parser's failure mode does not transfer to
    another's, and "we have a mojibake check" is not the same as "we would catch this."
    Run both checks, and re-calibrate whenever the parser changes — which is one more
    reason `parser_version` belongs on every chunk (§2, §8.1).
    """
    s = text[:sample]
    if not s:
        return 0.0
    return sum(len(m.group()) for m in _GLYPH_NAME_LEAK.finditer(s)) / len(s)


def gate(
    doc: ParsedDoc,
    *,
    min_yield: float = 100.0,
    min_sanity: float = 0.80,
    max_glyph_leak: float = 0.20,
) -> GateVerdict:
    """Route a parsed document: index it, send it to OCR, or send it to a human.

    The thresholds are placeholders and they are *supposed* to be. Calibrating them
    means eyeballing documents you have verified by hand and picking values that
    separate the ones you know are broken from the ones you know are fine — §15's lab
    1. A threshold copied from a document is a number you invented.

    Note that the verdict is a *route*, never a warning log. A gate whose breach only
    writes to a log nobody reads has not prevented the data loss, it has documented it.
    """
    y = extraction_yield(doc.raw_text, doc.page_count)
    q = script_sanity(doc.raw_text)
    leak = glyph_leakage(doc.raw_text)
    if y < min_yield:
        return GateVerdict(
            ROUTE_OCR, y, q, f"{y:.0f} chars/page — probable scan or empty text layer"
        )
    if q < min_sanity:
        return GateVerdict(
            ROUTE_REVIEW, y, q, f"script sanity {q:.2f} — probable encoding failure"
        )
    if leak > max_glyph_leak:
        return GateVerdict(
            ROUTE_REVIEW, y, q,
            f"{leak:.0%} of text is un-decoded glyph names — font has no usable CMap",
        )
    return GateVerdict(ROUTE_INDEX, y, q, "within thresholds")


# --------------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------------


def parse_text(doc_id: str, uri: str, data: bytes) -> ParsedDoc:
    """Blank-line-separated paragraphs. There is nothing else to recover.

    This is the honest floor: the format carries line breaks and nothing more, so the
    only strategy available downstream is recursive splitting (§6.2). Any richer
    chunking would be inventing structure the document does not have.
    """
    text = data.decode("utf-8")
    elements = [
        Element(PARAGRAPH, block.strip())
        for block in re.split(r"\n\s*\n", text)
        if block.strip()
    ]
    return ParsedDoc(doc_id, uri, "text/plain", elements, 1, text)


# --------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)\s*(\w+)?")
_MD_LIST = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MD_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def parse_markdown(doc_id: str, uri: str, data: bytes) -> ParsedDoc:
    """Headings, fenced code, pipe tables, lists, block quotes, paragraphs.

    Markdown is the format where almost nothing is lost, which makes it the useful
    control in this corpus: any retrieval failure on a Markdown document is a chunking
    or embedding failure, never a parsing one. When something breaks on the PDFs and
    not here, §1's ceiling chain tells you where to look.
    """
    text = data.decode("utf-8")
    lines = text.split("\n")
    elements: list[Element] = []
    buffer: list[str] = []
    i = 0

    def flush() -> None:
        nonlocal buffer
        if buffer:
            body = "\n".join(buffer).strip()
            if body:
                elements.append(Element(PARAGRAPH, body))
        buffer = []

    while i < len(lines):
        line = lines[i]

        fence = _MD_FENCE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            lang = fence.group(2) or ""
            block = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                block.append(lines[i])
            elements.append(Element(CODE, "\n".join(block), meta={"lang": lang}))
            i += 1
            continue

        heading = _MD_HEADING.match(line)
        if heading:
            flush()
            elements.append(Element(HEADING, heading.group(2), level=len(heading.group(1))))
            i += 1
            continue

        if _MD_ROW.match(line):
            flush()
            block = []
            while i < len(lines) and _MD_ROW.match(lines[i]):
                block.append(lines[i])
                i += 1
            table = _parse_pipe_table(block)
            if table:
                elements.append(Element(TABLE, "\n".join(block), meta={"table": table}))
            else:
                elements.append(Element(PARAGRAPH, "\n".join(block)))
            continue

        if line.startswith(">"):
            flush()
            block = []
            while i < len(lines) and lines[i].startswith(">"):
                block.append(lines[i].lstrip("> ").rstrip())
                i += 1
            elements.append(Element(QUOTE, "\n".join(block).strip()))
            continue

        item = _MD_LIST.match(line)
        if item:
            flush()
            elements.append(
                Element(LIST_ITEM, item.group(2).strip(), level=len(item.group(1)) // 2)
            )
            i += 1
            continue

        if not line.strip():
            flush()
        else:
            buffer.append(line)
        i += 1

    flush()
    return ParsedDoc(doc_id, uri, "text/markdown", elements, 1, text)


def _parse_pipe_table(block: list[str]) -> Table | None:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in block
        if not _MD_SEP.match(line)
    ]
    if len(rows) < 2:
        return None
    return Table(rows[0], rows[1:])


# --------------------------------------------------------------------------------
# HTML (§3.5)
# --------------------------------------------------------------------------------

_CHROME_TAGS = {"nav", "footer", "aside", "header"}
_CHROME_CLASSES = {"cookie-banner", "site-nav", "related", "breadcrumbs"}
_SKIP_TAGS = {"script", "style", "noscript", "svg"}
_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "th"}


class _HTMLCollector(HTMLParser):
    """Collect block-level text, recording whether each block sits inside site chrome.

    Chrome membership is *recorded*, not acted on. That lets the lab run both of
    §3.5's approaches over the same parse — tag/class heuristics here, corpus-level
    repeated-block detection in `dedup.py` — and show where they disagree. Neither one
    alone is sufficient: the tag heuristic misses site-specific boilerplate in an
    ordinary `<div>`, and the corpus heuristic needs more than one page from a domain.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, bool]] = []  # (tag, text, in_chrome)
        self._stack: list[tuple[str, bool]] = []
        self._buffer: list[str] = []
        self._current: str | None = None
        self._skip_depth = 0
        self._chrome_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        is_chrome = tag in _CHROME_TAGS or bool(classes & _CHROME_CLASSES)
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if is_chrome:
            self._chrome_depth += 1
        self._stack.append((tag, is_chrome))
        if tag in _BLOCK_TAGS:
            self._flush()
            self._current = tag

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._flush()
        while self._stack:
            top, is_chrome = self._stack.pop()
            if is_chrome:
                self._chrome_depth = max(0, self._chrome_depth - 1)
            if top in _SKIP_TAGS:
                self._skip_depth = max(0, self._skip_depth - 1)
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._current:
            self._buffer.append(data)

    def _flush(self) -> None:
        if self._current and self._buffer:
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            if text:
                self.blocks.append((self._current, text, self._chrome_depth > 0))
        self._buffer = []
        self._current = None


def parse_html(doc_id: str, uri: str, data: bytes) -> ParsedDoc:
    collector = _HTMLCollector()
    collector.feed(data.decode("utf-8"))
    collector._flush()

    elements: list[Element] = []
    for tag, text, in_chrome in collector.blocks:
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            kind, level = HEADING, int(tag[1])
        elif tag == "li":
            kind, level = LIST_ITEM, 0
        elif tag == "blockquote":
            kind, level = QUOTE, 0
        else:
            kind, level = PARAGRAPH, 0
        elements.append(Element(kind, text, level=level, meta={"chrome": in_chrome}))

    raw = "\n".join(e.text for e in elements)
    chrome_chars = sum(len(e.text) for e in elements if e.meta.get("chrome"))
    return ParsedDoc(
        doc_id,
        uri,
        "text/html",
        elements,
        1,
        raw,
        notes={"chrome_chars": chrome_chars, "chrome_fraction": chrome_chars / max(len(raw), 1)},
    )


# --------------------------------------------------------------------------------
# CSV / spreadsheets (§3.6) — the "wrong tool" answer, implemented
# --------------------------------------------------------------------------------


def parse_csv(doc_id: str, uri: str, data: bytes) -> ParsedDoc:
    """Emit a natural-language *description* of the table, not prose per row.

    §3.6's argument: a spreadsheet is a relation, and the right way to answer "total
    revenue in EMEA in Q3" over a relation is a query. Vector search over embedded
    rows will return rows that are lexically similar to the question and will never
    aggregate, never join, and never be exhaustive — the three things the question
    needs.

    So the ingest rule is: do not chunk a spreadsheet into prose because your pipeline
    only knows how to do that. Route it to a query engine, and index a description of
    the table — its schema, its grain, its ranges — so that a question about it
    retrieves a *pointer to the right table* rather than a scattering of its rows.

    The description below is generated from the data, deterministically. That matters:
    an LLM-written table summary would reintroduce §6.5's fabrication risk at ingest,
    where no output-side guardrail can catch it.
    """
    text = data.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    if not rows:
        return ParsedDoc(doc_id, uri, "text/csv", [], 1, text, warnings=["empty csv"])

    header, *body = rows
    profile: list[str] = []
    for i, name in enumerate(header):
        values = [r[i] for r in body if i < len(r) and r[i] != ""]
        distinct = sorted(set(values))
        numeric = [v for v in values if re.fullmatch(r"-?\d+(\.\d+)?", v)]
        if numeric and len(numeric) == len(values):
            nums = [float(v) for v in numeric]
            profile.append(
                f"`{name}` is numeric, ranging from {min(nums):,.0f} to {max(nums):,.0f}"
            )
        elif len(distinct) <= 8:
            profile.append(f"`{name}` takes {len(distinct)} values: {', '.join(distinct)}")
        else:
            profile.append(f"`{name}` has {len(distinct)} distinct values")

    description = (
        f"Table `{doc_id}` is a relational dataset with {len(body):,} rows and "
        f"{len(header)} columns. Its grain is one row per "
        f"{' × '.join(header[:3])}. Columns: {'; '.join(profile)}. "
        f"Aggregate and comparative questions about this data should be answered by "
        f"querying the table, not by retrieving its rows."
    )

    table = Table(header, body[:5], caption=f"First 5 of {len(body)} rows")
    elements = [
        Element(DESCRIPTION, description, meta={"routed_to": "query_engine"}),
        # Same rule as the PDF path: an element with no text is dropped downstream.
        Element(TABLE, to_markdown(table), meta={"table": table, "sample": True}),
    ]
    return ParsedDoc(
        doc_id,
        uri,
        "text/csv",
        elements,
        1,
        text,
        notes={"rows": len(body), "columns": len(header), "route": "query_engine"},
    )


# --------------------------------------------------------------------------------
# Source code (§3.7)
# --------------------------------------------------------------------------------


def parse_code(doc_id: str, uri: str, data: bytes) -> ParsedDoc:
    """Split at AST boundaries, carrying enclosing context into every chunk.

    Code has a free, exact parser, so using anything else discards structure that is
    already available. Two rules from §3.7 are implemented here:

    - **Carry the enclosing context.** `def process(self, batch):` alone tells you
      nothing about what is being processed. Each element's embedded text is prefixed
      with the file path, the enclosing class signature, and the module's imports —
      `01` §9.1's problem in its purest form, fixed for free.
    - **Keep the docstring with the code it describes.** They are the two halves of
      the (code, docstring) pair that code-retrieval models were contrastively trained
      on. `ast.get_source_segment` keeps them together by construction, which is one
      more reason to use the real parser rather than a regex over `def`.
    """
    text = data.decode("utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return ParsedDoc(
            doc_id, uri, "text/x-python", [], 1, text, warnings=[f"syntax error: {exc}"]
        )

    imports = [
        ast.get_source_segment(text, node) or ""
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    import_block = "\n".join(i for i in imports if i)

    elements: list[Element] = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        elements.append(Element(PARAGRAPH, module_doc, meta={"symbol": "<module>"}))

    def emit(node: ast.AST, qualname: str, enclosing: str) -> None:
        source = ast.get_source_segment(text, node)
        if not source:
            return
        context_lines = [f"# file: {doc_id}"]
        if enclosing:
            context_lines.append(f"# in: {enclosing}")
        if import_block:
            context_lines.append(import_block)
        elements.append(
            Element(
                CODE,
                source,
                page=getattr(node, "lineno", None),
                meta={
                    "symbol": qualname,
                    "lang": "python",
                    "context": "\n".join(context_lines),
                    "has_docstring": bool(ast.get_docstring(node))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    else False,
                },
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit(node, node.name, "")
        elif isinstance(node, ast.ClassDef):
            # A class is emitted whole *and* per method. The whole class is the parent
            # unit (§7.1); the methods are the precise children. Which one is indexed
            # and which is returned is the splitter's decision, not the parser's.
            emit(node, node.name, "")
            signature = f"class {node.name}:"
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit(child, f"{node.name}.{child.name}", signature)

    return ParsedDoc(
        doc_id,
        uri,
        "text/x-python",
        elements,
        1,
        text,
        notes={"symbols": len(elements), "imports": len(imports)},
    )


# --------------------------------------------------------------------------------
# Email (§10.3)
# --------------------------------------------------------------------------------

_QUOTE_LINE = re.compile(r"^\s*>")
_QUOTE_INTRO = re.compile(r"^On .*wrote:\s*$", re.IGNORECASE)


def parse_email(doc_id: str, uri: str, data: bytes) -> ParsedDoc:
    """Split an mbox thread, strip quoted replies, keep the thread as metadata.

    §10.3's rule: quoted replies are duplicated across every message in a thread.
    Collapsing them after the fact loses the thread structure; keeping them means the
    first message's text occupies as many chunks as there are replies. **Strip the
    quoted blocks at parse time and index the thread structure as metadata** — which
    is a parsing decision, not a deduplication one, and belongs here.

    The `duplication_ratio` in `notes` is what the decision should be made on: it is
    the fraction of the file's characters that are quoted repetition.
    """
    import email
    from email import policy

    text = data.decode("utf-8")
    raw_messages = [m for m in re.split(r"^From \S+\n", text, flags=re.MULTILINE) if m.strip()]

    elements: list[Element] = []
    quoted_chars = 0
    kept_chars = 0

    for position, raw in enumerate(raw_messages, start=1):
        message = email.message_from_string(raw, policy=policy.default)
        body = message.get_content() if message.get_content_type() == "text/plain" else ""

        kept_lines: list[str] = []
        for line in body.split("\n"):
            if _QUOTE_LINE.match(line) or _QUOTE_INTRO.match(line.strip()):
                quoted_chars += len(line) + 1
                continue
            kept_lines.append(line)
        clean = "\n".join(kept_lines).strip()
        kept_chars += len(clean)

        meta = {
            "message_id": message.get("Message-ID", ""),
            "sender": message.get("From", ""),
            "date": message.get("Date", ""),
            "in_reply_to": message.get("In-Reply-To", ""),
            "thread_position": position,
            "thread_length": len(raw_messages),
        }
        subject = message.get("Subject", "")
        if position == 1 and subject:
            elements.append(Element(HEADING, subject, level=1, meta=meta))
        if clean:
            elements.append(
                Element(
                    PARAGRAPH,
                    clean,
                    meta={**meta, "context": f"{meta['sender']} on {meta['date']}"},
                )
            )

    total = quoted_chars + kept_chars
    return ParsedDoc(
        doc_id,
        uri,
        "message/rfc822",
        elements,
        1,
        "\n".join(e.text for e in elements),
        notes={
            "messages": len(raw_messages),
            "quoted_chars_stripped": quoted_chars,
            "duplication_ratio": quoted_chars / max(total, 1),
        },
    )


# --------------------------------------------------------------------------------
# PDF (§3.2, §3.3) — tier 1
# --------------------------------------------------------------------------------


def parse_pdf(
    doc_id: str,
    uri: str,
    data: bytes,
    *,
    reading_order: str = "columns",
    strip_running: bool = True,
    detect_tables: bool = True,
) -> ParsedDoc:
    """Tier-1 geometric extraction, with every reconstruction step made a parameter.

    The parameters exist so the lab can run the same bytes through the naive and the
    careful configuration and diff the result (§15, lab 1). In production you would
    pick one — but you would pick it *after* looking, which is the entire point.

    `reading_order="naive"` is not a straw man. It is what you get from the default
    path of most tier-1 libraries, and it is correct on a single-column page. It
    destroys a two-column one.
    """
    extracted = P.extract(data)
    warnings: list[str] = []

    running = P.detect_running_lines(extracted) if strip_running else set()
    pages = P.page_text(extracted, reading_order=reading_order, drop_lines=running)

    elements: list[Element] = []
    tables_found = 0

    for page in extracted.pages:
        page_no = page.number
        body = pages[page_no - 1]

        if detect_tables and page.runs:
            recovery = recover_grid([(r.x, r.y, r.text) for r in page.runs], min_rows=4)
            if recovery.table and recovery.confidence == "high":
                tables_found += 1
                grid_texts = {
                    cell for row in [recovery.table.header, *recovery.table.rows] for cell in row
                }
                # Remove the grid's own lines from the prose, or every cell is indexed
                # twice: once as a table row and once as a line of nonsense.
                body = "\n".join(
                    line
                    for line in body.split("\n")
                    if not _is_grid_line(line, grid_texts)
                )
                # The element MUST carry text, not just a grid in `meta`. An element
                # whose text is empty normalizes to nothing and is dropped by
                # `build_canonical`, so the recovered table would never reach the
                # splitter — the grid recovery would work perfectly and the result
                # would be silently discarded one stage later. That is the bug this
                # lab is about, committed by the lab: found only by counting the
                # chunks that came out of statement.pdf and getting 1 instead of 7.
                elements.append(
                    Element(
                        TABLE,
                        to_markdown(recovery.table),
                        page=page_no,
                        meta={"table": recovery.table, "confidence": recovery.confidence},
                    )
                )
            elif recovery.table:
                warnings.append(
                    f"page {page_no}: possible table with {recovery.confidence} "
                    f"confidence — {recovery.note}"
                )

        for block in re.split(r"\n\s*\n", body):
            block = block.strip()
            if block:
                elements.append(Element(PARAGRAPH, block, page=page_no))

        if page.has_image and not page.runs:
            elements.append(
                Element(FIGURE, "", page=page_no, meta={"reason": "image with no text layer"})
            )

    if not extracted.has_tounicode and extracted.unresolved_glyphs:
        warnings.append(
            f"{extracted.unresolved_glyphs} glyphs could not be mapped to Unicode and "
            f"no /ToUnicode CMap is present — extracted text is unreliable"
        )

    raw = "\n".join(pages)
    return ParsedDoc(
        doc_id,
        uri,
        "application/pdf",
        elements,
        extracted.page_count,
        raw,
        warnings=warnings,
        notes={
            "reading_order": reading_order,
            "running_lines_stripped": sorted(running),
            "tables_recovered": tables_found,
            "unresolved_glyphs": extracted.unresolved_glyphs,
            "has_tounicode": extracted.has_tounicode,
            "pages_without_text": sum(1 for p in extracted.pages if not p.runs),
        },
    )


def _is_grid_line(line: str, grid_texts: set[str]) -> bool:
    """A prose line is part of the grid if most of its whitespace tokens are cells."""
    parts = [p for p in line.split() if p]
    if not parts:
        return False
    hits = sum(1 for p in parts if p in grid_texts)
    return hits / len(parts) >= 0.6


# --------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------

REGISTRY = {
    ".txt": parse_text,
    ".md": parse_markdown,
    ".html": parse_html,
    ".csv": parse_csv,
    ".eml": parse_email,
    ".pdf": parse_pdf,
}


def parse_file(path: Path, root: Path, **kwargs) -> ParsedDoc:
    """Dispatch on extension. `service.py.txt` is routed to the code parser by name.

    Extension-based dispatch is what most pipelines do and it is worth naming as a
    weakness: a `.txt` holding Python, a `.pdf` holding a scan, and a `.csv` holding
    prose are all mis-routed by it. Content sniffing is the fix; this lab keeps the
    naive version and handles the one case it gets wrong explicitly, so the seam is
    visible rather than papered over.
    """
    doc_id = path.relative_to(root).as_posix()
    data = path.read_bytes()
    # A machine-independent URI on purpose: an absolute `file://` path would put this
    # developer's home directory into every manifest, and the manifest is supposed to
    # be byte-identical on every machine that regenerates it.
    uri = f"corpus://{doc_id}"

    if path.name.endswith(".py.txt"):
        return parse_code(doc_id, uri, data)

    parser = REGISTRY.get(path.suffix.lower())
    if parser is None:
        raise ValueError(f"no parser registered for {path.suffix!r} ({doc_id})")
    if parser is parse_pdf:
        return parse_pdf(doc_id, uri, data, **kwargs)
    return parser(doc_id, uri, data)
