"""The pipeline: seven stages, each versioned, each with its own failure mode (`02` §2).

    Acquire → Parse → Normalize → Split → Enrich → (Embed) → (Index)

Embedding and indexing are stubbed — this lab produces *chunks and their metadata*,
not vectors, and everything it measures is measurable without a model. That is not a
limitation being apologised for; §11.4's finding is that recall barely separates
chunking strategies while token efficiency separates them by an order of magnitude,
and token efficiency is arithmetic.

Two architectural commitments are implemented here rather than described:

**Stage outputs are kept, not just the final chunks.** `Result` holds the parse, the
canonical text and the chunks. Persisting each stage is the cheapest architectural
decision in an ingestion pipeline (§2): it costs object storage — the cheapest
resource in the system — and converts "we changed the chunker, so re-download and
re-parse 400k PDFs" into "re-run stages 4-7 from the cached parse". Parsing is
frequently the most expensive stage and the one you least often need to redo.

**Every stage is version-stamped independently.** `parser_version`,
`normalizer_version`, `chunker_version` all land on every chunk. They cost a few bytes
and they are the difference between "some chunks in this index are stale" and
`WHERE chunker_version < 7`. Teams omit them and then wish they had them during an
incident.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import normalize as N
import parse as PA
import split as SP
from dedup import content_hash
from identity import Store, assign_ids, reindex_document
from normalize import NORMALIZER_VERSION, CanonicalDoc
from parse import ParsedDoc
from split import CHUNKER_VERSION, Chunk

EMBEDDING_MODEL_VERSION = "none@lab"  # no model is called; the field must still exist


# --------------------------------------------------------------------------------
# The chunk record — §8.1's table, as a schema
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkRecord:
    """What actually gets written to the index: one embedded string plus a payload.

    The split between `embed_text` and everything else is §8.2's rule, and it is the
    one people get wrong in the most tempting way. Prepending
    `"created_at: 2024-03-14 | tenant: acme"` to a chunk before embedding *feels* like
    it works, because retrieval still returns results. What it actually does is add a
    small amount of noise to every vector in the corpus, uniformly, in exchange for a
    matching behaviour dense models do not reliably provide for dates and IDs.

    The rule that holds:

    - **Structured, enumerable or ordered attributes → payload, queried with a
      filter.** Dates, tenant IDs, document types, statuses, numeric ranges.
    - **Natural-language context a human would need to interpret the chunk →
      embedded text.** Title, heading path, table caption, enclosing class signature.

    The test is simple: *would a human reading only this chunk need it to know what the
    chunk is about?* A heading path passes. A tenant ID never does. `assert_no_leakage`
    below enforces it, because "we accidentally embedded the tenant ID" is invisible
    to every test that only checks retrieval works.
    """

    # identity and provenance
    chunk_id: str
    doc_id: str
    source_uri: str
    content_hash: str

    # the text, in its three roles (§4.3)
    text: str  # canonical slice — stored, cited, shown
    embed_text: str  # what the embedding model sees

    # offsets — §4.4, two integers that buy citation, eval and forensics
    char_start: int
    char_end: int

    # structure and the §7 pattern
    kind: str
    heading_path: tuple[str, ...]
    page: int | None
    parent_id: str | None

    # filterable payload — never embedded (§8.2)
    tenant_id: str
    acl: tuple[str, ...]
    created_at: str
    modified_at: str

    # migration, shadow indexing, incident forensics (§2, §8.1)
    parser_version: str
    normalizer_version: str
    chunker_version: str
    embedding_model_version: str

    strategy: str = ""
    meta: dict = field(default_factory=dict, compare=False)

    def lexical_text(self) -> list[str]:
        """The BM25 branch, derived on demand and never stored (§4.3)."""
        return N.analyze(self.text)


PAYLOAD_ONLY_FIELDS = ("tenant_id", "acl", "created_at", "modified_at", "chunk_id")


def assert_no_leakage(record: ChunkRecord) -> None:
    """Fail loudly if a filterable attribute reached the embedded text (§8.2).

    Cheap, and it catches the single most common metadata mistake at the only moment
    it is still free to fix. Once those vectors are written, finding out costs a
    re-embed of the entire corpus.
    """
    for name in PAYLOAD_ONLY_FIELDS:
        value = getattr(record, name)
        values = value if isinstance(value, tuple) else (value,)
        for item in values:
            if item and str(item) in record.embed_text:
                raise AssertionError(
                    f"{record.doc_id}: payload field {name}={item!r} leaked into "
                    f"embed_text — see §8.2"
                )


# --------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Every parameter that changes the output, in one place with an explicit value.

    §13's anti-pattern 2: *treat any splitter parameter you did not set deliberately
    as unset.* Defaults live here rather than in function signatures scattered across
    four modules, so that "what was this index built with?" has one answer.
    """

    strategy: str = "structural"  # fixed | recursive | structural | parent_child
    max_tokens: int = 256
    overlap: int = 0
    min_tokens: int = 24
    child_tokens: int = 64
    separators: tuple[str, ...] | None = None
    reading_order: str = "columns"  # naive | columns
    strip_running: bool = True
    id_scheme: str = "content"
    tenant_id: str = "acme"
    acl: tuple[str, ...] = ("group:engineering",)
    # A fixed timestamp: real pipelines use the source's mtime, and a wall-clock call
    # here would make the manifest non-reproducible for no benefit.
    ingested_at: str = "2024-03-14T00:00:00Z"

    def label(self) -> str:
        base = f"{self.strategy}/{self.max_tokens}"
        if self.overlap:
            base += f"+{self.overlap}ov"
        if self.strategy == "parent_child":
            base = f"parent_child/{self.child_tokens}->{self.max_tokens}"
        return base


