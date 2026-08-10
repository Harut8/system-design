"""Table representation and the three serializations from `02` §3.4.

A table's meaning lives in the *association* between a cell, its column header and its
row label. Every serialization below keeps or breaks that association differently, and
the chapter's non-obvious claim is that they are **not a single choice**: the winning
pattern is to index one form and return another.

    index the row-wise serialization, return the full table

which is §7.1's parent-document retrieval applied to a table. Each row-wise sentence
is a self-contained factual statement — the shape dense retrievers are trained on —
while the HTML form is what a generator needs to reason across rows.

The second fix in §3.4 costs nothing and is implemented here as `split_with_headers`:
**if a table is split across chunks, repeat the header row in every chunk.** A 200-row
table chunked at 512 tokens otherwise produces one chunk with headers and a dozen
unlabeled number grids.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Table:
    """A recovered grid. `caption` and `context` are what make chunks interpretable."""

    header: list[str]
    rows: list[list[str]]
    caption: str = ""
    context: str = ""  # heading path or surrounding sentence — free contextualization

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.rows), len(self.header)


def to_scan_order(table: Table) -> str:
    """What a naive extractor produces: cells in reading order, associations gone.

    This is not a serialization anybody should choose. It is here because it is what
    you get *by default* from tier-1 PDF extraction, and seeing it next to the others
    is the argument for doing anything else.
    """
    cells = list(table.header)
    for row in table.rows:
        cells.extend(row)
    return " ".join(cells)


def to_markdown(table: Table) -> str:
    """Pipe table. Preserves column alignment for a simple rectangular grid.

    Loses merged cells, nesting and multi-row headers — none of which this lab's
    fixtures contain, which is exactly why a real corpus is the only place to find out
    whether that loss matters to you.
    """
    out = ["| " + " | ".join(table.header) + " |"]
    out.append("|" + "|".join("---" for _ in table.header) + "|")
    for row in table.rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def to_html(table: Table) -> str:
    """Full structure, including anything a future fixture adds (rowspan, nesting).

    Highest token cost — markup is tokens — and the right thing to hand a generator
    when it has to reason across rows rather than look one value up.
    """
    out = ["<table>"]
    if table.caption:
        out.append(f"  <caption>{table.caption}</caption>")
    out.append("  <thead><tr>" + "".join(f"<th>{h}</th>" for h in table.header) + "</tr></thead>")
    out.append("  <tbody>")
    for row in table.rows:
        out.append("    <tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    out.append("  </tbody>")
    out.append("</table>")
    return "\n".join(out)


def to_row_sentences(table: Table) -> list[str]:
    """One self-contained sentence per row, with the header association made explicit.

    `"For Q2 2024, revenue was 1,318 and cost was 1,275."` — the header↔cell link is
    stated in prose the embedding model was trained on rather than implied by column
    position, which no pooled vector recovers. Each row becomes independently
    retrievable, which is the whole point.

    The first column is treated as the row label. That is a heuristic and it is wrong
    for a table whose key is the last column or a composite of two — a real
    implementation should take the key column as a parameter, and the fact that this
    one does not is the kind of default §13's anti-pattern 2 is about.
    """
    if not table.header:
        return []
    key_name, *value_names = table.header
    prefix = f"{table.context}. " if table.context else ""
    caption = f"{table.caption} " if table.caption else ""

    out: list[str] = []
    for row in table.rows:
        if not row:
            continue
        key, *values = row
        pairs = [
            f"{name.lower()} was {value}"
            for name, value in zip(value_names, values)
            if value.strip()
        ]
        if not pairs:
            continue
        body = ", ".join(pairs[:-1]) + (" and " if len(pairs) > 1 else "") + pairs[-1]
        out.append(f"{prefix}{caption}For {key_name.lower()} {key}, {body}.")
    return out


def split_with_headers(table: Table, rows_per_chunk: int) -> list[Table]:
    """Split a long table, repeating the header, caption and context in every piece.

    Free, deterministic, and it converts unlabeled number grids into interpretable
    chunks (§3.4, mental model 4). The context being prepended is already in the
    document, so unlike `01` §9.2's contextual retrieval this costs no generation
    tokens and introduces no chunk-ID churn on reprocessing.
    """
    if rows_per_chunk < 1:
        raise ValueError("rows_per_chunk must be >= 1")
    out: list[Table] = []
    for i in range(0, len(table.rows), rows_per_chunk):
        window = table.rows[i : i + rows_per_chunk]
        part = i // rows_per_chunk + 1
        total = (len(table.rows) + rows_per_chunk - 1) // rows_per_chunk
        caption = table.caption
        if total > 1 and caption:
            caption = f"{caption} (part {part} of {total})"
        out.append(Table(table.header, window, caption, table.context))
    return out


@dataclass
class GridRecovery:
    """Result of reconstructing a grid from positioned glyph runs, with its evidence."""

    table: Table | None
    column_x: list[float] = field(default_factory=list)
    confidence: str = "none"  # none | low | high
    note: str = ""


def recover_grid(
    runs: list[tuple[float, float, str]], *, x_tol: float = 12.0, min_rows: int = 3
) -> GridRecovery:
    """Rebuild a table from (x, y, text) runs by clustering x-positions into columns.

    This is the concrete answer to "the extractor emits cells in scan order" (§3.2).
    Cells in one column share an x origin because that is how the producer laid them
    out, so clustering x recovers the columns and clustering y recovers the rows.

    It works here because this lab's fixture is a clean, left-aligned, rectangular
    grid with no merged cells and no wrapped cell text. **Do not mistake that for a
    general table extractor.** Right-aligned numeric columns break the x-clustering,
    wrapped cells break the y-clustering, and merged headers break both. That gap is
    precisely what a tier-2 layout model is being paid for (§3.3), and the honest
    output of this function on a hard table is `confidence="low"` and a routing
    decision, not a wrong grid presented as a right one.
    """
    if len(runs) < min_rows:
        return GridRecovery(None, note="too few runs to be a grid")

    rows: dict[float, list[tuple[float, str]]] = {}
    for x, y, text in runs:
        key = next((k for k in rows if abs(k - y) <= 2.0), y)
        rows.setdefault(key, []).append((x, text))

    banded = sorted(rows.items(), key=lambda kv: -kv[0])
    grid_rows = [sorted(cells) for _, cells in banded if len(cells) >= 2]
    if len(grid_rows) < min_rows:
        return GridRecovery(None, note="fewer than min_rows multi-cell bands")

    # Column origins: every x that appears in a multi-cell band, clustered by x_tol.
    xs = sorted({x for row in grid_rows for x, _ in row})
    columns: list[float] = []
    for x in xs:
        if not columns or x - columns[-1] > x_tol:
            columns.append(x)

    def to_cells(row: list[tuple[float, str]]) -> list[str]:
        cells = [""] * len(columns)
        for x, text in row:
            idx = min(range(len(columns)), key=lambda i: abs(columns[i] - x))
            cells[idx] = (cells[idx] + " " + text).strip()
        return cells

    all_cells = [to_cells(row) for row in grid_rows]
    header, *body = all_cells

    widths = {len(row) for row in grid_rows}
    filled = [c for row in all_cells for c in row if c]
    mean_cell_words = sum(len(c.split()) for c in filled) / max(len(filled), 1)

    # Three conditions, and the middle one was added because the fixture corpus proved
    # it necessary. Clustering x alone reports a confident 2-column "table" for every
    # two-column *text* page in `report_twocol.pdf`: left and right columns share y
    # bands, so each band looks exactly like a two-cell row. Geometry cannot tell a
    # two-column layout from a two-column table — they are the same picture. So:
    #
    #   - >= 3 columns, because a 2-column grid is genuinely ambiguous;
    #   - short cells, because table cells are values and prose lines are sentences;
    #   - uniform row widths, the original check.
    #
    # This is the honest boundary of tier 1 (§3.3). A layout model resolves the
    # ambiguity by *looking at the page* — ruling lines, cell padding, alignment — and
    # that is what its CPU-seconds per page are buying.
    reasons = []
    if len(columns) < 3:
        reasons.append(f"{len(columns)} columns — indistinguishable from a text layout")
    if mean_cell_words > 4:
        reasons.append(f"mean cell is {mean_cell_words:.1f} words — reads as prose")
    if len(widths) != 1:
        reasons.append(f"ragged rows (widths {sorted(widths)})")

    confidence = "high" if not reasons else "low"
    note = "uniform short cells in >=3 columns" if confidence == "high" else "; ".join(reasons)
    return GridRecovery(Table(header, body), columns, confidence, note)
