"""Generate the fixture corpus: one document per failure class in `02` §3.

Run `python3 make_fixtures.py` to (re)generate `corpus/`. Output is byte-for-byte
deterministic — no timestamps, no unseeded randomness, sorted iteration everywhere —
because a fixture corpus that changes between runs cannot be used to demonstrate
§9's content-addressed IDs. The whole point of that section is that identical input
produces identical chunk IDs, and you cannot show it with a corpus that drifts.

**Why generate instead of committing real documents.** Two reasons, and the second
matters more. The first is licensing: a corpus of real 10-Ks and clinical PDFs is not
mine to redistribute. The second is that a real corpus does not tell you *which*
failure it contains. Here the fixture and the failure are written together, so
`report_twocol.pdf` is known to contain exactly column interleaving, a running head,
a hyphenated line break and a ligature — which means a gate that fails to fire on it
is a bug in the gate, not an ambiguity about the document. That is what makes the
test suite possible at all.

**What is synthetic and what is real.** The *bytes* are real: `report_twocol.pdf` is a
valid PDF that a viewer renders, and its text is destroyed by naive extraction for
exactly the reason real PDFs' text is destroyed. The *prose* is written for the lab.
`book.md`'s body is composed from a fixed sentence pool by a seeded generator, so it
is structurally realistic (parts, chapters, sections, a table, footnotes, an epigraph)
and semantically thin. Nothing in this lab measures answer quality, so thin prose
costs nothing; if you extend the lab toward retrieval quality, swap the book for a
real one.
"""

from __future__ import annotations

import random
import textwrap
from pathlib import Path

import pdfmini as P

CORPUS = Path(__file__).parent / "corpus"


# --------------------------------------------------------------------------------
# 1. Pure text — no structure survives because none was ever there
# --------------------------------------------------------------------------------

TRANSCRIPT = """\
Support call transcript, ticket 44821. Recorded line. Transcription is automatic and
unpunctuated in places. No speaker labels were captured by the telephony bridge, which
is the normal case for this vendor and the reason this file has no structure at all.

so the issue started tuesday afternoon when the batch job stopped writing to the
warehouse we noticed because the morning dashboard was empty and the on call engineer
got paged at six twenty. we looked at the airflow logs first and there was nothing
obviously wrong the dag showed success on every task which is what confused us for the
first hour or so.

then we checked the actual table and the partition for tuesday was there but it had
about four hundred rows instead of the usual two million. so the job ran it just
processed almost nothing. we traced it back to the upstream export which had silently
started emitting an empty manifest file. the export itself succeeded so nothing alerted.

the fix was to add a row count assertion on the manifest before the load step. if the
manifest declares fewer than a hundred thousand rows the task fails loudly instead of
loading whatever it found. we also backfilled tuesday and wednesday. total data loss
window was about thirty one hours.

what i would want going forward is a check that compares each days row count against
the trailing seven day median and pages if it deviates by more than say forty percent.
that would have caught this in the first hour rather than the fifth. we have that for
revenue tables already it just was never added for this one.

the other thing worth writing down is that the dashboard being empty was the actual
detection mechanism. a human noticed. that is not a monitoring strategy it is luck and
it means anything that breaks on a weekend goes undetected until monday morning.
"""


# --------------------------------------------------------------------------------
# 2. Markdown — the format where structure is free
# --------------------------------------------------------------------------------

HANDBOOK = """\
# Ingestion Service Handbook

The ingestion service turns source documents into indexed chunks. This handbook covers
operation, not design; the design rationale lives in the architecture decision records.

## 1. Running the service

### 1.1 Local development

The service reads from a local directory when `INGEST_SOURCE=file://`. No credentials
are required in this mode, which makes it the right way to reproduce a parsing bug
reported from production.

```python
from ingest import Pipeline

pipeline = Pipeline.from_env()
result = pipeline.run(source="file://./samples", dry_run=True)
print(result.summary())
```

Note that `dry_run=True` still parses and chunks; it only skips the embed and index
stages. That is deliberate — the stages you want to debug are the cheap ones, and
making them free to re-run is the whole reason the pipeline persists intermediate
artifacts.

### 1.2 Production

Production runs on the batch cluster. The unit of work is a document, not a file, so a
multi-document archive is expanded during acquisition rather than during parsing.

## 2. Operational limits

### 2.1 Document size

Documents above 200 MB are routed to the large-document queue, which runs with a
higher memory limit and a lower concurrency. Nothing else about their processing
differs.

### 2.2 Rate limits

The embedding provider enforces a tokens-per-minute quota shared across all tenants.
When the quota is exhausted the pipeline backs off and retries; it does not drop work.
Sustained backoff for more than fifteen minutes raises an alert.

## 3. Failure handling

### 3.1 Quarantine

A document that fails a parse gate is written to the quarantine bucket with the gate's
verdict attached. Quarantine is not a dead-letter queue: documents there are expected
to be re-driven after the parser is fixed, and the verdict is what tells you which
parser change to make.

### 3.2 Poison documents

A document that crashes the parser twice is marked poison and skipped. The count of
poison documents is a tracked metric, and it should be zero. A non-zero steady state
means the parser has a bug nobody has prioritised.
"""