# --------------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------------


@dataclass
class Metrics:
    chunks: int
    tokens_total: int
    p50: int
    p95: int
    max_tokens_seen: int
    orphans: int
    oversize: int
    index_bytes: int
    parse_ms: float
    split_ms: float

    @property
    def orphan_rate(self) -> float:
        return self.orphans / max(self.chunks, 1)


@dataclass
class Result:
    parsed: ParsedDoc
    gate: PA.GateVerdict
    canonical: CanonicalDoc | None
    chunks: list[Chunk]
    records: list[ChunkRecord]
    metrics: Metrics
    config: Config

    @property
    def doc_id(self) -> str:
        return self.parsed.doc_id

    @property
    def quarantined(self) -> bool:
        return not self.gate.ok


def _percentile(values: list[int], p: float) -> int:
    """Nearest-rank percentile, clamped. Correct for n=1 and n=2, unlike index math.

    Written out because the obvious `values[int(len(v)*p)-1]` returns a *lower* value
    than the median for n=2, which produced a p95 below p50 in the first run of this
    lab's report and looked like a bug in the chunker rather than in the statistic.
    """
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), -(-int(p * len(ordered) * 100) // 100)))
    return ordered[rank - 1]


def estimate_index_bytes(chunks: int, dim: int = 1536, bytes_per_component: int = 4) -> int:
    """Vector bytes only — §12.2's table, before graph overhead and payload.

    Halving chunk size roughly doubles this. It is the recurring bill, and it composes
    multiplicatively with the representation decisions in `01`: MRL truncation and int8
    or binary quantization multiply against this number rather than adding to it.
    """
    return chunks * dim * bytes_per_component


def build_chunks(canonical: CanonicalDoc, elements: list[PA.Element], config: Config) -> list[Chunk]:
    """Dispatch to the configured strategy. The one place strategy choice happens."""
    title = canonical.doc_id
    separators = list(config.separators) if config.separators else None

    if config.strategy == "fixed":
        return SP.fixed(canonical, size=config.max_tokens, overlap=config.overlap)
    if config.strategy == "recursive":
        return SP.recursive(
            canonical,
            max_tokens=config.max_tokens,
            separators=separators,
            min_tokens=config.min_tokens,
        )
    if config.strategy == "structural":
        return SP.structural(
            canonical,
            elements,
            max_tokens=config.max_tokens,
            min_tokens=config.min_tokens,
            doc_title=title,
        )
    if config.strategy == "parent_child":
        return SP.parent_child(
            canonical,
            elements,
            child_tokens=config.child_tokens,
            parent_tokens=config.max_tokens,
            doc_title=title,
        )
    raise ValueError(f"unknown strategy {config.strategy!r}")


def process(path: Path, root: Path, config: Config = Config()) -> Result:
    """Run one document through every stage, keeping each stage's output.

    The gate short-circuit is the important control flow: a document routed to OCR or
    review produces **zero chunks and a recorded reason**, rather than zero chunks and
    silence. That distinction is the entire content of §3.2's argument.
    """
    t0 = time.perf_counter()
    parsed = PA.parse_file(
        path, root, reading_order=config.reading_order, strip_running=config.strip_running
    )
    parse_ms = (time.perf_counter() - t0) * 1000

    verdict = PA.gate(parsed)
    if not verdict.ok:
        return Result(
            parsed, verdict, None, [], [],
            Metrics(0, 0, 0, 0, 0, 0, 0, 0, parse_ms, 0.0), config,
        )

    # `build_canonical` drops elements that normalize to nothing, so the element list
    # must be filtered identically or `structural` sees a span/element mismatch. This
    # is exactly the kind of coupling that argues for keeping both in one function.
    elements = [e for e in parsed.elements if N.canonicalize(e.text).strip()]
    canonical = N.build_canonical(parsed.doc_id, [e.text for e in elements])

    t1 = time.perf_counter()
    chunks = build_chunks(canonical, elements, config)
    split_ms = (time.perf_counter() - t1) * 1000

    ids = assign_ids(chunks, CHUNKER_VERSION, config.id_scheme)
    parent_ids = {
        (c.parent_span.start, c.parent_span.end): cid
        for c, cid in zip(chunks, ids)
        if c.parent_span
    }

    records: list[ChunkRecord] = []
    for chunk, cid in zip(chunks, ids):
        parent_id = (
            parent_ids.get((chunk.parent_span.start, chunk.parent_span.end))
            if chunk.parent_span
            else None
        )
        record = ChunkRecord(
            chunk_id=cid,
            doc_id=chunk.doc_id,
            source_uri=parsed.source_uri,
            content_hash=content_hash(chunk.text),
            text=chunk.text,
            embed_text=chunk.embed_text,
            char_start=chunk.span.start,
            char_end=chunk.span.end,
            kind=chunk.kind,
            heading_path=chunk.heading_path,
            page=chunk.page,
            parent_id=parent_id if parent_id != cid else None,
            tenant_id=config.tenant_id,
            acl=config.acl,
            created_at=config.ingested_at,
            modified_at=config.ingested_at,
            parser_version=parsed.parser,
            normalizer_version=NORMALIZER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model_version=EMBEDDING_MODEL_VERSION,
            strategy=chunk.strategy,
            meta=chunk.meta,
        )
        assert_no_leakage(record)
        records.append(record)

    counts = [c.tokens() for c in chunks]
    metrics = Metrics(
        chunks=len(chunks),
        tokens_total=sum(counts),
        p50=_percentile(counts, 0.50),
        p95=_percentile(counts, 0.95),
        max_tokens_seen=max(counts, default=0),
        orphans=sum(1 for c, n in zip(chunks, counts) if n < config.min_tokens and c.kind != "table_row"),
        # An atomic element over budget is a *correctness* problem, not a style one:
        # the embedding provider will silently truncate it (§5.1's C1, `01` §8) and
        # store a vector for the first part of the chunk that ranks exactly like a
        # complete one. This count must be zero or explained.
        oversize=sum(1 for n in counts if n > config.max_tokens),
        index_bytes=estimate_index_bytes(len(chunks)),
        parse_ms=parse_ms,
        split_ms=split_ms,
    )
    return Result(parsed, verdict, canonical, chunks, records, metrics, config)


def process_corpus(root: Path, config: Config = Config()) -> list[Result]:
    """Every file under `root`, in sorted order so runs are comparable."""
    return [
        process(path, root, config)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    ]


def index_corpus(results: list[Result]) -> Store:
    """Write every non-quarantined document into a store via the diff path (§9.2)."""
    store = Store()
    for result in results:
        if result.quarantined:
            continue
        reindex_document(result.doc_id, result.chunks, store, scheme=result.config.id_scheme)
    return store
