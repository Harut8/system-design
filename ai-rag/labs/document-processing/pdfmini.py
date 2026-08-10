"""A minimal PDF writer and a from-scratch PDF text extractor.

This module exists to make `02` §3.2 true rather than merely stated. The claim in the
chapter is that **PDF is a print format, not a document format** — a content stream
places glyph runs at coordinates and contains no paragraph, no column, no table, and
no reading order. That claim is easy to nod along to and hard to believe until you
have written both halves: the writer that emits `(text) Tj` at an (x, y), and the
extractor that has to invent lines, columns and reading order back out of nothing but
those coordinates.

So this file is deliberately not a wrapper around `pypdf`. Every failure mode the
chapter lists is *produced here on purpose* by the writer and then *reconstructed
here on purpose* by the extractor:

- **Column interleaving** (§3.2). `reading_order="naive"` sorts every run on the page
  by (-y, x), which is the obvious implementation and is wrong on a two-column page:
  it joins across the gutter and alternates between two unrelated topics mid-clause.
  `reading_order="columns"` finds the gutter first. Same bytes, two texts.
- **Header/footer injection** (§3.2). Running heads are just more glyph runs. The
  writer emits them at a fixed y on every page; `detect_running_lines()` finds them
  the way §3.5's last paragraph says to — same text at the same vertical position on
  most pages of one document.
- **Hyphenation and ligatures** (§3.2). The writer breaks `organi-` / `zational`
  across a line and emits `ﬁ` as a single glyph through an `/Encoding /Differences`
  map, which is what real subset fonts do.
- **Missing ToUnicode** (§3.2). `write_pdf(..., resolvable=False)` emits a font whose
  Differences name glyphs `/g1 /g2 ...` — subset-font names with no standard meaning
  — and no `/ToUnicode` CMap. Extraction then returns plausible-length text that is
  total garbage, which is exactly the dangerous case: it will be embedded and indexed
  without complaint unless a gate catches it (`gates.py`).
- **No text layer** (§3.2). `ImageBlock` pages carry an image XObject and zero text
  operators. Extraction returns `""`, produces zero chunks, and raises no error
  anywhere. That silence is the whole point of the extraction-yield gate.

Two deliberate simplifications, stated so nothing here is mistaken for a real parser:

- **Streams are uncompressed and images use `/ASCIIHexDecode`**, so a generated PDF is
  pure ASCII and you can read one in a text editor. Real PDFs use `/FlateDecode`; the
  difference is a `zlib.decompress` call, not a concept.
- **`WinAnsiEncoding` is approximated by `cp1252`.** They agree on every code point
  used here. A production extractor must handle arbitrary embedded CMaps.

What this module does *not* do is any layout analysis beyond gutter detection — no
table region detection, no heading classification. That is precisely the tier-1 /
tier-2 boundary from §3.3, and the lab's point is to show what tier 1 cannot recover.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PAGE_WIDTH = 612.0  # US Letter, in points
PAGE_HEIGHT = 792.0

# Glyph names the extractor knows how to turn back into characters. A real extractor
# would consult the font's /ToUnicode CMap; this table stands in for it. Names outside
# this table are what "unresolvable subset glyph" means below.
GLYPH_TO_UNICODE = {
    "fi": "ﬁ",
    "fl": "ﬂ",
    "ffi": "ﬃ",
    "quotesingle": "'",
    "quoteright": "’",
    "endash": "–",
    "emdash": "—",
    "bullet": "•",
}
UNICODE_TO_GLYPH = {v: k for k, v in GLYPH_TO_UNICODE.items()}


# --------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextRun:
    """One `Tj` operation: draw this string with its origin at (x, y).

    y is measured from the *bottom* of the page, which is why every reading-order
    routine below sorts on -y. Getting that backwards silently reverses the document.
    """

    x: float
    y: float
    text: str
    size: float = 10.5


@dataclass(frozen=True)
class ImageBlock:
    """A grayscale image XObject placed on the page. Carries no text whatsoever."""

    x: float
    y: float
    width: float
    height: float
    pixels: bytes  # one byte per pixel, row-major
    px_w: int
    px_h: int


@dataclass
class PageSpec:
    runs: list[TextRun] = field(default_factory=list)
    images: list[ImageBlock] = field(default_factory=list)


class _Objects:
    """Sequential PDF object allocator that tracks byte offsets for the xref table."""

    def __init__(self) -> None:
        self._bodies: list[bytes] = []

    def add(self, body: bytes) -> int:
        self._bodies.append(body)
        return len(self._bodies)  # PDF object numbers are 1-based

    def add_stream(self, extra: bytes, data: bytes) -> int:
        head = b"<< /Length %d %s >>" % (len(data), extra)
        return self.add(head + b"\nstream\n" + data + b"\nendstream")

    def render(self) -> bytes:
        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for number, body in enumerate(self._bodies, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

        xref_at = len(out)
        n = len(self._bodies) + 1
        out += b"xref\n0 %d\n" % n
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n, xref_at)
        return bytes(out)


def _escape(raw: bytes) -> bytes:
    """PDF literal-string escaping. Backslash first, or you double-escape the others."""
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _encode_text(text: str, differences: dict[int, str], subset: bool) -> bytes:
    """Map a Python string to font byte codes.

    cp1252 handles everything in WinAnsiEncoding. Anything else must have been given a
    slot in the font's /Differences array — and if it wasn't, this raises rather than
    dropping the character, because a fixture that silently loses a ligature would
    defeat the purpose of the fixture.

    With `subset=True` *every* character goes through /Differences, which is what an
    embedded subset font actually does: the byte on the page is an index into that
    font's own glyph list and means nothing outside it.
    """
    reverse = {glyph: code for code, glyph in differences.items()}
    out = bytearray()
    for ch in text:
        if not subset:
            try:
                out += ch.encode("cp1252")
                continue
            except UnicodeEncodeError:
                pass
        glyph = UNICODE_TO_GLYPH.get(ch) if not subset else _uni_name(ch)
        if glyph is None or glyph not in reverse:
            raise ValueError(
                f"character {ch!r} (U+{ord(ch):04X}) has no cp1252 slot and no "
                f"/Differences entry — add one in the fixture's font spec"
            )
        out.append(reverse[glyph])
    return bytes(out)


def _uni_name(ch: str) -> str:
    """The `uniXXXX` glyph-naming convention from the Adobe Glyph List."""
    return "uni%04X" % ord(ch)


def build_subset_encoding(texts: list[str]) -> dict[int, str]:
    """Assign every distinct character a sequential code, as a subset font would.

    Codes start at 1 and are assigned in order of first appearance — arbitrary, font-
    local, and meaningless without the map. That is the entire reason a `/ToUnicode`
    CMap has to exist, and the reason a PDF missing one extracts to garbage.
    """
    seen: dict[str, int] = {}
    for text in texts:
        for ch in text:
            if ch not in seen:
                if len(seen) >= 255:
                    raise ValueError("subset font fixture exceeds 255 distinct characters")
                seen[ch] = len(seen) + 1
    return {code: _uni_name(ch) for ch, code in seen.items()}


def _content_stream(page: PageSpec, differences: dict[int, str], subset: bool) -> bytes:
    parts: list[bytes] = []
    for i, img in enumerate(page.images):
        # q/Q brackets the graphics state; `cm` scales the unit square to the target
        # rectangle. `Do` paints it. Note there is no text operator anywhere here.
        parts.append(
            b"q\n%.2f 0 0 %.2f %.2f %.2f cm\n/Im%d Do\nQ\n"
            % (img.width, img.height, img.x, img.y, i)
        )
    if page.runs:
        parts.append(b"BT\n")
        for run in page.runs:
            parts.append(b"/F1 %.2f Tf\n" % run.size)
            # An explicit text matrix per run. Real producers usually emit one Tm and
            # then relative Td/TL/T* moves; the extractor below handles both.
            parts.append(b"1 0 0 1 %.2f %.2f Tm\n" % (run.x, run.y))
            parts.append(b"(" + _escape(_encode_text(run.text, differences, subset)) + b") Tj\n")
        parts.append(b"ET\n")
    return b"".join(parts)


def write_pdf(
    pages: list[PageSpec],
    *,
    differences: dict[int, str] | None = None,
    resolvable: bool = True,
    subset: bool = False,
) -> bytes:
    """Render pages to PDF bytes.

    differences: {byte_code: glyph_name} for characters outside WinAnsiEncoding.
    subset:      route every character through /Differences, as an embedded subset
                 font does. Pair with `build_subset_encoding()`.
    resolvable:  when False, the emitted font names its glyphs /g1 /g2 ... and ships
                 no /ToUnicode CMap — the missing-CMap failure from §3.2. The page
                 still *renders* correctly in a viewer; only extraction is destroyed,
                 which is why this class of bug reaches production so often.
    """
    differences = dict(differences or {})
    objs = _Objects()

    objs.add(b"<< /Type /Catalog /Pages 2 0 R >>")  # object 1
    pages_obj = objs.add(b"")  # object 2, patched below once page refs are known

    font_extra = b""
    if differences:
        items = b" ".join(
            b"%d /%s" % (code, name.encode("ascii"))
            for code, name in sorted(differences.items())
        )
        font_extra = (
            b" /Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding "
            b"/Differences [ " + items + b" ] >>"
        )
    font_ref = objs.add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica" + font_extra + b" >>"
    )

    page_refs: list[int] = []
    for page in pages:
        content = _content_stream(page, differences, subset)
        content_ref = objs.add_stream(b"", content)

        xobjects = []
        for i, img in enumerate(page.images):
            hex_data = img.pixels.hex().encode("ascii") + b">"
            img_ref = objs.add_stream(
                b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /ASCIIHexDecode"
                % (img.px_w, img.px_h),
                hex_data,
            )
            xobjects.append(b"/Im%d %d 0 R" % (i, img_ref))

        resources = b"/Font << /F1 %d 0 R >>" % font_ref
        if xobjects:
            resources += b" /XObject << " + b" ".join(xobjects) + b" >>"

        page_refs.append(
            objs.add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.0f %.0f] "
                b"/Resources << %s >> /Contents %d 0 R >>"
                % (PAGE_WIDTH, PAGE_HEIGHT, resources, content_ref)
            )
        )

    kids = b" ".join(b"%d 0 R" % r for r in page_refs)
    objs._bodies[pages_obj - 1] = b"<< /Type /Pages /Count %d /Kids [ %s ] >>" % (
        len(page_refs),
        kids,
    )

    pdf = objs.render()
    if not resolvable:
        # Rename every Differences glyph to a subset-style /gN name that no standard
        # table can resolve. The bytes on the page are unchanged — only the map back
        # to Unicode is destroyed, which is precisely what a subset font without a
        # /ToUnicode CMap does.
        for code, name in differences.items():
            pdf = pdf.replace(b"/%s" % name.encode("ascii"), b"/g%d" % code)
    return pdf


# --------------------------------------------------------------------------------
# Extractor
# --------------------------------------------------------------------------------

_OBJ_RE = re.compile(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", re.DOTALL)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_DIFF_RE = re.compile(rb"/Differences\s*\[(.*?)\]", re.DOTALL)
_DIFF_ITEM_RE = re.compile(rb"(\d+)\s*/([A-Za-z0-9._]+)")


@dataclass(frozen=True)
class ExtractedRun:
    page: int  # 1-based
    x: float
    y: float
    text: str


@dataclass
class ExtractedPage:
    number: int
    runs: list[ExtractedRun]
    has_image: bool = False


@dataclass
class ExtractedDoc:
    pages: list[ExtractedPage]
    has_tounicode: bool
    unresolved_glyphs: int  # count of byte codes no glyph table could interpret

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _parse_string_literal(data: bytes, i: int) -> tuple[bytes, int]:
    """Read a `(...)` literal starting at data[i] == '('. Returns (bytes, next_index)."""
    assert data[i : i + 1] == b"("
    out = bytearray()
    depth = 1
    i += 1
    while i < len(data):
        ch = data[i : i + 1]
        if ch == b"\\":
            nxt = data[i + 1 : i + 2]
            # Only the escapes this writer emits, plus the octal form real producers use.
            if nxt in (b"(", b")", b"\\"):
                out += nxt
                i += 2
                continue
            if nxt.isdigit():
                octal = data[i + 1 : i + 4]
                out.append(int(octal, 8) & 0xFF)
                i += 1 + len(octal)
                continue
            out += nxt
            i += 2
            continue
        if ch == b"(":
            depth += 1
        elif ch == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out += ch
        i += 1
    return bytes(out), i


def _decode(raw: bytes, differences: dict[int, str], counter: list[int]) -> str:
    """Byte codes → text, via /Differences then cp1252.

    A code whose glyph name is not in GLYPH_TO_UNICODE is the missing-CMap case: there
    is no honest answer, so we fall back to interpreting the raw byte, which is what
    every real extractor does and what produces mojibake. We count these, because the
    count is the signal a pipeline can actually act on.
    """
    out = []
    for b in raw:
        if b in differences:
            mapped = glyph_to_char(differences[b])
            if mapped is not None:
                out.append(mapped)
                continue
            counter[0] += 1
        out.append(bytes([b]).decode("cp1252", errors="replace"))
    return "".join(out)


def glyph_to_char(glyph: str) -> str | None:
    """Resolve a glyph name to a character, or None if the name carries no meaning.

    Two naming conventions are honoured, both from the Adobe Glyph List: the standard
    names (`fi`, `emdash`) and the `uniXXXX` form. A subset font that names its glyphs
    `g1`, `g2`, ... satisfies neither, which is the whole failure: the name is an index
    into a table that was never shipped.
    """
    if glyph in GLYPH_TO_UNICODE:
        return GLYPH_TO_UNICODE[glyph]
    if len(glyph) == 7 and glyph.startswith("uni"):
        try:
            return chr(int(glyph[3:], 16))
        except ValueError:
            return None
    return None


def _tokenize_content(data: bytes) -> list[tuple[str, object]]:
    """Content-stream tokenizer: strings, numbers, names, operators. Nothing else."""
    tokens: list[tuple[str, object]] = []
    i = 0
    n = len(data)
    while i < n:
        ch = data[i : i + 1]
        if ch.isspace():
            i += 1
        elif ch == b"(":
            raw, i = _parse_string_literal(data, i)
            tokens.append(("str", raw))
        elif ch == b"/":
            j = i + 1
            while j < n and not data[j : j + 1].isspace() and data[j : j + 1] not in b"/[]<>(":
                j += 1
            tokens.append(("name", data[i + 1 : j].decode("latin-1")))
            i = j
        elif ch in b"[]":
            tokens.append(("op", ch.decode()))
            i += 1
        elif ch in b"+-.0123456789":
            j = i
            while j < n and data[j : j + 1] in b"+-.0123456789":
                j += 1
            try:
                tokens.append(("num", float(data[i:j])))
            except ValueError:
                pass
            i = j
        else:
            j = i
            while j < n and not data[j : j + 1].isspace() and data[j : j + 1] not in b"/[]()":
                j += 1
            tokens.append(("op", data[i:j].decode("latin-1")))
            i = max(j, i + 1)
    return tokens


def _runs_from_content(
    content: bytes, page_no: int, differences: dict[int, str], counter: list[int]
) -> list[ExtractedRun]:
    """Replay the text operators and record where each string landed.

    This is the whole of "PDF text extraction" at tier 1: there is no paragraph to
    read, so you track a text matrix and write down coordinates.
    """
    runs: list[ExtractedRun] = []
    stack: list[object] = []
    x = y = 0.0
    line_x = line_y = 0.0
    leading = 0.0

    for kind, value in _tokenize_content(content):
        if kind in ("num", "str", "name"):
            stack.append(value)
            continue
        op = value
        if op == "BT":
            x = y = line_x = line_y = 0.0
        elif op == "Tm" and len(stack) >= 6:
            x = line_x = float(stack[-2])  # type: ignore[arg-type]
            y = line_y = float(stack[-1])  # type: ignore[arg-type]
        elif op == "Td" and len(stack) >= 2:
            line_x += float(stack[-2])  # type: ignore[arg-type]
            line_y += float(stack[-1])  # type: ignore[arg-type]
            x, y = line_x, line_y
        elif op == "TD" and len(stack) >= 2:
            leading = -float(stack[-1])  # type: ignore[arg-type]
            line_x += float(stack[-2])  # type: ignore[arg-type]
            line_y += float(stack[-1])  # type: ignore[arg-type]
            x, y = line_x, line_y
        elif op == "TL" and stack:
            leading = float(stack[-1])  # type: ignore[arg-type]
        elif op == "T*":
            line_y -= leading
            x, y = line_x, line_y
        elif op == "Tj" and stack:
            text = _decode(stack[-1], differences, counter)  # type: ignore[arg-type]
            if text.strip():
                runs.append(ExtractedRun(page_no, x, y, text))
        elif op == "TJ":
            # Array form: strings interleaved with kerning offsets. Real producers use
            # this constantly; the offsets are what makes naive extraction insert or
            # drop spaces between words.
            parts = [v for v in stack if isinstance(v, bytes)]
            text = "".join(_decode(p, differences, counter) for p in parts)
            if text.strip():
                runs.append(ExtractedRun(page_no, x, y, text))
        if op in ("Tj", "TJ", "Tm", "Td", "TD", "T*", "TL", "Tf", "BT", "ET", "]", "["):
            stack.clear()
    return runs


def extract(pdf_bytes: bytes) -> ExtractedDoc:
    """Parse a PDF and return per-page glyph runs. Deliberately tier 1 (§3.3)."""
    objects: dict[int, bytes] = {}
    streams: dict[int, bytes] = {}
    for match in _OBJ_RE.finditer(pdf_bytes):
        number = int(match.group(1))
        body = match.group(2)
        objects[number] = body
        stream = _STREAM_RE.search(body)
        if stream:
            streams[number] = stream.group(1)

    differences: dict[int, str] = {}
    for body in objects.values():
        diff = _DIFF_RE.search(body)
        if diff:
            for code, name in _DIFF_ITEM_RE.findall(diff.group(1)):
                differences[int(code)] = name.decode("ascii")

    has_tounicode = b"/ToUnicode" in pdf_bytes
    counter = [0]

    # Page order comes from /Kids, not from object number. Producers routinely write
    # page objects out of order, and trusting object order silently shuffles the
    # document — a reading-order bug one level above the ones §3.2 lists.
    kids_order: list[int] = []
    for body in objects.values():
        if b"/Type /Pages" in body:
            kids = re.search(rb"/Kids\s*\[(.*?)\]", body, re.DOTALL)
            if kids:
                kids_order = [int(m) for m in re.findall(rb"(\d+)\s+0\s+R", kids.group(1))]
            break

    pages: list[ExtractedPage] = []
    for page_no, obj_num in enumerate(kids_order, start=1):
        body = objects.get(obj_num, b"")
        has_image = b"/XObject" in body
        contents = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        runs: list[ExtractedRun] = []
        if contents:
            content = streams.get(int(contents.group(1)), b"")
            runs = _runs_from_content(content, page_no, differences, counter)
        pages.append(ExtractedPage(page_no, runs, has_image))

    return ExtractedDoc(pages, has_tounicode, counter[0])


# --------------------------------------------------------------------------------
# Reading order — where the information actually gets destroyed
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    page: int
    y: float
    x: float
    text: str


def group_lines(runs: list[ExtractedRun], y_tol: float = 2.0) -> list[Line]:
    """Cluster runs into visual lines by y, then order left-to-right within the line.

    `y_tol` exists because glyphs on one visual line are not emitted at identical y —
    superscripts, inline font changes and baseline shifts all perturb it. Too small
    and one line becomes three; too large and a two-column page's rows merge across
    the gutter. There is no correct value, only a corpus-calibrated one, which is the
    first hint that everything above the glyph level is a heuristic.
    """
    lines: list[Line] = []
    for page in sorted({r.page for r in runs}):
        page_runs = sorted((r for r in runs if r.page == page), key=lambda r: (-r.y, r.x))
        buffer: list[ExtractedRun] = []
        for run in page_runs:
            if buffer and abs(buffer[0].y - run.y) > y_tol:
                buffer.sort(key=lambda r: r.x)
                lines.append(
                    Line(page, buffer[0].y, buffer[0].x, " ".join(r.text for r in buffer))
                )
                buffer = []
            buffer.append(run)
        if buffer:
            buffer.sort(key=lambda r: r.x)
            lines.append(Line(page, buffer[0].y, buffer[0].x, " ".join(r.text for r in buffer)))
    return lines


def find_gutter(runs: list[ExtractedRun], min_width: float = 24.0) -> float | None:
    """Locate a vertical whitespace band wide enough to be a column gutter.

    The method is the simplest thing that works: project every run's x-extent onto the
    x axis, find the widest uncovered band that is not at the page margin, and call it
    the gutter if it clears `min_width`. A real layout model does this with a trained
    region detector — that is the tier-1 → tier-2 step in §3.3, and the reason tier 2
    costs CPU-seconds per page instead of milliseconds.

    Returns the gutter's centre x, or None on a single-column page.
    """
    if not runs:
        return None
    # Estimate each run's width. Helvetica averages ~0.5em per character; this is a
    # crude approximation and it is *enough*, which is itself the lesson.
    spans = sorted((r.x, r.x + 0.5 * len(r.text) * 10.5) for r in runs)
    left = min(s[0] for s in spans)
    right = max(s[1] for s in spans)

    best: tuple[float, float] | None = None
    cursor = left
    for start, end in spans:
        if start - cursor > min_width:
            candidate = (cursor, start)
            if best is None or (candidate[1] - candidate[0]) > (best[1] - best[0]):
                best = candidate
        cursor = max(cursor, end)
    if best is None:
        return None
    centre = (best[0] + best[1]) / 2
    # A band hugging either margin is padding, not a gutter.
    if centre < left + 0.2 * (right - left) or centre > right - 0.2 * (right - left):
        return None
    return centre


def page_text(
    doc: ExtractedDoc,
    *,
    reading_order: str = "naive",
    drop_lines: set[str] | None = None,
) -> list[str]:
    """Reconstruct one text string per page under a stated reading-order policy.

    reading_order:
      "naive"   — sort all runs by (-y, x). Correct on one column, catastrophic on two.
      "columns" — detect a gutter per page and read each column top-to-bottom in turn.

    Both are honest tier-1 implementations. The gap between their outputs on the same
    file is the concrete measure of what "the parser sets the ceiling" (§1) means.
    """
    drop_lines = drop_lines or set()
    out: list[str] = []
    for page in doc.pages:
        if not page.runs:
            out.append("")
            continue
        if reading_order == "columns":
            gutter = find_gutter(page.runs)
            if gutter is not None:
                left = [r for r in page.runs if r.x < gutter]
                right = [r for r in page.runs if r.x >= gutter]
                # Runs spanning the full width (titles, running heads) sit in whichever
                # column their origin falls in. A layout model would classify them as
                # page-level elements instead; tier 1 cannot, and that is the defect.
                lines = group_lines(left) + group_lines(right)
            else:
                lines = group_lines(page.runs)
        else:
            lines = group_lines(page.runs)
        kept = [ln.text for ln in lines if mask_digits(ln.text.strip()) not in drop_lines]
        out.append("\n".join(kept))
    return out


def mask_digits(text: str) -> str:
    """Collapse digit runs to `#`, so `Page 1 of 3` and `Page 2 of 3` compare equal.

    This one line is the difference between catching a running footer and missing it.
    A footer bearing a page number is *never* byte-identical across pages, so exact
    matching finds the header and silently leaves the footer spliced into the body —
    the §3.2 failure the filter was supposed to remove. Found by building it: the
    first version of `detect_running_lines` matched on exact text and caught only the
    header of `report_twocol.pdf`.
    """
    return re.sub(r"\d+", "#", text)


def detect_running_lines(doc: ExtractedDoc, threshold: float = 0.6) -> set[str]:
    """Find running heads and footers: same text, same vertical band, most pages.

    This is §3.5's repeated-block filter applied *within* one document rather than
    across a domain, and it is a better fix than hoping a layout model classifies the
    header correctly — it needs no model and no per-document rule.

    Returns digit-masked line forms; compare candidates with `mask_digits()`.
    """
    if len(doc.pages) < 3:
        return set()  # Not enough pages to distinguish a running head from a heading.
    from collections import defaultdict

    seen: dict[tuple[str, int], set[int]] = defaultdict(set)
    for page in doc.pages:
        for line in group_lines(page.runs):
            # Bucket y to absorb sub-point jitter between pages.
            seen[(mask_digits(line.text.strip()), round(line.y / 4))].add(page.number)

    n = len(doc.pages)
    out: set[str] = set()
    for (text, _), page_numbers in seen.items():
        if text and len(page_numbers) / n >= threshold:
            out.add(text)
    return out
