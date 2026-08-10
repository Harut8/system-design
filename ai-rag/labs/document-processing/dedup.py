"""Deduplication, at ingest and at merge time (`02` §10, §3.5).

Three techniques, three different jobs, and the chapter is explicit that the *last*
one is worth the most:

1. **Exact duplicates** (§10.1) — `content_hash` over normalized text. Free, exact,
   and it catches the enormous literal duplication in real corpora: the same PDF
   attached to forty tickets, a wiki page copied into a new space.
2. **Near duplicates** (§10.2) — MinHash over word shingles. Catches the revised
   document, the boilerplate section with one changed date, the syndicated page.
3. **Retrieval-time suppression** (§10.4) — *the important half*. Ingest-time dedup
   shrinks the index; suppression at merge time protects the context budget. If your
   top-10 holds four variants of one paragraph, the generator has six pieces of
   evidence and you paid for ten.

The asymmetry in (3) is worth restating because it decides where the work goes:
ingest-time dedup **has to decide without knowing what will be asked**. Merge-time
suppression has the query in hand and can weigh diversity against relevance, which is
why `04-retrieval-hybrid-and-reranking.md` owns MMR and why this module only measures
the size of the prize (`distinct_after_collapse`, §15's lab 8).

§10.3's warning is implemented as a policy argument rather than a footnote: not all
repetition is noise. A licence header in 400 files is duplicated, and "what licence is
this file under?" is a real question about each of them.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

_MERSENNE = (1 << 61) - 1


def content_hash(text: str) -> str:
    """Exact-duplicate key. Normalize *before* hashing, always (§4.1, §10.1).

    Hashing pre-normalization text means the same document from two sources — one with
    CRLF, one with a BOM, one with decomposed accents — produces three different
    hashes and three copies in the index, which is precisely the duplication the hash
    was added to prevent.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def shingles(text: str, k: int = 5) -> set[str]:
    """Overlapping k-word sequences.

    Word-level shingles are more robust to whitespace and punctuation noise than
    character-level ones on prose. `k` is the sensitivity knob: small k finds
    similarity between documents that merely share vocabulary, large k only finds
    near-verbatim reuse. 5 is a reasonable prose default and a poor code default.
    """
    tokens = text.split()
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def minhash(sh: set[str], num_perm: int = 128, seed: int = 0) -> list[int]:
    """MinHash signature. Expected collision rate equals the true Jaccard similarity."""
    rng = random.Random(seed)
    params = [
        (rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE)) for _ in range(num_perm)
    ]
    sig = [_MERSENNE] * num_perm
    for s in sh:
        hv = int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
        for i, (a, b) in enumerate(params):
            candidate = (a * hv + b) % _MERSENNE
            if candidate < sig[i]:
                sig[i] = candidate
    return sig


def estimated_jaccard(sig_a: list[int], sig_b: list[int]) -> float:
    """Standard error is roughly `1/sqrt(num_perm)`.

    That number sets your parameter, and it is the part people skip: 128 permutations
    gives roughly ±0.09, which is fine for a 0.9 threshold and useless for
    distinguishing 0.85 from 0.90. If you need a tighter threshold, pay for more
    permutations.
    """
    return sum(x == y for x, y in zip(sig_a, sig_b)) / len(sig_a)


def signature_error(num_perm: int) -> float:
    """The `1/sqrt(num_perm)` bound, so a threshold can be chosen rather than guessed."""
    return num_perm**-0.5


# --------------------------------------------------------------------------------
# LSH banding — because the pairwise loop does not scale
# --------------------------------------------------------------------------------


def lsh_buckets(
    signatures: dict[str, list[int]], bands: int = 16
) -> dict[tuple[int, tuple[int, ...]], list[str]]:
    """Band a signature set so only plausible pairs are compared.

    The pairwise loop in §10.2's sketch is O(n²) and is fine for this lab's few hundred
    chunks and hopeless at corpus scale. Banding splits each signature into `bands`
    stripes and buckets on each stripe; two items share a bucket if any stripe matches
    exactly, which happens with probability `1 - (1 - s^r)^b` for similarity `s` and
    `r = num_perm / bands` rows per band. Choosing `bands` *is* choosing the threshold,
    and it is the parameter that decides both recall and cost.
    """
    if not signatures:
        return {}
    num_perm = len(next(iter(signatures.values())))
    if num_perm % bands:
        raise ValueError(f"bands ({bands}) must divide num_perm ({num_perm})")
    rows = num_perm // bands

    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = {}
    for key, sig in signatures.items():
        for b in range(bands):
            stripe = tuple(sig[b * rows : (b + 1) * rows])
            buckets.setdefault((b, stripe), []).append(key)
    return buckets


