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

### 7.1 Reading a recall@k curve, in plain language

> **The numbers in this subsection are made up.** They are a worked example of how to
> *read* a curve, not a measurement of anything — this lab has not run a retriever yet.
> When you have your own curve from exercise 2, these numbers get replaced by yours,
> each with the one-sentence account of how it was measured that README §6 requires.

Recall@k answers one question: **if I take the top k results, how often is the answer
in there?** Nothing more. A worked curve, with what each row would mean:

| Metric | Illustrative value | In a search UI | In a RAG prompt |
|---|---|---|---|
| **Recall@1** | 0.52 (52%) | Half of users find their answer as the #1 result — no scrolling, instant. Critical where screen space is tiny: voice, mobile, a chat widget. | Cheapest possible context: one chunk, minimum input tokens, minimum distraction. |
| **Recall@3** | 0.71 (71%) | Most users find it in the top 3 — minimal scrolling, all above the fold. Good balance of precision against user effort. | The usual sweet spot: enough evidence to be right most of the time, still a small prompt. |
| **Recall@5** | 0.83 (83%) | Most users find it within the top 5; some scrolling. The standard benchmark point for retrieval systems. | A normal shipped budget. Note 17% of queries are already unanswerable at this k. |
| **Recall@10** | 0.91 (91%) | Nearly all relevant documents are eventually retrieved, but it takes real scrolling to get there. Useful as a coverage number. | 10 chunks of input tokens on *every* request, forever, plus 5 extra distractors the model has to read past. |

**Three things this curve tells you, in order of usefulness:**

1. **The ceiling.** Recall@k at the k you actually ship is the hard cap on end-to-end
   correctness (`00` §4). Ship k=5 with the numbers above and 17% of queries cannot be
   answered correctly no matter how good the model or the prompt is. That is the single
   most useful sentence you can say about a retrieval system.
2. **The shape, not the values.** A curve that climbs steeply from 0.52 to 0.91 means the
   right chunks *are* being found and merely ranked badly — a ranking problem, fixed with
   a reranker or better fusion (failure class (c)). A curve that is flat and low
   everywhere — 0.40 at k=1 and still 0.45 at k=50 — means the chunks are not findable at
   all, and no amount of reranking touches it: that is a chunking, embedding, or
   ingestion problem (classes (a)/(b)). Same four points, completely different week of
   work.
3. **The gap between generous k and shipped k.** Recall@50 minus recall-at-what-survives-
   reranking-and-truncation is exactly the size of failure class (c) — evidence retrieval
   found and the budget threw away. Exercise 8 is that measurement.

