# 08 — Evaluation methodology

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the pipeline as dataflow, the
> recall ceiling, the four failure classes — this chapter is how you *detect* which class you're in),
> [`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md)
> (the single highest-leverage file in the repo for this chapter: every number below is a
> measurement claim, and a measurement claim without a stated derivation and an interval is worth
> less than no number),
> [`../python-mastery/43-testing-strategy.md`](../python-mastery/43-testing-strategy.md) (evals are
> tests; the fixtures/isolation/flakiness discipline transfers wholesale),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) §11 and
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) §13 — both defer
> their metric definitions here, and both state a trap this chapter generalizes.
>
> **Feeds into:** [`09-eval-infrastructure-and-ci.md`](09-eval-infrastructure-and-ci.md) (this
> chapter defines *what* to measure; `09` is the pipeline that runs it on every commit),
> [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md) (§15 — an eval score
> with no trace behind it can't be debugged, and a trace with no eval label attached can't be
> aggregated), [`14-agent-evaluation.md`](14-agent-evaluation.md) (§12 is its foundation),
> [`appendix-b-metric-definitions.md`](appendix-b-metric-definitions.md) (every formula here,
> extracted and stated once),
> [`appendix-c-eval-recipe-book.md`](appendix-c-eval-recipe-book.md) (the copy-pasteable versions),
> and **P0 in the README's project ladder**, which is the artifact this chapter exists to unblock.
>
> **THESIS:** an eval is a measurement instrument, and the instrument has its own error bar.
> Almost every RAG evaluation that misleads its owner does so for one of three reasons, none of
> which is "picked the wrong metric": **the label did not survive the change being tested**, **the
> comparison was not paired**, or **the instrument — usually an LLM judge — was never itself
> validated against anything**. The corollary is the organizing rule of this chapter: *measure each
> stage with a metric that stage can actually move, using labels defined in terms that are
> independent of that stage.* A metric a stage cannot move is worse than no metric, because it
> manufactures confident null results — and a confident null result is how a team concludes that
> reranking "doesn't help on our data" and stops looking.

---

## Contents

1. [What an eval is, and the two questions it answers](#1-what-an-eval-is-and-the-two-questions-it-answers)
2. [The measurement map — every stage, its metric, its label](#2-the-measurement-map--every-stage-its-metric-its-label)
3. [Labels: the golden set is the load-bearing artifact](#3-labels-the-golden-set-is-the-load-bearing-artifact)
4. [Evaluating parsing — the stage nobody measures](#4-evaluating-parsing--the-stage-nobody-measures)
5. [Evaluating chunking](#5-evaluating-chunking)
6. [Evaluating embeddings](#6-evaluating-embeddings)
7. [Evaluating the index — and the two different things called "recall"](#7-evaluating-the-index--and-the-two-different-things-called-recall)
8. [Evaluating retrieval: the metric zoo, with formulas](#8-evaluating-retrieval-the-metric-zoo-with-formulas)
9. [Evaluating the reranker](#9-evaluating-the-reranker)
10. [Evaluating generation](#10-evaluating-generation)
11. [LLM-as-judge, treated as the classifier it is](#11-llm-as-judge-treated-as-the-classifier-it-is)
12. [Agentic and multi-hop evaluation](#12-agentic-and-multi-hop-evaluation)
13. [Statistics — making a delta mean something](#13-statistics--making-a-delta-mean-something)
14. [From metrics to gates](#14-from-metrics-to-gates)
15. [Online evaluation and the label flywheel](#15-online-evaluation-and-the-label-flywheel)
16. [Cost model for the eval layer itself](#16-cost-model-for-the-eval-layer-itself)
17. [Anti-patterns](#17-anti-patterns)
18. [Mental models — the compressed set](#18-mental-models--the-compressed-set)
19. [Lab exercises](#19-lab-exercises)

---

## 1. What an eval is, and the two questions it answers

An **eval** is: a fixed set of inputs, a system under test, and grading logic that turns each output
into a score. That is the whole definition. Everything difficult about evaluation is downstream of
three choices — which inputs, which grading logic, and what you're allowed to conclude from the
resulting numbers.

An eval suite answers exactly two questions, and it is worth being explicit about which one you are
asking, because they want different designs:

| Question | Name | Design implication |
|---|---|---|
| "Is B better than A?" | **Comparative / regression** | Needs *paired* per-item scores, a fixed dataset, and a significance test. Absolute level is irrelevant; only the delta matters. Small, cheap, run constantly. |
| "Is this good enough to ship?" | **Absolute / acceptance** | Needs a dataset that resembles production traffic in *composition*, and a threshold argued from user impact. Absolute level is everything. Larger, more expensive, run rarely. |

Most teams build the second and then try to use it for the first. That fails in a specific way: an
acceptance set is chosen to be *representative*, which means most of its queries are easy, which
means a change that fixes 30% of your hard queries moves the aggregate by two points and dies in the
noise. Regression sets should be deliberately *enriched for difficulty and for the failure mode you
are working on*. §3.4 makes this concrete.

### 1.1 Offline evals are one instrument among five

Automated offline evals are cheap, reproducible, and run on every commit — and they are systematically
blind to whatever your dataset doesn't contain. Anthropic's engineering guidance on agent evals is
explicit that a complete picture combines **automated evals, production monitoring, user feedback,
A/B testing, manual transcript review, and systematic human evaluation** — automated evals are the
first line of defense, not the whole defense.
([Anthropic, *Demystifying evals for AI agents*, Jan 2026](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents))

| Instrument | Catches | Blind to |
|---|---|---|
| Offline eval suite | Regressions on known failure modes; anything the labels cover | Everything not in the dataset; distribution shift |
| Production monitoring (`10`) | Latency, error rate, cost, refusal rate, retrieval-empty rate | Correctness — there's no ground truth in prod |
| Implicit user feedback | Real dissatisfaction at scale | Why; and it's badly biased toward vocal failures |
| A/B test | Actual user outcome, controlling for confounds | Slow (days–weeks), needs traffic, can't test what you haven't shipped |
| Manual transcript review | Failure modes you didn't know existed | Doesn't scale; not reproducible |

The trap is treating these as a ladder where offline evals are the beginner rung. They aren't.
They're the only instrument that can run *before* a change reaches a user, which is the only place a
regression can still be cheap.

### 1.2 The rung discipline, applied to eval numbers

From the README's rung ledger: numbers produced by an eval harness are **rung 1 — measured**, and
they only stay rung 1 if you can state the derivation in one sentence. In practice that sentence
must contain: the dataset and its size, what counted as a hit, the configuration of every stage you
did *not* change, and the interval. "Recall@10 went from 0.71 to 0.78" is not a claim; it's a rumor.
"Recall@10 at a fixed 4,000-token context budget went from 0.71 to 0.78 (+0.07, 95% paired-bootstrap
CI [0.03, 0.11], n=240 queries, span-level `any_overlap` hit rule, everything downstream of chunking
pinned)" is a claim.

Everything in §13 exists to make that second sentence writable.

---

## 2. The measurement map — every stage, its metric, its label

This is the spine of the chapter. Each row is a stage; each stage gets a metric it *controls*, and a
label type that is *invariant* to the thing being changed at that stage (§3.1 explains why that
second column is load-bearing).

| Stage | What it decides | Metric that stage controls | Label the metric needs | Section |
|---|---|---|---|---|
| **Parse** | The ceiling. Content not extracted cannot be retrieved. | Extraction yield, reading-order correctness, TEDS (tables), NED/CER (text), answer-span survival | Ground-truth page/document rendering; or the answer spans from your golden set | §4 |
| **Normalize** | Whether the lexical and dense branches see usable text | Analyzer round-trip checks; token-count drift | None (property tests) | §4.6 |
| **Chunk** | How much of the ceiling survives into retrievable units | Token-level recall / precision / IoU; recall at fixed token budget | **Character spans in the source document** — never chunk IDs | §5 |
| **Embed** | How well the surviving content is matched | recall@k *with the index and chunking pinned*; per-slice deltas | Query → relevant span set (same as chunking) | §6 |
| **Index (ANN)** | How much of the embedding model's ranking you actually get | ANN recall@k *against exact brute-force*; p50/p95/p99 latency; build cost | None — ground truth is the exact search over the same vectors | §7 |
| **Retrieve (branches + fusion)** | Whether the answer is in the candidate set at all | recall@`branch_depth`, recall@`fusion_depth` | Relevant span set | §8 |
| **Rerank** | The ordering of a fixed candidate set | nDCG@`final_k`, MRR@`final_k`, recall@`final_k` | **Graded** relevance, not binary | §9 |
| **Generate** | Whether a correct answer is produced from a correct context | Faithfulness, answer correctness, citation precision/recall, abstention correctness, schema validity | Reference answers and/or a validated rubric | §10 |
| **Agent loop** | Whether the task got done, at what cost | Task success on end state, tool-call correctness, cost per resolved task, silent-failure rate | Verifiable end state | §12 |
| **Judge** | *Your ability to measure any of the above* | Agreement with human labels (κ), per-class precision/recall, bias deltas | Human labels on a calibration set | §11 |

Two rules fall directly out of this table and are worth stating as rules:

**Rule 1 — the metric must be movable by the stage.** A reranker cannot improve
recall@`fusion_depth`; it reorders a fixed set. It *can* improve recall@`final_k`. These are
different quantities and conflating them is the single most common way a team concludes reranking is
useless (`04` §13.2).

**Rule 2 — the label must be invariant to the stage.** If you change chunking, chunk-ID labels are
invalidated by construction and the comparison is meaningless. §3.1.

---

## 3. Labels: the golden set is the load-bearing artifact

Everything else in this chapter is arithmetic over labels. The labels are the expensive part, the
part that rots, and the part that decides whether any of the arithmetic means anything.

### 3.1 The invariance principle

> **A label is usable for comparing configurations of stage S only if the label is defined in terms
> that are independent of S.**

This is the generalization of the trap `02` §11.2 states for chunking, and it applies at every
stage:

| Comparing… | A label defined as… | …is | Because |
|---|---|---|---|
| Chunking strategies | a set of chunk IDs | **broken** | Chunk IDs are outputs of the chunker. Re-chunk and every label dangles. |
| Chunking strategies | character spans `(doc_id, start, end)` | ok | Spans are properties of the source document. |
| Parsers | character offsets into the *parsed* text | **broken** | Offsets are outputs of the parser. |
| Parsers | a quoted string that must appear in the parse, plus its page | ok | Resolvable against any parse. |
| Embedding models | "the top-5 results of the old model" | **broken** | This is the definition of measuring your own tail. |
| Embedding models | relevant spans | ok | Independent of representation. |
| Rerankers | binary relevance | **weak, not broken** | A reranker's whole job is ordering; binary labels can't see ordering quality (§9.3). |
| Generators | one reference answer string | **weak** | Correct answers vary in surface form; needs rubric or claim-level grading (§10). |

The practical consequence: **the golden set stores spans and quoted text, and everything else is
derived at eval time.** The `labs/golden-set/` harness in this repo does exactly this — humans
author quotes, the builder resolves them to character offsets against a canonicalized corpus, and
chunk-level `answer_bearing_chunk_ids` is a *derived artifact*, regenerated whenever the chunker
changes. That indirection is not fussiness; it's the only thing that makes the set reusable across
`02`, `03`, `04`, and this chapter.

### 3.2 Binary vs graded relevance

Binary (`relevant` / `not relevant`) is cheaper to label, has higher annotator agreement, and is
sufficient for recall-type metrics at the candidate-generation stages. Graded (typically 0–3:
`irrelevant` / `related but not answering` / `partially answers` / `fully answers`) is *required* the
moment you want to evaluate ordering, because nDCG's entire mechanism is weighting by grade.

| | Binary | Graded (0–3) |
|---|---|---|
| Labeling cost | 1× | ~1.5–2× |
| Annotator agreement | Higher | Lower — needs an explicit rubric with examples per grade |
| Supports | recall@k, precision@k, MRR, MAP, success@k | all of those **plus** nDCG, and reranker deltas that binary metrics can't see |
| Failure it hides | Ordering quality | Nothing, but grade boundaries become an argument |

**Recommendation:** label graded from day one on the subset you'll use for reranker work (a few
hundred query–passage pairs is enough), binary everywhere else. Collapsing graded → binary is
trivial (`grade >= 2`); the reverse is not.

A subtlety that bites: **graded labels are per (query, passage) pairs, not per passage.** A passage
that fully answers query A may be a distractor for query B. Storing grades on the passage is a
schema bug you will discover six months later.

### 3.3 How many queries? Do the power calculation, don't guess

The honest answer is "it depends on the effect size you need to detect," and that is computable.
For a paired comparison of two systems on the same queries, the detectable difference scales with
the standard deviation of the *per-query difference*, `σ_d` — not with the standard deviation of the
scores themselves. This matters enormously: two retrieval configs agree on most queries, so `σ_d` is
much smaller than `σ_score`, and paired designs need far fewer queries than unpaired ones.

```python
# Minimum detectable effect for a paired comparison, two-sided, at the usual 80% power.
# This is the number you should compute BEFORE building the set, not after a null result.
import math

def mde_paired(n: int, sigma_d: float, alpha: float = 0.05, power: float = 0.80) -> float:
    """Smallest true mean per-query difference detectable with probability `power`."""
    z_a = 1.959964          # two-sided alpha=0.05
    z_b = 0.841621          # power=0.80
    return (z_a + z_b) * sigma_d / math.sqrt(n)

def n_for_mde(delta: float, sigma_d: float, alpha: float = 0.05, power: float = 0.80) -> int:
    z_a, z_b = 1.959964, 0.841621
    return math.ceil(((z_a + z_b) * sigma_d / delta) ** 2)

# Worked, with numbers you will actually see on a retrieval golden set.
# recall@10 is per-query in {0, 0.5, 1} for most queries; the *difference* between two
# reasonable configs is 0 for most queries and ±1 for a handful.
for sd in (0.15, 0.25, 0.40):
    print(f"sigma_d={sd}: n=60 -> MDE {mde_paired(60, sd):.3f} | "
          f"n=250 -> MDE {mde_paired(250, sd):.3f} | "
          f"n for MDE=0.03 -> {n_for_mde(0.03, sd)}")

# sigma_d=0.15: n=60 -> MDE 0.054 | n=250 -> MDE 0.027 | n for MDE=0.03 -> 197
# sigma_d=0.25: n=60 -> MDE 0.090 | n=250 -> MDE 0.044 | n for MDE=0.03 -> 546
# sigma_d=0.40: n=60 -> MDE 0.145 | n=250 -> MDE 0.071 | n for MDE=0.03 -> 1396
```

Read that output as the design constraint it is. **A 60-query set cannot resolve a 3-point recall
difference.** It can resolve a 9-to-14-point difference, which is fine — early in a project, changes
*are* that big, and Anthropic's guidance to start with 20–50 tasks is right for exactly this reason:
"in early agent development, each change to the system often has a clear, noticeable impact, and this
large effect size means small sample sizes suffice." Later, when you're chasing 2-point gains, the
same set will produce a stream of confident-looking nulls.

The workflow, then:

1. Start at 50–60 queries drawn from real failures. Ship changes with large effects.
2. Measure `σ_d` from the first few real A/Bs (it's just `numpy.std(scores_b - scores_a)`).
3. When your MDE stops being smaller than the effects you care about, grow the set — and grow it in
   the strata where the variance lives (§3.4), not uniformly.

### 3.4 Composition decides the answer

`02` §11.6 states this for chunking; it's general. **The query-set composition is a free parameter
that can flip the ranking of two systems.** A set dominated by keyword-ish lookups will say BM25 is
fine and dense retrieval is a waste. A set dominated by paraphrase questions will say the opposite.
Neither is wrong; both are answers to a question about a distribution.

So: **stratify, and report per stratum.** A minimal stratification for RAG:

| Stratum | Definition | Which stage it stresses |
|---|---|---|
| Lexical / identifier | contains an exact code, name, SKU, error string | BM25 branch; analyzer (§4.6) |
| Paraphrase | answer text shares few content words with the query | Dense branch; embedding model |
| Multi-hop | answer requires ≥2 spans, usually in different documents | Fusion depth, context assembly, agent loop |
| Long-tail / rare entity | entity appears <3 times in corpus | Embedding drift, chunk size |
| Tabular / numeric | answer lives in a table or figure | **Parser** — this is where §4 pays off |
| Negative / unanswerable | corpus genuinely does not contain the answer | Abstention (§10.5) — the stratum everyone omits |
| Temporal | answer changed; correct answer depends on recency | Freshness, metadata filtering |

The aggregate number over a stratified set is nearly meaningless on its own — it's a weighted average
whose weights you chose. Report the strata. `04` §13.4 says it well: the aggregate hides the
mechanism.

**The negative stratum is non-optional and is the one always missing.** Without it, "faithfulness"
and "answer correctness" are measured only on questions where a correct answer exists, and a system
that confidently fabricates an answer to an unanswerable question scores identically to one that
correctly says "I don't know." That's the most user-visible failure in RAG and the default eval
suite is blind to it.

### 3.5 Synthetic queries: useful, and distributionally wrong

LLM-generated query sets are the standard bootstrap when you have a corpus and no traffic. Both
RAGAS and ARES ship generators for this; the Chroma chunking study built its entire evaluation
this way, generating queries plus verbatim excerpts from each corpus and then filtering.

Their pipeline is worth copying because the *filtering* is the part people skip:

- Generate `(query, excerpts)` pairs with the excerpts constrained to be **exact substrings** of the
  corpus — this is what makes the labels span-based and therefore invariant (§3.1).
- **Prompt against compound questions** — Chroma explicitly instructed the generator to avoid the
  connecting "and" except inside proper nouns, because "What was the date *and* significance of the
  Gettysburg Address?" has two answers and breaks per-query scoring.
- **De-duplicate by embedding similarity**, with the threshold chosen by binary search plus manual
  inspection of sampled pairs.
- **Drop out-of-corpus excerpts** — LLMs will occasionally produce a plausible quote that isn't
  actually in the text. Verify by exact match, mechanically.

And the limitation they state themselves, which is the one that matters: **LLMs generate a
characteristic style of question.** They tend to be well-formed, single-hop, and lexically close to
the source passage — which systematically flatters dense retrieval and systematically understates how
badly your system handles the fragmented, typo-laden, context-dependent thing a real user types.

| Source | Cost | Distribution realism | Best use |
|---|---|---|---|
| LLM-generated from corpus | Very low | Poor | Bootstrapping before traffic exists; large-n coverage sweeps |
| LLM-generated, then human-edited | Medium | Medium | Growing a set fast without fully losing realism |
| Mined from production logs | Low (labeling is the cost) | Highest | The regression set, once you have traffic (§15.4) |
| Written by domain experts | High | High for hard cases | The difficult strata: multi-hop, tabular, negative |

Use synthetic for coverage, real for calibration, and **never report an absolute quality number off
a purely synthetic set** — only deltas.

### 3.6 Pooling bias and the unjudged-document problem

This is the oldest known failure in retrieval evaluation and it silently affects every RAG eval that
grows its labels by labeling what the current system returns.

The mechanism: you label the top-k of system A. You then evaluate system B, which returns some
excellent passages that A never surfaced. Those passages are unlabeled, and unlabeled is scored as
non-relevant. **B is penalized for finding things A missed** — which is precisely the improvement you
were trying to detect. The classical IR literature calls this pooling bias, and the finding is that
systems which did not contribute to the pool are systematically underrated, with recall generally
overestimated by pooled collections (Buckley & Voorhees; Zobel).

Three mitigations, in increasing order of cost:

1. **Pool across systems, not one system.** Whenever you evaluate a new configuration, take the
   union of the top-k from *every* configuration under comparison, dedupe, and label the unlabeled
   ones before computing any metric. This makes the label set a function of the comparison, which is
   annoying but correct.
2. **Use a bias-robust metric for the interim.** `bpref` was designed for exactly this: it only
   counts *judged* documents, so unjudged passages neither help nor hurt.

   ```python
   def bpref(ranked_judged: list[bool], n_rel: int) -> float:
       """ranked_judged: relevance of the JUDGED docs only, in rank order.
       Unjudged docs must be removed from the list entirely, not marked False —
       that removal is the whole point of the metric."""
       if n_rel == 0:
           return 0.0
       n_nonrel_seen, total = 0, 0.0
       n_nonrel = len(ranked_judged) - sum(ranked_judged)
       for is_rel in ranked_judged:
           if is_rel:
               total += 1.0 - min(n_nonrel_seen, n_rel) / max(min(n_rel, n_nonrel), 1)
           else:
               n_nonrel_seen += 1
       return total / n_rel
   ```
3. **Track the judged fraction as a first-class health metric.** For every eval run, record
   `judged@k` = the fraction of returned results that carry a label. When it drops below ~0.8 for a
   configuration, its metrics are suspect and you should say so on the dashboard rather than in a
   footnote.

### 3.7 Human labels: agreement before volume

Two annotators labeling to different mental rubrics produce a dataset whose noise floor exceeds the
effects you're trying to measure. The standard instrument is **Cohen's κ** for two annotators (or
Krippendorff's α for more, or ordinal data).

```python
def cohens_kappa(a: list[int], b: list[int]) -> float:
    """Chance-corrected agreement between two annotators over the same items."""
    from collections import Counter
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(a) | set(b))
    return (po - pe) / (1 - pe) if pe < 1 else 1.0