# --------------------------------------------------------------------------------
# 3. Markdown with a table — §3.4's whole argument in one document
# --------------------------------------------------------------------------------

METRICS = """\
# Regional Performance Review

## Summary

Revenue grew in three of four regions. The decline in APAC is attributable to the
contract renegotiation completed in the second quarter, and is expected to reverse in
the next reporting period.

## Quarterly revenue by region

All figures in thousands of USD. Margin is gross margin after allocated cost of
delivery.

| Region | Q1 revenue | Q2 revenue | Q1 margin | Q2 margin | Headcount |
|---|---|---|---|---|---|
| EMEA | 1,204 | 1,318 | 41.2% | 43.8% | 62 |
| AMER | 2,880 | 3,142 | 38.9% | 39.4% | 118 |
| APAC | 940 | 812 | 44.1% | 36.2% | 47 |
| LATAM | 315 | 388 | 29.7% | 31.1% | 19 |

## Commentary

AMER remains the largest region by revenue and by headcount. LATAM grew fastest in
percentage terms from the smallest base, which is the pattern every year and should
not be read as a signal.
"""


# --------------------------------------------------------------------------------
# 4. Normalization hazards — the §4.2 fixture
# --------------------------------------------------------------------------------
# Every line here is a place where a reflexive `NFKC(text.lower())` destroys meaning.
# The characters are written as escapes so the intent survives an editor that helpfully
# "cleans up" the file.

NOTATION = (
    "# Notation, Units, and Names\n"
    "\n"
    "A reference sheet for the normalization stage. Every line below is a case where\n"
    "the reflexive `unicodedata.normalize(\"NFKC\", text).lower()` changes what the text\n"
    "means rather than what it looks like.\n"
    "\n"
    "## 1. Mathematical notation\n"
    "\n"
    "The kinetic energy term is E = mc², and the constraint surface is x² + y² = r².\n"
    "Under NFKC the superscript two becomes an ordinary digit, so x² collapses to x2 —\n"
    "a different expression, silently.\n"
    "\n"
    "The detector noise floor is 10⁻⁹ A/√Hz. Sample volumes are quoted in m³ and areas\n"
    "in m². A ½ cup is not the same as a ¼ cup, and NFKC rewrites both into digit-slash\n"
    "sequences that no longer sort or compare as quantities.\n"
    "\n"
    "Concentration was 5 µM using the micro sign (U+00B5) and 5 μM using Greek small mu\n"
    "(U+03BC). These are distinct codepoints that NFKC merges. Bond length was 1.54 Å.\n"
    "\n"
    "## 2. Roman numerals\n"
    "\n"
    "Schedule Ⅶ of the Act supersedes Schedule Ⅳ and amends Schedule Ⅻ. Those are the\n"
    "Unicode Number Forms characters, not the ASCII letters. Written in ASCII the same\n"
    "sentence reads: Schedule VII supersedes Schedule IV and amends Schedule XII.\n"
    "\n"
    "A citation index that NFKC-folds one form and not the other will treat the two\n"
    "sentences as unrelated in the lexical branch and as near-identical in the dense one.\n"
    "\n"
    "## 3. Case carries entity signal\n"
    "\n"
    "The US filing deadline differs from the one us contractors were given.\n"
    "A Polish supplier will polish the housing before shipment.\n"
    "IT owns the ticket, and it is assigned to the platform team.\n"
    "Revenue in March exceeded plan; the auditors march through the ledger in April.\n"
    "The figure was disclosed by Apple; the apple harvest was unaffected.\n"
    "The vendor of record is SAP; sap ingress damaged two units.\n"
    "Guidance was published by WHO, and nobody recorded who approved it.\n"
    "The AI team shipped it, and the ai particle is unrelated.\n"
    "\n"
    "Lowercasing merges every left-hand term into its right-hand homograph. BM25 wants\n"
    "that folding; the dense branch does not (§4.3).\n"
    "\n"
    "## 4. Full-width and compatibility forms\n"
    "\n"
    "The invoice number was recorded as ＡＢＣ１２３ in the source system and as ABC123 in\n"
    "the replica. Under NFKC these become identical, which is convenient right up to the\n"
    "point where the two systems disagree about which record is authoritative.\n"
    "\n"
    "## 5. Ligatures and invisible characters\n"
    "\n"
    "The classiﬁcation of ﬂow is deﬁned in the oﬃce manual. Those three ligatures are\n"
    "single codepoints; a BM25 query for classification will not match this line until\n"
    "they are expanded, which is the one compatibility mapping you do want (§4.1).\n"
    "\n"
    "This line contains a soft hyphen in the word docu­ment, a zero-width space in\n"
    "cost​centre, and curly quotes around ‘identity’ rather than 'identity'. All three\n"
    "are invisible in a rendered view and all three break exact lexical match.\n"
)