**Where the search-UI intuition breaks down for RAG.** In a search interface, a bigger k
costs the *user* effort — scrolling. In a RAG pipeline nobody scrolls: every one of those
k chunks is pasted into the prompt, so a bigger k costs **input tokens on every request
forever** (`00` §9's dominant cost term) and adds distractor chunks that measurably
degrade the answer (`00` §11's context rot). So recall@10 = 0.91 is not straightforwardly
"better" than recall@3 = 0.71 — it is +20 points of ceiling bought with roughly 3× the
per-query context bill and a noisier prompt. Which trade wins is a decision, and you can
only make it with both numbers in front of you.

**Two things a recall number is not:**

- **It is not precision.** Recall@10 = 0.91 says the answer is somewhere in those ten
  chunks. It says nothing about the other nine being junk. Report a token-efficiency
  measure next to it (`02` §11.4's IoU) or you will conclude that retrieval quality is
  fine while the generator drowns in irrelevant context.
- **It is not comparable across hit rules or chunkings.** Recall computed with
  `any_overlap` is a different quantity from recall computed with `span_containment`, and
  neither is comparable to a run at a different chunk size. State the rule with the
  number every time (`02` §11.2) — this is why the manifest pins `build_hit_rule`.

### 7.2 What's "good enough" to ship

There is no universal threshold, and anyone quoting one without asking what your system
does is guessing. But "it depends" is not an answer either, so: here is how to *derive*
your threshold, and a table of starting numbers to beat while you do.

**Derive it from the correctness you're promising.** `00` §4's inequality rearranges
into a requirement:

```
P(correct) ≤ P(retrieved) × P(used correctly | retrieved)

    ⇒   required recall@k_shipped  ≥  target_correctness / faithfulness
```

`faithfulness` is measurable today with the oracle-context test (exercise 3): feed the
known-correct chunk directly and see how often the model still gets it right. For a good
model on clean, non-conflicting context it usually lands around 0.90–0.95 — **measure
yours, don't borrow that range**. Then:

| You promise | Measured faithfulness | Recall@k_shipped you actually need |
|---|---|---|
| 80% correct | 0.90 | ≥ 0.89 |
| 90% correct | 0.90 | ≥ 1.00 — not achievable; fix retrieval *and* faithfulness first |
| 90% correct | 0.95 | ≥ 0.95 |
| 95% correct | 0.95 | ≥ 1.00 — not achievable without an abstain path |

That second row is the useful one. It shows why "recall@5 = 0.83, ship it" is incoherent
next to a claim of 90% accuracy, and it does so before anyone has argued about prompts.

**Starting thresholds, by k.** Heuristics for a general-purpose text corpus with hybrid
retrieval and a reranker — numbers to beat and then replace with your own, not acceptance
criteria and not measured on this lab's corpus:

| k | Weak — go fix ingestion/chunking | Typical | Good | What this k is for |
|---|---|---|---|---|
| **@1** | < 0.35 | 0.35–0.55 | > 0.55 | Single-answer surfaces: voice, a chat widget, an agent that takes the first tool result at face value |
| **@3** | < 0.55 | 0.55–0.75 | > 0.75 | The common shipped budget — small prompt, low cost per query |
| **@5** | < 0.65 | 0.65–0.85 | > 0.85 | Standard reporting point; a generous shipped budget |
| **@10** | < 0.75 | 0.75–0.92 | > 0.92 | Coverage; usually past the point where extra tokens pay for themselves |
| **@50** (pre-rerank) | **< 0.95 — stop** | 0.95–0.98 | > 0.98 | Candidate generation. Not a quality target: a **hard floor** |

**The @50 row is the only one close to a rule.** Stage one exists to not lose the answer
(`00` §7). Whatever stage one drops is gone permanently — no reranker, prompt, or model
recovers it. So if recall@50 is below ~0.95, every hour spent on reranking, fusion
weights, or prompts is spent on the wrong stage, and the fix is upstream: chunking,
hybrid retrieval, embedding choice, or ingestion coverage. Treat that as the first gate
you check and the last one you're allowed to fail.

**Scale the target to what a wrong answer costs.** Same math, different `target_correctness`:

| System | Reasonable target | And also |
|---|---|---|
| Internal doc search, engineer in the loop | recall@5 ≈ 0.80 | The user can rephrase; a miss costs a few seconds |
| Customer-facing support assistant | recall@5 ≥ 0.90 | Plus citations, plus a handoff path when evidence is thin |
| Medical, legal, financial advice | recall@k as high as you can buy | Plus mandatory citations, plus an **abstain path** — past ~0.95, buying recall gets exponentially expensive, and "I don't have a source for that" is worth more than the last 3 points |

The abstain path is the part people skip. Above roughly 0.95 the cheapest way to raise
*correctness* is usually to stop answering when retrieval scores are weak, not to chase
recall toward 1.0.

**Ratios beat absolute values for deciding what to work on.** These need no threshold
table at all, and they point at a stage:

| Signal | What it means | What to do |
|---|---|---|
| recall@1 / recall@10 < 0.6 | The answer is being found and ranked badly | Add or improve the reranker; tune fusion. Cheapest win available |
| recall@1 / recall@10 > 0.85 | Ranking is already good | Further reranking work is wasted; gains must come from candidate generation |
| recall@50 ≈ recall@10 | Depth is exhausted — more candidates find nothing new | Representation/chunking/ingestion problem (classes (a)/(b)), not a ranking one |
| recall@50 − recall@k_shipped is large | Retrieval found it, budget or reranker discarded it | Class (c): rerank quality, truncation policy, or a bigger context budget |
| Recall high, answers still wrong | Evidence is present and unused | Class (d): run the oracle test, work on prompt and faithfulness |

**Gate CI on regressions, not on absolute thresholds.** Absolute numbers move whenever
the corpus or query mix changes, so a hard `recall@5 >= 0.85` gate eventually fails for
reasons that have nothing to do with the change under review. The durable gate is: block
the merge if recall at the shipped k drops and the bootstrap CI on the delta excludes
zero (exercise 6). Keep one absolute floor alongside it — recall@50 ≥ 0.95 — because that
one is structural rather than tuned.

**Do not apply any of this to 60 queries without checking the interval first.** At this
size one query is 1.7 points, and a 95% bootstrap CI is roughly ±10 points — wide enough
that 0.80 and 0.90 are not distinguishable. Thresholds this precise need hundreds of
labelled queries. With 60, report the interval, compare against your own previous run,
and resist the urge to read three-decimal-place meaning into it.
