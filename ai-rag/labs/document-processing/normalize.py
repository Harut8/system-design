"""Stage 3 — normalization, and the three artifacts it must produce (`02` §4).

The architecture that survives contact with hybrid search is not "clean the text." It
is **one canonical form, with per-branch forms derived from it**:

    canonical_text     → stored, cited, shown to users, hashed for identity (§9)
          ├── embed_text    = canonical_text (+ prepended context, §6.3 / 01 §9.2)
          └── lexical_text  = analyze(canonical_text)   # lowercase, fold — BM25's job

The failure this prevents is subtle, silent, and presents as "hybrid search doesn't
help much on our corpus": the lexical analyzer's output becomes the text that gets
embedded, or the embedding-side text becomes what is stored for citation. Neither
raises an error. Both cost recall in a way no test that exercises one code path can
see.

The second rule, from §4.1, is the one that actually bites in production: **any
normalization applied at ingest must be applied identically to queries at search
time.** That is why `canonicalize()` lives in this shared module with a version
stamp, rather than inside the ingestion service where the query service cannot call
it. A normalizer that runs on one side only is a bug that manifests as unexplained
lexical recall loss.

Sections §4.2's destructive transforms are implemented as *reports* rather than as
transforms, because the point is to see what they would destroy on this corpus before
deciding. `corpus/notation.md` exists to make those reports non-empty.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Bump when any rule in `canonicalize()` changes. Every chunk offset and every
# content-addressed chunk ID is defined against this function's output, so a silent
# change here invalidates the entire index without changing a single document.
NORMALIZER_VERSION = "canon-v1"

# Invisible characters. Every one of these breaks exact lexical match while being
# undetectable in a rendered view, and they are epidemic in PDF and DOCX text.
INVISIBLE = {
    "­": "soft hyphen",
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "﻿": "BOM / zero-width no-break space",
}

# The compatibility mappings we *do* want, applied by name rather than by asking NFKC
# for its whole table (§4.2). A ligature is a typographic artifact with no semantic
# content; a superscript two is not.
LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}

QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'"}


def canonicalize(raw: str) -> str:
    """The canonical form. Every rule here is a decision the whole index depends on.

    Ordering is load-bearing and is not alphabetical:

    1. **BOM and CRLF first**, because both shift every subsequent character offset.
    2. **Invisible characters next**, before de-hyphenation, or a soft hyphen sitting
       between `organi` and `-` defeats the line-break pattern.
    3. **De-hyphenate across line breaks only.** The `\\n` in the pattern is the entire
       safety mechanism: it fires on `"organi-\\nzational"` and never on
       `"state-of-the-art"`. It is still wrong for a genuine compound that happens to
       break across a line — an ambiguity no rule resolves, because the information
       needed to resolve it was destroyed by the line breaking itself.
    4. **Ligatures by explicit table**, never NFKC (§4.2).
    5. **NFC last**, so composition sees the final character sequence.

    Note what is *not* here: no lowercasing, no stopword removal, no NFKC, no
    stemming. Those belong to the lexical branch (`analyze()`), and applying them here
    would push the embedded text away from the distribution the model was trained on
    for no benefit (§4.2).
    """
    text = raw.replace("﻿", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    for ch in INVISIBLE:
        text = text.replace(ch, "")

    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    for ligature, expansion in LIGATURES.items():
        text = text.replace(ligature, expansion)
    for curly, straight in QUOTES.items():
        text = text.replace(curly, straight)

    # Collapse horizontal whitespace runs but preserve blank lines: paragraph breaks
    # are the best chunk boundaries an unstructured document has (§6.2), and a
    # normalizer that flattens them has thrown away the splitter's only signal.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    return unicodedata.normalize("NFC", text).strip() + "\n"


# --------------------------------------------------------------------------------
# The lexical branch (§4.3)
# --------------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")

# A deliberately tiny stopword list. BM25's scoring model already discounts frequent
# terms via IDF, so aggressive stopword removal is mostly a lexical-era habit; it is
# here to be *shown*, alongside what it destroys, not because it is recommended.
STOPWORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "is", "was"}


def analyze(text: str, *, fold_case: bool = True, drop_stopwords: bool = False) -> list[str]:
    """Produce the lexical branch's token stream from the canonical text.

    BM25 genuinely benefits from case folding — `Revenue` and `revenue` should match.
    That same folding destroys the entity signal in `US` vs `us` and `Polish` vs
    `polish`, which is why this runs on the *lexical* branch only and never on the
    text that gets embedded.

    `drop_stopwords` defaults to False on purpose: "revenue *before* tax" and "revenue
    *after* tax" are different facts, and both become "revenue tax" once the function
    words are gone.
    """
    tokens = _WORD.findall(text)
    if fold_case:
        tokens = [t.lower() for t in tokens]
    if drop_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS]
    return tokens


# --------------------------------------------------------------------------------
# Damage reports — §4.2's destructive transforms, measured instead of assumed
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class NFKCDamage:
    char: str
    codepoint: str
    name: str
    becomes: str
    count: int
    verdict: str  # "wanted" | "destroys meaning"


# Categories where a compatibility mapping changes what the text *means* rather than
# how it looks. Superscripts and fractions are quantities; Roman numerals are
# identifiers in legal citation; full-width forms distinguish two source systems.
_MEANINGFUL = {
    "No": "numeric form — a quantity, not a digit",
    "Nl": "letter-number — Roman numerals are citation identifiers",
}


def nfkc_damage(text: str) -> list[NFKCDamage]:
    """Every character NFKC would rewrite, with a verdict on whether you want it.

    §4.2's argument, made checkable on your own corpus. NFKC maps `ﬁ`→`fi`, which you
    want. It also maps `²`→`2`, `½`→`1⁄2`, `Ⅻ`→`XII`, `µ`→`μ`, and full-width forms to
    ASCII. In a chemistry, mathematics, legal or CJK corpus those are semantic changes
    — `x²` and `x2` are different expressions.

    The right move is NFC plus an explicit audited list of the compatibility
    replacements you actually want. This function produces the audit.
    """
    from collections import Counter

    counts = Counter(
        ch for ch in text if unicodedata.normalize("NFKC", ch) != ch
    )

    out: list[NFKCDamage] = []
    for ch, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        category = unicodedata.category(ch)
        becomes = unicodedata.normalize("NFKC", ch)
        if ch in LIGATURES:
            verdict = "wanted"
        elif category in _MEANINGFUL:
            verdict = f"destroys meaning — {_MEANINGFUL[category]}"
        elif category == "Lo" or "FULLWIDTH" in unicodedata.name(ch, ""):
            verdict = "destroys meaning — full-width and half-width are distinct forms"
        elif category in ("Sk", "Sm", "So"):
            verdict = "destroys meaning — symbol folded into a letter or digit"
        else:
            verdict = "review"
        out.append(
            NFKCDamage(
                ch,
                f"U+{ord(ch):04X}",
                unicodedata.name(ch, "<unnamed>"),
                becomes,
                count,
                verdict,
            )
        )
    return out


@dataclass(frozen=True)
class CaseCollision:
    folded: str
    forms: tuple[str, ...]
    counts: tuple[int, ...]
    significant: bool
    reason: str


# Characters after which a capital letter is just orthography, not signal.
_SENTENCE_END = set(".!?:;")
# Markdown headings, list bullets, table pipes and section numbers all begin a line
# without ending a sentence, so a capital after them is orthography too. Stripping
# this set is what stops every heading word being reported as a proper noun.
_LINE_MARKERS = " \t#->*|`0123456789.)"


def case_collisions(text: str) -> list[CaseCollision]:
    """Tokens that appear in more than one case form and merge under lowercasing.

    `US` and `us`, `Polish` and `polish`, `IT` and `it`, `WHO` and `who`, `March` and
    `march`, `Apple` and `apple`, `SAP` and `sap` — each pair is an entity and an
    ordinary word that case alone distinguishes.

    This is what the lexical branch is *supposed* to do, and exactly what the dense
    branch must not. Reporting the collisions on your own corpus turns §4.2's
    assertion into a count: if it is empty, folding case everywhere costs you nothing,
    and if it is long, §4.3's two-branch split is earning its keep.

    **The `significant` flag is the part that makes the report usable.** A first pass
    that only counted case variants returned `The`/`the`, `This`/`this`, `Every`/
    `every` — every sentence-initial capital in the corpus — and buried the eight
    collisions that actually matter. So a collision counts as significant only when
    the capitalized form carries information that position cannot explain:

    - an internal capital (`US`, `IT`, `WHO`, `SAP`, `AI`, `ABC`) — acronyms, always
      significant; or
    - a capitalized form appearing **mid-sentence**, where orthography does not
      require it (`Polish`, `March`, `Apple`).

    Without that distinction the report is 21 rows of mostly noise on a 2.8 KB
    document, and nobody reads it twice.
    """
    from collections import Counter, defaultdict

    forms: dict[str, Counter] = defaultdict(Counter)
    midsentence: dict[str, set[str]] = defaultdict(set)

    for match in _WORD.finditer(text):
        token = match.group()
        # A single letter is never entity signal: `A` in "A/√Hz" and `M` in "5 µM"
        # collide with the article and the unit and mean nothing either way.
        if len(token) < 2:
            continue
        forms[token.lower()][token] += 1

        prefix = text[: match.start()]
        line_prefix = prefix[prefix.rfind("\n") + 1 :]
        if line_prefix.lstrip().startswith("#"):
            # Headings are Title Case, a third orthographic convention on top of
            # sentence case and acronyms. Every capitalized word in a heading would
            # otherwise be reported as a proper noun.
            at_start = True
        elif line_prefix.strip(_LINE_MARKERS) == "":
            at_start = True  # start of a line, bullet or numbered item
        else:
            stripped = prefix.rstrip(" \t")
            at_start = not stripped or stripped[-1] in _SENTENCE_END
        if not at_start:
            midsentence[token.lower()].add(token)

    out: list[CaseCollision] = []
    for folded, variants in sorted(forms.items()):
        if len(variants) < 2:
            continue
        ordered = variants.most_common()
        names = tuple(v for v, _ in ordered)

        acronyms = [v for v in names if any(c.isupper() for c in v[1:])]
        mid_caps = [v for v in midsentence[folded] if v[:1].isupper()]
        has_lower = any(v.islower() for v in names)

        if acronyms and has_lower:
            significant, reason = True, f"internal capital in {acronyms[0]} — acronym"
        elif mid_caps and has_lower:
            significant, reason = True, f"{mid_caps[0]} appears mid-sentence — proper noun"
        else:
            significant, reason = False, "sentence-initial capitalization only"

        out.append(
            CaseCollision(folded, names, tuple(c for _, c in ordered), significant, reason)
        )
    return out


def invisible_census(raw: str) -> dict[str, int]:
    """Count the invisible characters `canonicalize()` removes. Usually surprising."""
    return {name: raw.count(ch) for ch, name in INVISIBLE.items() if ch in raw}


# --------------------------------------------------------------------------------
# Canonical document assembly — where offsets come from (§4.4)
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A half-open character range into a document's canonical text."""

    start: int
    end: int

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end

    def __len__(self) -> int:
        return self.end - self.start