# --------------------------------------------------------------------------------
# 5. HTML with site chrome — §3.5
# --------------------------------------------------------------------------------

HTML_NAV = """\
<nav class="site-nav">
  <a href="/">Home</a> <a href="/docs">Docs</a> <a href="/blog">Blog</a>
  <a href="/pricing">Pricing</a> <a href="/contact">Contact</a>
</nav>
<div class="cookie-banner">
  We use cookies to improve your experience. By continuing to browse this site you
  agree to our use of cookies. Read our cookie policy for details.
</div>
"""

HTML_FOOTER = """\
<aside class="related">
  <h3>Related articles</h3>
  <ul><li><a href="/a">Scaling ingestion</a></li><li><a href="/b">Index sizing</a></li></ul>
</aside>
<footer>
  <p>Copyright 2024 Northwind Data Systems. All rights reserved.</p>
  <p>Registered in England and Wales, company number 08812441. VAT GB 187 4421 09.</p>
  <p>Terms of service | Privacy policy | Modern slavery statement | Accessibility</p>
</footer>
"""

HTML_ARTICLES = [
    (
        "backpressure.html",
        "Backpressure in ingestion pipelines",
        [
            "A pipeline without backpressure does not fail gracefully; it fails all at once, "
            "at the stage with the smallest buffer, usually at three in the morning.",
            "The useful mental model is that every stage has a queue, and the queue you did "
            "not configure is the one with the default depth chosen by a library author.",
            "Measuring queue depth per stage is cheap and it turns an incident narrative "
            "into an arithmetic problem about which stage is slower than its upstream.",
        ],
    ),
    (
        "parser-tiers.html",
        "Choosing a parser tier",
        [
            "Parser selection is usually made once, in week one, by whoever was setting up "
            "the repository, and then never revisited even as the corpus changes underneath it.",
            "The cost difference between geometric extraction and a layout model is three "
            "orders of magnitude per page, so the decision deserves a measurement rather than a default.",
            "The outcome tier one was sufficient is a valid and valuable result, and it is "
            "the one nobody publishes.",
        ],
    ),
    (
        "quarantine.html",
        "Why quarantine beats a dead letter queue",
        [
            "A dead letter queue accumulates documents nobody looks at, because nothing in "
            "the queue says what to do about them.",
            "Attaching the gate verdict to each quarantined document converts the pile into "
            "a work list sorted by which parser change unblocks the most documents.",
            "The metric that matters is not quarantine depth but quarantine age, because a "
            "shallow queue nobody drains is the same failure as a deep one.",
        ],
    ),
    (
        "offsets.html",
        "Keep the character offsets",
        [
            "Recording each chunk's start and end offset into the canonical text costs two "
            "integers and buys citation highlighting, span level evaluation, and incident forensics.",
            "Retrofitting offsets after the fact means re-running ingestion over the whole "
            "corpus, which is the expensive stage, for information you could have kept for free.",
            "Storing citations as document plus offset rather than as a chunk identifier is "
            "what lets saved conversations survive a re-chunk.",
        ],
    ),
    (
        "deletes.html",
        "The delete you skipped",
        [
            "Upsert works and deletes are extra code, so deletes are the step that gets "
            "skipped, and nothing visibly breaks on the day you skip them.",
            "Content removed from a document but left in the index is retrieved and cited as "
            "current, with a source link that no longer contains it.",
            "That is worse than missing data, because it is confidently wrong data carrying a "
            "citation that survives casual checking.",
        ],
    ),
]