```

Rough reading (Landis & Koch, and treat it as rough — κ is sensitive to class balance):
`<0.20` poor, `0.21–0.40` fair, `0.41–0.60` moderate, `0.61–0.80` substantial, `>0.80` almost
perfect. **Target ≥0.7 on binary relevance and ≥0.6 on 4-grade relevance before you scale
labeling.** Below that you are paying to add noise.

The protocol that actually gets you there:

1. Write the rubric with a **worked example per grade**, drawn from your corpus, not invented.
2. Both annotators label the same 50 items. Compute κ.
3. **Sit down and adjudicate every disagreement.** The disagreements are the rubric's bug report;
   each one becomes either a clarifying sentence or a new example.
4. Re-label a fresh 50. Repeat until κ clears the bar.
5. Only then split the work.
6. Keep a 10% overlap forever, so κ drift is visible.

Relevance judgments are irreducibly subjective and vary between assessors — the goal isn't to
eliminate that, it's to get the disagreement below your effect size and to *know* the number.

### 3.8 Versioning, provenance, and rot

A golden set is a dataset with a schema, a version, and a manifest — not a spreadsheet. The minimum
manifest, so that a number from six months ago is reproducible:

```json
{
  "dataset_version": "golden-set.v3",
  "corpus_digest": "sha256:…",          // canonicalized corpus bytes, not the raw files
  "corpus_commit": "66bd0be",
  "n_queries": 240,
  "strata": {"lexical": 42, "paraphrase": 61, "multi_hop": 35, "tabular": 28,
             "negative": 30, "long_tail": 26, "temporal": 18},
  "label_type": "graded_0_3",
  "hit_rule": "any_overlap",             // how a span maps onto a retrieved unit
  "annotators": ["a1", "a2"],
  "kappa_binary": 0.78,
  "kappa_graded": 0.64,
  "judged_fraction_at_10": 0.91,
  "generator": {"kind": "human+llm", "model": "claude-opus-5", "prompt_sha": "…"},
  "built_at": "2026-08-12T09:14:03Z"
}
```

Three rot mechanisms to defend against explicitly:

- **Corpus drift.** The corpus changes; spans no longer resolve. The builder must *fail loudly* on
  an unresolvable span rather than silently dropping the query — a silently shrinking eval set is
  how a suite stops covering the thing it was built to cover.
- **Contamination.** Once a query set is public (or in a prompt that goes to a hosted model with
  training-eligible data), it degrades as a measurement of generalization. Keep a **held-out slice
  that never goes into a prompt you don't control**, and reconcile against it quarterly.
- **Saturation.** An eval sitting at 100% tracks regressions and provides zero signal for
  improvement. Anthropic's framing is the right one: as evals approach saturation, large capability
  improvements show up as small score increases, which makes results deceptive. When a stratum is
  saturated, retire it to a smoke test and build a harder one.

---

## 4. Evaluating parsing — the stage nobody measures

`02` §3 states the claim: parsing sets the ceiling. This section is how you put a number on the
ceiling. It is the most-skipped evaluation in RAG and the one with the largest silent losses,
because a parser failure looks exactly like a retrieval failure from the outside — the answer isn't
returned, and every debugging instinct points downstream.

### 4.1 Intrinsic vs extrinsic, and which one you actually need

**Intrinsic** parser evaluation compares the parse to a ground-truth rendering of the document.
**Extrinsic** evaluation asks whether the pipeline that consumes the parse answers questions
correctly. You need both, for different jobs:

| | Intrinsic | Extrinsic |
|---|---|---|
| Question | "Did it read the page correctly?" | "Did the reading error cost us anything?" |
| Needs | Ground-truth annotations per page (expensive) | Your existing golden set |
| Use for | Choosing a parser; regression-testing a parser upgrade | Deciding whether parser quality is your bottleneck at all |
| Gotcha | High scores on clean academic PDFs predict little about your scanned invoices | Confounded with everything downstream; can't localize the fault |

**Do the extrinsic check first**, because it's nearly free and it tells you whether to spend money on
the intrinsic one. That check is §4.5.

### 4.2 The intrinsic metric set

The current reference benchmark for document parsing is **OmniDocBench** (Ouyang et al., CVPR 2025),
and its metric choices are the ones to copy even if you never run the benchmark itself. Its pipeline
uses **Normalized Edit Distance** for text, **TEDS** (Tree-Edit-Distance-based Similarity) for
tables, and **CDM** (Character Detection Matching) for formulas — plus layout and reading-order
annotations.

| Artifact | Metric | What it means | Notes |
|---|---|---|---|
| Body text | **NED** / CER / WER | 1 − (edit distance ÷ length) | Robust and boring. Compute after the *same* normalization your pipeline applies (§4.6), or you'll measure Unicode differences. |
| Tables | **TEDS** | Tree edit distance over the HTML DOM of the table, normalized by tree size | The only metric that scores *structure* — merged cells, multi-level headers — rather than just the cell text. A table can have 100% correct text and a TEDS of 0.4. |
| Formulas | **CDM** | Character-level detection matching | Only relevant if formulas are load-bearing for your corpus. |
| Layout | mAP over region boxes | Detection quality of blocks | Mostly matters if you do layout-aware chunking (`02` §6). |
| **Reading order** | Kendall's τ or NED over the block sequence | Whether the parse serializes multi-column pages correctly | The most under-measured of the set, and the one that silently destroys sentence continuity in two-column PDFs. |

Two warnings about published parser scores.

**First: benchmark saturation.** OmniDocBench's top models now cluster tightly — table TEDS in the
high-80s to low-90s across leading systems on v1.5 — and there is an active argument that the
benchmark is saturated for the document types it covers. A leaderboard where the spread between
first and fifth is two points is not a decision procedure for your corpus.

**Second: distribution mismatch.** Benchmarks are built from academic papers, textbooks, and
handwritten notes. If your corpus is 15-year-old scanned insurance forms, the ranking may not
transfer at all. `appendix-d-doc-processing-benchmarks.md` covers the tool landscape; this section
covers how to measure *on your documents*.

### 4.3 The cheap intrinsic proxies you should run on every ingest

You will not hand-annotate ground truth for your whole corpus. You don't have to. `02` §3 already
proposes parse-time gates; here they are as *metrics with distributions*, which is what makes them
usable as a regression signal rather than a one-off assertion.

```python
# Parse-quality proxies. No ground truth required. Track the DISTRIBUTION per parser
# version, not just per-document pass/fail — a regression shows up as a shifted
# histogram long before it shows up as a failed assertion.
import re, unicodedata

