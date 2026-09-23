# 00 — Mental models: the retrieval→generation pipeline as a data system

> **Prerequisites:** [`../databases/06-indexing-internals.md`](../databases/06-indexing-internals.md)
> (why an index is a tradeoff, not a win), [`../databases/03-access-methods-and-table-scans.md`](../databases/03-access-methods-and-table-scans.md)
> (selectivity and access-method choice), [`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md)
> (a bad benchmark is worse than no benchmark — you will need this discipline the moment
> you try to prove a change helped), and [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
> (the single most on-target existing document in this repo for this whole folder — worth
> skimming before anything else here). No AI-specific prerequisite: this is chapter 00.
>
> **Feeds into:** every other chapter in this folder. Specifically
> [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) (§10 here),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (§5b),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (§7),
> [`06-context-engineering.md`](06-context-engineering.md) (§11),
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§4, §6),
> [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md) (§13),
> [`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md) (§9),
> [`13-agents-and-tool-calling.md`](13-agents-and-tool-calling.md) and
> [`14-agent-evaluation.md`](14-agent-evaluation.md) (§12).
>
> **THESIS:** an LLM pipeline is a data system. Correctness, measurability and unit cost
> are engineering properties of the pipeline, not afterthoughts bolted on once the demo
> works — and that is exactly where the difficulty lives. Wiring an embedding model to a
> vector store and stuffing the top-k results into a prompt is a two-week ramp. Everything
> in this folder is about the part that isn't: knowing which of eight or nine possible
> stages produced a wrong answer, what it costs to find out, and what it costs to fix. This
> chapter is the decomposition that makes that diagnosis possible. Nothing here is novel
> systems theory — it's the same discipline you already have for databases and distributed
> systems, applied to a pipeline that happens to have a language model glued to the end of
> it.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Thesis — an LLM pipeline is a data system](#1-thesis--an-llm-pipeline-is-a-data-system)
2. [The pipeline as dataflow](#2-the-pipeline-as-dataflow)
3. [The index is a materialized view over the corpus](#3-the-index-is-a-materialized-view-over-the-corpus)
4. [Where correctness actually lives: the recall ceiling](#4-where-correctness-actually-lives-the-recall-ceiling)
5. [The four irreducible failure classes](#5-the-four-irreducible-failure-classes)
6. [Diagnosis: attributing a failure to a stage](#6-diagnosis-attributing-a-failure-to-a-stage)
7. [Two-stage retrieval is classical IR](#7-two-stage-retrieval-is-classical-ir)
8. [The latency budget](#8-the-latency-budget)
9. [The cost model](#9-the-cost-model)
10. [Reindex cost as a coupling constraint](#10-reindex-cost-as-a-coupling-constraint)
11. [When NOT to RAG](#11-when-not-to-rag)
12. [The 2026 shape: from pipeline to loop](#12-the-2026-shape-from-pipeline-to-loop)
13. [Observability as a design constraint, not an add-on](#13-observability-as-a-design-constraint-not-an-add-on)
14. [Anti-patterns](#14-anti-patterns)
15. [Mental models — the compressed set](#15-mental-models--the-compressed-set)
16. [Lab exercises](#16-lab-exercises)
17. [Interview questions and system design prompts](#17-interview-questions-and-system-design-prompts)
18. [Real-world cases — incidents with numbers](#18-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** A RAG system ("retrieval-augmented generation") answers questions about *your*
documents. It first finds the few passages that probably contain the answer, then hands them to a
language model and asks it to write the answer from them. A demo of this takes two weeks. Making it
right on real questions is the hard part, because when the answer is wrong, you can't see which
step failed. This chapter splits the system into steps, gives each step a number you can measure,
and gives you a short test that tells you which step broke. Every later chapter goes deep on one
of those steps.

### One question, all the way through the pipeline

The scenario: an **HR help bot** for a company with 5,000 employees. It answers questions from
**2,000 HR documents** (policies, handbooks, benefit PDFs), about **3 million tokens** of text in
total. (A *token* is a piece of a word; 1,000 tokens is roughly 750 English words.) An employee
types:

> "How many vacation days do I get in my first year?"

The true answer, which sits in a table in `leave-policy-2026.pdf`, is "15 days, prorated from your
start date". Here is what happens to that question at each step. All numbers are illustrative but
consistent with each other.

**Ingest time — done once, before anyone asks anything:**

1. **Parse** (chapter `02`). Turn each PDF, web page and Word file into clean text. The leave
   policy has a table: "Year 1 → 15 days, Year 2–5 → 20 days". A good parser keeps it as a table.
   *Without care:* the PDF is a scan, the parser returns an empty page, and the answer is lost
   before anything else runs. No later step can recover it.
2. **Chunk** (`02`). Cut the text into passages of about **500 tokens** with **50 tokens of
   overlap**, so each chunk starts 450 tokens after the previous one. 3,000,000 ÷ 450 ≈ **6,700
   chunks**. Each chunk keeps metadata: source file, section title, page. *Without care:* a cut
   lands between the table header "Vacation days by year of service" and the row "Year 1 → 15",
   so the chunk with the number no longer says what the number is.
3. **Embed** (`01`). Send every chunk to an embedding model, which turns it into a list of
   **1,536 numbers** (a *vector*) that describes its meaning. Texts with similar meaning get
   similar vectors. Cost: 6,700 chunks × 500 tokens = 3.35M tokens × $0.02 per million =
   **about 7 cents**, paid once.
4. **Index** (`03`). Store the 6,700 vectors (6,700 × 1,536 × 4 bytes ≈ **41 MB**) in a vector
   index, plus a keyword index (BM25) over the same text. The index is a precomputed shortcut,
   like a library catalogue. *Without care:* someone edits the leave policy next month and the
   index still holds the old version (a *stale* index).

**Query time — done on every question, while the employee waits:**

5. **Embed the query** (`01`). The 12-token question becomes one vector with the *same* model.
   Cost: 12 × $0.02/M ≈ $0.00000024. About 30 ms.
6. **Retrieve** (`04`). Two searches run side by side: the vector index returns the **50** chunks
   whose vectors are closest to the question's; BM25 returns the **50** chunks that share the most
   words. The policy says "annual leave" and the employee said "vacation days", so the vector search
   ranks the right chunk only **37th**. BM25 matches the words "first year" and ranks it **9th**.
   Either list alone would leave it far outside the top 5. About 15 ms.
7. **Fuse** (`04`). Merge the two lists into one (for example with RRF, which rewards chunks that
   rank high in either list). The right chunk now sits around **12th** of ~80 unique candidates,
   because it did reasonably well in both lists. About 1 ms.
8. **Rerank** (`04`). A slower, more accurate model (a *cross-encoder*) reads the question and each
   of the top 50 fused candidates together and scores them. The leave-policy table moves from 12th
   to **2nd**. Keep the top **5**. About 120 ms.
9. **Assemble the prompt** (`06`). Build the text the model will read: instructions (≈300 tokens)
   + 5 chunks (≈2,500 tokens) + the question (12 tokens) ≈ **2,800 input tokens**. Each chunk gets a
   label like `[leave-policy-2026 §1]` so the answer can cite it. *Without a budget:* someone
   pastes in 40 chunks "to be safe": the prompt grows from ~2,800 to ~20,300 tokens, input cost
   goes up about 7×, and the model gets distracted.
10. **Generate** (`07`). The language model reads the prompt and writes: "You get 15 vacation days
    in your first year, prorated from your start date [leave-policy-2026 §1]." About **150 output
    tokens**. First word appears after ~600 ms (*TTFT*); the full answer streams in over ~3 s.
    Cost with an illustrative price of $3/M input and $15/M output tokens: 2,812 × $3/M + 150 ×
    $15/M ≈ **$0.011** per question, about **$320 a month** at 1,000 questions a day. The
    generator's input tokens are most of that bill.

**After the fact — how you know it works:**

11. **Evaluate** (`08`). Write a *golden set*: 50 real employee questions, each with the chunk that
    holds the answer. Run all 50:
    - The right chunk is in the top 5 for **43 of 50** questions → **recall@5 = 0.86**.
    - The final answer is correct for **40 of 50** → **accuracy 0.80**.
    - Of the 43 questions where the chunk *was* found, 40 were answered correctly → the model used
      the evidence well **93%** of the time (40 ÷ 43). And 0.86 × 0.93 = 0.80: retrieval caps the
      whole system (§4).
    - For the 10 wrong answers, paste the right chunk into the prompt by hand (the
      *oracle-context test*, §6). **7** become correct → those were retrieval failures. **3** stay
      wrong → those were generation failures.
    - For the 7 retrieval failures, search deeper (top 50 instead of top 5): **1** is never found
      because the scanned PDF parsed to nothing (class (a), §5), **2** are never found because of
      wording mismatch (class (b)), and **4** are found at rank 6–50 but cut before the prompt
      (class (c)).

    So the next week of work goes into the reranker and the context budget (4 questions), the
    parser (1) and hybrid search or query rewriting (2), not into the prompt (3). That split,
    computed from your own failures, is the main tool this chapter gives you.

```
 INGEST (once)                        QUERY (every question)                    AFTER
 parse → chunk → embed → index   ═►   embed query → retrieve → fuse → rerank    evaluate:
 2,000 docs  6,700   1,536-dim  41 MB  12 tokens    50 + 50    ~80   top 5     recall@5 0.86
 3M tokens   chunks  vectors    index               candidates             →   accuracy 0.80
                                       → assemble prompt → generate → answer    oracle test:
                                         ~2,800 tokens     ~150 tokens  +cite   7 retrieval,
                                                                                3 generation
```

### Key terms in this chapter

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| RAG (retrieval-augmented generation) | find relevant passages first, then let the model answer from them | an open-book exam: look it up, then write the answer |
| Corpus | all the documents the system can answer from | the whole library |
| Ingest time vs query time | work done once in advance vs work done on every question | cooking in a restaurant: prep in the morning vs cooking each order |
| Index | a precomputed structure that makes search fast | the library catalogue |
| Materialized view | stored results of a computation, which go stale when the source changes | a printed train timetable: fast to read, wrong after the schedule changes |
| Stale / staleness | the index no longer matches the current documents | an old map that still shows a closed road |
| Reindex / full rebuild | recompute every vector and rebuild the index | reprinting every page of the catalogue |
| Recall ceiling | the system can't be more right than the share of questions where it found the evidence | a detective can't solve a case from clues nobody collected |
| Failure classes (a)–(d) | the four places a wrong answer can come from: not in corpus, not findable, found but cut, shown but misused | a missing parcel: never sent, lost in the warehouse, left off the truck, or delivered and ignored |
| Oracle-context test | give the model the correct passage by hand and see if it answers correctly | give the student the right page; if they still fail, the problem is reading, not finding |
| Recall@k sweep | measure recall at k = 1, 5, 10, 20, 50 to see where the answer sits | checking the first page of results, then the second, then the fifth |
| Two-stage retrieval | a cheap broad search, then an expensive careful one on the few survivors | a CV screen, then interviews for the shortlist |
| Latency budget | how many milliseconds each step may spend | a travel plan: 20 min to the station, 2 h on the train, 10 min walk |
| Cost model | a formula for money per question and per corpus | a phone bill: one-time setup fee + per-minute charges |
| Agentic retrieval | the model decides by itself whether and what to search, maybe several times | a researcher who keeps looking until they have enough sources |
| Trace / span | a timed record of each step for one request | a parcel's tracking history, one line per depot |

### Glossary — every core term used across this track

Use this table as a lookup. The chapter number says where each term is covered in depth.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Token | a word or word-piece; models read, write and bill in tokens | syllables: "unbelievable" = un-believ-able |
| Context window | the maximum number of tokens a model can read at once (`06`) | the size of the desk you can spread papers on |
| LLM (generator) | the language model that writes the final answer (`07`) | the writer who drafts the reply |
| Parsing | turning PDFs, HTML, scans into clean text and tables (`02`) | typing up handwritten notes |
| OCR | reading text from an image or scan (`02`) | someone reading a photo of a page aloud |
| Chunk | one passage of a document, the unit you search and paste (`02`) | an index card with one idea on it |
| Chunk size / overlap | how long each chunk is, and how much neighbours share (`02`) | cutting a long roll of paper into sheets, with each sheet repeating the last line of the previous one |
| Metadata | facts about a chunk: source, date, section, owner, permissions (`02`) | the label on a folder |
| Contextual retrieval | an LLM writes a short note placing each chunk in its document before embedding (`02`, `01`) | a sticky note on a photocopy: "from the 2026 leave policy, section 1" |
| Embedding / vector | a list of numbers that captures what a text means (`01`) | GPS coordinates for meaning |
| Dimension (`d`) | how many numbers are in one vector: 384, 768, 1,536, 3,072 (`01`) | how many questions a survey asks about each item |
| Cosine similarity | a score for how closely two vectors point the same way (`01`, `03`) | two arrows: same direction = similar |
| Asymmetric embedding | the model encodes questions and documents slightly differently (`01`) | a question and its answer look different but belong together |
| Matryoshka (MRL) | embeddings you can shorten (keep the first 256 numbers) and still use (`01`) | a thumbnail of a photo |
| Quantization | storing numbers with less precision to save memory (`01`, `03`) | rounding prices to the nearest euro |
| ANN (approximate nearest neighbour) | a fast search that finds *almost* the closest vectors (`03`) | asking locals for directions instead of checking every street |
| HNSW / IVF | the two most common ANN index designs: a graph of neighbours / buckets of similar items (`03`) | friends-of-friends / supermarket aisles |
| Vector store / vector database | the system that stores vectors and runs ANN search (`03`) | a warehouse organised for fast picking |
| Dense retrieval | search by vector similarity (meaning) (`04`) | "find me something like this" |
| Sparse retrieval / BM25 | search by shared words, weighted by how rare they are (`04`) | Ctrl+F that knows rare words matter more |
| Hybrid search | run dense and sparse together and combine (`04`) | asking two experts and merging their lists |
| RRF (reciprocal rank fusion) | merge ranked lists by adding `1/(60 + rank)` from each list (`04`) | a league table combining points from two tournaments |
| Reranker / cross-encoder | a slower model that reads question + passage together and scores relevance (`04`) | the final interview after a CV screen |
| Bi-encoder | the embedding model: encodes question and passage separately (`01`, `04`) | judging two CVs without meeting the people |
| Top-k / candidate set | the k best results kept after a step (`04`) | the shortlist |
| Filter / ACL | only search chunks this user may see, or that match a condition (`03`, `04`, `17`) | "nearest pharmacy that is open now" |
| Query rewriting / HyDE / decomposition | improve the question before searching: rephrase, draft a fake answer to search with, split into parts (`05`) | a librarian who rephrases your question into catalogue terms |
| Prompt / prompt template | the full text sent to the model: instructions + chunks + question (`06`) | the brief you hand a contractor |
| Context budget | how many tokens you allow for retrieved chunks (`06`) | a suitcase weight limit |
| Lost in the middle | models use facts at the start and end of a long prompt better than in the middle (`06`) | the middle of a long meeting that everyone forgets |
| Context rot | answer quality drops as the prompt gets longer, even with relevant text (`00` §11, `06`) | the more pages in the pile, the more likely you misread one |
| Prompt caching | the provider reuses an identical prompt prefix at a lower price (`06`) | a coffee shop remembering your usual order |
| Structured output | forcing the model to answer in a fixed format such as JSON (`07`) | a form with fixed fields instead of a free letter |
| Hallucination | the model states something not supported by the evidence (`07`, `08`) | a witness filling gaps with guesses |
| Faithfulness / groundedness | does the answer only say what the retrieved chunks support (`08`) | a report that only cites what's in the file |
| Citation | the answer points to the chunk that supports each claim (`06`, `07`) | footnotes |
| Golden set | a fixed list of test questions with known correct answers and chunks (`08`) | the answer key for an exam |
| Recall@k | share of test questions whose correct chunk appears in the top k (`08`) | the right book was on the first shelf you checked |
| Precision@k / MRR / nDCG | other ranking scores: how many top results are right, how high the first right one is (`08`) | how early in the list the right answer shows up |
| LLM-as-judge | using a model to grade answers automatically (`08`) | a teaching assistant marking papers with a rubric |
| Bootstrap CI | a range showing how much a score could move by chance (`08`) | "72% ± 4%" in a poll |
| TTFT (time to first token) | how long until the first word of the answer appears (§8) | how long before the waiter brings anything to the table |
| p50 / p95 / p99 latency | the time that 50% / 95% / 99% of requests are faster than (§8) | "99 of 100 buses arrive within 12 minutes" |
| Trace / span / OTEL | per-request timing records per step, in the OpenTelemetry standard (§13) | a parcel's tracking history |
| Guardrails | checks on inputs and outputs: PII, unsafe content, policy (`17`) | a security check at the door and at the exit |
| Prompt injection | text in a question or document that tries to override the model's instructions (`17`) | a forged note slipped into a stack of real memos |
| Tool calling | the model asks the program to run a function (search, API call) and uses the result (`24`) | an assistant who can pick up the phone |
| Agent / agent loop | a model that plans, calls tools, looks at results, repeats until done (§12, `22`) | a researcher working through a to-do list |
| LangChain / LangGraph | popular libraries for chaining LLM steps and building agent graphs with state (`20`, `21`) | a kit of plumbing parts / a flowchart that runs |
| Memory / state | what a chatbot or agent remembers between turns or sessions (`25`) | notes from the last meeting |
| Model gateway | one service in front of many LLM providers: routing, fallback, cost limits (`23`) | a switchboard |

### Symbols and parameters used in this chapter

This chapter has few formulas, but they use short names. Here is each one.

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `P(correct answer)` | chance the final answer is right | 0.6 – 0.95 | 40 of 50 right → 0.80 |
| `P(evidence retrieved)` | chance the chunk with the answer reaches the prompt (recall at the k you ship) | 0.6 – 0.98 | 43 of 50 → 0.86 |
| `P(model uses it correctly \| retrieved)` | chance the model answers right *when* it has the evidence (faithfulness) | 0.85 – 0.98 | 40 of 43 → 0.93 |
| `≤`, `×` | "at most" and "multiplied by" | — | 0.86 × 0.93 = 0.80, so accuracy is at most 0.80 |
| (a) (b) (c) (d) | the four failure classes of §5 | — | scanned PDF parsed empty = (a) |
| `k` | how many results a step keeps | 5 – 200 | rerank 50, keep 5 |
| generous k vs shipped k | a large k used only for diagnosis (e.g. 50) vs the k that really reaches the prompt (e.g. 5) | 50 vs 5 – 10 | found at rank 30 but only top 5 shipped → class (c) |
| `recall@k` | share of test questions whose answer chunk is in the top k | 0.7 – 0.98 | recall@5 = 0.86 |
| k ∈ {1, 5, 10, 20, 50} | the k values to sweep; `∈` means "is one of" | — | run the same 50 questions 5 times |
| MRR | mean reciprocal rank: average of 1 ÷ (rank of first right chunk) | 0.3 – 0.9 | right chunk at rank 2 → 0.5 for that question |
| τ (tau) | coverage threshold in a hit rule: how much of the labelled text a chunk must contain to count (§16) | 0.5 – 1.0 | τ = 0.8 → chunk must hold 80% of the answer span |
| `N_tokens_corpus` | total tokens you embed at ingest (including overlap and context notes) | 1M – 1T | 6,700 × 500 = 3.35M |
| `price_embed`, `p_embed` | price of the embedding model per million tokens | $0.02 – $0.13 /M | `text-embedding-3-small` = $0.02/M |
| `C_ingest` | one-time cost to embed the corpus | cents to thousands of $ | 3.35M × $0.02/M ≈ $0.07 |
| `C_query` | cost of answering one question | $0.001 – $0.05 | ≈ $0.011 in the HR example |
| `q_tokens` | tokens in the user's question | 5 – 50 | 12 |
| `rerank_cost` | reranker cost per question (price per candidate × candidates) | often under $0.001 per query when hosted | 50 candidates |
| `prompt_tokens` | tokens sent to the generator: instructions + chunks + question | 1,000 – 20,000 | ≈ 2,800 |
| `output_tokens` | tokens the generator writes | 50 – 1,000 | ≈ 150 |
| `p_in`, `p_out` | generator price per million input / output tokens | varies by provider; output costs several times input | illustrative $3 / $15 per M |
| $/M | dollars per million tokens | — | $0.02/M → 1M tokens cost 2 cents |
| `Σ_stages` | "add up over all stages" | — | cost = embed + retrieve + rerank + generate |
| `t_embed_q` | time to embed the question | 5 – 50 ms | 30 ms |
| `t_ann`, `t_sparse` | time for vector search / keyword search | 1 – 50 ms | 15 ms (run in parallel) |
| `t_fuse` | time to merge result lists | < 1 – 2 ms | 1 ms |
| `t_rerank` | time for the reranker | 30 – 300 ms | 120 ms for 50 candidates |
| `t_assemble` | time to build the prompt | 1 – 10 ms | 5 ms |
| `t_ttft` | time to first token from the generator | 200 ms – 2 s | 600 ms |
| `t_decode` | time to write the rest of the answer | 1 – 10 s | 150 tokens at 50 tokens/s = 3 s |
| p50 / p95 / p99 | latency that 50% / 95% / 99% of requests beat | ms or s | p99 = 4 s → 1 request in 100 is slower |
| `efSearch`, `M`, `nprobe` | HNSW / IVF index knobs: search width, links per node, buckets searched (`03`) | 40 – 400, 16, 1 – 5% of buckets | higher = more accurate, slower |
| `input_type` | embedding API flag for "query" vs "document" on asymmetric models (`01`) | `query` / `document` | wrong flag quietly lowers recall |
| `O(candidates)` | work grows in proportion to the number of candidates | — | 2× candidates → 2× fusion work |
| `concurrency=8` | how many oracle tests run at once in the §6 harness | 4 – 32 | 8 calls in flight to the model API |
| CI | confidence interval: the range a measured number could plausibly be in | ±0.02 – 0.10 on 50 – 500 questions | recall 0.72, 95% CI [0.66, 0.78] |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Thesis — an LLM pipeline is a data system

> **In plain words.** A RAG system is not just a clever prompt. It is a pipeline of steps, like a factory line, and a wrong answer can come from any step. Treat it like any other data system: split it into steps, measure each one, and fix the step that is actually broken.
>
> **Real-world example.** The HR bot from Start here answers 40 of 50 test questions correctly. Rewriting the prompt for a week moves that to 41. Measuring each step shows 7 of the 10 failures happen before the model ever sees the evidence, so the prompt was never the main problem.

Start with the two-week version, because you need to see it clearly in order to see past
it. You take a corpus of documents. You split them into chunks. You call an embedding API
on each chunk and get back a vector. You put the vectors in a vector database. At query
time you embed the user's question, ask the vector database for the nearest neighbors,
paste those chunks into a prompt template, and call a chat completion endpoint. It works
on the first try, on the three example queries you tried, and it feels like magic. This is
not a criticism — it is genuinely the fastest path to a working demo of anything in this
folder, and most tutorials, cookbooks and framework quickstarts stop approximately here.

The problem is that "it works on the three example queries" is not an engineering claim.
It's an anecdote. The moment you put that system in front of real queries at real volume,
you get wrong answers, and the two-week version gives you no way to say *why*. Was the
right chunk not in the corpus? Was it in the corpus but never retrieved? Was it retrieved
but pushed out of the context window by less relevant chunks? Was it sitting right there
in the prompt and the model ignored it? Each of these has a different owner, a different
fix, and a different chapter in this folder — and from the outside, all four look
identical: the user asked a question and got a wrong answer.

This is the same shape of problem you already know from databases: a slow query can be a
missing index, a bad plan, lock contention, or a cold cache, and "the query is slow" tells
you nothing about which. Nobody would accept "just add caching" as a diagnosis for a slow
query without first running `EXPLAIN`. Yet "just improve the prompt" is exactly this move,
applied to a RAG pipeline, and it is a very common reflex. The reason is that a
RAG pipeline *looks* like an application concern — a clever prompt, an API call — when it
is structurally a data pipeline with a probabilistic reader at the end: ingestion,
transformation, storage, retrieval, ranking, and a consumption stage that happens to be a
language model instead of a report or a dashboard.

Treating it as a data system buys you three things, in order:

1. **A decomposition.** A wrong answer factors into a small number of independently
   measurable stages (§5), each with its own metric, its own failure mode, and its own
   fix. You stop debugging the whole pipeline and start debugging one stage of it.
2. **A cost model that separates two different economic questions**: what does it cost to
   build the index (amortized, paid once per corpus version), and what does it cost to
   answer one query (paid on every request, forever). Conflating these — treating a
   one-time ingestion cost like it recurs, or treating a per-query cost like it's sunk — is
   a common cost-reasoning error in this space (§9).
3. **A way to know when you're done.** "Better" stops being a feeling about output quality
   and becomes a number, on a versioned dataset, with a stated method of computation — the
   same discipline `../python-mastery/31-measurement-methodology.md` insists on for
   performance work, applied to retrieval quality instead of wall-clock time.

None of this is exotic. It is ordinary systems engineering — the kind you'd apply to a
search index, an ETL pipeline, or a recommendation system — applied without exception to
the newer, shinier thing that has a chat interface bolted onto it. The rest of this chapter
builds the vocabulary and the diagnostic procedure. The chapters after it build the pieces.

---

## 2. The pipeline as dataflow

> **In plain words.** There are two separate systems. One runs once, ahead of time: it reads the documents, cuts them into chunks, turns them into vectors and builds the index. The other runs on every question: search, rerank, build the prompt, generate. They only meet at the index.
>
> **Real-world example.** Embedding 6,700 HR chunks costs about 7 cents, once. Generating one answer costs about 1 cent, every time. At 1,000 questions a day the per-question side costs about $320 a month, so a one-time ingest step that makes each prompt smaller is usually worth it.

Draw the whole thing as one diagram before splitting it into stages, because the shape of
the diagram is the first thing that's usually wrong in people's heads: they think of it as
one system, when it's structurally two systems glued at the index.

```
 INGEST-TIME  (batch, amortized, runs on your schedule)
 ══════════════════════════════════════════════════════════════════════════
 corpus ──▶ parse ──▶ chunk ──▶ represent(embed) ──▶ index
   │           │          │             │              │
 raw files   clean     bounded       dense/sparse    ANN graph /
 (PDF, HTML,  text,    passages     vectors (+ optional  inverted list /
  Markdown,   struct   w/ metadata   sparse terms)      hybrid store,
  DB rows)    stripped                                  on disk + memory


 QUERY-TIME  (per-request, latency-bound, runs on every user turn)
 ══════════════════════════════════════════════════════════════════════════
 user query ──▶ embed(query) ──┬─▶ retrieve (dense) ──┐
                                 └─▶ retrieve (sparse) ──┼─▶ fuse ──▶ rerank ──▶ assemble
                                                          │                        context
                                                          ▼
                                                     candidate set
                                                     (recall-oriented,
                                                      cheap, broad)

 assemble context ──▶ generate (LLM) ──▶ response
        │                    │
   prompt template      streamed tokens,
   + budget + citations   TTFT-sensitive

 ══════════════════════════════════════════════════════════════════════════
                          observe (both time domains)
       every stage above emits a span, a metric, and (for a sample) a
       full record — this is §13, and it is not optional instrumentation
       bolted on after the fact, it is how you answer "which stage broke"
```

The critical structural fact this diagram is trying to force into view: **ingest-time and
query-time are different systems with different cost models, different latency budgets,
and different failure modes, and they only share one interface — the index.** Ingest-time
runs once per corpus version, off the request path, and you can throw arbitrary compute at
it (an LLM call per chunk, a slow high-quality reranker-grade embedding pass) because the
cost is amortized over every future query. Query-time runs on every single request, is
latency-bound by a human waiting for tokens, and every millisecond and every token you
spend there is multiplied by request volume.

People conflate these constantly, in both directions. They avoid a genuinely cheap
ingest-time improvement (Anthropic's contextual retrieval, §9 — a one-time $1.02 per
million document tokens) because it "sounds expensive," while thinking in the same breath
about the per-query cost of running a cross-encoder reranker on every request. Or the
inverse: they cache nothing at ingest time and recompute expensive per-chunk metadata on
every query because the ingest/query boundary was never made explicit in the code, only in
someone's head. The fix is definitional, and cheap: label every stage with which time
domain it belongs to, and never let a query-time stage do work whose cost should have been
paid at ingest-time — or vice versa, never let a query-time system depend on freshness that
only an ingest-time job can guarantee (that's §3).

Stage-by-stage table — this is the reference you'll come back to when something breaks:

| Stage | Input | Output | When it runs | What it costs | What breaks |
|---|---|---|---|---|---|
| Parse | Raw files (PDF, HTML, DB rows, Markdown) | Clean text + structural metadata | Ingest-time | CPU/IO, occasionally OCR/VLM calls for scanned docs | Tables flattened wrong, headers lost, boilerplate kept, encoding garbage |
| Chunk | Clean text | Bounded passages + metadata (source, offsets, section) | Ingest-time | CPU, negligible | Boundary cuts mid-fact; chunk too small to be self-contained, too large to be precise |
| Represent (embed) | Chunk text (+ optional context prefix) | Dense vector, optional sparse terms | Ingest-time (docs), query-time (query) | $/M tokens to the embedding API or GPU-hours self-hosted | Wrong `input_type` (asymmetric models), truncation silently dropping the tail, dimension mismatch after MRL truncation without renormalization |
| Index | Vectors + metadata | ANN graph / inverted index / hybrid store | Ingest-time (build), query-time (probe) | Build: CPU+memory, once. Probe: sub-ms to tens of ms per query | Stale index vs. corpus, recall/latency mistuned (`efSearch`, `M` — see `../databases/11-hnsw-vector-search-internals.md`) |
| Retrieve | Query representation | Ranked candidate list per method (dense, sparse) | Query-time | ANN probe cost, BM25 scan cost | Query embedded with wrong asymmetric setting; candidate set too narrow (low k) |
| Fuse | Multiple ranked lists | One merged candidate list | Query-time | Cheap (RRF is O(candidates)) | Fusion weights untuned, one method dominates silently |
| Rerank | Merged candidates | Reordered, truncated candidates | Query-time | Cross-encoder inference cost × candidates examined | Latency blown past budget, reranker trained on mismatched domain |
| Assemble context | Reranked chunks | Final prompt | Query-time | Prompt-construction CPU, negligible | Budget overflow silently truncates, no citation mapping, duplicate/contradictory chunks kept |
| Generate | Prompt | Response tokens | Query-time | $/M input + output tokens, TTFT + decode latency | Model ignores retrieved evidence, hallucinates past it, or mixes stale and fresh chunks without flagging conflict |
| Observe | Every stage above | Spans, metrics, sampled records | Both | Storage + query cost of the telemetry backend itself | No stage-level breakdown, no trace↔eval join, sampling that drops the interesting requests |

Nine pipeline stages plus observation (which spans both time domains) — ten rows, two time
domains, one shared interface between them. Everything that follows is about making that
interface — the index — and the diagnostic path through these rows precise enough to
actually use under pressure.

---

## 3. The index is a materialized view over the corpus

> **In plain words.** The index is a copy of your documents in a searchable form, built ahead of time. Like any copy, it goes out of date when the originals change, and it must be rebuilt completely if you change how it was made (for example, a new embedding model).
>
> **Real-world example.** HR updates the leave policy from 15 to 18 first-year days on 1 March. If the index is refreshed weekly, the bot can give the old answer for up to 7 days. If HR deletes an old policy PDF and nobody removes its chunks, the bot keeps quoting it indefinitely.

This is the load-bearing framing of the whole chapter, so take it slowly: **a vector index
(or a hybrid dense+sparse index) is a materialized view over the corpus, computed once at
ingest-time and consulted many times at query-time.** Everything you already know about
materialized views from [`../databases/06-indexing-internals.md`](../databases/06-indexing-internals.md)
transfers directly, and none of it is optional trivia — it is the mechanism that explains
almost every operational headache in a production RAG system. The storage-engine mechanics
underneath that transfer too: how a page is written, when it's durable, and what a rebuild
actually does to disk are exactly the concerns in
[`../databases/01-storage-engine-fundamentals.md`](../databases/01-storage-engine-fundamentals.md),
and for the HNSW case specifically — the graph itself, with its layered proximity edges, *is*
the materialized view, and the graph-construction cost documented in
[`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md)
is exactly why a full rebuild (below) is expensive rather than free.

A materialized view is a precomputed answer to a specific class of question, stored so
that answering it again is cheap. A B-tree index precomputes "which rows have this key in
this range." A vector index precomputes "which chunks are near this point in embedding
space." Both are derived data: correct only insofar as they stay synchronized with the
source of truth, and both impose write cost in exchange for read speed. Once you see the
vector index this way, several consequences that people treat as separate, ad hoc RAG
problems turn out to be the *same* consequence, restated:

**Staleness.** A materialized view answers questions about the state of the corpus at the
time it was last refreshed, not the corpus as it is now. If a document changes after
ingestion and the index isn't updated, every subsequent query against that region of the
corpus returns an answer that was true and is no longer true. This is not an AI-specific
bug; it's the identical staleness problem every cached read-model has, and it needs the
identical answer: a defined refresh policy (§ below) and a staleness SLO someone actually
owns — see `../sre-observability/13-slo-engineering.md`.

**Invalidation.** When source documents are deleted or superseded, their vectors don't
disappear from the index automatically. You need an explicit invalidation path — soft
delete flags checked at query time, or physical removal from the ANN graph — or the index
keeps confidently returning chunks from documents that no longer exist. This is the vector
store's version of the classic cache invalidation problem, and it is exactly as hard here
as it is everywhere else it appears in this repo.

**Incremental refresh vs. full rebuild.** Most materialized views support incremental
maintenance: apply only the delta since the last refresh. Vector indexes support this too,
up to a point — you can append new vectors to an HNSW graph without touching existing
edges, or delete-and-mark rather than physically rebuild. But some changes are not
incremental by nature; they invalidate the *entire* view, not a row of it. That's the next
point, and it's the one people don't see coming. Whichever refresh strategy you pick, the
durability question is the same one a database's write-ahead log answers: if the process
applying an incremental update crashes mid-write, is the index left half-updated or does it
recover to a consistent state — see
[`../databases/14-write-ahead-log-internals.md`](../databases/14-write-ahead-log-internals.md)
for the mechanism, because "the vector store ate a batch of upserts and nobody noticed" is
a WAL problem wearing a RAG costume.

**Write amplification.** Every document, once ingested, produces one row of derived state
per chunk per representation (a dense vector, optionally sparse terms, optionally a
contextual-retrieval blurb). A single source-document edit doesn't touch one row — it
invalidates every chunk boundary and every vector downstream of that document, because
chunk boundaries shift and each shifted chunk needs to be re-embedded. Small upstream
change, disproportionate downstream write cost. This is the same amplification factor you
already reason about for LSM-tree compaction or secondary-index maintenance
(`../databases/13-lsm-trees-and-compaction.md` if you want the general mechanism); it
shows up here as "why did re-ingesting one changed PDF trigger re-embedding of the whole
document."

**Schema change forces full rebuild — the one people don't expect.** In a relational
database, changing a column's type is a migration, and you know it's a migration going in.
In a RAG pipeline, swapping the embedding model *feels* like changing a config value —
it's one line in a YAML file — but it is exactly the schema-change case. Vectors from two
different embedding models are not comparable: different dimensionality, different
training objective, different geometry. There is no meaningful cosine similarity between a
`text-embedding-3-small` vector and a `voyage-4` vector. Swapping the model means every
existing vector in the index is now garbage with respect to new queries, which means the
entire corpus must be re-embedded and re-indexed before the new model can serve a single
correct query. This is developed fully in §10 and in
[`01-embeddings-and-representation.md`](01-embeddings-and-representation.md); the point to
absorb here is structural: **the embedding model is part of the index's schema, not a
runtime parameter**, and the materialized-view framing is precisely why that's true — a
view's definition includes the function that computed it, and changing the function
invalidates the whole view, not a slice of it.

**The classic DB lesson applies unmodified: an index is a tradeoff, not a win.** Every
index — B-tree, inverted, HNSW — buys faster reads by spending write throughput, storage,
and staleness risk. Nobody would add a database index to every column "just in case";
you'd reason about read/write ratio, selectivity, and maintenance cost first. Apply the
same discipline here: a second embedding representation (say, a domain-fine-tuned model
alongside a general one), a second index for a filtered subset, a rerank-stage cache — each
of these is a tradeoff you're choosing, with a maintenance bill attached, not a free
quality upgrade. `../databases/06-indexing-internals.md` develops this argument for
databases in full; nothing about the vector case exempts it.

The refresh-policy decision table you'll actually need, once you accept the materialized-
view framing:

| Refresh strategy | When it fits | Cost shape | Staleness bound |
|---|---|---|---|
| Full rebuild | Small-to-medium corpus, infrequent changes, or any embedding-model swap | O(corpus size), spiky, all-at-once | Zero staleness right after rebuild, then grows until next one |
| Incremental append | New documents only, no edits/deletes to existing ones | O(delta), steady | Bounded by refresh interval |
| Incremental append + soft delete | New + deleted documents, no in-place edits | O(delta) + query-time filter overhead | Bounded by refresh interval; deleted docs filtered, not purged, until compaction |
| Streaming upsert | High-frequency changes, freshness SLO in minutes | O(delta) continuous, requires a queue and idempotent writers | Bounded by pipeline lag, often the tightest and most expensive option |

`15-ingestion-pipelines-and-freshness.md` (planned) is where this table gets built out into
an actual system; here it's enough to see that "how fresh does retrieval need to be" is a
design input you choose at the start, not a property that falls out of whichever vector
database you happened to pick.

---

## 4. Where correctness actually lives: the recall ceiling

> **In plain words.** The model can only answer from what you show it. If the right passage never reaches the prompt, even a perfect model can't give the right answer from your documents. So measure whether search finds the evidence before you touch the prompt.
>
> **Real-world example.** Search finds the right chunk for 43 of 50 questions (0.86). The model answers 40 of those 43 correctly (0.93). 0.86 × 0.93 = 0.80. A better prompt can at best move 0.93 toward 1.0, which gives at most 0.86 overall. Better search can move both numbers.

Here is the chain argument, stated as a formula because a formula is harder to weasel out
of than a paragraph:

```
P(correct answer) ≤ P(evidence retrieved) × P(model uses it correctly | retrieved)
```

Read the right-hand side left to right. `P(evidence retrieved)` is retrieval recall at
whatever context budget you actually ship: the probability that the chunk(s) containing the
answer are present in the assembled context handed to the model. `P(model uses it correctly
| retrieved)` is generation faithfulness: given that the evidence *is* in front of the
model, the probability it reads it correctly, doesn't contradict it, and doesn't get
distracted by an irrelevant chunk sitting next to it. The product is an upper bound on
end-to-end correctness, and it's an upper bound for a reason worth sitting with: **if the
supporting chunk never makes it into the context, no amount of prompt engineering,
few-shot examples, chain-of-thought, or model upgrade can recover it.** For a fact that
exists only in your corpus (a private policy, a customer's contract — the case RAG exists
for), the generator cannot produce a correct answer from evidence it was never shown; it can
only guess, or answer from general training knowledge that happens to match. This isn't a claim about how good current models are; it holds for a perfect generator too
— a perfect reader of an empty book still can't answer a question the book doesn't
contain.

This inequality is the single strongest argument in the chapter for *why retrieval is the
first thing to measure and generation is the last thing to tune*, and it's worth being
explicit about why the industry so often does it backwards. Generation quality is
*visible*: you read the response and it sounds wrong, so you edit the prompt, add
instructions, try a bigger model, and the response looks better on the three examples you
just tried — visible, fast feedback, satisfying. Retrieval quality is *invisible* unless
you specifically go measure it: you don't see the ten chunks that got compared and lost,
you just see the transcript the model produced from whatever won. So people iterate on the
part they can see, which multiplies effort on the term of the inequality that's usually
already close to 1, while the term that's actually capping the product goes unmeasured.
Concretely: if retrieval recall at your context budget is 0.6, then no matter how good
generation faithfulness is, end-to-end correctness cannot exceed 0.6. Spending a week on
prompt engineering to try to close that gap is spending effort on a term that mathematically
cannot fix the problem.

The practical consequence: **measure `P(evidence retrieved)` — recall@k on a golden set —
before you touch a single word of the prompt.** If it's low, you have an ingestion,
chunking, or ranking problem (§5a–c), and the fix lives upstream of the model entirely. If
it's high and the answer is still wrong, *now* you're diagnosing generation (§5d), and now
prompt work is the right lever. Doing this in the wrong order doesn't just waste time, it
actively misleads you: a prompt tweak that happens to nudge the model toward guessing
correctly on a retrieval-broken query will look like progress and will not generalize,
because it fixed a symptom of a term that's still capped.

One caveat, stated honestly because the inequality above is a clean idealization and real
systems have a wrinkle: `P(evidence retrieved)` is really `P(evidence retrieved | context
budget)`, since retrieval and the assembly step that truncates to a token budget are
coupled — a chunk can be retrieved (present in the ranked list) and still lost before
generation if it's ranked below the cutoff. That's exactly failure class (c) in §5, and
it's precisely why the recall you measure has to be recall *at the k you actually ship*,
not recall at some generous k that never makes it into the real prompt.

---

## 5. The four irreducible failure classes

> **In plain words.** Every wrong answer comes from one of four places: (a) the answer is not in the documents the system has, (b) it is there but search can't find it, (c) search found it but it was cut before reaching the prompt, or (d) the model had it and still got it wrong. All four look the same to the user.
>
> **Real-world example.** Of the HR bot's 10 wrong answers: 1 is a scanned PDF that parsed to nothing (a), 2 use "vacation" where the policy says "annual leave" (b), 4 were found at rank 6–50 but only the top 5 reach the prompt (c), and 3 had the right chunk in the prompt and still went wrong (d). Four different fixes.

Every "the RAG system gave a wrong answer" report factors into exactly one of four classes.
The classes are irreducible in the sense that they map onto disjoint stages of the pipeline
in §2 — fixing one never fixes another, and conflating them is why so much RAG debugging
goes in circles.

```
                     ┌─────────────────────────────────────────┐
                     │   USER-VISIBLE SYMPTOM (identical        │
                     │   across all four): "wrong answer"       │
                     └─────────────────────────────────────────┘
                                       │
        ┌──────────────┬──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   (a) NOT IN      (b) IN CORPUS   (c) RETRIEVED   (d) IN CONTEXT
       CORPUS          NOT           RANKED OUT       BUT UNUSED /
                    RETRIEVABLE      OF BUDGET         MISREAD /
                                                        CONTRADICTED
```

**(a) Not in corpus — an ingestion problem.** The document containing the answer was never
ingested, was ingested but parsed so badly the fact was lost (a table flattened into
unreadable text, an image with no OCR/caption, a section silently dropped by the parser),
or was deleted/never synced from the source system. Symptom the user reports: "it says it
doesn't know" or confidently answers from something adjacent and wrong. Metric that catches
it: recall@k stays at zero for this query no matter how high you push k, because the
target isn't in the index at all — you can confirm this directly by grep-ing the raw
corpus for the expected answer text. Fix: ingestion coverage, parser quality, sync
freshness. Chapter: `02-chunking-and-document-processing.md` (parsing) and
`15-ingestion-pipelines-and-freshness.md` (coverage and sync).

**(b) In corpus but not retrievable — a representation/chunking problem.** The fact is in
the index, but the chunk containing it never surfaces near the top of the ranked list for
this query — because the chunk boundary split the fact from the context needed to make it
findable ("the company" three sentences after the paragraph that named the company), the
embedding model doesn't capture the relevant similarity for this domain, or the query and
the document use different vocabulary and there's no lexical (BM25) path to bridge that
gap. Symptom: "it says it doesn't know" or hallucinates, same as (a), which is exactly the
diagnostic trap. Metric: recall@k is nonzero at some k but low at the k you actually ship —
sweeping k (§6) tells you whether the chunk exists in the index at all and just needs a
deeper search, or whether it's fundamentally unrankable for this query. Fix: chunking
strategy, contextual/late chunking, hybrid retrieval, embedding model choice. Chapters:
`02-chunking-and-document-processing.md`, `01-embeddings-and-representation.md`,
`04-retrieval-hybrid-and-reranking.md`.

**(c) Retrieved but ranked out of the context budget — a ranking/budget problem.** The
chunk is in the top-k candidate set at some stage of the pipeline, but by the time fusion,
reranking, and context assembly are done trimming to whatever token budget you ship, it
didn't make the cut. This is functionally distinct from (b): the representation *did* find
it, the *ranking or budgeting* discarded it. Symptom: identical wrong-answer report.
Metric: recall@k is high at generous k (say k=50) but the effective recall at the shipped
context budget is much lower — the gap between those two numbers is exactly the size of
this failure class. Fix: reranker quality, fusion weights, larger context budget (with the
cost consequences from §9), better truncation policy. Chapters:
`04-retrieval-hybrid-and-reranking.md`, `06-context-engineering.md`.

**(d) In context but unused, misread, or contradicted — a generation/faithfulness
problem.** The evidence is sitting in the prompt, verbatim, and the model still gets it
wrong: it ignores the retrieved chunk in favor of parametric knowledge, misreads a number,
conflates two similar chunks, or fails to notice that one retrieved chunk contradicts
another (stale doc alongside a fresh one) and picks the wrong one. Symptom: again,
identical — "wrong answer." Metric: this is the one class the oracle-context test in §6
isolates directly — feed the known-correct chunk by hand, bypassing retrieval entirely, and
if the model *still* gets it wrong, the failure is here. Fix: prompt structure, citation
requirements, explicit conflict-flagging instructions, smaller/cleaner context (fewer
distractor chunks), possibly a stronger model. Chapters: `06-context-engineering.md`,
`07-generation-and-structured-output.md`.

| Class | Symptom | Diagnostic metric | Owning fix | Chapter |
|---|---|---|---|---|
| (a) Not in corpus | "It doesn't know" / confidently wrong | Recall@k = 0 at all k; confirm via corpus grep | Ingestion coverage & parsing | `02`, `15` |
| (b) In corpus, not retrievable | "It doesn't know" / hallucinates | Recall@k low even at large k | Chunking, representation, hybrid retrieval | `01`, `02`, `04` |
| (c) Retrieved, ranked out of budget | Wrong or partial answer | Recall@k(generous) − recall@k(shipped) is large | Reranking, fusion, budget policy | `04`, `06` |
| (d) In context, unused/misread | Wrong answer despite right evidence present | Oracle-context test still fails | Prompt design, faithfulness, citations | `06`, `07` |

The table's last column is the whole point of doing this exercise: four distinct one-line
fixes, four distinct owning chapters, and a *single* user-visible symptom that gives you no
information about which one you're looking at until you measure. §6 is the procedure for
actually distinguishing them on a real failing query.

---

## 6. Diagnosis: attributing a failure to a stage

> **In plain words.** Two simple tests tell you which of the four classes you have. First, paste the correct passage into the prompt by hand. If the answer becomes right, search was the problem. Then check how deep in the result list the passage sits, to tell "never found" from "found but cut".
>
> **Real-world example.** 10 wrong answers, oracle test on each: 7 become right, 3 stay wrong. For the 7, check the top 50: 4 are there (ranked 6–50, so ranking or budget), 3 are not. Search the raw text for those 3: 1 isn't in the corpus at all, 2 are. Total time: an afternoon, not a week of prompt tweaks.

Two instruments do almost all of the work: the **oracle-context test** separates (a)+(b)+(c)
from (d), and the **recall@k sweep** separates (a) from (b) from (c). Run them in that
order, because the oracle-context test is cheaper and answers the higher-order question
first — "is this a retrieval problem or a generation problem at all" — before you spend
effort subdividing the retrieval side.

**The oracle-context test.** Take a query where the pipeline produced a wrong answer. You
(or your golden set, see §16) already know which chunk(s) actually contain the answer —
that's the whole point of building a golden set first. Bypass retrieval entirely: construct
the prompt by hand, inserting the known-correct chunk directly into the context, and call
the generator. Two outcomes:

- **Answer is now correct** → the evidence works when present; the failure was upstream,
  somewhere in (a)/(b)/(c). Retrieval — or the ranking/budget path — did not deliver the
  evidence in the real run. Go to the recall@k sweep to find out which.
- **Answer is still wrong** → the model had the right evidence in front of it and still
  failed. This is class (d), generation/faithfulness, and no amount of retrieval tuning
  will touch it. Go work on prompt structure and faithfulness instead.

This single test is disproportionately high-leverage because it converts a fuzzy "is
retrieval or generation to blame" argument into a binary, reproducible, five-minute
experiment per failing query.

```python
# oracle_context_harness.py — sketch, not production code
import asyncio
from dataclasses import dataclass

@dataclass
class GoldenCase:
    query: str
    answer_bearing_chunk_ids: list[str]
    expected_answer_substring: str  # or a judge function

async def run_oracle_test(
    case: GoldenCase,
    chunk_store: "ChunkStore",       # lookup by id, bypasses the retriever entirely
    generate: "Callable[[str, list[str]], Awaitable[str]]",
    judge: "Callable[[str, str], bool]",
) -> dict:
    oracle_chunks = [chunk_store.get(cid) for cid in case.answer_bearing_chunk_ids]
    answer = await generate(case.query, oracle_chunks)
    passed = judge(answer, case.expected_answer_substring)
    return {
        "query": case.query,
        "oracle_passed": passed,
        # oracle_passed=False -> generation/faithfulness failure (class d)
        # oracle_passed=True  -> failure is upstream of generation; sweep recall@k next
    }

async def run_suite(cases: list[GoldenCase], chunk_store, generate, judge, concurrency=8):
    sem = asyncio.Semaphore(concurrency)
    async def bounded(case):
        async with sem:
            return await run_oracle_test(case, chunk_store, generate, judge)
    return await asyncio.gather(*(bounded(c) for c in cases))
```

The important design choice in that sketch: `chunk_store.get(cid)` is a direct lookup by
chunk id, not a call into the retriever. That's what makes it an *oracle* — it removes
retrieval from the loop entirely, on purpose, so the only variable left is generation. If
you accidentally route this through the real retrieval path "for convenience," you've built
a slightly different retrieval test, not an oracle-context test, and it will not tell you
what you think it tells you.

**The recall@k sweep.** For failures the oracle test pushed upstream, sweep k — 1, 5, 10,
20, 50 — and check at each k whether the answer-bearing chunk appears anywhere in the
top-k candidate list, *before* reranking and budget truncation. This separates:

- **Recall@k stays at (or near) zero even at k=50** → the chunk is essentially unfindable
  by the current representation for this query. That's class (a) if it isn't in the index
  at all, or class (b) if it is indexed but the representation can't surface it. Check
  which by directly searching the raw corpus text for the answer; if it's not there, it's
  (a), otherwise it's (b).
- **Recall@k is high at k=50 but you only ship k=10 (post-rerank, post-budget)** → the
  representation found it, but ranking or the budget truncation is throwing it away.
  That's class (c).

```
   Failure on query Q
          │
          ▼
   ┌─────────────────────┐
   │ Oracle-context test  │
   └─────────┬────────────┘
             │
     ┌───────┴────────┐
     ▼                ▼
  still wrong      now correct
     │                │
     ▼                ▼
 (d) generation   recall@k sweep
     fix: §6/07        │
                ┌───────┴────────┐
                ▼                ▼
          zero at all k    high at large k,
                │           low at shipped k
                ▼                ▼
        in corpus? ─No→ (a)   (c) ranking/budget
             │Yes              fix: §7/04/06
             ▼
        (b) representation
            fix: 01/02/04
```

Two edge cases in this procedure trip people up, and both are worth naming explicitly
rather than discovering the hard way. First, a query can have **multiple answer-bearing
chunks** — the fact is stated in more than one place in the corpus, or the answer requires
synthesizing two chunks together. Recall@k in that case has to be defined precisely: does
the query count as a hit if *any* answer-bearing chunk is retrieved, or does it require
*all* of them? Multi-hop questions need the stricter "all" definition, or you'll report a
recall number that looks healthy while the pipeline is actually failing every synthesis
query it sees — pick the definition before you run the sweep, and write it down next to
the number, because "recall@10" without that qualifier is not a fully specified metric.
Second, the oracle-context test itself has a subtler failure mode worth watching for: if
you feed the oracle chunk *alongside* a full realistic set of distractor chunks (rather than
alone), a "still wrong" result conflates two different things — the model failing to use
correct evidence (class d) versus the model being distracted by the other chunks in the
context (also arguably class d, but with a different fix: fewer distractors, not better
instructions). If you want the cleanest signal, run the oracle test with *only* the correct
chunk(s) in context first; only add realistic distractors as a second pass once you know
the model can use the evidence in isolation at all.

Run both instruments over your whole golden set, not one query at a time by hand, and you
get exactly the split §16's exercises ask you to report: what percentage of failures are
retrieval versus generation, and within retrieval, what percentage are (a) versus (b)
versus (c). That percentage split, computed from real failures on your corpus, is worth
more than any leaderboard number — it's *your* failure distribution, not a vendor's.

---

## 7. Two-stage retrieval is classical IR

> **In plain words.** Search in two rounds. A fast, rough search picks about 50–200 candidates from the whole collection. A slow, careful model then scores only those candidates. Never run the careful model on everything.
>
> **Real-world example.** The HR bot has 6,700 chunks. At an illustrative ~2 ms per chunk, a cross-encoder over all of them would take about 13 seconds per question. Over 50 candidates from the fast search it takes about 100–120 ms.

Strip away the LLM and RAG's retrieval half is a textbook information retrieval system:
a **recall-oriented candidate generation stage** followed by a **precision-oriented
reranking stage**. This isn't a design choice invented for RAG — it's decades old, and the
reason it's the right shape is a cost argument that should feel familiar from database
access-method selection.

Stage one has to look at a very large fraction of the corpus — potentially all of it — so
it must be cheap per item examined: an ANN probe (HNSW, IVF) or a BM25 postings-list scan,
both sub-millisecond to low-millisecond per query even against millions of chunks, because
they're built specifically to avoid comparing the query against every document. Stage one's
job is not to get the ranking right, it's to *not lose the answer* — recall over precision,
by design, at whatever k you choose to carry forward. Stage two — reranking, usually a
cross-encoder that jointly attends over the query and each candidate — is far more accurate
at judging true relevance and far more expensive per item, because it can't be indexed and
precomputed the way stage one's vectors can; it has to run inference on every
(query, candidate) pair at query time. Running it against the full corpus would be
computationally absurd. Running it against the 50–200 candidates stage one already
narrowed down to is cheap enough to fit a latency budget (§8).

The general principle: `cost_per_query ≈ Σ_stages (cost_per_candidate_at_stage ×
candidates_examined_at_stage)`. You minimize this by making the cheap stage examine many
candidates and the expensive stage examine few, in that order — never the reverse. This is
*exactly* the reasoning [`../databases/03-access-methods-and-table-scans.md`](../databases/03-access-methods-and-table-scans.md)
develops for choosing between a full table scan, an index scan, and a bitmap-and-recheck
plan: use a cheap, imprecise filter to shrink the candidate set fast, then spend expensive
precise work only on what survived the filter. A query planner choosing "index scan then
recheck the predicate against the row" versus "just scan the table" is making the identical
cost-shape tradeoff as a retrieval pipeline choosing "ANN search then cross-encoder
rerank" versus "cross-encoder every document." Selectivity is the variable in both cases:
the more the cheap stage can shrink the candidate set without losing the true positive,
the more budget is left for the expensive stage to spend on precision. The moment a query
adds metadata filters — "only chunks from documents tagged `finance`," "only the last 90
days" — retrieval stops being a pure ANN problem and becomes a filtered-search problem:
whether the filter is applied before the ANN search (shrinking the graph traversal),
after it (rechecking each candidate), or pushed into the index structure itself is the
same predicate-pushdown and join-ordering reasoning
[`../databases/04-query-engine-internals.md`](../databases/04-query-engine-internals.md)
covers for relational queries — get the order wrong and you either scan far more of the
graph than necessary or silently under-fill k after the filter discards most of the
candidates.

One consequence worth stating plainly because it's counterintuitive to people used to
thinking "more retrieval = better": there is a real tension between stage-one recall and
stage-one k. A larger k costs more at *every* downstream stage — more candidates for fusion
to merge, more candidates for the reranker to score (which is the dominant cost, since
reranking is the expensive stage), and a larger candidate pool for the budget-truncation
step in §5c to have to discard from. Choosing k for stage one is choosing a point on the
recall/cost tradeoff curve, not choosing "as much as possible" — this is developed with
actual mechanics (HNSW's `efSearch`, IVF's `nprobe`) in
[`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md),
and with the reranking-specific budget math in `04-retrieval-hybrid-and-reranking.md`
(planned).

---

## 8. The latency budget

> **In plain words.** Every step takes time, and the user waits for the sum. Write down how many milliseconds each step may use. The number users feel most is the time until the first word appears (TTFT), because the rest streams in.
>
> **Real-world example.** Embed 30 ms + search 15 ms + fuse 1 ms + rerank 120 ms + prompt 5 ms + TTFT 600 ms = 771 ms until the first word. The remaining 150 tokens take about 3 more seconds but arrive while the user is already reading.

Every query-time stage in §2's dataflow spends latency, and the sum of them is what the
user waits for. Write the budget down as a table with symbolic placeholders — you fill in
real numbers by measuring your own system, per §16's exercises, never by copying someone
else's:

| Stage | Symbol | Typical driver | Notes |
|---|---|---|---|
| Embed query | `t_embed_q` | API round-trip or local model forward pass | Usually small, but a network hop to an external embedding API is not free |
| ANN search | `t_ann` | Index size, `efSearch`/`nprobe`, filter selectivity | Sub-ms to tens of ms; filtered search costs more, see [`../databases/11-hnsw-vector-search-internals.md`](../databases/11-hnsw-vector-search-internals.md) |
| Sparse search | `t_sparse` | Postings-list length, term count | Often parallel with ANN search, not additive if run concurrently |
| Fuse | `t_fuse` | Candidate count | Cheap, O(candidates), rarely the bottleneck |
| Rerank | `t_rerank` | Cross-encoder inference × candidates examined | Frequently the single largest query-time cost — see §7's cost-shape argument |
| Assemble context | `t_assemble` | Prompt construction, template rendering | Cheap unless you're doing nontrivial compaction/summarization here |
| TTFT | `t_ttft` | Model, prompt length, provider load, prompt caching | The number the user actually *feels* first |
| Generation (decode) | `t_decode` | Output tokens × per-token latency | Streamed, so perceived cost is spread out, not paid up front |

Total wall-clock latency is roughly `t_embed_q + max(t_ann, t_sparse) + t_fuse + t_rerank +
t_assemble + t_ttft + t_decode` (some of these overlap if you pipeline them; sequential
sum is the pessimistic bound worth budgeting against). The point of writing it this way,
symbolically, is that **reranking spends latency to buy precision, and that spend has to
be a deliberate line item in this budget, not something that shows up as a surprise in
production the day someone adds a reranker to "improve quality."** A cross-encoder rerank
over 100 candidates can easily cost more wall-clock time than the entire rest of the
retrieval path combined; whether that's worth it is a real tradeoff against a latency SLO,
not a free upgrade.

Streaming changes which number the user actually experiences. Without streaming, the user
waits for `t_ttft + t_decode` in full before seeing anything. With streaming, the user sees
the first token at `t_ttft` and then a steady trickle — the *perceived* latency is
dominated by TTFT, not total generation time, even though total generation time is what
you'd naively sum. This is why TTFT specifically, not end-to-end latency, is the metric
people obsess over in inference observability: it's the number that determines whether the
interaction feels responsive.
[`../gpu-observability/14-llm-inference-observability.md`](../gpu-observability/14-llm-inference-observability.md)
covers TTFT/inter-token latency/batching-and-queueing mechanics for anyone self-hosting the
generator, in the depth this chapter has no room for;
[`../sre-observability/13-slo-engineering.md`](../sre-observability/13-slo-engineering.md)
covers how to turn a latency number like this into an SLO with an error budget rather than
a vague aspiration, and
[`../sre-observability/12-alerting.md`](../sre-observability/12-alerting.md) covers what
fires when the budget is being burned. Set your latency SLO on TTFT and a tail (p95/p99)
end-to-end number, not on a mean — a mean hides exactly the timeouts and retries that make
users abandon a query.

---

## 9. The cost model

> **In plain words.** There are two bills. Building the index is paid once per version of your documents. Answering questions is paid on every request, forever. The biggest part of the per-question bill is usually the text you paste into the prompt, so sending fewer, better chunks saves money as well as improving answers.
>
> **Real-world example.** HR bot: ingest ≈ $0.07 once. Per question ≈ $0.011, almost all of it the ~2,800 prompt tokens and 150 output tokens (illustrative $3/M and $15/M prices). Cutting the prompt from 5 chunks to 3 saves 1,000 input tokens, about $0.003 per question or about $90 a month at 1,000 questions a day.

Same discipline as §2's time-domain split, applied to money instead of milliseconds:
**ingest cost is paid once per corpus version and amortizes over every future query;
query cost is paid on every single request and multiplies by volume.** Conflating the two
— reasoning about a one-time ingestion expense as if it recurred per query, or the
reverse — is a common cost-modeling mistake in this space, and it's an easy one to
avoid once the two formulas are written down separately.

**Ingest cost**, symbolically:

```
C_ingest = N_tokens_corpus × price_embed          (+ optional LLM cost if contextualizing)
```

`N_tokens_corpus` is every token across every chunk you embed — note this can exceed raw
corpus token count if you're prepending contextual blurbs (§9's second term) or embedding
overlapping chunk windows. `price_embed` is whatever your chosen model actually charges;
from the verified fact sheet, OpenAI's `text-embedding-3-small` is **$0.02 per million
tokens** and `text-embedding-3-large` is **$0.13 per million tokens** (platform.openai.com
pricing page, checked 2026-08-08) — note `text-embedding-ada-002` at **$0.10 per million
tokens** is simultaneously worse on OpenAI's own MTEB numbers (61.0% vs. 3-small's 62.3%)
and five times the price, which is a real, citable example of "we already use ada-002"
being a cost bug and not merely a quality one.

If you adopt contextual retrieval — an LLM writes a 50–100 token situating blurb per chunk
before embedding, so the chunk isn't orphaned from the document it came from — Anthropic's
published figure for that step is **$1.02 per million document tokens**, one-time, and it's
only that cheap because of prompt caching (the source document is cached once and reused
across all its own chunks) (Anthropic, *Introducing Contextual Retrieval*, Sep 2024 — their
eval set, their product, their caching feature; the method generalizes, that dollar figure
is theirs). This is squarely an ingest-time cost: paid once per document per embedding-model
version, never touched again until the corpus or the model changes.

**Query cost**, symbolically:

```
C_query = q_tokens × p_embed
        + rerank_cost
        + (prompt_tokens × p_in + output_tokens × p_out)
```

`q_tokens × p_embed` is trivial — a handful of tokens per query against the same per-token
embedding price as above. `rerank_cost` is whatever your reranker charges per candidate
examined (API-metered if hosted, GPU-time if self-hosted) — this is exactly the
`cost_per_candidate × candidates_examined` term from §7, now expressed in dollars instead
of latency. The last term — `prompt_tokens × p_in + output_tokens × p_out` — is where the
money actually goes, and it's worth stating as the chapter's one unambiguous cost claim:
**the dominant per-query cost in most RAG systems is generator input tokens,
i.e. how much retrieved context you stuffed into the prompt.** Input-token pricing applies
to every chunk you assembled into context, whether the model needed it or not, and that
volume is routinely an order of magnitude larger than the query itself or the reranking
step. This has a consequence worth sitting with: it makes retrieval precision a *cost*
lever, not merely a quality lever. A retrieval stage that surfaces 20 tightly relevant
chunks instead of 50 loosely relevant ones isn't just giving the generator a cleaner signal
(§4's faithfulness term) — it's directly cutting the largest line item in `C_query`, every
single request, forever. Precision work upstream pays for itself downstream in a way that's
easy to miss if you only ever look at retrieval quality metrics and never put a dollar sign
next to the assembled context.

Do not fabricate generator token prices here — put your actual provider's current published
per-token input/output rates into the formula when you run this for real; they change
often enough that hardcoding a 2026 number into a study document would be stale before you
finish reading it. `11-token-accounting-and-cost.md` (planned) is where this formula becomes
an actual per-request, per-tenant, per-model accounting system with budgets and alerts —
and it will not be reinvented from scratch, because the cost-attribution pattern already
exists for exactly this shape of problem in
[`../sre-observability/31-finops-for-observability.md`](../sre-observability/31-finops-for-observability.md)
(reused wholesale, tokens standing in for whatever telemetry billed by there). The moment
that accounting is broken down by tenant and by model, watch the label cardinality: the
same explosion [`../sre-observability/18-cardinality-and-cost.md`](../sre-observability/18-cardinality-and-cost.md)
warns about for metric labels happens identically to a token-cost table keyed on
`(tenant, model, route)` — here it's enough to have the two formulas cleanly separated and
to know which one dominates.

A worked hypothetical makes the "input tokens dominate" claim concrete without inventing a
generator price (this is an illustrative example with assumed round numbers, not a
benchmark — do not cite the dollar figures below as real prices for anything). Suppose a
query embeds at 20 tokens, a reranker scores 50 candidates at some small fixed cost per
candidate, and the assembled context runs 4,000 tokens of retrieved chunks plus a 300-token
answer. Using OpenAI's actual verified `text-embedding-3-small` rate of $0.02/M tokens for
the embedding term, the query-embedding cost is on the order of $0.0000004 — vanishingly
small. Now *assume*, purely for illustration, a generator priced at $3/M input tokens and
$15/M output tokens (illustrative numbers, not sourced): the generation term alone is
`4000 × $3/M + 300 × $15/M ≈ $0.012 + $0.0045 ≈ $0.0165`. Even with the embedding term
literally rounding to zero, the assumed generation cost is already four orders of magnitude
larger. The reranker term is the only one that could plausibly compete with generation cost
at scale, and only if it's priced per-candidate rather than amortized — which is exactly
why §7 and §8 both treat reranking as a cost you have to budget deliberately rather than
add for free. The lesson to take from the arithmetic, not the specific numbers: shrinking
`prompt_tokens` by improving retrieval precision (fewer, better chunks) moves the term that
was already several orders of magnitude larger than everything else in the formula, which
is why precision work is a cost lever and not just a quality one.

---

## 10. Reindex cost as a coupling constraint

> **In plain words.** Changing the embedding model means re-embedding every chunk and rebuilding the index, because vectors from different models can't be compared. For a small collection that is trivial. For a huge one it takes days to weeks, so choosing the embedding model becomes a long-term decision.
>
> **Real-world example.** 6,700 HR chunks re-embed in minutes for under 10 cents. 500 million chunks at 500 tokens each is 250 billion tokens: about $5,000 at $0.02/M, but roughly 17 days of wall-clock time at an illustrative 10 million tokens per minute rate limit, with two indexes running side by side the whole time.

§3 stated that swapping the embedding model forces a full rebuild because it's a schema
change to the materialized view, not a config edit. Here's the consequence that follows
mechanically from that fact, and why it deserves to be called out as a *coupling
constraint* rather than just an annoyance: **the choice of embedding model gets coupled to
corpus size, because the cost of changing your mind scales with `N_tokens_corpus`.** A
10,000-chunk internal wiki can be re-embedded on a whim, in minutes, for about ten cents
(10,000 × 500 tokens × $0.02/M) — the `C_ingest` formula from §9 applied to a small corpus
barely registers. A 500-million-chunk production corpus at 500 tokens per chunk is 250
billion tokens: about $5,000 at OpenAI's cheapest published rate ($0.02/M for
`text-embedding-3-small`), about $32,500 at $0.13/M for `text-embedding-3-large`, and more
for longer chunks or pricier models. The API bill is often the smaller part. The larger costs
are wall-clock time (at an illustrative rate limit of 10M tokens per minute, 250 billion
tokens take about 17 days), the operational cost of running two indexes in parallel during
cutover, and the eval work to confirm the new model is actually better before you commit the
corpus to it (§16 builds exactly that eval harness). Vectors from two models are never
comparable — there is no cosine similarity worth computing between embeddings from
different training runs, different objectives, different dimensionalities — so there is no
partial migration path: you either serve entirely on the old index, entirely on the new
one, or run both simultaneously as separate systems until cutover, there is no "migrate
gradually chunk by chunk" middle ground the way there sometimes is for a relational schema
migration with a compatible column type.

The practical upshot: model choice, once you've ingested a nontrivial corpus, becomes a
semi-permanent decision — closer in weight to choosing a database engine than to choosing a
config flag. That should change how you evaluate embedding models *before* committing: the
switching cost is a real input to the decision, not a footnote, and "we'll just swap it
later if a better model comes out" is a much weaker plan than it sounds like once you've
seen the `C_ingest` bill for a corpus at scale.

There is one documented, notable exception worth naming because it's architecturally
interesting: Voyage AI states that all embeddings created with its 4-series models are
compatible with each other across size tiers — meaning documents embedded with the cheap
`voyage-4-lite` and queries embedded with the higher-quality `voyage-4-large` land in the
same comparable space (Voyage AI docs, docs.voyageai.com, checked 2026-08-08). That's a
genuinely different architecture from the norm: it decouples the *document-side* embedding
cost from the *query-side* embedding cost within a model family, letting you tier cost
asymmetrically without a reindex, though it does not (per the same source) claim
cross-generation compatibility — moving from voyage-3 to voyage-4 is still a full rebuild,
same as any other vendor's model-generation change. It's an exception to "different tiers
need different indexes," not an exception to "different generations need different
indexes." `01-embeddings-and-representation.md` (planned) is where the full evaluation
methodology for choosing a model under this switching-cost constraint gets built out.

---

## 11. When NOT to RAG

> **In plain words.** Search is not always needed. If the question is about one document that fits in the model's context window, just give the model the whole document. Use retrieval when the collection is big, changes often, has per-user permissions, or when cost per question must stay low.
>
> **Real-world example.** Summarising one 40-page contract: paste the contract (~20,000 tokens), no index needed. Answering questions over 2,000 HR documents (3M tokens) that change weekly and differ by country: retrieval, because pasting 3M tokens into every question is impossible or very expensive.

Retrieval is a tool for a specific shape of problem, not a default. The honest framing,
straight from the 2026 architectural-context material (secondary, blog-grade sources —
flagged as such below): the question isn't "RAG or long context," it's which one — or what
mix — fits the access pattern of the actual task.

| Situation | Favors | Why |
|---|---|---|
| Coherent single document, question spans the whole thing | Long context | No chunk boundary can cut a coherent narrative without losing cross-references; just give the model the whole document |
| Exploratory queries not known in advance | Long context | Retrieval needs a query to rank against; if you can't anticipate the query shape, there's nothing to index against yet |
| Large corpus, only a small selective slice is relevant per query | Retrieval | Paying to stuff an entire corpus into every prompt is both a latency and a `C_query` disaster (§8, §9) when only a handful of chunks are ever relevant |
| Freshness matters, corpus changes frequently | Retrieval | An index can be refreshed incrementally (§3); a long-context window has to be rebuilt from scratch on every call regardless |
| Access control varies per user/tenant | Retrieval | Filtering at the index/retrieval layer is the natural place to enforce per-document permissions; stuffing everything into context and hoping the prompt says "ignore what you're not allowed to see" is not access control |
| Per-query cost must stay low at volume | Retrieval | `C_query`'s dominant term is prompt input tokens (§9); a bounded, precise context keeps that term small on every request, where long-context-everything pays the full corpus's token cost on every single call |
| Agent conversation history | Long context (mostly) | Recency, not similarity, is usually the right relevance signal — "you often need the last 50 turns, not the most semantically similar 50 turns" (2026 context-engineering framing) |

That last row deserves its own sentence because it cuts against RAG's own instinct:
similarity search assumes relevance correlates with embedding-space distance, but a lot of
agent and conversational state is relevant *because it's recent*, not because it's
semantically close to the current turn — a tool-call result from three turns ago that set
up the current state is exactly the thing pure similarity search is liable to rank low.

The degradation that makes "just always use long context" a bad default even where context
windows are enormous is **context rot** — the common name (popularized by Chroma's 2025 technical report
*Context Rot*) for the fact that model quality
degrades as the context you stuff in grows, even when every added token is nominally
relevant, purely from the load of more content to attend over ("A Survey of Context
Engineering for LLMs," arXiv 2507.13334 — secondary source, cited here as the term-of-art
reference, not as a specific measured number). This is why "just put the whole corpus in
the context window" isn't a free win even on a model whose context window is technically
large enough to fit it: more tokens in context is not free precision, it's a real quality
cost that has to be weighed against retrieval's alternative cost (missing a chunk
entirely).

The stated 2026 default, and the honest position of this chapter: **hybrid.** Retrieve a
bounded-but-generous candidate set — enough to be confident the answer-bearing chunk
survives §5's failure classes (a)–(c) — then let a long-context-capable model reason over
that bounded set rather than either extreme (a single top-3 sliver, or the entire corpus
unfiltered). This is not a compromise adopted reluctantly; it's the shape that both
inequalities in this chapter point toward at once — §4's recall ceiling says retrieve
generously enough that the evidence is almost certainly present, and context rot says don't
retrieve so generously that the model can no longer make good use of what you gave it.
`06-context-engineering.md` (planned) is where that budget gets tuned in practice.

---

## 12. The 2026 shape: from pipeline to loop

> **In plain words.** Newer systems let the model decide when to search, what to search for, and when it has enough. That turns a straight line into a loop. The loop needs hard limits (maximum searches, maximum cost), and you now have to check the whole sequence of steps, not only the final answer.
>
> **Real-world example.** "Compare my parental leave with the Berlin office's policy" needs two searches: one for the employee's country and one for Germany. An agent does 2 searches and stops. A badly limited agent does 14 searches, spends 14× the retrieval cost and still answers from the wrong country's policy.

Everything in §2 through §11 assumes a linear dataflow: one retrieval pass, one context
assembly, one generation call, done. That assumption is already breaking down in the
systems being built in 2026. **Agentic retrieval** replaces the single retrieve→generate
pass with a loop where the system itself decides whether to retrieve at all, what to
retrieve, and when it has enough evidence to stop — retrieval becomes a tool the model
calls zero, one, or many times per turn, potentially chaining multiple retrieval calls
across different sub-questions before producing a final answer (multi-hop retrieval).

This breaks the linear model from §2 in a specific, mechanical way: there is no longer a
fixed number of pipeline stages to point a diagnostic procedure at, because the number of
retrieval calls, and their content, is now a decision the system makes at runtime and can
vary per query. What replaces the linear dataflow diagram is a **loop with an explicit
budget and an explicit termination condition** — a max number of tool calls, a cost
ceiling, a confidence threshold for "I have enough evidence" — because an unbounded loop
that decides for itself when to stop is a latency and cost time bomb (§8's budget and §9's
cost formula both become per-turn accumulators instead of single line items).

```
 §2's shape (linear, fixed stage count):

   query ──▶ retrieve ──▶ rerank ──▶ assemble ──▶ generate ──▶ done

 §12's shape (loop, variable stage count, explicit budget):

   query ──▶ [ decide: retrieve? what? ] ──▶ retrieve ──▶ evaluate evidence
                     ▲                                          │
                     │            "not enough yet, and          │
                     │             budget remains"               │
                     └──────────────────────────────────────────┘
                                          │
                          "enough evidence, or budget exhausted"
                                          ▼
                                     generate ──▶ done
```

The diagram's point is narrow but important: the linear pipeline has a fixed number of
boxes you can put a span around ahead of time; the loop has a *variable* number of retrieval
iterations decided at runtime, bounded only by whatever budget you wired in. Every span
design, every recall@k measurement, every cost formula from §2–§11 still applies *inside*
one iteration of that loop — they just now have to be measured per-hop and rolled up, rather
than measured once per request.

The evaluation consequence is the one worth flagging clearly here, even though the depth
belongs in later chapters: **you can no longer evaluate a single output, you have to
evaluate a trajectory** — which tools got called, in what order, whether each call was
justified, whether the loop terminated for the right reason, and whether the final answer
was actually grounded in what got retrieved along the way. The §5/§6 failure taxonomy still
applies to each individual retrieval call inside the trajectory, but a new failure class
appears on top of it that has no analogue in the linear pipeline: the loop calling the
wrong tool, calling it with a malformed argument, retrieving successfully but never using
the result, or stopping too early/too late relative to its budget. `13-agents-and-tool-
calling.md` (planned) covers building this loop; `14-agent-evaluation.md` (planned) covers
trajectory evaluation specifically. This section is a pointer forward, not the treatment —
the point to take from it here is narrow: the mental model in this chapter is the
prerequisite for the agentic case, not a competing one. You still need recall ceilings,
still need the four failure classes, still need per-stage observability — you just need
them applied per hop, inside a loop with a budget, instead of once per request.

---

## 13. Observability as a design constraint, not an add-on

> **In plain words.** Record what every step did for every request: which chunks it returned, how long it took, how many tokens it used. Give each request one ID that also appears in your quality scores and your cost records. Without that, you can't investigate a bad answer after it happened.
>
> **Real-world example.** A user reports a wrong answer from yesterday at 14:02. With traces, you look up request `r-81f2`, see the rerank step dropped the leave-policy chunk to rank 7, and know it is class (c) in two minutes. Without traces, you can only guess, and the same query today may behave differently.

§6's diagnostic procedure — oracle-context test, recall@k sweep — works by hand on one
failing query at a time. It does not scale to production volume unless every stage in §2's
dataflow emits a span, and the trace *is* the decomposition from §5 made queryable: one
span for embed-query, one for each retrieval method, one for fuse, one for rerank, one for
context assembly, one for generation, each carrying enough structured data (candidate
count, top-k scores, token counts, chunk ids returned) to answer "which stage did this
request's time and money go to" and "which chunks did this request actually retrieve"
without re-running anything. This has to be designed in from the start, not retrofitted
once something is already broken in production — retrofitting instrumentation after an
incident means the incident itself is permanently undiagnosable, because the request that
mattered already happened and left no trace.

The piece that's easy to build wrong is the **join key**. A trace, an eval result, and a
cost row are three different records, usually written by three different systems (an OTEL
backend, an eval pipeline running against a golden set, a billing/token-accounting job),
and they are useless in isolation from each other. The same request id has to thread
through all three: the trace shows *what the pipeline did* for that request, the eval
result (when the request is part of a golden-set run, or scored post-hoc) shows *whether it
was correct*, and the cost row shows *what it cost*. Without a shared join key, you end up
with three separate dashboards that each look fine independently and no way to answer "the
requests that cost the most, were they also the ones that were wrong" — which is usually
the exact question that matters when someone asks why the RAG system got both slower and
worse at the same time.

[`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
is, per this repo's own cross-reference map, **the single most on-target existing document
in the entire repo for this chapter's subject** — read it in place rather than having it
re-derived here; nothing in this section should be taken as a substitute for it.
[`../sre-observability/02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md)
is the substrate underneath it: OTEL's GenAI semantic conventions give you the standard
span/attribute shapes for LLM calls specifically, so "one span per stage" doesn't mean
inventing your own schema per project, and
[`../sre-observability/03-instrumentation.md`](../sre-observability/03-instrumentation.md)
is where "add a span" turns into disciplined instrumentation practice rather than
scattered `print`-debugging with extra steps. Governing that schema as more stages and
more teams add spans over time — so `retrieval.top_k` doesn't mean three different things
across three services — is exactly the problem
[`../sre-observability/34-schema-and-semantic-conventions-governance.md`](../sre-observability/34-schema-and-semantic-conventions-governance.md)
covers, applied here to GenAI semantic conventions specifically. And spans are not free to
keep: retention on a high-cardinality, high-volume trace stream is a real storage cost
decision, covered generally in
[`../sre-observability/08-traces-storage.md`](../sre-observability/08-traces-storage.md) —
worth reading before you set "trace everything, forever" as your default sampling policy.
`10-llm-observability-and-tracing.md` (planned) is where this gets built out into the P2
project from the README's project ladder — span design, the trace↔eval join in practice,
sampling strategy that doesn't drop the requests you'd actually want to look at.

---

## 14. Anti-patterns

**Tuning the prompt when retrieval is broken.** Tempting because prompt edits are fast,
visible, and don't require touching the index or the retriever. Costs you the actual fix
getting delayed indefinitely, plus a prompt that's now overfit to compensating for missing
evidence on the three examples you tested, which won't generalize. Do instead: run the
oracle-context test (§6) before changing a single word of the prompt. If it passes, the
prompt was never the problem.

**Eyeballing outputs.** Tempting because it's zero setup and feels like real engineering
judgment. Costs you the ability to detect regressions, compare two versions of the
pipeline, or defend a claim of improvement to anyone who wasn't in the room — "it looks
better to me" collapses under one follow-up question, always. Do instead: a golden set with
a stated metric, scored the same way every time (§16).

**Reporting a quality number with no dataset description.** Tempting because "85% accurate"
sounds authoritative on its own. Costs you credibility the moment someone asks what the
denominator was, and rightly so — 85% on 20 easy queries you already knew the answer to is
a different claim than 85% on 500 queries sampled from real production traffic. Do
instead: every number ships with dataset size, provenance, and what counted as a hit — the
same rule README §6 states for this whole folder.

**Treating chunk size as a global constant.** Tempting because one number is simple to
configure and ship. Costs you recall on documents whose natural unit of meaning doesn't
match that number — a table needs different chunking than prose, a legal clause different
from a changelog entry. Do instead: chunk strategy as a property of document type, measured
per type, not a single repo-wide constant (`02-chunking-and-document-processing.md`).

**Measuring retrieval only on queries that already work.** Tempting because it's the
easiest golden set to build — you already know these queries succeed, so scoring them feels
like confirmation. Costs you the only information that matters: your recall ceiling on the
queries that are actually failing in production. Do instead: sample real production
queries, including ones with wrong answers already reported, into the golden set (§16).

**No golden set before the first optimization.** Tempting because building one feels like
overhead standing between you and shipping the "obviously better" change you already have
in mind. Costs you the ability to know, afterward, whether the change helped, hurt, or did
nothing — which makes every subsequent optimization a guess stacked on an unmeasured guess.
Do instead: P0 from the README's project ladder, before P1.

**Conflating ingest and query cost.** Tempting because both show up on the same monthly
invoice line for "the AI vendor." Costs you correct reasoning about tradeoffs — avoiding a
cheap one-time ingest-time improvement while shipping an expensive-per-request query-time
one, or the reverse (§2, §9). Do instead: keep the two formulas from §9 separate, always,
and label every cost line item with which one it belongs to.

**Adding a reranker without a latency budget.** Tempting because "add a reranker" is a
one-line integration and the quality lift is well documented (in Anthropic's
contextual-retrieval eval, adding reranking on top of contextual embeddings + contextual BM25
cut the top-20 retrieval failure rate from 2.9% to 1.9%; the full stack together took it from
5.7% to 1.9%). Costs you a latency regression nobody budgeted for,
discovered in production when p99 blows past the SLO. Do instead: write the latency table
from §8 first, put a number in the `t_rerank` row, and confirm the total still fits the SLO
before shipping.

**Skipping the recall@k sweep and reporting recall at only one k.** Tempting because
sweeping k means running the eval multiple times instead of once. Costs you the ability to
distinguish "not retrievable at all" from "retrievable but ranked too low for the shipped
budget" (§5b vs. §5c) — two failure classes with completely different fixes that look
identical at a single k. Do instead: sweep k ∈ {1,5,10,20,50} as a matter of course, every
time (§16, exercise 2).

---

## 15. Mental models — the compressed set

1. **An LLM pipeline is a data system.** Correctness, measurability, and cost are
   engineering properties of the pipeline's design, not something a better prompt can
   retrofit onto a broken stage.
2. **Ingest-time and query-time are different systems that share one interface: the
   index.** Reasoning about their costs and latencies as one undifferentiated blob is a
   common category error in this space.
3. **The index is a materialized view.** Staleness, invalidation, write amplification, and
   "schema changes force full rebuilds" all follow mechanically from that one framing —
   they are not separate ad hoc RAG problems.
4. **`P(correct) ≤ P(retrieved) × P(used correctly | retrieved)`.** Retrieval is an upper
   bound on end-to-end quality; you cannot generation-tune your way past it.
5. **Four failure classes, one symptom.** Not-in-corpus, not-retrievable,
   ranked-out-of-budget, and unused-in-context all present identically as "wrong answer" —
   which is exactly why the decomposition in §5 has to be explicit rather than assumed.
6. **The oracle-context test is the fastest bug bisection you have.** Feed the known
   answer directly; if the model still fails, stop looking at retrieval.
7. **Retrieval is two-stage IR: cheap-and-broad, then expensive-and-narrow.** The same
   selectivity reasoning that picks a database access method picks a retrieval pipeline
   shape.
8. **TTFT is the number users feel, not total generation time.** Streaming decouples
   perceived latency from total latency; budget and alert on TTFT specifically.
9. **The dominant per-query cost is almost always generator input tokens.** Which makes
   retrieval precision a cost lever, not only a quality lever — every unnecessary chunk in
   context is a recurring charge.
10. **Swapping the embedding model is a migration, not a config change.** Vectors from two
    models are incomparable; the switching cost scales with corpus size and makes model
    choice semi-permanent.
11. **RAG versus long-context is not a binary; it's an access-pattern question.** Coherent
    documents and unpredictable exploratory queries favor long context; large, selective,
    fresh, access-controlled corpora favor retrieval. The 2026 default is both, in
    sequence.
12. **A trace is a decomposition, not a debugging convenience.** If a request can't be
    joined across trace, eval, and cost by a shared id, you cannot answer "which requests
    are both expensive and wrong," which is usually the question that matters.

---

## 16. Lab exercises

These build toward the README's **P0 — eval harness**, the project everything else in this
folder depends on. Do not skip to P1 without these; every later chapter assumes you have a
golden set and a way to measure against it.

**1. Build a 50-query golden set over a real corpus you own.** *(~half a day; unblocks
everything else, and specifically `08-evaluation-methodology.md`)*
Pick a corpus you actually have — your own notes, a project's docs, an open dataset, this
repo itself. Write 50 realistic questions against it. For each, record the exact chunk
id(s) that contain the answer — you need to decide your chunking scheme first, since the
chunk id is only meaningful relative to a fixed chunking. Deliverable: a versioned JSONL
file, one record per query: `{query, answer_bearing_chunk_ids, expected_answer}`. Success
criterion: every record has at least one verified chunk id, and you can regenerate the file
deterministically if the corpus is re-chunked (i.e., chunk ids are stable identifiers, not
array indices).

> **Worked: [`labs/golden-set/`](labs/golden-set/)** — 60 questions over the four chapters
> in this folder, with the builder that produces the labels and the 20-assertion test
> suite that keeps them from rotting. Read that directory's README for the full method
> and build log, including what a production golden set does differently. The four
> decisions worth carrying into your own are below.

**How to build one so it survives its own corpus.** The naive version of this exercise —
type 50 questions into a spreadsheet, paste in the chunk ids your retriever printed — is
worse than not doing it, because it produces a number that looks like measurement and
decays into fiction the first time anything upstream changes. Four decisions prevent
that, and none of them cost more than an hour up front:

1. **Label spans, not chunk ids.** A chunk id only exists relative to a chunking
   (`02-chunking-and-document-processing.md` §11.2). Store `(doc_id, char_start,
   char_end)` into the *canonical text* of the corpus, and derive the chunk ids from
   (span × chunking × hit rule) at build time. Spans survive a re-chunk because the
   corpus is the thing that didn't change; chunk ids don't. This is what actually makes
   the exercise's own success criterion — "you can regenerate the file deterministically
   if the corpus is re-chunked" — achievable rather than aspirational.
2. **Author quotes; let the builder compute offsets.** Nobody can review
   `char_start: 48122`, and every offset shifts when a paragraph above it is edited. So
   the human writes a short exact quote and the builder resolves it, treating both a
   missing quote and an ambiguous one (two occurrences) as build errors. That is the
   whole mechanism by which an edited corpus produces a failed build instead of a label
   that quietly points at the wrong paragraph.
3. **Write down the expansion rule and the hit rule, and never report a number without
   them.** A short quote anchors; something has to decide how far the labelled span
   extends around it, and something has to decide when a chunk counts as containing that
   span (`02` §11.2's table: any overlap, containment, coverage ≥ τ, union coverage ≥ τ).
   Both choices move recall by more than most of the pipeline changes you'll be trying to
   measure, so "recall@10" without them is not a fully specified metric — the same point
   §6 makes about the "any vs. all" definition for multi-hop queries.
4. **Content-address the chunk ids.** `sha256(doc_id, chunker_version, chunk_text)`, per
   `02` §9.1. With position-addressed ids, inserting one paragraph at the top of a
   document renames every chunk below it and invalidates every derived label in the same
   motion.

Then treat the whole thing as a test suite from the first commit, not a data file: the
labels themselves are an experiment, and an unexamined experiment yields a confident
number about nothing. Two failures from the worked build make the case concretely. Two
anchors didn't resolve on the first run — quotes transcribed across a line wrap — and the
builder refused to write a partial set rather than silently dropping two records and
making this week's recall incomparable to last week's. More instructive: the first
span-expansion rule was "expand to the enclosing paragraph," which on Markdown numbered
lists (no blank lines between items) swallowed the *entire list* for fourteen records —
a 3,245-character span covering a dozen unrelated facts. Every test passed. Every label
was "correct." Recall@k computed against those labels would have been inflated for a
reason nothing downstream would ever have surfaced. It was caught by printing the
resolved spans and reading them, which is the one step in this exercise that cannot be
automated and is always the first one skipped.

**2. Implement recall@k and sweep k ∈ {1, 5, 10, 20, 50}.** *(~half a day; unblocks
`04-retrieval-hybrid-and-reranking.md`)*
For each query in the golden set, run retrieval at each k and check whether any
answer-bearing chunk id appears in the top-k results. Plot recall as a function of k.
Deliverable: a recall@k curve (a simple line plot is fine) plus the raw numbers in a table —
land the raw per-query, per-k rows in DuckDB rather than a spreadsheet from the start
([`../databases/21-in-process-olap-duckdb-chdb.md`](../databases/21-in-process-olap-duckdb-chdb.md),
the intended home for eval results per the README's P0 design), since every later exercise
in this list adds more rows to the same table. Success criterion: you can state, in one
sentence, your recall ceiling at the k you actually plan to ship — e.g., "recall@10 is
0.72 on this golden set." For how to *read* the resulting curve — what the ceiling means,
why the curve's shape matters more than its values, and why the search-UI intuition that
"bigger k just costs the user some scrolling" is wrong for RAG — see
[`labs/golden-set/README.md`](labs/golden-set/README.md) §7.1. For deciding whether a
given curve is good enough to ship — deriving the required recall from the correctness
you're promising, per-k starting thresholds, and the one structural floor (stage-one
recall below ~0.95 makes every downstream fix futile) — see §7.2 there. §7.3–7.4 do the
same for MRR, including why it is never reported alone (three systems with very different
failure profiles all score 0.50) and why it is blind to multi-hop queries by
construction.

**3. Implement the oracle-context test and split failures into retrieval vs. generation.**
*(~1 day; unblocks `06-context-engineering.md` and `07-generation-and-structured-output.md`)*
For every golden-set query where the real pipeline gets the answer wrong, run the harness
sketched in §6: feed the known-correct chunk directly, bypassing retrieval, and check if
the answer is now right. Treat this harness as a test suite from day one, not a one-off
script — the pass/fail-per-query shape is exactly what
[`../python-mastery/43-testing-strategy.md`](../python-mastery/43-testing-strategy.md)
means by "evals are tests," and structuring it that way now is what makes it re-runnable
as a regression gate later (P0). Deliverable: a table of failing queries with an
`oracle_passed` column. Success criterion: report the split as a percentage — "N% of
failures are retrieval, M% are generation" — computed from your own failure set, not
asserted.

**4. Instrument the pipeline with one span per stage; produce a per-stage latency table
for 100 real queries.** *(~1 day; unblocks `10-llm-observability-and-tracing.md` and P2)*
Add a span around each stage from §2's table (embed-query, retrieve, fuse, rerank, assemble,
generate). Run 100 queries — real ones if you have traffic, otherwise your golden set
repeated with variation. At 100 requests a notebook query is fine; the moment this becomes a
standing habit rather than a one-off exercise, the spans belong in the kind of pipeline
[`../sre-observability/35-telemetry-lakehouse.md`](../sre-observability/35-telemetry-lakehouse.md)
describes, so latency regressions show up without re-running the exercise by hand every
time. Deliverable: a table of p50/p95/p99 latency per stage, matching §8's shape but with
real numbers instead of symbols. Success criterion: you can name which single stage owns
the largest share of p95 latency, with a number backing the claim.

**5. Compute your actual per-query cost from token counts and current published prices;
compare it to your invoice.** *(~half a day; unblocks `11-token-accounting-and-cost.md`)*
Log token counts (embedding tokens, prompt tokens, output tokens) for the same 100 queries
from exercise 4. Multiply by your provider's *currently published* per-token prices — do
not reuse this chapter's prices without rechecking, they drift. Deliverable: a per-query
cost breakdown by the three terms in §9's `C_query` formula. Success criterion: your
computed total for the period is within a stated tolerance of the real invoice for the same
period — if it isn't, find out why (uncounted retries, caching you didn't account for, a
model you forgot was in the mix).

**6. Take one degradation and prove the effect is real with a bootstrap confidence
interval.** *(~1 day; directly applies*
[`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md)*'s
noise-floor discipline; unblocks any future "did this change help" claim)*
Pick one deliberate degradation — halve k, or truncate chunks to half length — and rerun
recall@k on the golden set before and after. Compute a bootstrap CI on the difference
(resample queries with replacement, recompute the recall delta, repeat a few thousand
times, take the percentile interval). Deliverable: a before/after recall number with a CI,
not a bare point estimate. Success criterion: state whether the CI excludes zero — if it
doesn't, you have not shown the change did anything, no matter how the point estimate
looks, exactly per `31-measurement-methodology.md`'s central argument that a bad benchmark
is worse than no benchmark.

**7. Write the failure taxonomy for 20 of your own bad answers.** *(~half a day; unblocks
`08-evaluation-methodology.md` and reinforces §5/§6 directly)*
Take 20 real wrong answers from your pipeline (reuse exercise 3's failures if you have
enough, or collect fresh ones). Assign each to exactly one of the four classes from §5,
using the oracle-context test and recall@k sweep as your instruments, not judgment calls.
Deliverable: a table — query, symptom, class, evidence for the classification. Success
criterion: every row has a metric backing the classification (an oracle result or a
recall@k number), not a guess.

**8. Measure recall@k separately at "generous k" vs. "shipped k" to isolate the
ranking/budget failure class.** *(~half a day; unblocks `04-retrieval-hybrid-and-reranking.md`)*
Using exercise 2's sweep, compare recall@50 (post-retrieval, pre-rerank) against effective
recall at whatever k actually survives reranking and context-budget truncation in your real
pipeline. Deliverable: the gap between the two numbers, per query where they differ.
Success criterion: you can name how many golden-set queries are class (c) specifically —
found by retrieval, lost by ranking or budget — as distinct from class (b).

**9. Compute the reindex cost for your corpus under a hypothetical embedding-model
swap.** *(~2 hours; unblocks `01-embeddings-and-representation.md`)*
Take your golden-set corpus's total token count and multiply by two different embedding
models' current published prices (§9's `C_ingest` formula). Deliverable: a two-line cost
comparison plus an estimate of wall-clock rebuild time given your embedding API's rate
limits. Success criterion: you can state, in dollars and hours, what it would actually cost
you to switch embedding models today — turning §10's abstract "coupling constraint" claim
into a number for your own corpus.

---

## 17. Interview questions and system design prompts

> **In plain words.** In an interview, explain the idea in one simple sentence first, then give one number, then name one trade-off. The strongest signal for this chapter is that you debug a RAG system step by step instead of reaching for "improve the prompt".
>
> **Real-world example.** "Our RAG bot gives wrong answers. What do you do?" → "First I find out which step fails. I paste the right passage into the prompt by hand: if the answer becomes correct, search is the problem. Then I check whether the passage is in the top 50 at all, to tell 'never found' from 'found but cut'. Only failures that survive both tests are prompt or model problems."

Same format as `03` §16 and `04` §18: each question names the sections it draws from and gives
the answer structure an interviewer is listening for.

### 17.1 Conceptual questions — "explain X"

**Q: Walk me through a RAG pipeline end to end.**
*Sections: Start here, §2*
Two systems joined at the index. Ingest time (once per corpus version): parse → chunk → embed →
index. Query time (every request): embed query → retrieve (dense + sparse) → fuse → rerank →
assemble prompt → generate, with tracing across both. Give one number per step if you can (e.g. 500-token
chunks, top 50 candidates, rerank to 5, ~2,800 prompt tokens). The strong half of the answer: say
what breaks at each step, and that the only thing the two halves share is the index, so the
embedding model and chunking are part of the index's "schema".

**Q: Why is retrieval quality an upper bound on answer quality?**
*Sections: §4*
`P(correct) ≤ P(evidence retrieved) × P(used correctly | retrieved)`. If the chunk never reaches the
prompt, a perfect model still can't answer a question whose answer exists only in your corpus.
Example: recall@5 = 0.86 caps accuracy at 0.86 no matter what the prompt says. Add the nuance:
"retrieved" means *in the prompt at the shipped k*, not somewhere in a top-50 list.

**Q: Name the four failure classes and how you tell them apart.**
*Sections: §5, §6*
(a) not in corpus, (b) in corpus but not retrievable, (c) retrieved but ranked out of the budget,
(d) in the prompt but misused. Instruments: the oracle-context test splits (a/b/c) from (d); the
recall@k sweep at 1/5/10/20/50 splits (a/b) from (c); grepping the raw corpus splits (a) from (b).
Each class has a different fix and a different owner.

**Q: What is the oracle-context test and why is it so useful?**
*Sections: §6*
Build the prompt by hand with the known-correct chunk, bypassing the retriever, and call the
model. Correct now → the failure was upstream. Still wrong → generation. It turns "is it search or
the model?" into a five-minute experiment per query. Mention the trap: run it first with *only*
the correct chunk, then add realistic distractors as a second pass.

**Q: Why is a vector index like a materialized view? What follows from that?**
*Sections: §3, §10*
It is derived data, computed once from the corpus and read many times. So it goes stale, needs an
invalidation path for deletes, has write amplification (one edited document re-embeds all its
chunks), and a change to the function that built it (the embedding model) forces a full rebuild,
because vectors from two models are not comparable.

**Q: Why two-stage retrieval instead of just a better single model?**
*Sections: §7*
`cost ≈ Σ (cost per candidate × candidates examined)`. A cross-encoder is accurate but can't be
precomputed, so running it over 6,700 chunks at ~2 ms each is ~13 s. Run a cheap ANN/BM25 search
over everything, then the cross-encoder over 50–200 candidates. Same reasoning as "index scan,
then recheck" in a query planner.

**Q: Where does the money go in a RAG system?**
*Sections: §9*
Separate ingest cost (once: `N_tokens_corpus × price_embed`) from query cost (every request:
`q_tokens × p_embed + rerank_cost + prompt_tokens × p_in + output_tokens × p_out`). In most
systems the generator's input tokens dominate, so retrieval precision (fewer, better chunks) is a
cost lever. Example: 40 chunks → 8 chunks cut input tokens from 16,500 to 3,700 per query.

**Q: When would you not use RAG?**
*Sections: §11*
A single coherent document that fits in the context window, or exploratory questions where you
can't anticipate the query. Use retrieval for large, changing, permissioned corpora or when cost
per query matters. Mention context rot: long prompts are not free even when they fit. The usual
answer is hybrid: retrieve a generous-but-bounded set, let a long-context model reason over it.

### 17.2 System design round

**Q: Design an internal HR/IT assistant for 50,000 employees over 20,000 documents
(~60M tokens), 20,000 questions a day, p95 time-to-first-token under 1.5 s, and per-country
document permissions.**

```
1. CLARIFY + GOLDEN SET FIRST (§4, §16)
   - 200 real questions (include reported failures), each labelled with the answer span.
   - Metrics: recall@k at shipped k, answer accuracy, faithfulness, TTFT p95, $/query.

2. INGEST (§2, §3)
   - Parse with table-aware + OCR path for scans; store doc_id, country, version, ACL.
   - Chunk ~500 tokens, 50 overlap → 60M / 450 ≈ 133K chunks.
   - Embed: 133K × 500 ≈ 67M tokens × $0.02/M ≈ $1.33 one-time.
   - Index: 133K × 1,536 × 4 B ≈ 0.8 GB vectors → fits one node; add BM25.
   - Refresh: incremental upsert + soft delete, staleness SLO 24 h, nightly reconciliation
     against the source system (deleted docs → tombstones).

3. QUERY PATH + LATENCY BUDGET (§7, §8)
   embed 30 ms + hybrid search with country/ACL filter 20 ms + fuse 1 ms
   + rerank top 50 → keep 6, 120 ms + assemble 10 ms + TTFT 800 ms ≈ 981 ms < 1,500 ms

4. COST (§9) — illustrative $3/M in, $15/M out
   3,000 input + 200 output tokens → ≈ $0.012/query × 20,000/day ≈ $240/day.

5. OBSERVABILITY (§13)
   One span per stage with chunk ids, scores, token counts; one request id shared by
   trace, eval result and cost row.

6. DEBUG LOOP (§6)
   Weekly: oracle test + recall@k sweep on new failures → split into (a)/(b)/(c)/(d)
   → route to parser / chunking+hybrid / reranker+budget / prompt owners.
```

*What interviewers listen for:* a golden set before any tuning; ingest vs query split with a cost
for each; permissions enforced at retrieval, not in the prompt; an explicit latency budget with
the reranker as a line item; a plan for deletes and model changes; and a diagnosis procedure, not
"we'll improve the prompt".

**Q: The team wants to switch from one embedding model to a newer one. Plan it.**
*Sections: §3, §10*
Treat it as a schema migration. (1) Offline eval: embed the golden-set corpus with both models,
compare recall@k at shipped k with a bootstrap CI. (2) Cost: `N_tokens_corpus × new price`, plus
wall-clock time under the rate limit. (3) Build a shadow index with the new model; never mix old
and new vectors in one index. (4) Shadow-read or A/B a slice of traffic, then swap and keep the old
index for rollback. *What interviewers listen for:* "vectors from two models are not comparable",
"no gradual chunk-by-chunk migration", and a number for the rebuild.

### 17.3 Rapid-fire questions

| Question | Strong answer | Section |
|---|---|---|
| Ingest time vs query time? | Once per corpus version vs every request; they share only the index. | §2 |
| Why is swapping the embedding model not a config change? | Vectors from different models aren't comparable → full re-embed and rebuild. | §3, §10 |
| Recall@5 = 0.86, faithfulness = 0.93 — max accuracy? | 0.86 × 0.93 ≈ 0.80. | §4 |
| Oracle test passes — where is the bug? | Upstream of generation: class (a), (b) or (c). | §6 |
| Recall@50 = 0.95, recall@5 = 0.74 — which class? | (c): found, then ranked out of the budget. Fix reranker/budget. | §5, §6 |
| Recall@50 ≈ 0 for a query — next step? | Grep the raw corpus: absent → (a) ingestion; present → (b) representation. | §6 |
| Which query-time step is usually the slowest before the LLM? | The cross-encoder reranker; cost ∝ candidates scored. | §7, §8 |
| Which latency number do users feel? | TTFT; set SLOs on TTFT and p95/p99, not the mean. | §8 |
| Biggest per-query cost line? | Generator input tokens (retrieved context), in most systems. | §9 |
| Why is `text-embedding-ada-002` a cost bug today? | $0.10/M vs $0.02/M for `3-small`, and lower on OpenAI's own MTEB numbers. | §9 |
| RAG or long context for one 40-page contract? | Long context: one coherent document that fits. | §11 |
| What's new when retrieval becomes an agent loop? | Variable number of searches → needs caps, and you evaluate the trajectory. | §12 |
| Minimum join key for debugging production? | One request id across trace, eval result and cost row. | §13 |

### 17.4 Debugging prompts — "here are the symptoms, diagnose"

**"We spent two weeks on the prompt and accuracy moved from 0.71 to 0.72."**
Stop editing the prompt. Run the oracle test on the failures: if most pass with the right chunk,
the problem is upstream. Sweep recall@k: a big gap between recall@50 and recall at the shipped k
means class (c) → reranker and context budget. See §18 Case 1.

**"The bot quotes a policy we deleted months ago."**
Invalidation (§3). The source document is gone but its chunks are still in the index. Count chunks
whose `doc_id` no longer exists in the source system; add soft-delete sync and a reconciliation
job; set a staleness SLO. See Case 2.

**"Quality collapsed right after we 'just changed the embedding model' in config."**
Mixed vector spaces: new queries (or new docs) use the new model while old vectors remain. Check
the model version stored per vector. Fix: full re-embed into a shadow index, then swap. See Case 3.

**"p99 latency doubled after we added a reranker."**
`t_rerank` scales with candidates scored. Check how many candidates go in (200 instead of 50?),
batch size, and GPU/API queueing. Sweep candidate count vs recall after rerank; usually 50 keeps
almost all the quality at a quarter of the time. See Case 4.

**"Offline recall is 0.95 but users complain constantly."**
The golden set doesn't look like production traffic (only questions that already worked, or
written by the team). Re-sample from real queries including reported failures. See Case 6.

### 17.5 Common interview mistakes

1. **"Improve the prompt" as the first move.** Without the oracle test you don't know the prompt is
   the problem (§6).
2. **Quoting one recall number without k, dataset and hit rule.** "Recall 0.9" is not a fully
   specified metric (§6, §16).
3. **Mixing ingest cost and query cost.** A one-time $1 embedding bill and a recurring $0.01/query
   generation bill are different decisions (§9).
4. **Enforcing permissions in the prompt.** "Ignore documents you can't see" is not access
   control; filter at retrieval (§11).
5. **Adding a reranker with no latency budget.** Put `t_rerank` in the table first (§8).
6. **Forgetting deletes and model changes.** A freshly built demo index is not what runs after six
   months (§3, §10).

---

## 18. Real-world cases — incidents with numbers

> **In plain words.** Each case is a kind of problem teams hit in production: what users saw, how it was measured, the fix, and the numbers before and after.
>
> **Real-world example.** Quick index: prompt work doesn't help → Case 1; deleted documents still quoted → Case 2; quality collapses after a model change → Case 3; reranker blows latency → Case 4; bill too high → Case 5; offline great, users unhappy → Case 6; a few questions cost 10× → Case 7.

These are **composite scenarios** built from failure modes this chapter describes; numbers are
illustrative but internally consistent.

Quick index: accuracy stuck despite prompt work → Case 1 · deleted policy still quoted → Case 2 ·
collapse after embedding-model change → Case 3 · p99 blown by reranker → Case 4 · generation bill
too high → Case 5 · offline recall high, users unhappy → Case 6 · runaway agent loops → Case 7.

### Case 1 — Two weeks of prompt work, +1 point

**Setup.** Customer-support bot over 12,000 help-center articles. Golden set of 200 questions.
The pipeline ships the top 5 chunks from dense retrieval, no reranker.

**Symptom.** Accuracy 0.71. Two weeks of prompt rewrites → 0.72, and the gain didn't hold on new
questions.

**Measurement.** 58 wrong answers. Oracle test: 52 become correct with the right chunk → retrieval
failures; 6 stay wrong → generation. Recall sweep: recall@5 = 0.74 (148 of 200), recall@50 = 0.93.
Of the 148 questions with the evidence in the prompt, 142 were right (0.96), so 0.74 × 0.96 ≈ 0.71:
the cap was retrieval. The 0.19 gap between recall@50 and recall@5 is class (c).

**Fix.** Add a cross-encoder over the top 50 and ship the top 8. Recall at shipped k: 0.74 → 0.89
(178 of 200). Accuracy: 0.71 → 0.84 (168 of 200). Faithfulness dipped slightly (0.96 → 0.94)
because 8 chunks include more distractors.

**Lesson.** Measure recall at the shipped k before touching the prompt (§4, §6).

### Case 2 — The deleted rate sheet

**Setup.** Bank internal assistant over 38,000 chunks of product documents, indexed from a
document management system. Old documents are deleted when a new version is published.

**Symptom.** The bot tells staff a savings rate of 4.1%; the current sheet says 4.6%.

**Diagnosis.** The ingest job only upserts new and changed documents; it never removes chunks of
deleted ones. A reconciliation query finds **1,200 chunks (3.2% of the index)** whose `doc_id` no
longer exists in the source. The old and new rate sheets are near-identical text, so both rank
high, and the model picked the old one (class (d) on the surface, but caused by an index
invalidation bug, §3).

**Fix.** Soft-delete sync on every source delete event, nightly reconciliation that tombstones
orphans, and a staleness SLO of 24 h with an alert on orphan count. Orphans: 1,200 → 0. Stale-answer
reports from the rates team: several a week → none in the next month.

**Lesson.** The index is a materialized view; deletes need an explicit path (§3).

### Case 3 — "We just changed the model in the config"

**Setup.** E-commerce product search over 2M product descriptions (~200 tokens each). The team
changed the embedding model in a YAML file. New and updated products were embedded with the new
model; the existing 2M vectors were left alone.

**Symptom.** Search quality for existing products collapsed overnight; new products looked fine.

**Measurement.** Query vectors now come from the new model and are compared against old-model
vectors. Recall@10 on the golden set for existing products: 0.82 → 0.41.

**Fix.** Full re-embed into a shadow index: 2M × 200 = 400M tokens × $0.13/M ≈ **$52**, about
**7 hours** at ~1M tokens per minute. Evaluate, then swap. Recall@10: 0.84. Store `model_version`
on every vector and refuse queries across versions.

**Lesson.** The embedding model is part of the index's schema. The API bill was small; the outage
was the cost (§3, §10).

### Case 4 — The reranker that broke the SLO

**Setup.** Legal document search, p99 end-to-end SLO 3 s. A cross-encoder was added over the top
200 candidates "to improve quality".

**Symptom.** p99 went from 1.8 s to 4.9 s. Rerank alone: ~480 ms at p50, much worse at p99 under
load because of GPU queueing.

**Measurement.** Sweep the number of candidates reranked: recall@10 after rerank was 0.91 at 200
candidates and 0.90 at 50. Rerank time scales roughly linearly with candidates: 200 → 50 is 4×
less work (~480 ms → ~120 ms at p50).

**Fix.** Rerank the top 50. p99: 4.9 s → 2.6 s, inside the SLO, for a 0.01 recall loss.

**Lesson.** A reranker is a line item in the latency budget. Choose the candidate count from a
sweep (§7, §8).

### Case 5 — Paying for 40 chunks nobody reads

**Setup.** Support desk bot, 50,000 questions a day. The prompt includes 40 chunks of ~400 tokens
"to be safe" plus 500 tokens of instructions. Illustrative generator price $3/M input tokens.

**Symptom.** Generation bill of about $2,475 a day for input tokens alone.

**Measurement.** 16,500 input tokens × $3/M = $0.0495 per question × 50,000 = $2,475/day. The trace
shows the cited chunk is in the top 8 after reranking for almost every correct answer.

**Fix.** Rerank and keep 8 chunks: 3,700 input tokens → $0.0111 per question → **$555/day**,
saving about **$57,600 a month**. Accuracy went up slightly (0.78 → 0.80) because there were
fewer distractors.

**Lesson.** Retrieval precision is a cost lever; generator input tokens dominate `C_query` (§9).

### Case 6 — Recall 0.95 offline, complaints online

**Setup.** Internal IT assistant. The golden set was written by the team from questions they had
already tried, and the retriever does well on it.

**Symptom.** Recall@10 = 0.95 on the golden set. Users say the bot "never knows anything".

**Measurement.** A new sample of 300 real production queries, including 90 that users had flagged
as wrong: recall@10 = **0.68** (204 of 300). Many real questions use internal nicknames for systems
("the VPN thing", "Okta tile") that never appear in the documentation.

**Fix.** Rebuild the golden set from production traffic (30% flagged failures), add BM25 plus a
synonym list for internal nicknames. Recall@10 on the new set: 0.68 → 0.83.

**Lesson.** A golden set built from questions that already work measures nothing useful
(§14, §16).

### Case 7 — The agent that searched 30 times

**Setup.** Agentic research assistant (§12), 20,000 questions a day. Each loop iteration (one
search + one model call) costs about $0.012. No cap on iterations except a 30-call timeout.

**Symptom.** Mean cost looks fine, but spend is 19% higher than forecast.

**Measurement.** Mean 3.1 iterations per question → 20,000 × 3.1 × $0.012 ≈ $744/day. But 2% of
questions (400/day) hit the 30-call timeout: 400 × 30 × $0.012 = $144/day, **19% of spend** from 2%
of traffic, and most of those answers were still wrong.

**Fix.** Cap at 6 iterations and a per-question cost ceiling; when the cap is hit, answer "I
couldn't find this" with what was found. Mean iterations 3.1 → 2.62; spend ≈ $629/day. Accuracy
on the rest of traffic unchanged.

**Lesson.** A loop needs an explicit budget and stopping rule; evaluate the trajectory, not just
the answer (§12).

---

**Rung ledger.** Exercise 1 is built: [`labs/golden-set/`](labs/golden-set/) is **rung 2 —
implemented** (60 labelled queries over this folder's four chapters, a deterministic
builder, and a test suite that fails when the labels go stale). It produces labels, not
quality numbers — the first rung-1 figure arrives when exercise 2 runs a retriever against
them. Per README §6, this document itself sits on **rung 3 — studied** until the remaining
lab exercises above are actually run. The formulas, the failure taxonomy, and the diagnostic
procedure in this chapter are reasoning, not measurement — none of it counts as rung 1
until it produces a number from your own corpus with a stated method of computation. Once
exercises 1–9 are done, the *numbers they produce* — your recall@k curve, your
retrieval/generation failure split, your per-stage latency table, your actual cost, your
bootstrap-confirmed effect size — move to **rung 1 — measured**, each carrying its own
one-sentence account of how it was measured, per README §6's rule. This document itself
stays rung 3; it is the map, not the territory.