def html_page(title: str, paragraphs: list[str]) -> str:
    body = "\n".join(f"  <p>{p}</p>" for p in paragraphs)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        f"  <title>{title} — Northwind Data Systems</title>\n"
        "  <meta charset=\"utf-8\">\n</head>\n<body>\n"
        f"{HTML_NAV}"
        "<article>\n"
        f"  <h1>{title}</h1>\n{body}\n"
        "</article>\n"
        f"{HTML_FOOTER}"
        "</body>\n</html>\n"
    )


# --------------------------------------------------------------------------------
# 6. Spreadsheet — §3.6's "wrong tool"
# --------------------------------------------------------------------------------

REVENUE_CSV = """\
region,country,quarter,fiscal_year,revenue_usd,cost_usd,headcount
EMEA,United Kingdom,Q1,2024,412000,238000,21
EMEA,Germany,Q1,2024,388000,221000,19
EMEA,France,Q1,2024,404000,249000,22
AMER,United States,Q1,2024,2104000,1288000,88
AMER,Canada,Q1,2024,776000,468000,30
APAC,Japan,Q1,2024,512000,281000,24
APAC,Singapore,Q1,2024,428000,244000,23
LATAM,Brazil,Q1,2024,315000,221000,19
EMEA,United Kingdom,Q2,2024,455000,251000,22
EMEA,Germany,Q2,2024,431000,238000,20
EMEA,France,Q2,2024,432000,252000,20
AMER,United States,Q2,2024,2298000,1392000,92
AMER,Canada,Q2,2024,844000,512000,26
APAC,Japan,Q2,2024,441000,281000,24
APAC,Singapore,Q2,2024,371000,237000,23
LATAM,Brazil,Q2,2024,388000,267000,19
"""


# --------------------------------------------------------------------------------
# 7. Source code — §3.7
# --------------------------------------------------------------------------------

SERVICE_PY = '''\
"""Manifest validation for the ingestion pipeline.

The assertions here exist because of ticket 44821: an upstream export emitted an
empty manifest, every task reported success, and the load stage wrote four hundred
rows where two million were expected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MIN_DECLARED_ROWS = 100_000
DEVIATION_THRESHOLD = 0.40


class ManifestError(ValueError):
    """Raised when a manifest cannot be trusted enough to load from."""


@dataclass(frozen=True)
class Manifest:
    """A declaration of what an export contains, before any of it is read."""

    export_id: str
    declared_rows: int
    byte_size: int
    checksum: str

    @property
    def is_empty(self) -> bool:
        return self.declared_rows == 0


def validate_absolute(manifest: Manifest) -> None:
    """Reject manifests that are implausible on their own terms.

    This is the cheap check. It catches the total-failure case — an empty or
    truncated export — without needing any history to compare against.
    """
    if manifest.is_empty:
        raise ManifestError(f"{manifest.export_id}: manifest declares zero rows")
    if manifest.declared_rows < MIN_DECLARED_ROWS:
        raise ManifestError(
            f"{manifest.export_id}: {manifest.declared_rows} rows below floor"
        )


def validate_relative(manifest: Manifest, trailing_median: float) -> None:
    """Reject manifests that deviate sharply from recent history.

    The absolute check above would not have caught a partial export that still
    cleared the floor. This one would, and it is the check the incident review
    actually asked for.
    """
    if trailing_median <= 0:
        log.warning("no history for %s; skipping relative check", manifest.export_id)
        return
    deviation = abs(manifest.declared_rows - trailing_median) / trailing_median
    if deviation > DEVIATION_THRESHOLD:
        raise ManifestError(
            f"{manifest.export_id}: {manifest.declared_rows} rows deviates "
            f"{deviation:.0%} from trailing median {trailing_median:.0f}"
        )
'''


# --------------------------------------------------------------------------------
# 8. Email thread — §10.3's quoted-reply duplication
# --------------------------------------------------------------------------------