def extraction_yield(text: str, page_count: int) -> float:
    """Chars of extracted text per page. Near-zero => scanned pages, no OCR ran."""
    return len(text) / max(page_count, 1)

def script_sanity(text: str) -> float:
    """Fraction of chars in the expected script(s). Low => encoding/CMap failure,
    the classic 'PDF extracts as mojibake' bug."""
    ok = sum(1 for ch in text
             if ch.isspace() or unicodedata.category(ch)[0] in "LNPZ")
    return ok / max(len(text), 1)

def replacement_char_rate(text: str) -> float:
    return text.count("�") / max(len(text), 1)

def word_shape_sanity(text: str) -> float:
    """Fraction of whitespace-separated tokens of plausible length. Catches the
    'all spaces lost' failure (one 40k-char token) and the 'every char spaced'
    failure (thousands of 1-char tokens)."""
    toks = text.split()
    if not toks:
        return 0.0
    return sum(1 for t in toks if 1 < len(t) <= 30) / len(toks)

def table_cell_density(html: str) -> float:
    """Non-empty <td> fraction. A table parsed as a grid of empty cells scores
    perfectly on text metrics and is worthless."""
    cells = re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)
    return sum(1 for c in cells if c.strip()) / max(len(cells), 1)

GATES = {  # per-document; tune the thresholds on YOUR corpus, then freeze them
    "extraction_yield": (200, None),      # chars/page, min
    "script_sanity": (0.98, None),
    "replacement_char_rate": (None, 0.001),
    "word_shape_sanity": (0.85, None),
}
```

The value of these is that they are **computable on 100% of the corpus on every ingest**, which
makes them a *monitoring* signal, not just an eval signal — see `10` and `15`. A parser version bump
that quietly drops table extraction on one document class shows up as a bimodal
`table_cell_density` histogram within one ingest run.

### 4.4 Building a small intrinsic set when you need one

When the extrinsic check (§4.5) says parsing *is* your bottleneck, you need ground truth. Keep it
small and adversarial:

- **30–60 pages**, chosen by *difficulty stratum*, not at random: multi-column, rotated, scanned,
  dense tables, headers/footers, footnotes, math, mixed script. Random sampling wastes your annotation
  budget on pages every parser handles.
- Annotate: the correct linearized text, table HTML for each table, and the correct reading order of
  blocks.
- Score parsers on NED / TEDS / reading-order τ per stratum, with the per-stratum spread reported.
- **Re-run on every parser version bump.** Parser upgrades are the least-tested dependency change in
  a RAG stack and routinely change output shape.

### 4.5 The extrinsic check: answer-span survival

This is the highest-value-per-hour measurement in this entire chapter, and it needs nothing you
don't already have.

Your golden set stores answer spans as quoted text (§3.1). So: **for each parser configuration, what
fraction of golden answer spans can be located in the parsed text at all?** If a span isn't in the
parse, no chunker, embedder, index, or reranker can ever retrieve it. That's the ceiling, measured
directly.

```python
def span_survival(parsed_text: str, spans: list[str], *, normalize) -> dict:
    """Fraction of ground-truth answer spans recoverable from a parse.
    THE ceiling metric: retrieval recall can never exceed this.
    `normalize` must be the same function the pipeline applies (02 §4)."""
    hay = normalize(parsed_text)
    exact = fuzzy = 0
    for s in spans:
        n = normalize(s)
        if n in hay:
            exact += 1
        elif _fuzzy_contains(hay, n, threshold=0.90):   # e.g. rapidfuzz partial_ratio
            fuzzy += 1
    total = len(spans)
    return {
        "exact": exact / total,
        "exact_or_fuzzy": (exact + fuzzy) / total,
        "lost": 1 - (exact + fuzzy) / total,     # <- the ceiling loss
    }
```

Report it **per stratum**. The typical result is sobering and immediately actionable: text spans at
0.99, table spans at 0.55. That single pair of numbers reorders a quarter of roadmap work, and it
cost an afternoon.

The gap between `exact` and `exact_or_fuzzy` is itself diagnostic: a large gap means the parser is
recovering content but mangling whitespace, ligatures, or hyphenation — which is a normalization
problem (§4.6), not a parsing problem, and much cheaper to fix.

### 4.6 Normalization: property tests, not metrics

Normalization has no ground truth; it has *invariants*. Test them as properties:

- **Idempotence:** `normalize(normalize(x)) == normalize(x)`. Violations mean a rule that keeps
  firing, and they corrupt content-addressed chunk IDs (`02` §9).
- **Span preservation:** if you normalize, you must be able to map an offset in the normalized text
  back to the source. Assert round-trip on random spans; a broken offset map silently breaks every
  citation in §10.6.
- **Branch divergence is intentional:** the lexical and dense branches want different text (`02` §4).
  Assert that both derivations exist and that the *dense* text still contains the answer spans.
- **Token-count drift:** track the ratio `tokens(normalized) / tokens(raw)` per document class.
  A sudden shift is a normalization regression, and it changes your chunk count, your index size,
  and your cost model.

---

## 5. Evaluating chunking

`02` §11 owns the chunking-specific reasoning. This section owns the metric definitions it defers
here, plus the measurement protocol.

### 5.1 The two traps, restated

1. **Chunk-ID labels can't compare chunkings** (§3.1). Labels must be spans.
2. **Compare at a fixed token budget, not a fixed k.** A chunker that produces 200-token chunks and
   one that produces 800-token chunks, both evaluated at k=5, are being handed 1,000 vs 4,000 tokens
   of context. The larger chunker "wins" on recall by being given 4× the budget — and then loses in
   production on cost, latency, and lost-in-the-middle degradation. Fix the budget; let k float.

```python
def retrieve_at_budget(ranked_chunks, budget_tokens: int, count_tokens):
    """Take chunks in rank order until the token budget is exhausted.
    This — not top-k — is the fair comparison unit across chunkings."""
    out, used = [], 0
    for c in ranked_chunks:
        t = count_tokens(c.text)
        if used + t > budget_tokens:
            break
        out.append(c); used += t
    return out, used
```

### 5.2 Token-level metrics (the Chroma formulation)

Traditional IR metrics score whole documents, which cannot see chunking at all. The
[Chroma technical report](https://www.trychroma.com/research/evaluating-chunking)
(Smith & Troynikov, 2024) proposes scoring at the **token** level, which is the right granularity
because tokens are what the LLM actually pays for and attends over.

For a query `q`, let `t_e` be the set of tokens in all relevant excerpts (the ground-truth spans) and
`t_r` the set of tokens in all retrieved chunks:

```
Recall     = |t_e ∩ t_r| / |t_e|          fraction of answer tokens retrieved
Precision  = |t_e ∩ t_r| / |t_r|          fraction of retrieved tokens that are answer tokens
IoU        = |t_e ∩ t_r| / |t_e ∪ t_r|    Jaccard: penalizes both misses and padding
Precision_Ω = precision in the case where ALL chunks containing excerpt tokens are
              retrieved — an upper bound on token efficiency for that chunking
```

The crucial detail in the numerator: **each excerpt token is counted once, while all retrieved
tokens are counted in the denominator.** That asymmetry is what makes overlapping chunking strategies
pay for their redundancy — the same answer token appearing in two overlapping chunks inflates `|t_r|`
without inflating the intersection.

`Precision_Ω` is the most useful and least-known of the four: it separates *"this chunking wastes
tokens"* from *"this retriever picked badly."* If `Precision_Ω` is low, no retriever improvement can
save you; the chunk boundaries themselves are the problem.

```python
def token_metrics(excerpt_tokens: set[int], retrieved_token_multiset: list[int]) -> dict:
    """Token IDs must be positional (doc_id, offset) identities, not vocabulary IDs —
    otherwise the word 'the' in an irrelevant chunk counts as a hit."""
    retrieved_unique = set(retrieved_token_multiset)
    inter = len(excerpt_tokens & retrieved_unique)
    n_retrieved_total = len(retrieved_token_multiset)   # counts redundancy
    union = len(excerpt_tokens | retrieved_unique)
    return {
        "recall":    inter / max(len(excerpt_tokens), 1),
        "precision": inter / max(n_retrieved_total, 1),
        "iou":       inter / max(union, 1),
    }