@dataclass(frozen=True)
class DuplicatePair:
    a: str
    b: str
    similarity: float


def near_duplicate_pairs(
    texts: dict[str, str],
    *,
    threshold: float = 0.80,
    num_perm: int = 128,
    bands: int = 16,
    k: int = 5,
) -> list[DuplicatePair]:
    """Find near-duplicate pairs via MinHash + LSH banding."""
    signatures = {key: minhash(shingles(text, k), num_perm) for key, text in texts.items()}
    candidates: set[tuple[str, str]] = set()
    for members in lsh_buckets(signatures, bands).values():
        if len(members) < 2:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                candidates.add((a, b) if a < b else (b, a))

    out = []
    for a, b in sorted(candidates):
        similarity = estimated_jaccard(signatures[a], signatures[b])
        if similarity >= threshold:
            out.append(DuplicatePair(a, b, similarity))
    return sorted(out, key=lambda p: -p.similarity)


def distinct_after_collapse(
    ranked: list[tuple[str, str]], *, threshold: float = 0.80, num_perm: int = 128
) -> int:
    """How many *distinct* passages a ranked result set actually contains (§10.4).

    This is the number §15's lab 8 asks for, and the one that makes the MMR decision in
    `04` evidence-based instead of a default: the gap between this and `len(ranked)` is
    context budget you are currently paying for and not using.
    """
    kept: list[list[int]] = []
    for _, text in ranked:
        sig = minhash(shingles(text), num_perm)
        if any(estimated_jaccard(sig, other) >= threshold for other in kept):
            continue
        kept.append(sig)
    return len(kept)


# --------------------------------------------------------------------------------
# Corpus-level repeated blocks (§3.5)
# --------------------------------------------------------------------------------


def repeated_block_filter(
    pages: dict[str, list[str]], threshold: float = 0.30, max_len: int = 500
) -> set[str]:
    """Blocks appearing on more than `threshold` of a source's pages are chrome.

    Run **per source**: a footer that is boilerplate on one site is body text on
    another. This catches site-specific boilerplate that no generic extractor knows
    about and it needs no per-site rules, which is why §3.5 says to use it *alongside*
    readability-style extraction rather than instead of it.

    The `max_len` guard matters: a genuinely repeated *long* passage is more likely to
    be a syndicated article or a duplicated document than chrome, and that is §10's
    problem, handled differently.
    """
    from collections import Counter

    n = len(pages)
    if n == 0:
        return set()
    counts = Counter(block for blocks in pages.values() for block in set(blocks))
    return {b for b, c in counts.items() if c / n > threshold and len(b) < max_len}


@dataclass
class DuplicationReport:
    total: int
    exact_groups: int
    exact_redundant: int
    near_pairs: int
    near_redundant: int

    @property
    def redundancy(self) -> float:
        """Fraction of units that are a copy of something else already present."""
        return (self.exact_redundant + self.near_redundant) / max(self.total, 1)


def duplication_report(texts: dict[str, str], *, threshold: float = 0.80) -> DuplicationReport:
    """Corpus-level duplication census — the input to the §10.4 build/don't-build call."""
    from collections import defaultdict

    groups: dict[str, list[str]] = defaultdict(list)
    for key, text in texts.items():
        groups[content_hash(text)].append(key)
    exact_groups = sum(1 for g in groups.values() if len(g) > 1)
    exact_redundant = sum(len(g) - 1 for g in groups.values() if len(g) > 1)

    # Compare one representative per exact group, or exact duplicates dominate the
    # near-duplicate pair count and say nothing new.
    representatives = {g[0]: texts[g[0]] for g in groups.values()}
    pairs = near_duplicate_pairs(representatives, threshold=threshold)

    merged: dict[str, str] = {}

    def find(x: str) -> str:
        while merged.get(x, x) != x:
            x = merged[x]
        return x

    for pair in pairs:
        ra, rb = find(pair.a), find(pair.b)
        if ra != rb:
            merged[rb] = ra
    near_redundant = len({k for k in merged})

    return DuplicationReport(
        len(texts), exact_groups, exact_redundant, len(pairs), near_redundant
    )