def build_thread() -> str:
    """A four-message thread where each reply quotes the whole message before it.

    By the last message the first message's text appears four times in the file. That
    is the duplication §10.3 says to strip at parse time rather than deduplicate after
    the fact — collapsing it later loses the thread structure too.
    """
    m1_body = (
        "Team,\n\n"
        "The batch job stopped writing to the warehouse on Tuesday afternoon. The morning\n"
        "dashboard was empty and the on-call engineer was paged at 06:20.\n\n"
        "Airflow shows success on every task, which is why this took an hour to find.\n\n"
        "Priya"
    )
    m2_body = (
        "The partition exists but holds about four hundred rows against a usual two\n"
        "million. So the job ran and processed almost nothing.\n\n"
        "Tracing it to the upstream export now.\n\n"
        "Marcus"
    )
    m3_body = (
        "Confirmed: the export emitted an empty manifest. The export itself succeeded,\n"
        "so nothing alerted anywhere in the chain.\n\n"
        "Marcus"
    )
    m4_body = (
        "Adding a row-count assertion on the manifest before the load step, plus a\n"
        "trailing-seven-day-median deviation check at forty percent.\n\n"
        "Backfill for Tuesday and Wednesday is queued. Total data-loss window was about\n"
        "thirty-one hours.\n\n"
        "Priya"
    )

    def quote(text: str) -> str:
        return "\n".join(("> " + ln if ln else ">") for ln in text.split("\n"))

    messages = [
        ("Priya Raman <priya@northwind.example>", "Batch job wrote no data Tuesday", m1_body, None),
        ("Marcus Bell <marcus@northwind.example>", "Re: Batch job wrote no data Tuesday", m2_body, m1_body),
        ("Marcus Bell <marcus@northwind.example>", "Re: Batch job wrote no data Tuesday", m3_body, m2_body),
        ("Priya Raman <priya@northwind.example>", "Re: Batch job wrote no data Tuesday", m4_body, m3_body),
    ]

    out = []
    accumulated = ""
    for i, (sender, subject, body, _) in enumerate(messages, start=1):
        headers = [
            f"Message-ID: <thread44821.{i}@northwind.example>",
            f"From: {sender}",
            "To: platform@northwind.example",
            f"Subject: {subject}",
            "Date: Thu, 14 Mar 2024 09:0%d:00 +0000" % i,
            "MIME-Version: 1.0",
            'Content-Type: text/plain; charset="utf-8"',
        ]
        if i > 1:
            headers.append(f"In-Reply-To: <thread44821.{i - 1}@northwind.example>")
            headers.append(
                "References: "
                + " ".join(f"<thread44821.{j}@northwind.example>" for j in range(1, i))
            )
        full = body if i == 1 else body + "\n\nOn an earlier date, they wrote:\n" + accumulated
        accumulated = quote(full)
        out.append("\n".join(headers) + "\n\n" + full + "\n")
    # mbox-style concatenation; the parser splits on the From_ line
    return "".join("From thread44821\n" + m + "\n" for m in out)


# --------------------------------------------------------------------------------
# 9. The book — long, deeply structured, front matter and all
# --------------------------------------------------------------------------------

BOOK_SENTENCES = [
    "The failure was visible in the metrics for eleven hours before anyone read them.",
    "Every stage in a pipeline has a queue, and the dangerous one is the queue nobody configured.",
    "A default value is a decision made by someone who never saw your data.",
    "Cost that lands on a different line item is cost that nobody optimises.",
    "The cheapest observability you will ever add is an assertion on a count.",
    "Reprocessing is affordable exactly when the expensive stage has been cached.",
    "A system that cannot be re-run from the middle will be re-run from the beginning.",
    "Idempotency is not a property you add later; it is a property of the identifier scheme.",
    "The team measured the thing with a price tag and ignored the thing with a wall clock.",
    "Silence is the worst alert, because it is indistinguishable from health.",
    "Schema decisions announce themselves as configuration changes.",
    "The migration cost of a choice is the only honest measure of how much it matters.",
    "Determinism is what makes a cache correct rather than merely fast.",
    "Every heuristic that reconstructs structure will be wrong on some document you own.",
    "The document that extracts to an empty string generates no error anywhere.",
    "Two systems that normalise differently will disagree forever and never log why.",
    "A number without its conditions attached is a rumour with a decimal point.",
    "The baseline you did not configure is not a baseline, it is a straw man.",
    "Retrieval quality is bounded above by what the parser managed to recover.",
    "Storage is the cheapest resource in the system and the last one anyone spends.",
]

BOOK_STRUCTURE = [
    ("Part I — Acquisition", [
        ("Where documents come from", ["Source systems", "Crawl and push", "Snapshot semantics"]),
        ("What arrives is not what was sent", ["Truncation", "Re-encoding", "Silent substitution"]),
    ]),
    ("Part II — Extraction", [
        ("Print formats and document formats", ["Glyphs and coordinates", "Reading order", "Tables"]),
        ("Tiering the parser", ["Geometric extraction", "Layout models", "Page understanding"]),
        ("Gates and quarantine", ["Extraction yield", "Script sanity", "Routing"]),
    ]),
    ("Part III — Segmentation", [
        ("The unit of retrieval", ["Size constraints", "Overlap arithmetic", "Structure as boundary"]),
        ("Decoupling retrieval from generation", ["Parent documents", "Sentence windows", "Auto-merging"]),
    ]),
    ("Part IV — Maintenance", [
        ("Identity", ["Content addressing", "Position addressing", "Version stamps"]),
        ("Change", ["Diff-based update", "Deletion", "Tombstones and compaction"]),
        ("Duplication", ["Exact duplicates", "Near duplicates", "Duplication worth keeping"]),
    ]),
]


