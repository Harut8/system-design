# Appendix G — Precision, MRR, nDCG, faithfulness and the other metrics that get mixed up

> **Why this appendix exists.** [Appendix F](appendix-f-recall-at-every-layer.md) untangled the many
> "recalls". This page does the same for everything else: precision, hit rate, MRR, MAP, nDCG, the
> RAGAS names, the different kinds of "score", faithfulness vs correctness, and the numbers you use
> to *compare* runs (averages, percentage points, κ). Each one is easy to confuse with a neighbour,
> and each confusion produces a wrong decision.
>
> **Where the full detail lives:** formulas and code in
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) §8 (ranking metrics), §9 (reranker),
> §10 (answer metrics), §11 (judges), §13 (statistics). Scores and fusion in
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) §5. Cosine similarity
> in [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) §2.

## Contents

1. [The map — which question each metric answers](#1-the-map--which-question-each-metric-answers)
2. [One ranked list, every ranking metric computed](#2-one-ranked-list-every-ranking-metric-computed)
3. [Ranking metrics that get confused](#3-ranking-metrics-that-get-confused)
4. [Answer metrics that get confused](#4-answer-metrics-that-get-confused)
5. [RAGAS names vs this book's names](#5-ragas-names-vs-this-books-names)
6. [Scores are not metrics: cosine, BM25, RRF, reranker](#6-scores-are-not-metrics-cosine-bm25-rrf-reranker)
7. [Comparing numbers: averages, pp vs %, κ, significance](#7-comparing-numbers-averages-pp-vs--κ-significance)
8. [Which metric for which stage — the cheat sheet](#8-which-metric-for-which-stage--the-cheat-sheet)
9. [Interview questions](#9-interview-questions)

---

## 1. The map — which question each metric answers

> **In plain words.** Every metric answers exactly one question. Confusion starts when you use a
> metric to answer a question it can't see. MRR can't see the second right passage. Recall can't
> see order. Faithfulness can't see whether the source document is out of date.
>
> **Real-world example.** A team reports "MRR went up" after a change that pushed the *second*
> relevant passage out of the top 5. MRR only looks at the first hit, so it couldn't see the damage.

| Metric | The one question it answers | Needs | Blind to | Layer |
|---|---|---|---|---|
| **recall@k** | Of the right passages, how many are in the top k? | binary labels | order | candidate generation ([appendix F](appendix-f-recall-at-every-layer.md)) |
| **hit rate@k** (success@k) | Is *at least one* right passage in the top k? | binary labels | order; how many were found | single-answer QA |
| **precision@k** | Of the top k, how many are right? | binary labels | passages outside the top k | tight context budgets |
| **MRR** (mean reciprocal rank) | How high is the *first* right passage? | binary labels | everything after the first hit | single-answer QA, search boxes |
| **MAP** (mean average precision) | Are *all* right passages found, and near the top? | binary labels | how right each one is | multi-answer retrieval |
| **nDCG@k** | Are the *most* relevant passages at the *top*? | **graded** labels (0–3) | nothing major | rerankers, final ranking |
| **Faithfulness** | Is every claim in the answer backed by the retrieved text? | LLM or human judge | whether the source is true | generation |
| **Answer correctness** | Is the answer actually right? | reference answer | where the answer came from | generation |
| **Answer relevance** | Does the answer address the question asked? | judge or embedding proxy | truth, grounding | generation |
| **Citation precision** | Does each cited source support its claim? | judge | uncited claims | generation |
| **κ (Cohen's kappa)** | Does the judge agree with humans *beyond chance*? | human labels | — | the eval itself |

---

## 2. One ranked list, every ranking metric computed

> **In plain words.** Here is one search result and every ranking metric for it, so you can see
> what each one rewards. The numbers disagree because they're answering different questions, not
> because one of them is wrong.
>
> **Real-world example.** The same list scores 1.0, 0.4, 0.5 and 0.71 depending on the metric. All
> of them are correct.

**Setup** (the same example as `08` §8). Question: *"Can I carry over unused vacation days?"*
Humans graded passages from 0 to 3:

- `D3` = 3 (fully answers)
- `D7` = 2 (partly answers)
- `D5` = 1 (related, doesn't answer)
- everything else = 0.

For the binary metrics, "relevant" means `{D3, D7}`.

```
rank:      1    2    3    4    5
returned:  D5   D3   D9   D7   D2
grade:     1    3    0    2    0
relevant?  no   YES  no   YES  no
```

| Metric | Computation | Value | Reading |
|---|---|---:|---|
| recall@5 | {D3, D7} both in top 5 → 2 / 2 | **1.0** | Nothing missing. |
| hit rate@5 | at least one relevant in top 5 | **1** | Found something. |
| precision@5 | 2 relevant ÷ 5 returned | **0.4** | This is also the **best possible** precision@5 here, since only 2 passages are relevant (§3.2). |
| reciprocal rank | first relevant at rank 2 → 1/2 | **0.5** | The best answer is not first. |
| average precision | precision at each hit: 1/2 (rank 2), 2/4 (rank 4) → mean | **0.5** | Both found, both a bit low. |
| nDCG@5 (exponential gain `2^g − 1`) | DCG = 1/log₂2 + 7/log₂3 + 3/log₂5 = 6.71; ideal (3, 2, 1) = 9.39 | **0.71** | Good passages are present but not at the top. |
| nDCG@5 (linear gain `g`) | same list, gain = grade | **0.79** | Same list, different formula (§3.4). |
| nDCG@5 (binary labels) | grades collapsed to 0/1 | **0.65** | Loses the fact that D3 is better than D7. |

**After a reranker** reorders the list to `D3, D7, D5, D9, D2`:

| Metric | Before | After | Why |
|---|---:|---:|---|
| recall@5 | 1.0 | 1.0 | Same 5 passages. A reranker can't change recall at the depth it receives. |
| precision@5 | 0.4 | 0.4 | Same 5 passages. |
| MRR | 0.5 | **1.0** | Best passage now first. |
| nDCG@5 | 0.71 | **1.0** | Perfect order. |

This is the whole reason rerankers are measured with nDCG and MRR, not recall@`fusion_depth`
(`08` §9.1).

---

## 3. Ranking metrics that get confused

> **In plain words.** Five pairs that look alike and aren't. Know which one you're quoting before
> you put it on a slide.
>
> **Real-world example.** "Precision@10 is only 0.2, retrieval is terrible." There were only 2 right
> passages, so 0.2 is the maximum possible. Retrieval was perfect.

**3.1 Recall@k vs precision@k.** Recall is out of *what should be found*. Precision is out of *what
you returned*. Raising k almost always raises recall and lowers precision. In RAG, the first stages
(search, fusion) care about recall, because the reranker can fix order later. The final prompt cares
about precision, because junk passages cost tokens and distract the model (`06`).

**3.2 Precision@k when there are fewer right passages than k.** If a question has 2 right passages
and k = 10, precision@10 can't exceed 0.2. Averaging precision@10 over questions with 1, 2 or 8
right answers mixes very different ceilings. Use **R-precision** (precision at k = number of right
passages) or look at recall and nDCG instead.

**3.3 MRR vs MAP.** MRR looks only at the *first* right passage. MAP looks at *all* of them.
- For a single-answer FAQ bot, MRR is the right choice.
- For "list all policies that mention remote work", MRR hides whether the other 4 policies were
  found, so use MAP or recall.

Both use binary labels. Neither sees that one passage is better than another.

**3.4 nDCG variants.** Three choices change the number without changing the system:

| Choice | Options | Effect in §2 |
|---|---|---|
| Gain | exponential `2^g − 1` vs linear `g` | 0.71 vs 0.79 |
| Labels | graded vs binary | 0.71 vs 0.65 |
| Ideal list | from all judged passages vs only the returned ones | "only returned" inflates the score whenever a good passage was missed |

Pick one, write it down in the eval config, and never compare numbers computed differently
(`08` §8.1). Computing nDCG over binary labels adds little over MAP.

**3.5 Metrics per question vs per list.** MRR, MAP and nDCG are computed per question and then
averaged. A mean nDCG of 0.70 can mean "every question is 0.70" or "half are 1.0 and half are 0.40".
Always look at the per-question distribution and the worst slice. The "broke / rescued / kept"
breakdown in `08` §9.2 is the tool for this.

---

## 4. Answer metrics that get confused

> **In plain words.** An answer can be *faithful* (matches the sources) and still *wrong* (the
> sources are outdated). It can be *correct* and still *unfaithful* (the model guessed right from
> memory). It can be both and still *irrelevant* (right facts, wrong question). Each needs its own
> score.
>
> **Real-world example.** Below, one refund question with four answers. Each scores high on one
> metric and fails another.

**Setup.** Question: *"How long do I have to return an item?"* The current policy is **14 days**. The
index still holds an outdated page that says **30 days**, and retrieval returns that page.

| Answer | Faithfulness (backed by retrieved text?) | Correctness (true?) | Relevance (answers the question?) | What went wrong |
|---|---|---|---|---|
| "30 days, per the returns page [1]." | **1.0** | **0** | yes | Stale document. A freshness problem (`15`), not a generation problem. |
| "14 days." (from model memory, no source) | **0** | **1** | yes | Right by luck. Unsafe: next time the memory is wrong. |
| "Returns are free for Gold members [1]." | 1.0 | true, but useless | **no** | Answered a different question. |
| "I don't know." | — | — | — | Correct only if the answer truly isn't in the corpus (abstention, `08` §10.5). |

The confusions:

- **Faithfulness vs correctness.** Faithfulness compares the answer with the **retrieved context**.
  Correctness compares it with the **truth** (a reference answer). They are independent, and you
  need both (`08` §10.2, §10.4).
- **Faithfulness vs groundedness.** Same idea at different granularity. Faithfulness checks each
  atomic *claim*. Groundedness checks each *sentence* for a supporting span. Groundedness is cheaper
  and coarser, and works as a CI tripwire.
- **"Hallucination rate" is not one metric.** It sometimes means `1 − faithfulness` (unsupported
  claims) and sometimes means "factually false" (1 − correctness). Ask which one before comparing
  vendors.
- **Answer relevance vs context relevance.** Answer relevance: does the *answer* address the
  question? Context relevance: are the *retrieved passages* about the question? The first is a
  generation metric. The second is a retrieval metric.
- **Citation precision vs citation recall.** Precision: of the citations given, how many support
  their claim. Recall: of the claims needing a source, how many have a correct one. A bot citing one
  source per paragraph can score precision 1.0 and recall 0.3 (`08` §10.6).
- **Exact match / F1 vs judged correctness.** Token F1 works for short extractive answers (a date, a
  number). It punishes correct paraphrases in long answers. BLEU, ROUGE and embedding similarity to
  a reference are tripwires, not quality claims (`08` §10.1).

---

## 5. RAGAS names vs this book's names

> **In plain words.** RAGAS is a popular eval library, and its metric names reuse ordinary words
> with specific meanings. When a dashboard says "context precision", check which definition it uses.
>
> **Real-world example.** A dashboard shows "context precision 0.9" and "precision@5 0.4" for the
> same run. Both can be right: the first rewards the right chunks being *ranked first*, the second
> counts junk in the top 5.

| RAGAS name | What it actually computes | Closest classic metric | Easy to confuse with |
|---|---|---|---|
| **Context precision** | Are the relevant chunks ranked near the top of what was retrieved? Averaged precision at each relevant position. | Average precision (MAP) | precision@k, which ignores order |
| **Context recall** | Share of facts in a *reference answer* that the retrieved context supports (LLM-judged) | recall, but over facts, not passages | chapter 06's `context_recall` (chunks in prompt ÷ chunks retrieved) — see [appendix F §6.3](appendix-f-recall-at-every-layer.md#6-name-collisions-that-cause-confusion) |
| **Faithfulness** | Answer claims supported by retrieved context ÷ all claims | `08` §10.2, same idea | answer correctness |
| **Answer relevancy** | Generate questions from the answer, compare them with the real question by embedding similarity | `08` §10.3 | context relevance; correctness |

Library versions change their definitions and prompts. Pin the library version and the judge model
in the eval config, or your time series will jump when you upgrade (`08` §11.6, §14.3).

---

## 6. Scores are not metrics: cosine, BM25, RRF, reranker

> **In plain words.** A *score* is a number one component gives each passage for sorting. A
> *metric* is a number you compute afterwards against an answer key. Scores from different
> components use different scales and can't be compared or averaged.
>
> **Real-world example.** "Only show passages with cosine > 0.8" worked with model A. After a switch
> to model B, whose scores cluster between 0.3 and 0.6, it filtered out every passage and the bot
> said "I don't know" to everything.

| Score | Range | What a value means | Common mistake |
|---|---|---|---|
| Cosine similarity | −1 to 1, usually squeezed into a narrow band that differs per model | Closeness in *that* model's space | Treating 0.8 as "80% relevant", or reusing a threshold after a model change (`01` §2) |
| pgvector `<=>` | 0 to 2 (cosine **distance** = 1 − similarity) | Smaller = closer | Sorting the wrong way; mixing distance and similarity |
| BM25 | 0 to unbounded; depends on query length and corpus | Term-match strength | Comparing BM25 across queries, or adding it to cosine |
| RRF | about 0.016 per list at rank 1 with k = 60 (1/61), summed over lists | Rank agreement between branches | Reading it as a probability, or thresholding it |
| Cross-encoder reranker | logit (any real number) or 0–1 after a sigmoid, depending on the model | Relevance judged by *that* model | Reusing a cutoff after changing reranker models |

The rule: **use scores to sort, use metrics to decide.** If you need a cut-off ("don't answer below
X"), calibrate it on labeled data for that exact model and re-calibrate on every model change.
RRF exists precisely because BM25 and cosine scores can't be added (`04` §5).

---

## 7. Comparing numbers: averages, pp vs %, κ, significance

> **In plain words.** Even with the right metric, *how* you compare two runs can mislead: how you
> average, how you state the change, and whether the change is bigger than noise.
>
> **Real-world example.** "Recall improved 10%!" It went from 0.70 to 0.77. That's +7 percentage
> points, or +10% relative, on 50 questions, where the noise is about ±6 points. The honest summary
> is "maybe better, need more questions".

**7.1 Macro vs micro average.** Two questions. Q1 has 1 relevant passage and finds it. Q2 has 9 and
finds 3.
- **Macro** (average the per-question scores): (1.0 + 0.33) / 2 = **0.67**.
- **Micro** (pool all passages): 4 / 10 = **0.40**.

This book uses macro (per question), because users ask questions. Say which one you use.

**7.2 Percentage points vs percent.** 0.70 → 0.77 is **+7 pp** (absolute) and **+10%** (relative).
Always write "pp" for absolute changes.

**7.3 Agreement vs κ.** A judge that says "pass" to all 100 answers, when humans pass 90, agrees
90% of the time. Its κ is **0**: no better than always saying "pass". Report κ, and per-class
precision and recall for the "fail" class, not raw agreement (`08` §11.1).

**7.4 Significant vs meaningful.** A difference can be real but tiny (0.4 pp on 10,000 questions),
or large but noise (5 pp on 30 questions). Report the paired-bootstrap confidence interval, and gate
CI on a minimum meaningful change (`08` §13, §14.2).

**7.5 Offline vs online.** Offline metrics use your golden set. Online signals (thumbs-down rate,
re-ask rate, escalation to a human) come from real users. When they disagree, your golden set
probably doesn't look like real traffic (`08` §15.1).

---

## 8. Which metric for which stage — the cheat sheet

> **In plain words.** Each stage gets the metric it can actually change. Pair each quality number
> with its cost.
>
> **Real-world example.** For a reranker, report nDCG@10, MRR and the added p95 latency together.
> "+0.06 nDCG for +120 ms" is a decision someone can make.

| Stage | Main metric | Also report | Don't use |
|---|---|---|---|
| Parse / chunk | span survival, token recall at a fixed budget | Precision_Ω, IoU | chunk-ID recall across chunkers |
| Embed | recall@k with exact search | per-slice recall | public leaderboard rank |
| Index | ANN recall@k vs brute force | p95 latency, RAM | human-label metrics (they mix in embedding errors) |
| Search + fusion | recall@`fusion_depth` | hit rate | nDCG (order doesn't matter yet) |
| Rerank | nDCG@`final_k`, MRR | recall@`final_k`, broke/rescued counts, added ms | recall@`fusion_depth` (it can't move) |
| Prompt packing | `context_recall` (ch. 06), tokens used | precision@`final_k` | — |
| Generate | faithfulness **and** correctness | answer relevance, citation precision/recall, abstention | BLEU/ROUGE as a quality claim |
| Judge | κ vs humans, fail-class recall | bias checks | raw agreement % |
| Any comparison | paired-bootstrap CI | per-slice deltas | one number without k, labels or CI |

---

## 9. Interview questions

> **In plain words.** Interviewers use these to check that you pick metrics to match the stage and
> the product, and that you know each metric's blind spot.
>
> **Real-world example.** "Which metric would you use for the reranker?" The strong answer is
> "nDCG@10 with graded labels plus MRR, at fixed fusion depth, with added latency", not "accuracy".

**Conceptual.**

1. *"MRR or nDCG for a customer-support RAG bot?"* (§1, §3.3.) If there is usually one right
   article and the user reads the first, MRR. If several passages combine into an answer and some
   are better than others, nDCG with graded labels. Many teams report both.
2. *"Faithfulness is 0.95 but users say answers are wrong. How?"* (§4.) Faithfulness only checks
   against retrieved text. Stale or wrong documents give faithful wrong answers. Measure correctness
   against reference answers, and check document freshness.
3. *"Why can't you set one similarity threshold for 'relevant'?"* (§6.) Cosine ranges differ per
   model and per query. Scores sort, metrics decide. Calibrate cut-offs on labeled data per model.

**Rapid-fire.**

| Question | Strong answer | Section |
|---|---|---|
| Precision@10 is 0.2. Bad? | Not if only 2 passages are relevant. That's the maximum. | §3.2 |
| MRR vs MAP? | MRR: first hit only. MAP: all hits. | §3.3 |
| Why graded labels for nDCG? | Without grades nDCG can't tell "fully answers" from "mentions". | §3.4 |
| Can a reranker raise precision@`fusion_depth`? | No. Same set. It raises precision and nDCG at `final_k`. | §2 |
| Faithful but wrong? | Yes, if the source is stale. | §4 |
| RAGAS context precision is like…? | Average precision over the retrieved list: rewards relevant chunks ranked first. | §5 |
| Can you average BM25 and cosine scores? | No. Different scales. Use rank fusion (RRF). | §6 |
| 0.70 → 0.77 is how much? | +7 pp, +10% relative. Check the CI. | §7.2 |
| Judge agrees 90% with humans. Good? | Maybe not. Check κ: a judge that always says "pass" gets 90% if 90% pass. | §7.3 |

**Common mistakes.**

- Using one "quality" number for the whole pipeline.
- Comparing nDCG numbers computed with different gain formulas or label types.
- Reporting faithfulness without correctness, or the reverse.
- Reusing similarity thresholds after changing the embedding or reranker model.
- Saying "10% better" when you mean 10 percentage points, or the reverse.