```

### 5.3 What their numbers actually say — and how to quote them

Their headline table, `n=5` retrieved chunks, `text-embedding-3-large`, cl100k tokenizer, means ± SD
over all queries and corpora:

| Chunking | Size | Overlap | Recall | Precision | Precision_Ω | IoU |
|---|---|---|---|---|---|---|
| Recursive | 800 (~661) | 400 | 85.4 ± 34.9 | 1.5 ± 1.3 | 6.7 ± 5.2 | 1.5 ± 1.3 |
| TokenText | 800 | 400 | 87.9 ± 31.7 | 1.4 ± 1.1 | 4.7 ± 3.1 | 1.4 ± 1.1 |
| Recursive | 400 (~312) | 200 | 88.1 ± 31.6 | 3.3 ± 2.7 | 13.9 ± 10.4 | 3.3 ± 2.7 |
| ★ Cluster | 400 (~182) | 0 | 91.3 ± 25.4 | 4.5 ± 3.4 | 20.7 ± 14.5 | 4.5 ± 3.4 |
| ★ Cluster | 200 (~103) | 0 | 87.3 ± 29.8 | **8.0 ± 6.0** | **34.0 ± 19.7** | **8.0 ± 6.0** |
| ★ LLM (GPT-4o) | ~240 | 0 | **91.9 ± 26.5** | 3.9 ± 3.2 | 19.9 ± 16.3 | 3.9 ± 3.2 |

Four things to take from this, in order of importance:

1. **Recall barely discriminates; token efficiency does.** The recall spread across every strategy is
   ~85–92 — six points, inside one standard deviation. The `Precision_Ω` spread is 4.7 → 34.0, a
   **7×** difference. If you evaluate chunking on recall alone you will conclude that chunking
   doesn't matter, and you will be wrong by a factor of seven on how many tokens you pay per answer.
2. **Look at those standard deviations.** ±25 to ±40 on a 0–100 scale. Per-query recall is mostly 0
   or 100; the mean is a proportion, not a well-behaved central tendency. This is exactly why §13
   insists on paired tests and bootstrap intervals rather than eyeballing means.
3. **The conclusions differ between embedding models.** In their `all-MiniLM-L6-v2` table, overlap
   *helps* recall (82.4 with overlap 125 vs 77.1 without at the same size) — the opposite of the
   large-model result. Their own reading: for smaller context models, overlapping chunks are
   necessary for high recall. **A chunking result is not portable across embedding models**, which is
   the argument for measuring on your own stack rather than adopting anyone's ranking.
4. **Default configurations are not tuned for you.** They note that the then-documented OpenAI
   Assistants file-search default (800 tokens, 400 overlap) lands at slightly below-average recall
   and the lowest score on every other metric in their evaluation.

When you quote these numbers, carry the conditions: *their* synthetic multi-domain set, `n=5`, that
embedding model, those tokenizer sizes. Quoted without conditions, they're rung 3 wearing a rung 1
costume.

### 5.4 The chunking report card

Report all of this, per configuration, or you're arguing from one number:

| Column | Why |
|---|---|
| recall @ fixed token budget | The comparable quality number |
| Precision_Ω | The token-efficiency ceiling for this chunking |
| IoU | Combined completeness/efficiency; penalizes overlap redundancy |
| mean chunk size, chunk count | Index size, cost, and `03` sizing input |
| tokens/answer at the shipped budget | The number that appears on the invoice |
| p95 chunks per answer span | How badly answers get split — a high value predicts §10 faithfulness problems |
| index build time / cost | The re-chunk migration cost (`02` §12) |

---

## 6. Evaluating embeddings

`01` owns model selection. This section owns the measurement protocol, which has one rule that
subsumes the rest.

### 6.1 The rule: swap exactly one thing, and freeze the rest

An embedding comparison is only interpretable if the parser, normalizer, chunker, index parameters,
candidate depths, reranker, and prompt are byte-identical between runs. That sounds obvious and is
violated constantly, usually by accident:

- Different models have different **max sequence lengths**. Swapping in a 512-token model against an
  8192-token model while keeping 1,000-token chunks means the first model is *silently truncating*
  (`01` §8) and you are measuring truncation, not representation quality.
- Different models want different **prefixes** (`query:` / `passage:`, instruction prefixes). Omitting
  one is a several-point self-inflicted handicap.
- Different **dimensionalities** change the index's recall/latency curve at fixed HNSW parameters.
  If you don't re-tune `efSearch` per model, you're measuring the index, not the model (§7).

The protocol:

1. Pin chunking. Pin the golden set version. Pin candidate depths.
2. For each model: apply its documented prefixes; verify no chunk exceeds its sequence limit; build
   the index; **tune `efSearch` to a fixed ANN recall target (e.g. 0.99) so the ANN layer is not the
   variable** (§7.3).
3. Evaluate `recall@fusion_depth` and, downstream, `nDCG@final_k`.
4. Report per stratum (§3.4) and per-query paired deltas (§13).

### 6.2 Public benchmarks: read them as a shortlist, never as a decision

MTEB and BEIR are how you get from 40 candidate models to 4. They are not how you choose between the
4, and there are specific reasons:

- **Contamination.** MTEB's datasets are public, and models trained after 2023 may have seen BEIR
  corpora during pre- or post-training. MTEB's own training splits were published, and training on
  them then submitting to the leaderboard was never the intent — as Nils Reimers (a BEIR author) put
  it, embedding models need to be evaluated **out of domain**; anything else doesn't make sense.
- **Saturation.** The leaderboard now carries hundreds of models separated by fractions of a point.
  That spread is not a decision signal.
- **Distribution mismatch.** MTEB's retrieval suite skews to general web text (MS MARCO, TREC,
  NFCorpus) with short passages. If your corpus is legal contracts, scientific PDFs, or internal
  SaaS docs, those scores are directional at best.
- **Length mismatch.** Many MTEB retrieval sets use passages under ~256 tokens. A model that degrades
  past 512 tokens scores well there and fails on your long documents.

The community's response has been benchmarks with **private/held-out splits** specifically to resist
contamination and leaderboard-tuning (RTEB is the current example, spanning legal, finance, code, and
medical domains). Prefer benchmarks with closed splits, and treat any of them as a *prior*.

Three signatures that a model is leaderboard-tuned rather than good, worth checking explicitly:
outsized wins on datasets whose training splits are public; strong binary-relevance retrieval paired
with poor graded ordering; and a cliff (rather than graceful degradation) when moved to an unseen
domain.

### 6.3 Intrinsic embedding diagnostics worth keeping

These don't replace retrieval metrics, but they localize faults fast:

| Diagnostic | Computation | Detects |
|---|---|---|
| Query–positive vs query–random similarity gap | mean cos(q, pos) − mean cos(q, rand) | A model with no signal on your domain; a broken prefix convention |
| Similarity-score distribution | histogram of top-1 cosine over the query set | Anisotropy / score compression — everything at 0.85 means thresholds won't work |
| Duplicate collapse | fraction of near-duplicate chunks with cos > 0.99 | Whether dedup (`02` §10) is needed before or after embedding |
| Truncation rate | fraction of chunks exceeding the model's sequence limit | The silent killer of §6.1 |
| Embedding drift over time | cos between this month's and last month's embedding of a *fixed* probe set | Provider silently changed a hosted model version |

That last one deserves emphasis: a hosted embedding model can change under you. Keep a frozen
100-chunk probe set, re-embed it weekly, and alert if mean cosine to the stored vectors drops. A
provider-side model update is an **index-wide re-embed event**, and you want to learn about it from
a monitor, not from a recall regression three weeks later.

---

## 7. Evaluating the index — and the two different things called "recall"

This section exists because of a naming collision that causes real confusion in real design reviews.

### 7.1 The two recalls

| Name | Ground truth | Question | Typical value |
|---|---|---|---|
| **ANN recall@k** | The exact (brute-force) top-k over the *same* vectors | "How much of the exact nearest-neighbor result did the approximate index return?" | 0.95–0.999 |
| **Retrieval recall@k** | Human relevance labels | "Did the answer-bearing passage make it into the top-k?" | 0.5–0.9 |

They are unrelated quantities measured against different ground truths. ANN recall is a property of
the index; retrieval recall is a property of the whole pipeline. **An index at 0.99 ANN recall
contributes ~1 point of loss to a pipeline whose retrieval recall is 0.72** — which is exactly the
point: you should tune the index until it stops being a variable, then stop thinking about it.

### 7.2 Measuring ANN recall correctly

```python
def ann_recall_at_k(index, vectors, queries, k: int, ef_search: int) -> float:
    """Ground truth is exact search over the SAME vector set — no labels needed.
    This makes ANN recall the cheapest honest measurement in the whole stack."""
    import numpy as np
    hits = 0
    for q in queries:
        exact = set(np.argsort(-(vectors @ q))[:k].tolist())
        approx = set(index.search(q, k=k, ef_search=ef_search))
        hits += len(exact & approx) / k
    return hits / len(queries)
```

Two implementation notes that decide whether the number is meaningful: use **real query vectors**,
not random ones (query distribution matters for graph traversal), and use **at least a few hundred**
of them — ANN recall variance across queries is high and dominated by a hard tail.

### 7.3 The index report card

Never report ANN recall without the latency and cost it bought:

| Metric | Notes |
|---|---|
| ANN recall@k vs `efSearch` | The whole curve, not one point. This is the knob you tune. |
| p50 / p95 / p99 query latency | p99 is the one that shows up as a user complaint. Measure under realistic concurrency, not single-threaded. |
| Build time and peak build memory | Decides whether a re-index is a maintenance window or a Tuesday. |
| Memory / bytes per vector | With quantization, this is the entire cost argument. |
| **Filtered recall** | See below — the single most under-measured index property. |
| Recall under incremental writes | Graph indexes degrade with churn; measure after simulating a month of updates plus deletes. |

**Filtered search deserves its own row and usually its own investigation.** When you constrain a
vector search by metadata (tenant, ACL, date), recall can collapse in a way that is invisible in the
unfiltered benchmark, because the graph traversal keeps landing on filtered-out neighbors. Measure
ANN recall *at your real filter selectivities* — 0.1%, 1%, 10% — not just unfiltered. A pre-filter
strategy that's fine at 10% selectivity can be catastrophic at 0.1%. `03` covers the mechanisms;
this is the measurement that tells you which mechanism you need.

### 7.4 Quantization has a specific eval shape

Quantization (scalar, binary, PQ) trades recall for memory and speed, usually with a rescoring pass.
The evaluation is a **three-column Pareto table**, and the mistake is reporting only the first
column:

| Config | ANN recall@50 | Retrieval recall@10 (labels) | Bytes/vector | p95 latency |
|---|---|---|---|---|

The reason the second column is mandatory: quantization degrades the *ordering* of near-ties, and
near-ties are exactly where a downstream reranker recovers quality. It is entirely normal for binary
quantization + rescoring to drop ANN recall by 3 points and *end-to-end* quality by 0.2 points —
which is a spectacular trade you would have rejected on the first column alone.

---

## 8. Evaluating retrieval: the metric zoo, with formulas

### 8.1 The formulas, stated once

Notation: for query `q`, `R_q` is the set of relevant items (or a grade function `g(d) ∈ {0,1,2,3}`),
and `d_1..d_k` is the returned ranking.

```python
import math

def recall_at_k(ranked, relevant: set, k: int) -> float:
    """Fraction of relevant items that appear in the top k.
    THE metric for candidate generation. Cheap, and it bounds everything downstream."""
    if not relevant:
        return float("nan")          # undefined; see the negative stratum, §10.5
    return len(set(ranked[:k]) & relevant) / len(relevant)

def precision_at_k(ranked, relevant: set, k: int) -> float:
    return len(set(ranked[:k]) & relevant) / k

def success_at_k(ranked, relevant: set, k: int) -> float:
    """Did ANY relevant item make the top k? For single-answer QA this is often the
    metric you actually care about, and it has lower variance than recall."""
    return 1.0 if set(ranked[:k]) & relevant else 0.0

def reciprocal_rank(ranked, relevant: set) -> float:
    """1/rank of the FIRST relevant item. Mean over queries = MRR.
    Only sees the first hit — blind to everything after it."""
    for i, d in enumerate(ranked, start=1):
        if d in relevant:
            return 1.0 / i
    return 0.0

def average_precision(ranked, relevant: set) -> float:
    """Mean of precision@i at each rank i where a relevant item appears.
    Mean over queries = MAP. Rewards packing relevant items early AND finding all of them."""
    if not relevant:
        return float("nan")
    hits, total = 0, 0.0
    for i, d in enumerate(ranked, start=1):
        if d in relevant:
            hits += 1
            total += hits / i
    return total / len(relevant)

def dcg(gains: list[float]) -> float:
    """Standard (Burges) formulation: (2^g - 1) / log2(i+1).
    The linear variant g/log2(i+1) exists and gives different numbers — PICK ONE,
    write it in the manifest, and never mix them across runs."""
    return sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(gains, start=1))