def build_book() -> str:
    """Compose a long, deeply nested document with realistic front and back matter.

    Structure is the realistic part: four parts, ten chapters, thirty sections, plus a
    title page, a copyright block, a table of contents, an epigraph, a table, and
    footnotes. Body prose is drawn from a fixed sentence pool by a seeded RNG, so the
    file is deterministic and thin — see this module's docstring on why that is a fair
    trade here and where it would stop being one.
    """
    rng = random.Random(44821)
    out: list[str] = []

    out.append("# The Ingestion Handbook\n")
    out.append("*A field guide to getting documents into a retrieval system intact*\n")
    out.append("\nNorthwind Data Systems Press — First edition\n")
    out.append(
        "\nCopyright 2024 Northwind Data Systems. This fixture is synthetic and exists to "
        "exercise a chunker against a long, deeply nested document.\n"
    )
    out.append("\n> Everything above the glyph level is a guess.\n>\n> — attributed, apocryphally\n")

    out.append("\n## Contents\n")
    for part, chapters in BOOK_STRUCTURE:
        out.append(f"\n- {part}")
        for chapter, _ in chapters:
            out.append(f"\n  - {chapter}")
    out.append("\n")

    footnote_n = 0
    for part, chapters in BOOK_STRUCTURE:
        out.append(f"\n# {part}\n")
        out.append(f"\n{rng.choice(BOOK_SENTENCES)} {rng.choice(BOOK_SENTENCES)}\n")

        for chapter, sections in chapters:
            out.append(f"\n## {chapter}\n")
            for section in sections:
                out.append(f"\n### {section}\n")
                for _ in range(rng.randint(2, 3)):
                    sentences = rng.sample(BOOK_SENTENCES, rng.randint(3, 5))
                    para = " ".join(sentences)
                    if rng.random() < 0.18:
                        footnote_n += 1
                        para += f"[^{footnote_n}]"
                    out.append("\n" + textwrap.fill(para, 88) + "\n")

                if rng.random() < 0.22:
                    out.append(
                        "\n| Stage | Reversible | Primary cost |\n|---|---|---|\n"
                        "| Acquire | yes | network |\n| Parse | if bytes kept | CPU or API |\n"
                        "| Split | if parse kept | free |\n| Embed | at token cost | tokens |\n"
                    )

    if footnote_n:
        out.append("\n## Notes\n")
        for i in range(1, footnote_n + 1):
            out.append(f"\n[^{i}]: {rng.choice(BOOK_SENTENCES)}\n")

    return "".join(out)


# --------------------------------------------------------------------------------
# 10. The PDFs
# --------------------------------------------------------------------------------

CLEAN_PAGES = [
    [
        "Northwind Data Systems",
        "Ingestion Reliability Review",
        "",
        "This review covers the twelve months to March 2024. It summarises the incidents",
        "recorded against the ingestion service, the changes made in response, and the",
        "residual risks the team has accepted rather than mitigated.",
        "",
        "Three incidents in the period were classed as data loss. In each case the loss",
        "was silent: the pipeline reported success and the absence of data was noticed by",
        "a human reading a dashboard rather than by any automated check.",
        "",
        "The common cause was the same in all three. Each stage validated that it had",
        "completed, and no stage validated that it had produced a plausible quantity of",
        "output. A job that processes zero records successfully processes zero records.",
    ],
    [
        "Changes made",
        "",
        "Row-count assertions were added at the manifest boundary and at the load",
        "boundary. Both compare against a trailing median rather than a fixed floor,",
        "because a fixed floor is wrong for every table except the one it was set for.",
        "",
        "Extraction-yield gates were added to the parsing stage. A document producing",
        "fewer than one hundred characters per page is now routed to the OCR queue",
        "instead of being indexed as an empty document.",
        "",
        "Residual risks",
        "",
        "The team has accepted that documents in the quarantine bucket are drained",
        "manually and that no service level objective covers quarantine age.",
    ],
]


