# Lab: the golden set (`00-mental-models.md` §16, exercise 1)

A 60-query golden set over a real corpus — the four chapters in `ai-rag/` — plus the
builder that produces it and the test suite that keeps it honest. This is the first
half of **P0** in the folder README's project ladder: nothing else in this folder can
be measured until these labels exist.

**Status: rung 2 — implemented.** The code runs and the tests pass. It produces
*labels*, not quality numbers; no embedding model, vector index, or generator is
involved yet. The first rung-1 number comes from exercise 2, which consumes this
artifact.

```
python3 build.py             # regenerate the golden set from questions.jsonl
python3 build.py --check     # verify the committed artifact is current (exit 1 if not)
python3 test_golden_set.py   # 20 assertions, zero dependencies
pytest -q                    # same assertions, if you have pytest
```

---

## 1. What this is

| File | Role | Hand-edited? |
|---|---|---|
| `questions.jsonl` | **Source of truth.** 60 questions, each anchored to exact quotes in the corpus. | Yes — this is the only file you author |
| `corpus.py` | Canonical text: NFC, LF, BOM, trailing newline. Every offset is defined against its output. | No |
| `chunker.py` | Deterministic, structure-aware Markdown chunker with content-addressed IDs. | No |
| `goldenset.py` | Locator → span resolution, block expansion, and the four hit rules from `02` §11.2. | No |
| `build.py` | Resolves every question against the corpus and emits the artifact. | No |
| `golden-set.v1.jsonl` | **Derived artifact.** `{query, answer_bearing_chunk_ids, expected_answer}` + provenance. | Never |
| `golden-set.v1.manifest.json` | Corpus digest, commit, chunker version, hit rule, counts. | Never |
| `test_golden_set.py` | The regression gate. Fails when labels rot, the artifact goes stale, or a rule changes. | No |

Current build, from the manifest — every number here is a count, not a measurement:

- 4 documents, 302,826 canonical characters (corpus commit is pinned in the manifest)
- 265 chunks at `max_chars=1800`, chunker `md-struct-v1`
- 60 records (55 single-hop, 5 multi-hop), 65 answer spans, mean span 508 chars
- label distribution: `00` 26, `02` 20, `01` 13, `README` 6
- 54 records resolve to exactly one chunk, 5 to two, 1 to three
- span union coverage is 1.0 for every span under the `any_overlap` build rule

---

## 2. Why it is built this way

Four decisions carry the whole design. Each one is a place where the obvious
implementation is wrong in a way that stays invisible until a number is already being
quoted in a review.

### 2.1 Ground truth is a span of text, not a chunk ID

The obvious format is `{query, [relevant_chunk_ids]}`. It is unusable, for the reason
`02` §11.2 states: **chunk IDs only exist relative to a chunking.** Change the chunk
size, the splitter, or the overlap and every label you own names something that no
longer exists. Re-deriving them with "whichever new chunk overlaps the old chunk"
smuggles the old chunker's boundaries into the new chunker's score — you are no longer
measuring the new chunking, you are measuring its agreement with the old one.

So the label is `(doc_id, char_start, char_end)` into the canonical text. Spans survive
re-chunking because the corpus is the thing that did not change. Chunk IDs are still
what exercise 2 needs for recall@k, so they are **derived** at build time from
(span × chunking × hit rule) and written into the artifact — regenerable, never
maintained by hand.

### 2.2 Humans author quotes; the machine computes offsets

Nobody can write or review `char_start: 48122`, and every offset shifts the moment a
paragraph above it is edited. So `questions.jsonl` holds a short exact `quote`, and the
builder resolves it. Two outcomes are build errors on purpose:

- **not found** → the quote was mistyped, or the document changed under the label.
  Fuzzy-matching here would silently relabel the record against text nobody chose.
- **ambiguous** (two or more occurrences) → which occurrence the scorer picks would
  decide the recall number. Refuse, and make the author lengthen the quote.

This is the whole reason the set can survive its corpus being edited: a broken label
fails the build instead of quietly pointing at the wrong paragraph.

### 2.3 The span is expanded by a *stated* structural rule

