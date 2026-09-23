# Appendix F — "Recall" at every layer: one word, many meanings

> **Why this appendix exists.** The word *recall* appears in almost every chapter of this folder:
> chunking recall, embedding recall@k, ANN recall, retrieval recall, context recall, citation
> recall, abstention recall, detection recall. They are **not the same number**. They count
> different things, check against different answer keys, and live at different layers. Reading
> them as one metric is the fastest way to get lost, and it causes real mistakes in design reviews
> ("our recall is 0.99", about the index, while users can't find answers).
>
> This page puts them all in one place. Read it once, keep it open while reading
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md), and use the table in §3 as a
> lookup.
>
> **Companion:** [appendix G](appendix-g-ranking-and-answer-metrics.md) does the same for precision,
> MRR, MAP, nDCG, faithfulness and the other metrics that get mixed up.
>
> **Where the full detail lives:** formulas and code in `08` §2 (measurement map), §5 (chunking),
> §6 (embeddings), §7 (the two recalls), §8 (retrieval metrics), §10 (generation). Index recall in
> `03` §1 and §3. Candidate recall and the recall ceiling in `04` §1 and §13.

## Contents

1. [The one formula behind every recall](#1-the-one-formula-behind-every-recall)
2. [The pipeline, and where each recall sits](#2-the-pipeline-and-where-each-recall-sits)
3. [The lookup table — every recall in this book](#3-the-lookup-table--every-recall-in-this-book)
4. [One question, every recall computed](#4-one-question-every-recall-computed)
5. [The leak report — follow 200 questions through the pipeline](#5-the-leak-report--follow-200-questions-through-the-pipeline)
6. [Name collisions that cause confusion](#6-name-collisions-that-cause-confusion)
7. [Which recall do I check? Symptom → metric](#7-which-recall-do-i-check-symptom--metric)
8. [Interview questions](#8-interview-questions)

---

## 1. The one formula behind every recall

> **In plain words.** Recall always answers: "Of the things I *should* have caught, how many did I
> catch?" It never asks how much junk came along. That second question is *precision*.
>
> **Real-world example.** A fishing net. 10 salmon swim past, you catch 8: recall 0.8. You also
> caught 30 old boots. Recall doesn't care about the boots. Precision (8 of 38 ≈ 0.21) does.

Every recall in this book has the same shape:

```
            | things found  ∩  things that should be found |
recall  =  ---------------------------------------------------
                  | things that should be found |
```

So every recall is fully defined by **three answers**. When you see "recall" in a sentence, ask:

| Question | Why it matters | Examples of answers |
|---|---|---|
| **1. Recall of what? (the unit)** | Counting vectors, chunks, tokens, claims or questions gives different numbers | vector IDs, chunk IDs, answer tokens, facts in an answer, attack prompts |
| **2. Against which answer key? (the ground truth)** | Who decided what "should be found" | brute-force exact search, human labels, a reference answer, an LLM judge |
| **3. Found where? (the list, and its size k)** | Top 5 and top 50 are different lists | ANN top-10, fused top-50, reranked top-5, the final prompt |

If someone can't answer all three, the number isn't meaningful yet. "Recall is 0.9" is a rumor.
"Retrieval recall@50 against human-labeled answer spans, over 200 questions, is 0.86 ± 0.05" is a
measurement.

**Two related scores you will see next to recall:**

| Score | Formula | Plain meaning |
|---|---|---|
| **Precision** | found ∩ should-find ÷ found | "How much of what I returned was useful?" |
| **Hit rate@k** (also *success@k*) | share of questions with **at least one** right item in top k | "Did we find *something* right?" When every question has exactly one right passage, hit rate@k and recall@k are the same number. |

---

## 2. The pipeline, and where each recall sits

> **In plain words.** A RAG request is a relay race. Each runner (parser, chunker, embedder, index,
> retriever, reranker, prompt builder, LLM) can drop the baton. Each layer has its own recall, and it
> measures only *that* runner's drops.
>
> **Real-world example.** An HR bot can't answer "How much parental leave do I get?" The answer was
> in a scanned PDF table that the parser turned into garbage. Every later stage can score 1.0 and the
> bot still fails, because the answer was gone before the index was even built.

```
 documents
    │
    ▼
 [PARSE] ──────────── answer-span survival   "is the answer text still in the parsed output?"   08 §4.5
    │
    ▼
 [CHUNK] ──────────── token recall / recall@budget   "did the answer tokens end up in chunks we return?"   08 §5, 02 §11
    │
    ▼
 [EMBED] ──────────── retrieval recall@k with EXACT search   "does the model place the answer near the question?"   08 §6, 01 §5
    │
    ▼
 [INDEX / ANN] ─────── ANN recall@k vs brute force   "did the fast index return what exact search would?"   03 §1, 08 §7
    │     (no human labels needed)
    ▼
 [QUERY + BRANCHES + FUSION] ── candidate recall = recall@fusion_depth   "is the answer anywhere in the candidate pool?"   04 §1, 05 §1.3, 08 §8
    │
    ▼
 [RERANK] ─────────── recall@final_k (+ nDCG, MRR)   "did the answer survive the cut to the top few?"   04 §13, 08 §9
    │
    ▼
 [PACK THE PROMPT] ── context_recall (06) = chunks in prompt ÷ chunks retrieved   "did the answer fit in the window?"   06 §16
    │
    ▼
 [GENERATE] ───────── citation recall, RAGAS context recall, abstention recall   "did the answer use and cite it?"   08 §10
    │
    ▼
 [JUDGE / GUARDRAILS] ─ classifier recall   "did the grader / filter catch the bad cases?"   08 §11, 17
```

Two rules follow from the picture:

1. **Recall only goes down as you move along, except where you add a search branch.** Parsing,
   chunking, the index, the reranker and prompt packing can only keep or lose the answer. Adding a
   second search branch (BM25 next to dense) or a query rewrite can *add* answers back into the
   candidate pool. That is why hybrid search and query rewriting exist (`04` §3, `05` §1.3).
2. **An early loss caps every later number.** If parsing keeps 97% of answers, no later stage can
   reach 0.98. This cap is called the **recall ceiling** (`04` §1).

---

## 3. The lookup table — every recall in this book

> **In plain words.** Same word, different meaning per row. The "answer key" column tells them
> apart most quickly: brute force, human labels, reference answer, or "what we retrieved".
>
> **Real-world example.** "Recall 0.99" in the index team's report and "recall 0.72" in the eval
> team's report are both correct. They're different rows of this table.

| # | Name you'll see | Layer | Unit counted | Answer key | Formula | Typical value | Where |
|---|---|---|---|---|---|---|---|
| 1 | **Answer-span survival** | Parse | golden answer spans | quotes stored in the golden set | spans found in parsed text ÷ all spans | 0.90 – 1.0 (text), much lower for tables in bad parsers | `08` §4.5 |
| 2 | **Token recall** (Chroma) | Chunk | answer tokens (doc, offset) | labeled character spans | answer tokens in retrieved chunks ÷ all answer tokens | 0.8 – 0.92 | `08` §5, `02` §11 |
| 3 | **Recall@budget** | Chunk / embed | answer tokens or passages | labeled spans | recall when every method gets the **same token budget** (e.g. 2,000 tokens) | — | `08` §5, `01` §5 |
| 4 | **Retrieval recall@k** (exact search) | Embed | relevant passages | human labels | relevant in top k ÷ all relevant, with flat index | 0.5 – 0.9 | `08` §6 |
| 5 | **ANN recall@k** | Index | vector IDs | **brute-force exact top k over the same vectors** | overlap(ANN top k, exact top k) ÷ k | 0.95 – 0.999 | `03` §1, §3; `08` §7 |
| 6 | **Retrieval recall@k** (pipeline) | Retrieve | relevant passages | human labels | relevant in top k ÷ all relevant | 0.5 – 0.9 | `08` §8 |
| 7 | **Candidate recall** = recall@`fusion_depth`; **recall ceiling** | Branches + fusion | relevant passages | human labels | recall of the pool the reranker receives | 0.8 – 0.95 | `04` §1, §6; `05` §1.3 |
| 8 | **Recall@`final_k`** | Rerank | relevant passages | human labels | relevant in the reranked top 5–10 ÷ all relevant | below row 7 | `04` §13; `08` §9 |
| 9 | **`context_recall`** (this book's ch. 06 metric) | Prompt packing | chunks | *what retrieval returned* (no labels) | chunks in prompt ÷ chunks retrieved | target 1.0 | `06` §16 |
| 10 | **RAGAS "context recall"** | Retrieval, judged by LLM | facts in the reference answer | a written reference answer | facts supported by retrieved context ÷ facts in reference | 0.6 – 0.95 | `08` §10 |
| 11 | **Citation recall** | Generate | claims that need a source | judge or human | claims with a correct citation ÷ claims needing one | 0.7 – 0.95 | `08` §10.6 |
| 12 | **Abstention recall** | Generate | unanswerable questions | golden set's "no answer" stratum | declined correctly ÷ should decline | target ≥ 0.8 | `08` §10.5 |
| 13 | **Judge recall** (per class) | Eval grader | truly bad answers | human labels on a calibration set | bad answers the judge flags ÷ all bad answers | target ≥ 0.8 | `08` §11 |
| 14 | **Detection recall** | Guardrails | attacks / PII items | labeled attack or PII set | caught ÷ all attacks | 20 – 98% by method | `17` §13.2 |

**Read rows 4–8 as one family.** They all use human-labeled relevant passages. The only difference is
*which list* you look at: exact-search top k, pipeline top k, the candidate pool, or the reranked
top few. **Row 5 is the odd one out.** It uses no human labels at all.

---

## 4. One question, every recall computed

> **In plain words.** The same single question gives very different recall numbers depending on
> which row of §3 you compute. Here they are side by side, so you can see they measure different
> things.
>
> **Real-world example.** Below: one HR question, eight "recalls", values from 0.5 to 1.0, all
> correct at the same time.

**Setup.** Question: *"How much parental leave do I get?"* The corpus has chunks `C1…C20`.
Humans marked two chunks as relevant: `C7` (the main rule) and `C12` (the extra weeks for twins).
The answer span is 60 tokens: 45 of them are in `C7` and 15 are in `C12`.

The reference answer has 2 facts: *"16 weeks paid"* (in `C7`) and *"+4 weeks for multiple births"*
(in `C12`).

**What the system returned (top 5):**

```
exact (brute-force) search top 5 : C7, C3, C12, C9, C1
ANN (HNSW) top 5                 : C7, C3, C9,  C1, C4     ← the index skipped C12
put into the prompt (budget)     : C7, C3, C9              ← 2 chunks didn't fit
final answer                     : "You get 16 weeks paid [C7]. Twins add 4 weeks."  ← 2nd claim uncited, from memory
```

| Recall (row in §3) | Computation | Value | What it tells you |
|---|---|---:|---|
| ANN recall@5 (5) | ANN top 5 vs exact top 5: {C7, C3, C9, C1} shared | 4 / 5 = **0.8** | The index missed 1 of the true nearest vectors. It doesn't know or care that C12 was relevant. |
| Retrieval recall@5, exact search (4) | relevant {C7, C12} in exact top 5 | 2 / 2 = **1.0** | The embedding model did its job on this question. |
| Retrieval recall@5, pipeline (6) | relevant {C7, C12} in ANN top 5 | 1 / 2 = **0.5** | The user-facing search found half the answer. The loss is the index's (1.0 → 0.5). |
| Hit rate@5 | any relevant in ANN top 5? | **1** | "Found something", which hides the missing twin rule. |
| Precision@5 | relevant in top 5 ÷ 5 | 1 / 5 = **0.2** | 4 of the 5 chunks are noise. |
| Token recall (2) | answer tokens in retrieved chunks | 45 / 60 = **0.75** | 15 answer tokens (the twin rule) never arrived. |
| `context_recall`, ch. 06 (9) | chunks in prompt ÷ chunks retrieved | 3 / 5 = **0.6** | Prompt packing dropped 2 chunks (here, not relevant ones). |
| RAGAS context recall (10) | reference facts supported by retrieved context | 1 / 2 = **0.5** | Retrieved text supports only "16 weeks". |
| Citation recall (11) | claims with a correct citation ÷ claims | 1 / 2 = **0.5** | The twin claim has no source. The model may have guessed it. |

**Diagnosis from the table.** The embedding model is fine (row 4 = 1.0). The index lost `C12`
(row 5 = 0.8, row 6 = 0.5). Raising `ef_search` (`03` §2, §4) is the fix. It is also why the
answer's second claim is uncited. One question is only an anecdote, though. Section 5 does the same
thing across 200 questions.

---

## 5. The leak report — follow 200 questions through the pipeline

> **In plain words.** Take your test questions and, for each one, record at every stage whether the
> right answer is *still there*. The stage where the count drops the most is where you should spend
> your next week. This is the single most useful eval report you can build.
>
> **Real-world example.** Below, the team wanted to tune HNSW. The report shows the index loses 2
> questions and the embedding step loses 26. They switched to hybrid search instead.

**Setup (illustrative, internally consistent).** An HR bot. 200 golden questions, each with exactly
one right passage, so recall@k and hit rate@k are the same number here (§1).

| Stage | Questions where the answer is still reachable | Lost (−) or regained (+) here | Kept at this stage | Measured with (§3 row) |
|---|---:|---:|---:|---|
| Start | 200 | | | |
| Parse | 194 | −6 (scanned tables) | 0.970 | 1 — span survival |
| Chunk | 186 | −8 (answer split across two chunks) | 0.959 | 2 — token recall |
| Embed (exact search, top 50) | 160 | **−26** (jargon, part numbers) | 0.860 | 4 — recall@50, flat index |
| ANN index (HNSW, top 50) | 158 | −2 | 0.988 | 5 — ANN recall ≈ 0.99 |
| + BM25 branch, RRF fusion (top 50) | 172 | **+14** (exact terms recovered) | — | 7 — candidate recall |
| Rerank → top 5 | 163 | −9 | 0.948 | 8 — recall@5 |
| Pack prompt (3,000-token budget) | 161 | −2 (long chat history pushed chunks out) | 0.988 | 9 — `context_recall` |
| Generate a correct, grounded answer | 150 | −11 | 0.932 | faithfulness, correctness (`08` §10) |

What the report says:

- **End-to-end:** 150 / 200 = **0.75** of questions get a correct answer.
- **Candidate recall (recall@50 after fusion):** 172 / 200 = **0.86**. This is the ceiling for the
  reranker and the LLM.
- **Recall@5 after rerank:** 163 / 200 = **0.815**.
- **The index cost 2 questions** (1.25% of the 160 that reached it). Tuning HNSW further could win
  back at most 2 questions. Hybrid search won back 14.
- **Where to work next:** the 14 questions still lost at the embed stage after hybrid, then the 11
  generation failures, then the 8 chunk-boundary losses.

**How to build it.** For each golden question, store the right answer's character span. Then log
at every stage whether that span is still present:

```python
STAGES = ["parsed", "chunked", "exact_top50", "ann_top50", "fused_top50",
          "reranked_top5", "in_prompt", "answer_correct"]

def leak_report(traces: list[dict]) -> None:
    """traces: one dict per golden question, e.g. {"parsed": True, "chunked": True, ...}.
    Each value answers: is the right answer still reachable after this stage?"""
    prev = len(traces)
    print(f"{'stage':<16}{'reachable':>10}{'change':>8}")
    for s in STAGES:
        n = sum(t[s] for t in traces)
        print(f"{s:<16}{n:>10}{n - prev:>+8}")
        prev = n
```

Two measurement rules:

- **Use span labels, never chunk IDs** (`08` §3.1). If you change chunking, chunk IDs change and old
  labels become meaningless. Character spans survive any chunking change.
- **Measure the embed stage with exact search** (a flat index). Otherwise index losses and
  embedding losses get mixed into one number.

---

## 6. Name collisions that cause confusion

> **In plain words.** Most of the confusion comes from six pairs of names that look the same and
> aren't. Learn these six and the chapters read clearly.
>
> **Real-world example.** A vendor's slide says "99% recall". It is almost always ANN recall
> (row 5) on a public benchmark, not how often your users find answers.

**6.1 ANN recall vs retrieval recall.** ANN recall compares the index to brute force over the same
vectors, with no human labels. Retrieval recall compares results to human labels. An index can have
ANN recall 0.99 in a pipeline with retrieval recall 0.60 if the embedding model is weak. Tune the
index to about 0.99 ANN recall, then stop thinking about it (`08` §7).

**6.2 recall@k vs hit rate@k.** Recall@k counts *all* the right passages. Hit rate counts "at least
one". With one right passage per question they are equal. With several (the twin-leave example in
§4) hit rate can be 1.0 while recall is 0.5. Several chapter symbol tables (`02`, `05`) describe
recall@k as "share of questions whose answer is in the top k". That wording is exactly hit rate, and
matches recall only in the one-passage case.

**6.3 This book's `context_recall` vs RAGAS context recall.** Chapter 06's `context_recall` is a
*packing* metric: of the chunks retrieval returned, how many fit in the prompt. It needs no labels.
RAGAS "context recall" is a *retrieval quality* metric: of the facts in a reference answer, how many
the retrieved context supports. It needs a reference answer and an LLM judge. Same name, different
layer.

**6.4 Token recall vs chunk recall.** Chunk recall asks "is the right chunk in the list?" and treats
a 2,000-token chunk and a 200-token chunk as equal. Token recall asks "are the right *tokens* in the
list?" Big chunks inflate chunk recall and token recall cheaply, so compare chunking methods at a
**fixed token budget** (recall@budget, `08` §5).

**6.5 recall@5 vs recall@50.** Different lists, different numbers. A reranker can't change
recall@50 (it only reorders the same 50) but can raise recall@5. Always state k, and always say
*which* list (`08` §2, rule 1).

**6.6 Recall of a classifier vs recall of a search.** The judge (`08` §11) and guardrails (`17`) are
classifiers. Their recall means "of the truly bad cases, how many did it flag?" It has nothing to do
with search. It always comes paired with precision (false alarms) and, for guardrails, an
over-refusal rate.

---

## 7. Which recall do I check? Symptom → metric

> **In plain words.** Start from the complaint, pick the matching row, check that one number first.
> Don't tune a layer until its own number says it is the problem.
>
> **Real-world example.** "The bot says the policy doesn't exist, but it does": check candidate
> recall first. If the passage isn't in the top 50, no prompt change can fix it.

| Symptom | Check first | If that's fine, check next |
|---|---|---|
| "The answer is in our docs but the bot says it doesn't know" | Candidate recall (row 7) on similar questions | Span survival (row 1), then `context_recall` (row 9) |
| Fails only on scanned PDFs or tables | Span survival (row 1) | Token recall (row 2) |
| Fails on part numbers, error codes, names | Recall@50 dense-only vs hybrid (rows 6–7) | Query rewriting ablation (`05` §13) |
| Worked last month, worse now, nothing deployed | ANN recall (row 5), after heavy deletes or updates (`03` §8) | Embedding drift probe (`08` §6) |
| Good answers in short chats, bad in long ones | `context_recall` (row 9) | Lost-in-the-middle checks (`06` §5) |
| Right sources retrieved, answer still wrong or uncited | Citation recall (row 11), faithfulness | Recall@`final_k` (row 8) |
| Bot invents answers to questions with no answer | Abstention recall (row 12) | Score floor / threshold (`08` §10.5) |
| Eval dashboard says all good, users disagree | Judge recall (row 13) against human labels | Golden set coverage (`08` §3) |
| After a reranker upgrade, "recall didn't change" | You measured recall@50. Measure recall@5 / nDCG@10 | `04` §13 |

---

## 8. Interview questions

> **In plain words.** Interviewers use "recall" to check whether you know *which* recall. The strong
> answer always names the unit, the answer key and the list.
>
> **Real-world example.** "Our HNSW index has 0.98 recall. Is retrieval good?" The strong answer:
> "That's ANN recall against brute force. It says nothing about relevance. I'd need recall@k against
> labeled passages to answer."

**Conceptual.**

1. *"Explain the difference between ANN recall and retrieval recall. Which would you optimize?"*
   (§3 rows 5 and 6, §6.1.) ANN recall measures index fidelity against exact search. Retrieval
   recall measures relevance against human labels. Tune the index until ANN recall is about 0.99,
   so it adds about 1 point of loss. Then spend effort on whatever the leak report (§5) says loses
   the most.
2. *"You added a reranker and recall@50 didn't move. Is the reranker useless?"* (§6.5.) No. It
   reorders a fixed set, so recall@50 can't move by construction. Measure recall@5, nDCG@10 or
   MRR, which the reranker *can* change.
3. *"How do you find which stage of a RAG pipeline is losing answers?"* (§5.) Label answers as
   character spans and log, per question, whether the answer is still reachable after each stage.
   The biggest drop is the next thing to fix. Measure the embed stage with exact search so index
   losses don't mix in.

**Rapid-fire.**

| Question | Strong answer | Section |
|---|---|---|
| Recall vs precision in one line? | Recall: share of what should be found that was found. Precision: share of what was found that's useful. | §1 |
| When are recall@k and hit rate@k equal? | When each question has exactly one right passage. | §1, §6.2 |
| Does ANN recall need labeled data? | No. The answer key is brute-force search over the same vectors. | §3 row 5 |
| What is the recall ceiling? | The candidate recall the reranker and LLM receive. Nothing downstream can exceed it. | §2 |
| Can any stage raise recall? | Only stages that add search paths: extra branches (BM25), query rewrites, multi-query. | §2 rule 1 |
| Why compare chunkers at a fixed token budget? | Bigger chunks raise recall just by sending more text. | §6.4 |
| Why label answers as spans, not chunk IDs? | Chunk IDs change when chunking changes, so the labels break. | §5 |
| What does RAGAS context recall need? | A reference answer and an LLM judge to check each fact. | §6.3 |
| A vendor claims 99% recall. What do you ask? | Recall of what, against which answer key, at which k, on whose data? | §1, §6 |

**Common mistakes.**

- Quoting one "recall" number for a whole pipeline without naming the layer.
- Tuning `ef_search` when the leak report shows the embed or parse stage is losing answers.
- Comparing recall@5 from one run with recall@10 from another.
- Using chunk-ID labels, then changing the chunker and trusting the new numbers.
- Reading hit rate as recall on questions that have several right passages.