def two_column_pages() -> list[P.PageSpec]:
    """Three two-column pages with running head and foot, hyphenation and ligatures.

    The left and right columns discuss unrelated topics on purpose. Naive extraction
    joins them row-band by row-band, so a chunk cut anywhere in the resulting text
    alternates between two subjects mid-clause — which is the point: no chunk boundary
    and no embedding model can repair that, because the damage happened upstream.
    """
    left_col = [
        ["Reading order is not stored in the", "file. A content stream places glyph",
         "runs at coordinates, and any notion", "of a first and second column is",
         "reconstructed from the geometry by", "whichever extractor you happen to",
         "be running. Two extractors will", "disagree, and neither will warn you."],
        ["The organi-", "zational cost of this is that a bug", "report reading the text is wrong",
         "cannot be triaged without knowing", "which extractor produced it, which", "means the parser version has to be",
         "recorded on every chunk it emits."],
        ["A ﬁxed threshold for column", "detection fails on the ﬁrst document", "with three columns, and a document",
         "with a full-width table spanning", "both columns defeats the gutter", "heuristic entirely."],
    ]
    right_col = [
        ["Margin requirements for the retail", "portfolio were revised in February",
         "following the regulator's guidance", "on concentration limits. The revised",
         "figure is fourteen percent against", "the previous eleven, applied to all",
         "positions held longer than thirty", "days."],
        ["Counterparty exposure is measured", "gross rather than net, which the", "committee reviewed and elected to",
         "retain on the grounds that netting", "agreements are not enforceable in", "two of the jurisdictions where the",
         "book has material exposure."],
        ["The stress scenario assumes a two", "hundred basis point parallel shift", "combined with a fifteen percent",
         "decline in collateral values, held", "for a rolling ninety day window."],
    ]

    pages: list[P.PageSpec] = []
    for i in range(3):
        runs = [
            # The running head and foot: ordinary glyph runs, indistinguishable from body
            # text to anything that only looks at the text stream.
            P.TextRun(72, 742, "Northwind Data Systems — Conﬁdential", 9.0),
            P.TextRun(72, 56, f"Page {i + 1} of 3 — Internal distribution only", 9.0),
        ]
        for j, line in enumerate(left_col[i]):
            runs.append(P.TextRun(72, 700 - 15 * j, line))
        for j, line in enumerate(right_col[i]):
            runs.append(P.TextRun(330, 700 - 15 * j, line))
        pages.append(P.PageSpec(runs=runs))
    return pages


def two_column_interleaved_pages() -> list[P.PageSpec]:
    """The same two-column pages, with runs emitted **row-band by row-band**.

    This fixture exists because the bake-off proved the other one was too easy. In
    `two_column_pages()` the entire left column is written to the content stream before
    the right column, and pypdf, PyMuPDF and pdfminer all preserve emission order — so
    all three reconstructed the columns correctly and the "known answer" test passed
    for a reason that had nothing to do with layout analysis.

    Real producers do not cooperate like that. A two-column layout emitted by LaTeX or
    by a word processor typically writes each visual row across the gutter before
    moving down, because that is the order the text engine lays out lines. This fixture
    emits in that order, so *emission order is wrong* and the parser must use geometry
    to recover the columns — which is the actual §3.2 failure.

    The pair is the point: identical rendered pages, identical text content, and two
    content streams that differ only in the order of their `Tj` operators. Any parser
    whose output differs between them is ordering by emission rather than by position,
    and that is a fact worth knowing about your parser before it meets your corpus.
    """
    source = two_column_pages()
    pages: list[P.PageSpec] = []
    for page in source:
        head = [r for r in page.runs if r.size < 10.0]  # running head and foot
        body = [r for r in page.runs if r.size >= 10.0]
        left = sorted([r for r in body if r.x < 200], key=lambda r: -r.y)
        right = sorted([r for r in body if r.x >= 200], key=lambda r: -r.y)

        interleaved: list[P.TextRun] = []
        for i in range(max(len(left), len(right))):
            if i < len(left):
                interleaved.append(left[i])
            if i < len(right):
                interleaved.append(right[i])
        pages.append(P.PageSpec(runs=head + interleaved))
    return pages