An anchor is a few words; the answer is usually a sentence or three. The expansion rule
is deterministic and written down, because it silently determines every coverage number
computed later:

1. **Table row** → label the rows the quote touches, nothing else.
2. **List item** → label that item, up to the next item at the same or shallower indent.
3. **Otherwise** → the enclosing paragraph block (a maximal run of non-blank lines).

A sentence splitter would be neither deterministic nor correct on Markdown containing
code fences, tables, and list items.

### 2.4 Chunk IDs are content-addressed, exactly as `02` §9.1 specifies

`sha256(doc_id, chunker_version, embed_text)`, NUL-separated. Position-addressed IDs
(`hash(doc_id, version, ordinal)`) would mean that inserting one paragraph at the top of
a chapter renames every chunk below it — invalidating every derived label in the same
motion. `test_chunk_ids_are_stable_under_an_edit_elsewhere_in_the_document` asserts the
property directly rather than trusting it.

The chunker itself is deliberately boring: structure-aware split at headings (a heading
is a boundary the author already drew, `02` §6.3), heading path prepended to the
embedded text, **zero overlap** (overlap costs `1/(1-f)`, not `f`, and it puts one
answer span in two chunks by construction, which is noise in a labelling harness), and
code fences never split. It is not a claim about the best chunking — comparing
chunkings is `02`'s lab, and this set is designed to survive that comparison.

---

## 3. How to author a question

1. **Ask what a user would ask.** Not a keyword bag, and not a question whose wording
   copies the document — that measures string matching, not retrieval.
2. **Find the text that answers it** and copy a short exact substring as the anchor.
   Distinctive beats long: `$3.8 per GB/month` is a better anchor than a whole sentence
   with three chances to mistype it.
3. **Write `expected_answer`** so it can be graded — a fact, not a gesture at one.
4. **Set `hop`.** `multi` means the answer genuinely requires every span; it changes
   which recall definition applies (`00` §6: "any" vs "all" is not a detail).
5. **Run `python3 build.py`.** Fix what it rejects. Ambiguity is fixed by lengthening
   the quote, not by picking an occurrence.
6. **Read the resolved span.** The builder tells you where the label landed; a label you
   have not read is a label you are trusting on faith.

Never renumber an ID. Labels are cited by ID in later exercises, and a reused ID makes
two different measurements look like the same one.

---

## 4. Build log — what actually went wrong

Rung 2 means "here is what I built and what happened," so:

**Two anchors failed to resolve on the first build.** Both were quotes I had
transcribed across a line wrap. The builder refused to write a partial set, named both,
and the fix took thirty seconds. This is the failure mode the design is for: a golden
set that tolerates unresolvable labels loses records silently, and its trend lines stop
being comparable week to week.

**The first span-expansion rule was wrong, and only reading the output caught it.**
The original rule was "expand to the enclosing paragraph." Markdown numbered lists have
no blank lines between items, so fourteen records anchored in the chapters' summary
lists expanded to the *entire list* — a 3,245-character span covering a dozen unrelated
facts. Every test still passed. Every label was still "correct." And recall@k computed
against those labels would have been inflated for a reason nothing in the pipeline would
ever have reported. The fix is §2.3's three-case rule; mean span length dropped to ~500
characters, and `test_block_expansion_stops_at_list_and_table_boundaries` now pins it.

**Then the gate fired for real, unprompted.** Writing the §16 explanation in
`00-mental-models.md` edited a paragraph one of the labels was anchored to. The next
build refused with `quote not found`, naming the record; re-anchoring it took one line.
That is the entire argument for this design in miniature — the corpus changed, and the
system said so, instead of quietly scoring against a label pointing at deleted text.

The lesson is the one `31-measurement-methodology.md` keeps making: the labels are
themselves an experiment, and an unexamined experiment produces a confident number
about nothing.

**The gate was verified by breaking it.** A green suite that cannot go red is
decoration, so the artifact was truncated on purpose:
`build.py --check` exited 1 and `test_committed_artifact_matches_a_fresh_build` failed
(19/20), then `build.py` restored it and all 20 passed again.

---

## 5. What this deliberately is not