def ndcg_at_k(ranked, grade, k: int) -> float:
    """Position-weighted, grade-aware. The metric for RANKING quality (i.e. rerankers).
    Requires graded labels to be worth anything (§3.2)."""
    gains = [grade(d) for d in ranked[:k]]
    ideal = sorted((grade(d) for d in grade.universe), reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(gains) / idcg if idcg > 0 else 0.0
```

### 8.2 Choosing among them

| Metric | Sees | Blind to | Use when |
|---|---|---|---|
| recall@k | Whether relevant items are in the set | Order entirely | Candidate generation, fusion — anywhere a reranker follows |
| success@k | Whether at least one hit exists | Order; multiple answers | Single-answer QA; lowest-variance headline number |
| precision@k | Density of relevant items | Recall; grades | When context budget is tight and padding is costly |
| MRR@k | Rank of the *first* hit | Everything after the first hit | Single-answer QA where position matters |
| MAP@k | Order + completeness | Grades (binary only) | Multi-answer retrieval with binary labels |
| **nDCG@k** | Order + grades + position discount | Nothing much — it's the general case | Rerankers; final-stage ranking; anything user-facing |

nDCG correlates better with end-to-end RAG quality than binary metrics do, and it's the one to
report if you report one — but only if you have graded labels. **nDCG computed over binary labels is
just a fancy MAP**, and paying its complexity cost without graded labels is a common waste.

### 8.3 The three depths, again

`04` §6 names them; evals must report all three because a quality number without them is not
reproducible:

- `branch_depth` — how many candidates each branch (BM25, dense, learned-sparse) returns
- `fusion_depth` — how many survive fusion and enter the reranker
- `final_k` — how many reach the prompt

Recall@`fusion_depth` is the **ceiling on everything downstream**. If it's 0.78, no reranker and no
generator can exceed 0.78 answer availability. Debug in that order: ceiling first, then ordering,
then generation. A team that spends a month on prompt engineering against a 0.78 ceiling is a common
and entirely avoidable story.

### 8.4 Ablate one stage at a time, with an oracle at the top

The diagnostic ladder that localizes a fault in one eval run:

| Configuration | If quality is high here | Then the fault is… |
|---|---|---|
| **Oracle context** (feed the golden spans directly to the generator) | — | …*not* generation. Move upstream. |
| Oracle context and quality is *low* | — | Generation/prompting. Stop tuning retrieval. |
| Full pipeline, recall@`fusion_depth` low | — | Candidate generation: branches, embedding, chunking, or parsing (walk further up with §4.5) |
| recall@`fusion_depth` fine, nDCG@`final_k` low | — | The reranker or the fusion depth |
| Everything upstream fine, answers still wrong | — | Context assembly (`06`) or generation (`10`) |

The oracle-context run is the cheapest and most informative single experiment in RAG evaluation, and
almost nobody runs it. It takes an hour and permanently ends the "is it retrieval or is it the
prompt?" argument.

### 8.5 Tune the baseline as hard as the challenger

`04` §13.1's rule, worth repeating because it invalidates more published comparisons than anything
else: a default-configured BM25 is not a baseline, it's a strawman. Analyzer choice, `k1`/`b`,
stemming, and stopword handling routinely move BM25 by more than the improvement being claimed for
the new dense retriever. If you spent a week tuning the challenger and an afternoon on the baseline,
the comparison measures your attention allocation.

---

## 9. Evaluating the reranker

### 9.1 What a reranker can and cannot change

A cross-encoder reranker takes the `fusion_depth` candidates and reorders them into `final_k`.
Therefore, **by construction**:

- It **cannot** change recall@`fusion_depth`. Not "usually doesn't" — cannot.
- It **can** change recall@`final_k`, by promoting relevant items into the top-k.
- It **can** change nDCG@`final_k` and MRR@`final_k`. This is its actual job.

Every reranker evaluation should lead with the recall@`final_k` and nDCG@`final_k` deltas at a fixed
`fusion_depth`, and should state `fusion_depth` prominently, because the reranker's value is a
function of it: with `fusion_depth = final_k`, a reranker mathematically cannot improve recall@`final_k`
at all, and a surprising number of "our reranker didn't help" reports are exactly this configuration
error.

### 9.2 The counterfactual metric that explains the delta

Aggregate nDCG tells you *whether* the reranker helped. This tells you *how*:

```python
def rerank_movement(pre_ranked, post_ranked, relevant: set, final_k: int) -> str:
    """Per-query outcome of reranking. Aggregate these into a 4-way breakdown —
    it explains a delta that a mean cannot."""
    pre_hit = bool(set(pre_ranked[:final_k]) & relevant)
    post_hit = bool(set(post_ranked[:final_k]) & relevant)
    if post_hit and not pre_hit:
        return "rescued"     # the reranker earned its latency here
    if pre_hit and not post_hit:
        return "broke"       # it demoted a correct answer out of the window
    if pre_hit and post_hit:
        return "kept"
    return "missed_both"     # upstream problem; the reranker was never in play
```

A healthy reranker on a real corpus shows something like 12% rescued, 3% broke, 55% kept, 30%
missed_both. **The `broke` bucket is the one to read transcripts from** — it usually reveals a
specific systematic failure (numeric passages, tables, short chunks) that a fusion-depth change or a
score-floor fixes.

### 9.3 Binary labels hide reranker value

If your labels are binary and `fusion_depth` is generous, recall@`final_k` may barely move while the
*ordering* improves substantially — and ordering is what determines whether the generator attends to
the right passage. This is the strongest practical argument for graded labels (§3.2): without them,
you have no instrument that can see the thing rerankers do.

### 9.4 Quality per millisecond

A reranker is a latency purchase. Report it as one:

| Config | fusion_depth | nDCG@10 | Δ nDCG | added p95 ms | Δ nDCG per 100ms | $/1k queries |
|---|---|---|---|---|---|---|

The published rules of thumb are wide — reported nDCG@10 lifts commonly cited in the 5–15 point range
for under ~200ms of added latency, with larger gains on lexically hard sets — but the spread on real
systems is enormous, from ~18-point gains to ~2 points for the same 200ms. **That spread is precisely
why this must be measured on your corpus.** Reranker latency scales with candidate count × passage
length × model size, so the `fusion_depth` sweep *is* the latency sweep; run them together, one
table.

Also measure the **cheap alternatives on the same axes** before buying a cross-encoder: raising
`fusion_depth`, fixing the analyzer, or adding the second retrieval branch may buy the same nDCG for
less latency.

---

## 10. Evaluating generation

Retrieval metrics stop at "the right passage was in the prompt." Everything after that is this
section. There are four distinct output failure modes and they need four distinct metrics — a single
"answer quality" score blends them into something undebuggable.

| Failure | Name | Metric |
|---|---|---|
| States things the context doesn't support | Unfaithful / hallucinated | Faithfulness, groundedness (§10.2) |
| Faithful to context, but doesn't answer the question | Irrelevant | Answer relevance (§10.3) |
| Answers, but the answer is wrong vs the world | Incorrect | Answer correctness vs reference (§10.4) |
| Answers when it should have declined | Over-answering | Abstention correctness (§10.5) |

### 10.1 Extractive vs abstractive changes everything

If answers are short and extractive (a date, a name, a clause), you can use **exact match** and
**token F1** and skip most of this section. Cheap, deterministic, zero judge cost. Push your task
toward extractive whenever the product allows; the eval savings are large and permanent.

For abstractive answers, surface-form metrics (BLEU, ROUGE, embedding similarity to a reference) are
weak: they punish correct paraphrases and reward fluent wrongness. They're acceptable as a *cheap
regression tripwire* in CI and unacceptable as a quality claim.

### 10.2 Faithfulness by claim decomposition

The standard construction, from RAGAS onward: decompose the answer into atomic claims, then check
each claim for support in the retrieved context.

```
faithfulness = (# claims supported by the retrieved context) / (# claims in the answer)
```

A score of 0.6 means roughly 40% of the answer's statements have no basis in what was retrieved.
Note precisely what this does *not* measure: **faithfulness is entailment against the context, not
truth.** An answer can be perfectly faithful to a retrieved passage that is itself wrong or stale.
Faithfulness and correctness (§10.4) are orthogonal, and a suite that measures only faithfulness will
happily certify a system that faithfully reproduces bad documents.

Implementation notes that matter more than the formula:

- **Decomposition is itself an LLM call and itself a source of variance.** Two decompositions of the
  same answer can yield 6 and 9 claims. Pin the decomposition prompt and model version in the
  manifest, and treat a decomposition-prompt change as a metric change (§14.3).
- **Use structured output for both steps.** With the Claude API, `output_config.format` with a JSON
  schema removes an entire class of parse failures from your harness — see §11.5.
- **Per-claim output, not just a score.** Store which claims failed. The aggregate tells you there's
  a problem; the claim list tells you it's always the numeric ones.
- **Give the judge an explicit "insufficient information" option** so it isn't forced to guess on
  claims the context neither supports nor contradicts. Anthropic's guidance on model grading is
  direct about this: give the LLM a way out, like an instruction to return "Unknown," to avoid
  hallucinated verdicts.

**Groundedness** is the sentence-level sibling: fraction of answer sentences with a supporting span.
Cheaper, coarser, and a good CI tripwire when full claim decomposition is too slow.

### 10.3 Answer relevance

Does the answer address the question asked? The usual construction inverts the problem: generate `n`
questions *from the answer*, embed them, and measure mean similarity to the original question. It's a
reasonable proxy and it catches the "answered a related question" failure that faithfulness cannot
see. Validate it against human labels like any other judge (§11) — it is one, wearing an embedding
costume.

### 10.4 Answer correctness

Needs reference answers. Three tiers:

| Tier | Method | Cost | Use for |
|---|---|---|---|
| Exact / F1 | String or token overlap with reference | ~0 | Extractive answers, numbers, IDs |
| Rubric-graded | LLM judge with criteria + reference | $ | Abstractive answers |
| Human | Expert grades a sample | $$$ | Calibrating the tier above; final acceptance |

For rubric grading, **build in partial credit**. Anthropic's guidance is explicit: a support agent
that correctly identifies the problem and verifies the customer but fails to process a refund is
meaningfully better than one that fails immediately, and results should represent that continuum.
Binary task grading discards most of your signal precisely where the interesting differences live.

Rubric design that survives contact:

- 3–5 criteria, each independently checkable, each with a worked example.
- Score each criterion **separately**, then combine with fixed weights in *your* code — do not ask
  the model for a holistic 1–10. Holistic scores drift, cluster at 7, and can't be debugged.
- Prefer binary or 3-point per criterion. Narrower scales agree with humans better than 1–10 scales.

### 10.5 Abstention: the metric almost nobody has

For the negative stratum (§3.4), where the corpus genuinely lacks the answer:

```python
def abstention_scores(rows) -> dict:
    """rows: (answerable: bool, abstained: bool) per query.
    Two error types, and they are NOT symmetric in user impact."""
    tp = sum(1 for a, ab in rows if not a and ab)       # correctly declined
    fp = sum(1 for a, ab in rows if a and ab)           # refused an answerable question
    fn = sum(1 for a, ab in rows if not a and not ab)   # FABRICATED. the bad one.
    tn = sum(1 for a, ab in rows if a and not ab)
    return {
        "abstention_precision": tp / max(tp + fp, 1),
        "abstention_recall":    tp / max(tp + fn, 1),   # 1 − hallucination rate on unanswerables
        "over_refusal_rate":    fp / max(fp + tn, 1),   # the cost of tuning too far the other way
    }
```

Both errors are real. Tuning only for "never hallucinate" produces a system that refuses answerable
questions, which users experience as broken. **Report both, gate on both**, and make the tradeoff an
explicit product decision instead of an emergent property of a prompt.

There is a mechanical detail specific to current Claude models: a request can come back with HTTP 200
and `stop_reason: "refusal"` when safety classifiers decline it — this is a *third* outcome distinct
from "answered" and "abstained," and an eval harness that reads `response.content[0]` unconditionally
will crash on it. Check `stop_reason` before reading content, and count refusals as their own
category rather than silently scoring them as failures.

### 10.6 Citations

If the product cites sources, citations are a first-class output and need first-class metrics:

```
citation_precision = cited spans that actually support the claim / cited spans
citation_recall    = claims with a correct citation / claims requiring one
citation_validity  = citations resolving to a real, retrievable span / citations   # the "made up
                                                                                   #  a page number" check
```

`citation_validity` is a *deterministic* check — no judge needed — and it belongs in CI as a hard
gate. A system that invents citation targets is worse than one that omits citations, because it
manufactures unearned trust.

### 10.7 Structural and deterministic checks — run these first

Cheap, deterministic, no judge:

| Check | Gate |
|---|---|
| JSON schema validity | 100% when using constrained output; any failure is a bug |
| Required fields present, enums in range | 100% |
| Answer length distribution | Alert on shift — a silent verbosity change is a real regression, and a cost one |
| Forbidden content (PII patterns, internal hostnames, prompt leakage markers) | 0 occurrences |
| Latency p50/p95, tokens in/out | Budget-gated (§16) |
| Refusal rate by stratum | Alert on shift |

Anthropic's ordering advice generalizes cleanly: **deterministic graders where possible, LLM graders
where necessary, human graders judiciously for validation.** Every check you can make deterministic
is one whose result you never have to defend.

### 10.8 Nondeterminism is now structural — measure it

A note that changes how §13 must be run: on current Claude models (Opus 5, Sonnet 5, Fable 5, Opus
4.8/4.7), `temperature`, `top_p`, and `top_k` **are rejected with a 400** — the parameters were
removed. The old `temperature=0` reproducibility ritual is not available, and it never actually
guaranteed identical outputs anyway.

The consequence for evaluation is that **generation variance is a property you measure rather than
suppress**:

- Run each eval item `n` times (3–5 for CI, more for release decisions).
- Report `pass@1` averaged over trials **and** `pass^k` (all `k` trials pass) — the second is the one
  that predicts user experience for a task run once.
- Track per-item variance. A high-variance item is either genuinely borderline or badly specified;
  read its transcripts before trusting its contribution to the mean.
- The pinned things are now: model ID, `effort`, thinking mode, prompt bytes, tool set, and the
  dataset. Pin those in the manifest and accept residual sampling noise as measurement error, which
  is what §13's intervals are for.

---

## 11. LLM-as-judge, treated as the classifier it is

An LLM judge is a classifier you deployed without measuring. Everything in this section follows from
taking that sentence literally.

### 11.1 The validation protocol

You would not ship a relevance classifier without a labeled test set and a precision/recall number.
Do the same here:

1. **Build a calibration set**: 100–200 items, human-labeled, deliberately including the hard and
   ambiguous cases — the judge's behavior on easy items is not informative.
2. **Run the judge.** Compute agreement with humans: Cohen's κ, plus **per-class** precision/recall.
   Aggregate accuracy hides the failure that matters: a judge with 90% accuracy that never flags the
   12% of genuinely bad answers is worthless *precisely* on the class you built it for.
3. **Iterate the rubric, not the model.** Most judge failures are underspecified criteria. Add
   examples for the classes it gets wrong.
4. **Gate:** κ ≥ 0.7 against humans before the judge's output is allowed to gate anything. Some
   practitioners push for ≥0.8; below 0.6 you are gating on noise.
5. **Re-validate on every change** to judge model, judge prompt, or rubric. See §11.6.
6. **Keep a standing human sample**: 5–10% of judged items get human review forever, so κ drift is
   observable rather than assumed.

```python
def judge_report(human: list[int], judge: list[int]) -> dict:
    """Never report only accuracy. Per-class recall on the FAIL class is the number
    that decides whether the judge can gate anything."""
    from collections import Counter
    n = len(human)
    acc = sum(h == j for h, j in zip(human, judge)) / n
    classes = sorted(set(human) | set(judge))
    per = {}
    for c in classes:
        tp = sum(1 for h, j in zip(human, judge) if h == c and j == c)
        fp = sum(1 for h, j in zip(human, judge) if h != c and j == c)
        fn = sum(1 for h, j in zip(human, judge) if h == c and j != c)
        per[c] = {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1)}
    return {"accuracy": acc, "kappa": cohens_kappa(human, judge), "per_class": per,
            "judge_dist": Counter(judge), "human_dist": Counter(human)}
```

That last field pair — the two label distributions — catches the most common judge pathology in one
glance: the judge assigning 4/5 to 80% of items when humans assign it to 35%.

### 11.2 The known biases, and the mitigation for each

These are documented across the 2023–2026 literature and are properties of the method, not of any
one model:

| Bias | Mechanism | Mitigation |
|---|---|---|
| **Position** | In pairwise comparison, the response in a given slot is preferred regardless of content | **Swap and re-run.** Score = mean over both orders. Report the disagreement rate between orders as a bias diagnostic — it's your judge's noise floor. |
| **Verbosity / length** | Longer, more fluent answers score higher | Include length-matched pairs in calibration; add an explicit rubric line that length is not a criterion; regress score on length and check the slope |
| **Self-preference** | A judge prefers text produced by itself or its own family | **Use a judge from a different family than the generator** where feasible; at minimum, quantify by scoring a fixed answer set with two judge families and comparing |
| **Leniency / score compression** | Everything lands at 4/5 on a 1–10 scale | Narrower scales (binary or 3-point); anchor examples per level; forced criterion-by-criterion scoring |
| **Fluent-hallucination blindness** | Confident, well-written wrongness is under-penalized | Give the judge the context and require span-level citation for its verdict |
| **Prompt-position sensitivity** | Where the rubric sits relative to the content matters | Fix the template exactly; treat template edits as metric changes |

**The single highest-value mitigation is position swapping in pairwise judging.** It's a 2× cost
increase and it converts an unquantified bias into a measured one.

### 11.3 Pointwise vs pairwise

| | Pointwise (score this answer) | Pairwise (which is better?) |
|---|---|---|
| Cost | 1 call | 2 calls (with swap) |
| Absolute level | Yes | No — only relative |
| Agreement with humans | Lower | Higher |
| Position bias | N/A | Present; mitigable by swapping |
| Comparable across runs | Yes, if the rubric is frozen | Only within the compared pair |
| Best for | Dashboards, absolute gates, faithfulness | A/B decisions, model comparisons |

For **regression gating**, pairwise-with-swap against the current production output is usually the
better instrument: it's what you actually want to know, and it sidesteps the score-drift problem
where a frozen rubric slowly re-anchors.

### 11.4 Rubric design

- One criterion per question. Composite criteria ("accurate and well-organized") produce
  uninterpretable scores.
- Binary or 3-point per criterion. Combine in your code.
- **Worked example per level**, taken from your data.
- Require a short justification *before* the verdict — this measurably improves judge accuracy and,
  more importantly, gives you something to read when you audit disagreements.
- Provide an explicit escape hatch ("Unknown" / "insufficient information") so the judge isn't forced
  to fabricate a verdict.
- **Ask for structured output.** With the Claude API, `output_config: {"format": {...json schema...}}`
  (or `client.messages.parse()` with a Pydantic model) makes the verdict machine-readable by
  construction and eliminates prose-parsing failures from your harness. Do not prefill an assistant
  turn to force JSON shape — that returns a 400 on current models; structured outputs are the
  replacement.

### 11.5 A judge call, concretely

```python
import anthropic
from pydantic import BaseModel
from typing import Literal

client = anthropic.Anthropic()

class Verdict(BaseModel):
    justification: str                                    # BEFORE the verdict, deliberately
    supported: Literal["yes", "no", "insufficient_context"]
    supporting_quote: str | None                          # must be verbatim from the context

RUBRIC = """<rubric>…frozen text, versioned by sha…</rubric>"""   # stable prefix -> cacheable

def judge_claim(claim: str, context: str) -> Verdict:
    resp = client.messages.parse(
        model="claude-opus-5",
        max_tokens=1024,
        system=[{"type": "text", "text": RUBRIC,
                 "cache_control": {"type": "ephemeral"}}],   # cache the rubric, not the item
        messages=[{"role": "user",
                   "content": f"<context>{context}</context>\n<claim>{claim}</claim>"}],
        output_format=Verdict,
    )
    if resp.stop_reason == "refusal":                      # a real, distinct outcome
        raise JudgeRefused(resp.stop_details)
    v = resp.parsed_output
    if v.supported == "yes" and v.supporting_quote not in context:
        v.supported = "insufficient_context"               # deterministic backstop on the judge
    return v
```

Three things in that snippet are the actual engineering:

1. **The rubric is the cached prefix, the item is the suffix.** Prompt caching is a prefix match, so
   the frozen rubric caches across every item in the run and cache reads cost roughly 0.1× of input.
   Over a 5,000-item eval this is the difference between a $40 run and a $12 one. Watch the minimum
   cacheable prefix — it's **512 tokens on Opus 5**, 1024 on Sonnet 5, and **4096 on Haiku 4.5**, so
   a short rubric silently won't cache on the cheap judge, which is exactly the case where you
   expected the savings.
2. **The verbatim-quote backstop** turns an unverifiable judge assertion into a checkable one. Any
   judge output you can validate deterministically, validate deterministically.
3. **`stop_reason` is checked before content.** Eval harnesses run adversarial and edge-case inputs
   by design; refusals will happen and must be a counted outcome, not an exception trace.

### 11.6 Pin the judge, or you have no time series

A judge is part of the measurement apparatus. When it changes, historical numbers become
incomparable — the same way swapping a thermometer mid-experiment does.

- **Pin the model ID and the rubric SHA in every result row.** Non-negotiable.
- On a judge change, **re-score a frozen archive of past outputs** with the new judge and publish the
  offset. Then, and only then, switch the dashboard over.
- Treat effort/thinking settings as part of the judge identity too — on Opus 5, thinking is on by
  default and `effort` materially changes both cost and verdict distribution.

### 11.7 Judge cost model and the Batch API

The economics decide whether the eval runs on every commit or once a sprint, so they're a design
input, not an afterthought.

Sticker prices per million tokens (Anthropic first-party, from the model table cached 2026-06-24 —
re-check before quoting):

| Model | Input | Output |
|---|---|---|
| Claude Opus 5 | $5.00 | $25.00 |
| Claude Sonnet 5 | $3.00 | $15.00 |
| Claude Haiku 4.5 | $1.00 | $5.00 |

Three multipliers stack on top, and together they're worth an order of magnitude:

- **Batch API: 50% off** all token usage. Eval runs are the canonical batch workload — not
  latency-sensitive, embarrassingly parallel, up to 100k requests per batch, most complete within an
  hour (24h max). Nightly and release evals should be batch by default; only the fast CI tier needs
  synchronous calls. Note batches don't accept the `fallbacks` parameter, so handle refusals in your
  own result-merging code.
- **Prompt caching:** ~0.1× on cached reads against a ~1.25× write premium (5-minute TTL), so a
  shared rubric prefix pays for itself from the second item onward.
- **Judge tiering:** run the cheap judge on everything, escalate only disagreement-prone or
  fail-adjacent items to the expensive one. Validate *each tier* separately against humans (§11.1) —
  a tiering scheme where the cheap tier was never calibrated is just a cheaper way to be wrong.

```python
def eval_run_cost(n_items, in_tok, out_tok, price_in, price_out,
                  batch=True, cache_hit_frac=0.0, cached_frac_of_input=0.0):
    """Napkin cost for one judged eval run. Put this in the harness and print it
    every run — a cost you can see is a cost you can argue about."""
    eff_in = in_tok * ((1 - cached_frac_of_input)
                       + cached_frac_of_input * (0.1 * cache_hit_frac + 1.25 * (1 - cache_hit_frac)))
    cost = n_items * (eff_in / 1e6 * price_in + out_tok / 1e6 * price_out)
    return cost * (0.5 if batch else 1.0)

# 5,000 claims, 1,800 input tokens each (1,400 of it a shared rubric), 150 output tokens.
print(eval_run_cost(5000, 1800, 150, 5.00, 25.00,          # Opus 5, sync, no caching
                    batch=False))                            # ~$63.75
print(eval_run_cost(5000, 1800, 150, 5.00, 25.00,          # Opus 5, batch + cached rubric
                    batch=True, cache_hit_frac=0.99, cached_frac_of_input=0.78))  # ~$16.28
print(eval_run_cost(5000, 1800, 150, 1.00, 5.00,           # Haiku 4.5, batch
                    batch=True, cache_hit_frac=0.0, cached_frac_of_input=0.0))    # ~$6.37
```

The numbers matter less than the shape: **the same eval is ~10× cheaper or more expensive depending
on three configuration choices that have nothing to do with quality.** Teams that run evals rarely
usually believe evals are expensive because they measured the un-optimized version once.

---

## 12. Agentic and multi-hop evaluation

`14-agent-evaluation.md` goes deep. This section establishes what carries over from the single-shot
case and what genuinely changes.

### 12.1 What changes

Agents call tools over many turns, modify state, and adapt to intermediate results — so mistakes
propagate and compound, and a single scalar at the end tells you almost nothing about where things
went wrong. Three grading layers:

| Layer | Grades | Example |
|---|---|---|
| **Outcome** | The final state of the world | Ticket resolved; file compiles; correct row written |
| **Trajectory** | The sequence of steps | Correct tools, correct arguments, dependency order satisfied |
| **Per-turn** | Individual messages/decisions | Did this turn's tool choice make sense given what was known? |

### 12.2 Grade the product, not the path — mostly

The instinct is to assert a specific tool-call sequence. Anthropic's guidance is that this is too
rigid and produces brittle tests, because agents regularly find valid approaches the eval designer
didn't anticipate — better to **grade what the agent produced, not the path it took**. Their example
is unusually clean: Opus 4.5 solved a τ²-bench flight-booking task by discovering a genuine loophole
in the policy; it "failed" the eval as written while actually finding a better solution.

The nuance for RAG specifically: **trajectory metrics remain valuable as diagnostics, just not as
gates.** Tool-call correctness, retrieval-call count, and redundant-query rate are exactly what you
read when the outcome metric drops, and they should be recorded on every run — recorded, aggregated,
charted, and *not* used as pass/fail criteria.

### 12.3 The metric set

| Metric | Definition | Notes |
|---|---|---|
| Task success | Verified end state | Prefer programmatic verification over judged |
| Partial credit | Weighted sub-goals | Essential; binary success discards most of the signal |
| Tool-call correctness | Right tool + right arguments | Diagnostic layer |
| Step count / loop count | Turns to completion | Detects thrash; strongly correlated with cost |
| **Cost per resolved task** | $ ÷ successes | The number that decides deployment. Not cost per run. |
| p95 wall-clock | | Long-horizon agent turns can run minutes; this is a UX constraint |
| **Silent-failure rate** | Confidently reports success while the end state is wrong | The worst failure mode; only detectable by verifying end state independently of the agent's own claim |
| Retrieval sufficiency | Fraction of hops whose retrieval actually contained the needed span | Localizes multi-hop failures to a specific hop |

Cost per *resolved* task is the one to lead with. A cheap agent that fails 40% of the time is more
expensive than a costly one that fails 5%, because the failures cost a human.

### 12.4 Environment isolation is a correctness requirement

Every trial must start from a clean environment. Shared state between runs — leftover files, cached
data, resource exhaustion — causes correlated failures that reflect infrastructure flakiness rather
than agent quality, and it can also *inflate* performance: Anthropic reports observing Claude gain an
unfair advantage on internal evals by reading git history from previous trials. Correlated trials
break the independence assumption that every statistic in §13 depends on, so this isn't hygiene,
it's a prerequisite for the numbers meaning anything.

### 12.5 Read the transcripts

The one practice that separates teams whose evals work from teams whose evals mislead them: **read
eval transcripts regularly.** When a task fails, the transcript tells you whether the agent made a
genuine mistake or whether the grader rejected a valid solution. Failures should look *fair* — it
should be clear what the agent got wrong and why. If a score isn't moving, you need confidence that
it's the agent and not the eval, and transcript review is the only way to get it.

Budget it: 30 minutes per week, 10 failures sampled across strata. It will pay for itself in the
first month by catching a broken grader.

---

## 13. Statistics — making a delta mean something

The whole point of this section is to be able to write the sentence in §1.2.

### 13.1 Pair everything

Both systems run on the **same queries**. Therefore analyze *per-query differences*, not the two
means. This is not a refinement; it's what makes small eval sets viable at all, because it removes
query difficulty — the dominant variance component — from the comparison.

Keep the per-item scores. Always. A results table that stores only the mean has thrown away the
ability to compute any interval, run any test, or stratify after the fact. The results schema in
§14.4 stores one row per (run, item).

### 13.2 Which test

The classic IR study of this question (Smucker, Allan & Carterette, CIKM 2007) compared tests over
TREC runs and found: **randomization (permutation), bootstrap, and the paired t-test agree closely
in practice**, while the **Wilcoxon signed-rank and sign tests both detect significance poorly and
can produce false detections**. That result is 19 years old and has held up.

Practical recommendation: **paired bootstrap** as the default. It gives you a confidence interval
(which is what you actually want) rather than only a p-value, it makes no distributional assumption,
and it handles the bounded, spiky, zero-inflated per-query distributions that retrieval metrics
produce.

```python
import numpy as np

def paired_bootstrap(a: np.ndarray, b: np.ndarray, n_boot: int = 10_000, seed: int = 0):
    """a, b: per-query scores for the two systems, SAME queries, SAME order.
    Returns the observed delta, a 95% CI, and a two-sided p-value."""
    rng = np.random.default_rng(seed)
    d = b - a
    n = len(d)
    obs = d.mean()
    idx = rng.integers(0, n, size=(n_boot, n))
    boots = d[idx].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    # p-value by shifting the bootstrap distribution to the null
    centered = boots - obs
    p = (np.abs(centered) >= abs(obs)).mean()
    return {"delta": obs, "ci95": (lo, hi), "p": p, "n": n,
            "wins": int((d > 0).sum()), "losses": int((d < 0).sum()),
            "ties": int((d == 0).sum())}
```

Report `wins / losses / ties` alongside the delta. A +0.02 mean built from 3 wins and 0 losses is a
different and much less trustworthy fact than one built from 40 wins and 32 losses, and the mean
alone cannot distinguish them.

### 13.3 Multiple comparisons

Sweeping ten chunk sizes against one baseline at α=0.05 gives you roughly a 40% chance of at least
one spurious "significant" result. The IR literature is explicit that testing several systems
simultaneously inflates error rates, and most published test comparisons only validate the two-system
case.

Practical handling:

- **Sweeps are exploratory. Say so.** Report the whole curve with intervals and don't decorate any
  point with a p-value.
- When you must test `m` hypotheses, apply a correction (Bonferroni α/m is crude and fine;
  Benjamini–Hochberg if `m` is large).
- **Confirm the winner on a held-out query slice.** This is more convincing than any correction and
  is the standard that actually protects you from sweep-driven overfitting to the eval set.

### 13.4 Sources of variance, and which ones you can kill

| Source | Kill it? | How |
|---|---|---|
| Query sampling | No — it's the thing you're estimating over | Bootstrap it |
| Generation sampling | No (sampling params removed on current models, §10.8) | Repeat runs; report `pass^k`; include in the interval |
| Judge variance | Partly | Pin model + rubric; repeat judge calls on a sample to quantify |
| ANN nondeterminism | Yes | Fixed seed, fixed `efSearch`, fixed build |
| Environment state | Yes | Clean environment per trial (§12.4) |
| Corpus drift | Yes | Pinned corpus digest |
| Concurrency-dependent latency | Yes | Fixed load generator, fixed concurrency, warm-up excluded |

The ones you can kill, kill. The ones you can't, put in the interval. What's not allowed is leaving
them uncontrolled *and* unquantified, which is the default state of most eval harnesses.

### 13.5 Effect size, and what "significant" doesn't mean

A statistically significant +0.004 nDCG on 5,000 queries is real and irrelevant. Define, in advance,
a **minimum meaningful effect** grounded in product impact — "a change worth shipping moves recall@10
by ≥0.02 or nDCG@10 by ≥0.01" — and evaluate every result against both bars: is it significant, and
is it big enough to care? Publishing the pre-registered bar also removes a large source of
post-hoc rationalization.

### 13.6 Saturation

An eval at 100% tracks regressions and provides no signal for improvement. Near saturation, large
capability improvements appear as small score increases, which makes results deceptive — the field
has watched this happen on benchmark after benchmark. Monitor **headroom** (`1 − score`) per stratum
and retire saturated strata to a fast smoke tier while building harder replacements.

---

## 14. From metrics to gates

`09` owns the pipeline. This section owns the policy: what to gate on, and at what threshold.

### 14.1 Three tiers

| Tier | Trigger | Runtime | Content | Gates? |
|---|---|---|---|---|
| **Smoke** | Every commit | <2 min | 20–30 items, deterministic checks only (schema, citation validity, parse gates), no LLM judge | Hard fail |
| **Regression** | Every PR touching prompts, retrieval config, models, chunking, or parser | 10–30 min | Full golden set, retrieval metrics, cheap judge, batch API | Fail on significant regression |
| **Release / nightly** | Nightly + pre-release | Hours | Everything, expensive judge, `n=5` repeats, all strata, agent trajectories | Blocks release; reviewed by a human |

The most common CI mistake is a pipeline that doesn't trigger on **prompt and config files** — those
are the most frequent source of silent regressions and the least likely to be under test. Every
system-prompt edit, few-shot example change, and retrieval-config change must trigger the regression
tier.

### 14.2 Gate on the regression, not the absolute

An absolute threshold ("recall@10 must exceed 0.75") breaks the moment you add hard queries to the
golden set, and teams respond by weakening the eval. Gate relatively instead:

```python
def gate(baseline_scores, candidate_scores, *, min_meaningful=0.02, alpha=0.05):
    """Fail only on a regression that is BOTH statistically credible and large enough
    to matter. Do not fail the build on a single noisy run."""
    r = paired_bootstrap(np.asarray(baseline_scores), np.asarray(candidate_scores))
    if r["ci95"][1] < 0:                      # entire CI below zero: credible regression
        if abs(r["delta"]) >= min_meaningful:
            return "FAIL", r
        return "WARN", r                      # real but small — surface it, don't block
    return "PASS", r
```

Additional hard gates that are *not* statistical, because they're bugs rather than quality:

- Any schema-validity failure.
- Any invalid citation (§10.6).
- Any parse-gate failure on the fixture corpus (§4.3).
- Cost per query above budget (§16) — a 3× cost regression is a regression.
- p95 latency above the SLO.

### 14.3 Version the metric definition itself

A metric is code, and code changes silently invalidate time series. Version and record:

- The metric implementation SHA (which DCG variant? which hit rule?).
- The judge model ID + rubric SHA (§11.6).
- The dataset version + corpus digest.
- The full pipeline config: parser, chunker, embedder, index params, depths, reranker, prompt SHA.

When any of these changes, **re-baseline**: re-run the previous configuration under the new
definition and publish the offset before comparing anything across the boundary. Undocumented
metric-definition changes are how a dashboard becomes a fiction that everyone still trusts.

### 14.4 Results storage

Results are analytical data with a natural star schema; store them accordingly. DuckDB in-process is
the right default (`../databases/21-in-process-olap-duckdb-chdb.md`) — the whole result history fits
in a Parquet file, queries are instant, and there's no service to run.

```sql
-- One row per (run, item). Never store only aggregates: the per-item rows are what
-- make paired tests, stratification, and after-the-fact re-analysis possible.
CREATE TABLE eval_results (
  run_id            VARCHAR,      -- ULID
  ts                TIMESTAMP,
  git_sha           VARCHAR,
  dataset_version   VARCHAR,
  corpus_digest     VARCHAR,
  config_sha        VARCHAR,      -- hash of the full pipeline config blob
  judge_model       VARCHAR,      -- null for deterministic metrics
  rubric_sha        VARCHAR,
  metric_impl_sha   VARCHAR,
  query_id          VARCHAR,
  stratum           VARCHAR,
  trial             INTEGER,      -- for n>1 repeats (§10.8)
  metric            VARCHAR,      -- 'recall@10' | 'ndcg@10' | 'faithfulness' | ...
  value             DOUBLE,
  judged_fraction   DOUBLE,       -- §3.6 health signal
  latency_ms        INTEGER,
  tokens_in         INTEGER,
  tokens_out        INTEGER,
  cost_usd          DOUBLE,
  trace_id          VARCHAR       -- joins to OTEL spans (`10`); this is the debug path
);

-- The regression query the CI gate actually runs.
SELECT metric, stratum,
       avg(cand.value) - avg(base.value)               AS delta,
       count(*)                                        AS n
FROM eval_results base
JOIN eval_results cand USING (query_id, metric, stratum, trial)
WHERE base.run_id = ? AND cand.run_id = ?
GROUP BY metric, stratum
ORDER BY delta;
```

The `trace_id` column is what turns a bad score into a debuggable one: click the failing row, get
the spans for that exact request (`10`). Without it, every eval failure investigation starts with
"can I reproduce this?"

---

## 15. Online evaluation and the label flywheel

### 15.1 The offline–online gap is a measurement, not a mystery

Your offline set says +4 points; production says users are unhappier. Both can be true, and the
causes are enumerable: distribution mismatch (§3.4), labels encoding an outdated notion of relevance,
metric–utility mismatch (nDCG improved, answer length doubled and users stopped reading), or a
latency regression that the quality metric can't see.

**Track the gap explicitly**: for each release, record the offline predicted delta and the online
observed delta. After a handful of releases you have a calibration curve for your own eval suite,
which is more valuable than any individual number it produces.

### 15.2 Production signals worth instrumenting

| Signal | Proxy for | Caveats |
|---|---|---|
| Explicit feedback (👍/👎) | Satisfaction | Low volume, heavily biased to failures |
| Follow-up rephrase rate | Retrieval failure | Strong, cheap signal — a rephrase within 60s usually means the first answer missed |
| Copy / cite / click-through | Usefulness | Only if the UI supports it |
| Escalation to human | Task failure | The best signal in support workflows; low volume |
| Session abandonment | Dissatisfaction | Noisy |
| Retrieved-context-empty rate | Retrieval breakage | Deterministic, alertable, and always worth an alarm |
| Refusal / abstention rate by stratum | Over- or under-refusal drift | Deterministic |
| Judge-on-sample | Quality | Sample 1–5% of prod traffic through the offline judge; this is the only online *quality* number you get |

That last row is the important one: a validated judge (§11) can score a sample of live traffic
continuously, giving a quality time series with no labels and no users interrupted.

### 15.3 Shadow, canary, A/B

- **Shadow:** run the new pipeline on live traffic, serve the old one, diff the outputs. Catches
  crashes, latency, cost, and gross behavior changes with zero user risk. Cheap and underused.
- **Canary:** small traffic share, automated rollback on metric breach.
- **A/B:** the only instrument that measures actual user outcome — and it's slow (days to weeks to
  significance), needs traffic, and can only test what you've already built. Reserve it for changes
  where the offline metric and user value are known to diverge.

### 15.4 The flywheel

This is how a golden set stays alive rather than fossilizing:

```
production traffic
   → sample (stratified: include 👎, rephrases, escalations, empty-retrieval, high-latency)
   → human label (spans + graded relevance + reference answer)
   → add to golden set, tagged with source + date
   → measure the new items separately for a release or two (they're harder than the old ones)
   → merge into the regression tier; retire saturated items to smoke
```

Two disciplines make it work: **sample from failures deliberately** (uniform sampling of production
traffic mostly adds easy queries that every configuration already passes and only raise your
aggregate), and **date-tag every item** so you can measure whether performance on 2026-Q3 items
differs from 2026-Q1 items — which is drift detection for free.

---

## 16. Cost model for the eval layer itself

The eval layer has a budget and it competes with the thing it measures. Make it explicit, or it gets
cut in the first cost review.

| Cost | Driver | Control |
|---|---|---|
| Judge tokens | items × trials × (in+out) tokens | Batch API (−50%), prompt caching (~0.1× on the cached prefix), judge tiering, deterministic checks first |
| Embedding for eval | corpus re-embed per config swept | Cache embeddings keyed by (model, chunker_version, chunk_hash) — the single biggest saving in a sweep |
| Index builds | configs × corpus size | Build once per config, evaluate many query sets against it |
| Human labeling | items × minutes × rate | Front-load into rubric quality (§3.7); label spans once and derive everything |
| CI compute | runs/day × runtime | Tiering (§14.1) |
| Storage | rows × runs | Parquet + DuckDB; retain per-item rows, they're tiny |

```python
def eval_layer_monthly(commits_per_day=20, pr_runs_per_day=8, nightly=1,
                       smoke_cost=0.0, regression_cost=6.0, nightly_cost=45.0,
                       label_hours_per_month=6, label_rate=60):
    """Order-of-magnitude, so the eval budget is a line item instead of a surprise."""
    compute = 30 * (commits_per_day * smoke_cost
                    + pr_runs_per_day * regression_cost
                    + nightly * nightly_cost)
    human = label_hours_per_month * label_rate
    return {"compute_usd": compute, "human_usd": human, "total": compute + human}

print(eval_layer_monthly())   # {'compute_usd': 2790.0, 'human_usd': 360, 'total': 3150.0}
```

The comparison that justifies it: one retrieval regression reaching production and taking a week to
diagnose costs more than a year of the above. That's the argument to make, with your own numbers in
it.

---

## 17. Anti-patterns

| Anti-pattern | Why it's wrong | Do instead |
|---|---|---|
| **Labels stored as chunk IDs** | Invalidated by any chunking change; the comparison you most want to run is the one it forbids | Character spans in the source (§3.1) |
| **Comparing chunkings at fixed k** | Bigger chunks win by being handed more context | Fixed token budget (§5.1) |
| **Recall-only chunking evaluation** | Recall spread across strategies is ~6 points; token efficiency spread is ~7× | Report Precision_Ω and IoU too (§5.3) |
| **Reporting a mean with no interval** | Per-query SDs of 25–40 make means unstable at these n | Paired bootstrap CI (§13.2) |
| **Wilcoxon / sign test** | Documented poor power and false detections in IR evaluation | Paired bootstrap or permutation (§13.2) |
| **Expecting a reranker to raise recall@fusion_depth** | Mathematically impossible | Measure recall@final_k and nDCG@final_k (§9.1) |
| **Unvalidated LLM judge** | An unmeasured classifier gating your releases | κ ≥ 0.7 against humans before it gates (§11.1) |
| **Judge from the same family as the generator** | Self-preference bias | Cross-family judge, or quantify the bias (§11.2) |
| **1–10 holistic judge scores** | Compress to 7, drift, undebuggable | Per-criterion binary/3-point, combined in code (§11.4) |
| **Changing the judge model without re-baselining** | Silently rewrites history | Re-score the archive, publish the offset (§11.6) |
| **No negative/unanswerable stratum** | Blind to the most user-visible failure | §3.4, §10.5 |
| **Growing labels only from the current system's top-k** | Pooling bias penalizes exactly the improvements you want | Pool across systems; track judged fraction (§3.6) |
| **Gating on a single noisy run** | Flaky CI teaches people to ignore CI | Gate on significance + effect size (§14.2) |
| **Asserting exact tool-call sequences** | Brittle; punishes valid alternative solutions | Grade the end state; keep trajectory as diagnostic (§12.2) |
| **Shared state between agent trials** | Correlated failures; can also inflate scores | Clean environment per trial (§12.4) |
| **Trusting MTEB rank for your domain** | Contamination + saturation + distribution mismatch | Shortlist from it; decide on your corpus (§6.2) |
| **Never reading transcripts** | You can't tell agent failures from grader failures | 30 min/week, 10 sampled failures (§12.5) |
| **Eval set that only grows** | Saturates; large gains become invisible | Retire saturated strata; add harder ones (§13.6) |
| **No cost/latency column** | Quality-only decisions ship unaffordable systems | Every report card carries cost and p95 (§9.4, §16) |

---

## 18. Mental models — the compressed set

1. **An eval is an instrument with its own error bar.** Before trusting a number, ask what the
   instrument's noise floor is. If you can't answer, the number is decorative.
2. **Label invariance is the master rule.** A label can only compare configurations of stage S if
   it's defined independently of S. Spans, not chunk IDs. Quoted text, not parsed offsets.
3. **Measure each stage with a metric that stage can move.** Rerankers can't change recall at fusion
   depth. Faithfulness can't detect a stale corpus. A metric a stage cannot move manufactures
   confident nulls.
4. **Pair everything.** Query difficulty is the dominant variance component, and pairing removes it.
   That's what makes a 250-query set usable.
5. **The parser sets the ceiling, and the ceiling is directly measurable.** Answer-span survival
   costs an afternoon and reorders roadmaps.
6. **Recall is a ceiling; nDCG is an ordering; faithfulness is an entailment; correctness is a fact
   about the world.** Four different questions. One "quality score" answers none of them.
7. **Composition is a free parameter that can flip your conclusion.** Stratify, report per stratum,
   and never trust an unstratified aggregate you didn't design.
8. **The unanswerable stratum is the one everyone omits and the one users notice.**
9. **An LLM judge is a classifier.** Validate it, pin it, re-baseline it when it changes, and give
   it a way to say "I don't know."
10. **Deterministic first, judged second, human third.** Every check you can make deterministic is a
    check you never have to defend.
11. **Two things named recall live in this stack.** ANN recall (index vs exact search) and retrieval
    recall (pipeline vs labels). Tune the first until it stops being a variable, then stop thinking
    about it.
12. **Read the transcripts.** It's the only way to tell an agent failure from a grader failure, and
    scores that don't move are usually the eval's fault.
13. **Saturation makes progress invisible.** Track headroom per stratum, retire what's saturated.
14. **The eval's cost is a design parameter.** Batch + caching + tiering is a ~10× swing that has
    nothing to do with quality, and it decides whether the eval runs on every commit.
15. **Offline eval predicts; online eval confirms.** Track the gap between them as its own metric —
    it's the calibration curve for your entire suite.

---

## 19. Lab exercises

**Lab 1 — Answer-span survival across parsers.**
*Goal:* measure the ceiling directly, and find out whether parsing is your bottleneck before
spending a week downstream.
*Steps:* take the `labs/golden-set/` answer spans. Parse the corpus with two or three parsers
(`appendix-d` §2 for candidates). Implement §4.5's `span_survival` using the same normalizer the
pipeline uses. Report exact / exact-or-fuzzy / lost, **stratified** by text vs table vs multi-column
pages. Then compute §4.3's proxy distributions per parser and check whether any proxy predicts span
loss.
*Artifact:* a parser × stratum survival table, plus a scatter of `table_cell_density` vs table-span
survival.
*Success criterion:* you can state, with a number, the maximum recall your pipeline could achieve
even with a perfect retriever — and you know which stratum is costing you it.
*Time:* ~4 hours.
*Unblocks:* everything; this is the ceiling for `05`–`09`.

**Lab 2 — Token-level chunking evaluation, reproduced.**
*Goal:* build the metrics of §5.2 and reproduce the *shape* of the Chroma result on your own corpus.
*Steps:* implement token-level recall, precision, IoU, and Precision_Ω over positional token
identities (not vocabulary IDs — the code comment in §5.2 explains the bug). Sweep 4–6 chunking
configs (fixed 200/400/800 with and without overlap; structure-aware; semantic). Evaluate twice: at
fixed `k=5` and at a fixed token budget, using §5.1's `retrieve_at_budget`.
*Artifact:* two tables (fixed-k, fixed-budget) with all four metrics and per-query SDs, plus a
paragraph on whether the two tables rank the configs differently.
*Success criterion:* your recall spread across configs is small and your Precision_Ω spread is large
— the qualitative finding reproduces — **or** it doesn't, and you can say why your corpus differs.
*Time:* ~6 hours.
*Unblocks:* `02`'s open chunking question; §5.4's report card.

**Lab 3 — Power analysis on your own golden set.**
*Goal:* stop guessing at n.
*Steps:* run two genuinely different retrieval configs on your set. Compute the per-query difference
vector, `σ_d`, and the MDE at your current n using §3.3. Then compute the n needed for a 0.02 effect.
Bootstrap the delta and report the CI. Finally, subsample to n=30, 60, 120 and plot how the CI width
shrinks.
*Artifact:* a `σ_d` value, an MDE, an n-for-0.02 target, and the CI-width-vs-n curve.
*Success criterion:* you can state the smallest effect your current suite can detect, and you have a
concrete number for how much bigger the set needs to be for the next milestone.
*Time:* ~2 hours.
*Unblocks:* every "did that help?" question for the rest of the project.

**Lab 4 — Calibrate an LLM judge.**
*Goal:* produce a judge you are allowed to gate on.
*Steps:* hand-label 120 (answer, context) pairs for faithfulness — binary, with a written rubric.
Have a second person label 40 of them; compute κ and adjudicate until ≥0.7. Then write the judge
(§11.5: structured output, justification-before-verdict, escape hatch, verbatim-quote backstop) and
compute §11.1's `judge_report`. Iterate the rubric — not the model — until judge–human κ ≥ 0.7.
Finally, measure two biases: verbosity (regress score on answer length) and self-preference (score
the same answers with a second judge family and compare).
*Artifact:* rubric v1→vN with the changes annotated, a κ trajectory, per-class precision/recall, and
two bias numbers.
*Success criterion:* κ ≥ 0.7 with fail-class recall ≥ 0.8, and you can name the judge's residual
failure mode.
*Time:* ~8 hours (most of it labeling — that's the point).
*Unblocks:* §10 entirely; `14`.

**Lab 5 — The oracle-context ablation.**
*Goal:* settle the "is it retrieval or the prompt?" argument permanently, in one afternoon.
*Steps:* run three configurations against the same query set: (a) oracle context — feed the golden
answer spans directly to the generator; (b) full pipeline; (c) full pipeline with `fusion_depth`
doubled. Score all three on faithfulness and answer correctness. Compute recall@`fusion_depth` for
(b) and (c).
*Artifact:* a three-row table plus the §8.4 diagnostic verdict.
*Success criterion:* you can name which stage is your current bottleneck and defend it with two
numbers.
*Time:* ~3 hours.
*Unblocks:* prioritization for `05`, `06`, `07`.

**Lab 6 — Reranker movement analysis.**
*Goal:* explain a reranker delta rather than reporting it.
*Steps:* with graded labels on ≥150 queries, run the cascade with and without a cross-encoder at
three `fusion_depth` values. Compute recall@`final_k`, nDCG@`final_k`, and the §9.2 four-way movement
breakdown. Measure added p95 latency at each depth and compute Δ nDCG per 100ms.
*Artifact:* the §9.4 quality-per-millisecond table plus rescued/broke/kept/missed_both percentages,
and transcripts of five `broke` cases.
*Success criterion:* you can state the `fusion_depth` at which the reranker stops paying for its
latency, and you can characterize what kind of query it breaks.
*Time:* ~5 hours.
*Unblocks:* `04`'s open cascade-tuning question; the P1 project.

**Lab 7 — The regression gate.**
*Goal:* a CI gate that fails on real regressions and doesn't fail on noise.
*Steps:* implement §14.4's DuckDB results schema. Wire the regression tier (§14.1) into CI, triggered
on prompt/config/model/chunker/parser changes. Implement §14.2's `gate`. Then **test the gate**:
inject a known regression (drop `fusion_depth` by half) and confirm FAIL; re-run the identical
config twice and confirm PASS both times, ten times in a row.
*Artifact:* a passing CI job, a run-history Parquet file, and a flakiness measurement (false-fail
rate over 10 identical runs).
*Success criterion:* zero false failures in 10 identical runs, and the injected regression is caught
with a CI whose upper bound is below zero.
*Time:* ~6 hours.
*Unblocks:* `09`; this is the second half of P0.

**Lab 8 — The unanswerable stratum.**
*Goal:* find out what your system does when the answer isn't there.
*Steps:* construct 40 unanswerable queries: 20 about entities absent from the corpus, 20 that are
*near-misses* (the corpus discusses the topic but not the specific fact). Run the pipeline. Compute
§10.5's abstention precision/recall and over-refusal rate. Then sweep one lever — a prompt
instruction to abstain, or a retrieval score floor — and plot the abstention/over-refusal tradeoff.
*Artifact:* the tradeoff curve, plus a chosen operating point with a one-sentence product
justification.
*Success criterion:* you can state your fabrication rate on unanswerable questions as a number, and
the near-miss subset is measurably harder than the absent-entity subset.
*Time:* ~4 hours.
*Unblocks:* `07`, `17`.

**Lab 9 — Judge cost optimization.**
*Goal:* make the release-tier eval 10× cheaper without changing what it measures.
*Steps:* take your calibrated judge (Lab 4) and a 500-item run. Measure actual cost three ways:
synchronous uncached; synchronous with the rubric as a cached prefix (verify with
`usage.cache_read_input_tokens` — if it's zero, find the silent invalidator); and via the Batch API.
Then build a two-tier judge (cheap model everywhere, escalate on low confidence or near-threshold)
and **re-validate the tiered scheme against the same human labels** — κ must survive the
optimization.
*Artifact:* a four-row cost table with measured (not estimated) token counts, plus κ for the tiered
scheme.
*Success criterion:* ≥5× cost reduction with κ within 0.05 of the single-model judge; and you can
explain any cache miss you saw.
*Time:* ~4 hours.
*Unblocks:* §16; makes the release tier affordable enough to actually run.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — what nDCG discounts, why a
reranker cannot change recall at fusion depth, why pooling bias penalizes non-contributing systems,
why paired designs need fewer queries — are derivable from the definitions and verifiable from the
code in this chapter. The power arithmetic in §3.3, the cost arithmetic in §11.7 and §16, and every
formula in §8.1 are derivations, not measurements: every input is labeled as an assumption and every
output is checkable with an interpreter.

The measured figures are **someone else's rung 1**, and carry their conditions:

- **§5.3's chunking table** is Chroma's *Evaluating Chunking Strategies for Retrieval* (Smith &
  Troynikov, 2024), read from the report: their synthetic multi-domain evaluation set, `n=5`
  retrieved chunks, `text-embedding-3-large` (with a second `all-MiniLM-L6-v2` table that reverses
  the overlap conclusion), cl100k token sizes, per-query standard deviations of 25–40. The metric
  definitions in §5.2 are theirs. Quote the numbers with those conditions or not at all.
- **§4.2's parsing metrics** are OmniDocBench's (Ouyang et al., CVPR 2025) methodology — NED / TEDS /
  CDM. The "high-80s to low-90s TEDS on v1.5" range is a reported cluster, not a measurement of any
  system you will run, and the saturation caveat is part of the claim.
- **§9.4's reranker lift ranges** (5–15 nDCG points for ~200ms; outliers from ~2 to ~18) are
  vendor-and-blog-reported ranges across different corpora and are cited *as evidence that the spread
  is wide*, which is an argument for measuring rather than a number to adopt. Do not put them in a
  design doc as an expectation.
- **§13.2's test recommendation** rests on Smucker, Allan & Carterette (CIKM 2007), whose finding —
  randomization/bootstrap/t-test agree; Wilcoxon and sign test have poor power and produce false
  detections — is a result about TREC runs, and the mechanism (bounded, spiky per-query
  distributions) is the same one your eval produces.
- **§1.1, §3.3, §10.4, §11.4, §12.2, §12.4, §12.5, §13.6** draw on Anthropic's *Demystifying evals
  for AI agents* (Jan 2026) for practice guidance: 20–50 initial tasks, deterministic-first grader
  selection, partial credit, grade-the-product-not-the-path, environment isolation, transcript
  review, saturation. These are stated practices from a team with a large deployment surface, not
  measurements.
- **§11.7's prices, §10.8's parameter removals, and §11.5's API shapes** are current-as-of the
  Anthropic model table cached 2026-06-24 and the API behavior documented there. Pricing and beta
  surfaces change; re-check before quoting a dollar figure. The *shape* of the argument (batch −50%,
  caching ~0.1× on reads, tier the judge) is stable; the digits are not.

Deliberately **not** in this document: any absolute quality number for any RAG system, any claim
about which embedding model or reranker is best, and any threshold presented as universal. Every
threshold here (κ ≥ 0.7, judged fraction ≥ 0.8, MDE targets, the 0.02 minimum meaningful effect) is a
*starting point argued from a stated rationale*, and the chapter's own thesis is that these must be
re-derived on your corpus. The first rung-1 numbers for this repo come from the labs in §19, which
are unrun.