def statement_pages() -> list[P.PageSpec]:
    """A financial table drawn the way PDFs draw tables: independent positioned runs.

    There is no table object. Column headers are runs at one y; each cell is a run at
    the same x as its header and the y of its row. Scan-order extraction flattens this
    into a number soup; recovering the grid means clustering x-positions, which is
    what `parse.recover_grid()` does.
    """
    cols = [72.0, 210.0, 300.0, 390.0, 480.0]
    header = ["Period", "Revenue", "Cost", "Margin", "Headcount"]
    rows = [
        ["Q1 2023", "1,204", "1,190", "14", "58"],
        ["Q2 2023", "1,318", "1,275", "43", "61"],
        ["Q3 2023", "1,402", "1,301", "101", "62"],
        ["Q4 2023", "1,511", "1,388", "123", "64"],
        ["Q1 2024", "1,644", "1,402", "242", "66"],
        ["Q2 2024", "1,702", "1,455", "247", "68"],
    ]
    runs = [P.TextRun(72, 742, "Northwind Data Systems — Consolidated Results", 9.0)]
    runs.append(P.TextRun(72, 700, "Table 3. Quarterly results, thousands of USD.", 10.5))
    for x, text in zip(cols, header):
        runs.append(P.TextRun(x, 660, text))
    for r, row in enumerate(rows):
        for x, text in zip(cols, row):
            runs.append(P.TextRun(x, 636 - 18 * r, text))
    runs.append(
        P.TextRun(72, 500, "Margin improved in every quarter after the Q2 2023 renegotiation.")
    )
    return [P.PageSpec(runs=runs)]


def scan_pages() -> list[P.PageSpec]:
    """Two pages that are images of a document. No text operator is emitted at all.

    The pixel content is a deterministic gradient — its appearance is irrelevant,
    because nothing downstream can read it. What matters is that extraction returns
    the empty string and that no exception is raised anywhere in the process.
    """
    px_w, px_h = 48, 62
    pixels = bytes(((x * 5 + y * 3) % 256) for y in range(px_h) for x in range(px_w))
    return [
        P.PageSpec(images=[P.ImageBlock(72, 90, 468, 620, pixels, px_w, px_h)])
        for _ in range(2)
    ]


BROKEN_LINES = [
    "Clinical Study Report — Protocol NWD-2024-11",
    "",
    "Primary endpoint was met in the treatment arm with a hazard ratio of 0.68",
    "and a confidence interval of 0.51 to 0.89. The study enrolled 412 patients",
    "across nine sites between March 2023 and January 2024.",
    "",
    "Secondary endpoints were directionally consistent but did not reach the",
    "prespecified significance threshold. No new safety signals were observed.",
]


def write_all() -> list[tuple[str, int]]:
    CORPUS.mkdir(exist_ok=True)
    written: list[tuple[str, int]] = []

    def emit(name: str, data: bytes) -> None:
        path = CORPUS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append((name, len(data)))

    emit("transcript.txt", TRANSCRIPT.encode("utf-8"))
    emit("handbook.md", HANDBOOK.encode("utf-8"))
    emit("metrics.md", METRICS.encode("utf-8"))
    emit("notation.md", NOTATION.encode("utf-8"))
    emit("revenue.csv", REVENUE_CSV.encode("utf-8"))
    emit("service.py.txt", SERVICE_PY.encode("utf-8"))  # .txt so it is not importable
    emit("thread.eml", build_thread().encode("utf-8"))
    emit("book.md", build_book().encode("utf-8"))

    for name, title, paragraphs in HTML_ARTICLES:
        emit(f"site/{name}", html_page(title, paragraphs).encode("utf-8"))

    # --- PDFs -------------------------------------------------------------------
    ligature_diff = {1: "fi", 2: "fl", 3: "ffi"}

    clean = [
        P.PageSpec(runs=[P.TextRun(72, 700 - 15 * j, ln) for j, ln in enumerate(page) if ln])
        for page in CLEAN_PAGES
    ]
    emit("report_clean.pdf", P.write_pdf(clean, differences=ligature_diff))
    emit("report_twocol.pdf", P.write_pdf(two_column_pages(), differences=ligature_diff))
    emit(
        "report_twocol_interleaved.pdf",
        P.write_pdf(two_column_interleaved_pages(), differences=ligature_diff),
    )
    emit("statement.pdf", P.write_pdf(statement_pages(), differences=ligature_diff))
    emit("scan.pdf", P.write_pdf(scan_pages()))

    subset_enc = P.build_subset_encoding(BROKEN_LINES)
    broken_runs = [P.TextRun(72, 700 - 16 * j, ln) for j, ln in enumerate(BROKEN_LINES) if ln]
    emit(
        "subset_broken.pdf",
        P.write_pdf(
            [P.PageSpec(runs=broken_runs)], differences=subset_enc, subset=True, resolvable=False
        ),
    )
    # The same page with resolvable glyph names — the control, so the lab can show that
    # the bytes and the layout are identical and only the encoding differs.
    emit(
        "subset_ok.pdf",
        P.write_pdf(
            [P.PageSpec(runs=broken_runs)], differences=subset_enc, subset=True, resolvable=True
        ),
    )

    return written


if __name__ == "__main__":
    for name, size in write_all():
        print(f"{size:9,d}  {name}")