- **No retrieval, no embeddings, no generator.** Recall@k is exercise 2; the oracle-
  context harness is exercise 3. This lab produces the labels both of them consume.
- **No claim about chunk size.** `max_chars=1800` is a parameter, pinned and recorded,
  not a recommendation.
- **No quality number.** Nothing here is rung 1. The first measurement arrives when a
  retriever runs against these labels.

---

## 6. What a production golden set does differently

Everything above is honest at this scale and would be negligent at production scale.
The differences that matter, roughly in order of how much they change the numbers:

**Queries come from traffic, not from the author.** The single largest bias in this set
is that the person who wrote the questions had read the corpus. Real users ask about
things the corpus does not contain, ask with the wrong vocabulary, and ask the same
thing 400 different ways. Production sets are sampled from logs — stratified by intent,
by frequency, and deliberately over-sampling the tail, because the head is where a
system is already fine. Expect the failure distribution from `00` §6 to move
substantially when you make this switch.

**Labels are adjudicated, not authored once.** Two or more annotators label
independently, disagreements are adjudicated, and inter-annotator agreement is reported
alongside every metric. If two humans agree only 70% of the time about what counts as
answer-bearing, no retrieval number computed against those labels can be trusted to
better than that. Single-author labels — which is what this lab has — encode one
person's reading of the corpus.

**The set is split and held out.** Tuning `k`, the fusion weights, and the reranker
against the same 60 queries you report on overfits the harness. Production keeps a dev
split for iteration and a test split touched only for release decisions, plus a canary
slice never used for tuning at all.

**Size is set by the effect you need to detect, not by a round number.** 50–60 queries
gives bootstrap confidence intervals wide enough that only large effects clear zero
(exercise 6 makes this concrete). Detecting a two-point recall change usually needs
hundreds to low thousands of labelled queries. The honest move at this size is to report
the interval and let it be wide.

**Grading is a judge with a measured error rate.** `expected_answer` here is graded by a
human reading it. Production uses an LLM judge for faithfulness and answer correctness —
and then measures *the judge* against a human-labelled sample, reporting its agreement
rate, because an unvalidated judge is an unmeasured instrument in the middle of your
metric.

**Labels live in a versioned dataset, results in a warehouse.** JSONL in git is exactly
right for 60 records and stops being right around the point where humans are labelling
continuously. Production versions the label set (Delta/Iceberg/LakeFS or equivalent),
writes eval results to DuckDB or the warehouse per `../databases/21-in-process-olap-duckdb-chdb.md`,
and joins them to traces and token cost by request ID — which is `00` §13's point that a
trace, an eval, and a cost row that cannot be joined answer no question worth asking.

**Regeneration is continuous, and drift is alerted.** The builder runs in CI on every
corpus change. Unresolvable labels open a ticket; they do not sit in a branch. The set
is a living asset with an owner, and **every production incident becomes a new record** —
that is what turns the golden set into a regression suite rather than a snapshot.

**The corpus is real, which means it is governed.** Traffic-derived queries carry PII,
so the golden set inherits redaction, retention limits, and access control — and in a
multi-tenant system, per-tenant label sets and per-tenant metrics, because an aggregate
recall number across tenants hides the tenant whose corpus is broken.

**Eval runs cost money.** A few hundred queries × reranker + generator on every commit is
a real bill (`00` §9). Production gates PRs on a fast subset and runs the full set
nightly, with prompt caching and cached embeddings for unchanged chunks.

None of that changes the four decisions in §2 — spans over chunk IDs, resolved anchors,
a stated expansion rule, content-addressed IDs. Those are the parts that make a golden
set survive contact with a corpus that keeps changing, and they are the parts that are
expensive to retrofit once you have thousands of labels instead of sixty.

---

## 7. Next

Exercise 2 consumes `golden-set.v1.jsonl`: for each query, retrieve at k ∈ {1, 5, 10,
20, 50}, count a hit when a retrieved chunk ID is in `answer_bearing_chunk_ids`, and
land the per-query, per-k rows in DuckDB. Report the hit rule (`any_overlap`) and the
multi-hop rule ("any" or "all") next to every number, or the number is not fully
specified.