@dataclass
class CanonicalDoc:
    """Canonical text plus the element spans defined against it.

    §4.4's rule: every transformation must preserve a mapping back to character
    offsets, or at minimum the pipeline must retain the canonical text and record each
    chunk's `(char_start, char_end)` into *it*. That is what makes citation with
    highlighting possible, what makes span-level golden sets possible (and therefore
    what makes the sibling `golden-set` lab's labels valid across re-chunking), and
    what makes "show me exactly what the model was quoting" answerable during an
    incident.

    Retrofitting offsets means re-running ingestion. Recording them costs two integers.
    """

    doc_id: str
    text: str
    element_spans: list[Span]
    normalizer: str = NORMALIZER_VERSION

    def slice(self, span: Span) -> str:
        return self.text[span.start : span.end]


def build_canonical(doc_id: str, element_texts: list[str], separator: str = "\n\n") -> CanonicalDoc:
    """Normalize each element and concatenate, recording where each one landed.

    Normalizing per element and then joining — rather than joining and then
    normalizing — is what keeps the element spans exact. Normalize the joined string
    and every rule that deletes a character shifts every offset after it, which is the
    bug that makes citation highlighting drift by a few characters and nobody ever
    finds out why.
    """
    parts: list[str] = []
    spans: list[Span] = []
    cursor = 0
    for raw in element_texts:
        piece = canonicalize(raw).rstrip("\n")
        if not piece:
            continue
        if parts:
            cursor += len(separator)
        spans.append(Span(cursor, cursor + len(piece)))
        cursor += len(piece)
        parts.append(piece)
    return CanonicalDoc(doc_id, separator.join(parts), spans)
