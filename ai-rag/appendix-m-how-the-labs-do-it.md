# Appendix M — How the labs and the AI products actually do it: large documents, memory, caching, retrieval, gateways, orchestration

> **Why this appendix exists.** Every chapter in this repo teaches a mechanism. This appendix
> reads the mechanisms *back out of shipped products*: what Anthropic, OpenAI and Google
> actually do when a document is too big, what their consumer memory systems store and when
> they forget, how their caches are priced and invalidated, what their built-in retrieval
> defaults are, how a Cursor-class product builds its own gateway in front of all of them, and
> what the two public positions on agent orchestration disagree about. The point is not
> vendor trivia. It is that the same five-rung ladder shows up in every one of these products,
> and once you can see it you can stop designing context systems from scratch.
>
> **Where the neighbouring detail lives.** The window-budget mechanics, lost-in-the-middle,
> citation engineering and the long-context-vs-RAG argument are in
> [`06-context-engineering.md`](06-context-engineering.md) §1–§14. The agent-side primitives —
> the tool-definition tax, tool search, code mode, sub-agents, skills, clearing and compaction,
> just-in-time retrieval, cache-aware loops — are in
> [`30-context-engineering-for-agents.md`](30-context-engineering-for-agents.md). Memory
> architectures, buffers, trimming and long-term stores are in
> [`25-memory-and-state-management.md`](25-memory-and-state-management.md). Gateway mechanics,
> routing and fallback are in [`23-multi-llm-model-gateway.md`](23-multi-llm-model-gateway.md)
> and the control plane in
> [`appendix-l-control-plane-guardrails-budgets-and-gateways.md`](appendix-l-control-plane-guardrails-budgets-and-gateways.md).
> Orchestration patterns are in [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md);
> coding-agent reference stacks in [`appendix-j-production-ai-stack.md`](appendix-j-production-ai-stack.md).
> This appendix does not re-teach any of those. It reports what the products do and names the
> rung each choice sits on.

## Provenance of the numbers in this appendix

This page mixes three grades of evidence. Every claim below is tagged so you can tell which
you are reading, because the grades behave very differently in an interview and in a design
review.

| Grade | What it means | How it is marked | How much weight to put on it |
|---|---|---|---|
| **P — primary** | Read out of vendor API docs, help-centre articles or pricing pages, on the date given | plain statement + a dated source in §15 | High, but *check the date*. These change monthly |
| **V — vendor-published** | A lab's own engineering blog or paper about its own system | "Anthropic reports…", "OpenAI reports…" | Directionally reliable, self-selected. Vendors publish their wins |
| **S — secondary** | Third-party write-ups, reverse-engineering, community measurement | "as reported", "third-party" | Treat as a hypothesis to test, never as a number to quote |

Where a number exists only at grade S, this appendix says so rather than rounding it into a
fact. Collected 2026-10-16; the API surfaces moved three times in the twelve months before
that date, so re-read the primary docs before you rely on a specific parameter name.

## Contents

0. [The ladder every product converges on](#0-the-ladder-every-product-converges-on)
1. [Large documents: the four paths a lab can take](#1-large-documents-the-four-paths-a-lab-can-take)
2. [Caching mechanisms, compared as priced systems](#2-caching-mechanisms-compared-as-priced-systems)
3. [The three in-session primitives: clearing, compaction, memory](#3-the-three-in-session-primitives-clearing-compaction-memory)
4. [Prompt compression: the layer nobody ships in the API](#4-prompt-compression-the-layer-nobody-ships-in-the-api)
5. [Product memory: dreaming, entries, and personal context](#5-product-memory-dreaming-entries-and-personal-context)
6. [Agent-memory frameworks and the benchmark war](#6-agent-memory-frameworks-and-the-benchmark-war)
7. [The labs' own RAG, and its defaults](#7-the-labs-own-rag-and-its-defaults)
8. [Cursor-class systems: the gateway as a product surface](#8-cursor-class-systems-the-gateway-as-a-product-surface)
9. [Orchestration: the one public disagreement worth reading](#9-orchestration-the-one-public-disagreement-worth-reading)
10. [Long context vs retrieval, 2026 state](#10-long-context-vs-retrieval-2026-state)
11. [What to copy, what to skip](#11-what-to-copy-what-to-skip)
12. [Anti-patterns](#12-anti-patterns)
13. [Interview questions](#13-interview-questions)
14. [Lab exercises](#14-lab-exercises)
15. [Sources](#15-sources)

---

## 0. The ladder every product converges on

> **In plain words.** Nobody solves "the document is too big" with one technique. Every product
> that handles large inputs well implements the same five rungs and climbs them in the same
> order, because each rung is cheaper than the one above it and each one fails in a way the
> next one fixes.
>
> **Real-world example.** You paste a 900-page contract set into Claude's Projects. Under the
> window, the whole thing sits in the prompt and gets cached. Over the window, Claude *silently
> switches* to a project knowledge search tool and retrieves instead — and switches back if you
> delete files and drop under the limit again (P). That is rungs 1 → 2 → 3, implemented as a
> product feature, with the transition hidden from you.

| Rung | What it does | Cost of the rung | What failure pushes you up | Chapter |
|---|---|---|---|---|
| **1. Put it in the window** | Everything in the prompt, model attends to all of it | Linear tokens, every turn | Doesn't fit; or fits but accuracy sags in the middle | `06` §1, §5 |
| **2. Cache the stable prefix** | Same bytes, charged once at a discount | A write multiplier, then ~0.1× reads | Content changes per request, or the prefix churns | `06` §12, §2 below |
| **3. Retrieve instead of load** | Index it; fetch only what the question needs | Index build + per-query search | Retrieval misses; or the question is global, not local | `03`, `04`, `07` below |
| **4. Edit or compact the transcript** | Clear stale tool results; summarize the tail | Cheap; loses fidelity; invalidates cache | Information must survive past the session | `30` §8, §3 below |
| **5. Persist outside the window** | Files, memory stores, notes the agent re-reads | Storage + a read cost per session | — (top of the ladder) | `25`, §5–§6 below |

**The rule that falls out of the table.** Climb one rung at a time, and only when a *measured*
failure forces you. Most teams jump from rung 1 to rung 3 — building a vector store before
checking whether a cached 200K prompt answers the question for a tenth of the engineering. The
labs do not do this. Anthropic's Projects implementation is explicitly rung 1 **with a rung-3
escape hatch at the boundary** (P), and it converts back down when it can.

**The second rule.** Rungs 2 and 4 fight. Clearing or compacting the transcript changes bytes
in the middle of the prompt, which invalidates every cached token after the edit point (`30`
§8.2, appendix L §5.3). Every vendor that ships both has to tell you where to cut so the cut
lands below the breakpoint. If you build your own trimmer *and* leave the provider's on, you
get two independent trimmers producing prompts that are neither cacheable nor predictable
(appendix L §5).

---

## 1. Large documents: the four paths a lab can take

> **In plain words.** There are only four things a provider can do with a document you hand it:
> keep it in the window, keep it in the window but bill it once (cache), index it and search
> it, or hand you a file handle and let the model read it with a tool. Every product is a
> combination of these four, and the combination is what you are actually buying.

### 1.1 The paths, with the mechanics

| Path | Mechanism | What the model sees | Billing shape | When it is the right answer |
|---|---|---|---|---|
| **A. Inline document blocks** | A `document` content block with base64 or text, placed before the question | The whole document, every turn | Full input tokens per turn | One-shot analysis of one document; the document changes each request |
| **B. File handle + reuse** | Upload once, reference by ID across requests (Anthropic Files API; OpenAI file inputs) | The whole document, but you don't re-upload bytes | Still full input tokens per turn — **a file handle is not a cache** | Many requests over the same document, where you also cache (path C) |
| **C. Cached prefix** | The document sits in the cached part of the prefix | The whole document, charged at the cached-read rate after the first call | Write multiplier once, ~0.1× per reuse | Q&A sessions over a fixed corpus that fits the window (§2) |
| **D. Provider-side retrieval** | The provider chunks, embeds, indexes; the model calls a search tool | Only the retrieved spans | Index/storage + the retrieved tokens | The corpus exceeds the window, or exceeds it *economically* (§7) |

The trap is confusing B with C. A file handle saves you *upload bandwidth and parsing*, not
input tokens. On Anthropic's API the Files API is GA (`client.files.upload`, referenced as
`{"type": "document", "source": {"type": "file", "file_id": ...}}`) and it is orthogonal to
`cache_control` — you need both to get both benefits (P).

### 1.2 What each product actually does

| Product | Default path | The escalation | Notable mechanic |
|---|---|---|---|
| **Claude API** | A or B, your choice | You implement rung 3 | **Citations as a first-class feature**: `citations: {enabled: true}` per document block splits the response into text blocks carrying `cited_text` plus `char_location` / `page_location` spans. Incompatible with structured output formats (P) |
| **Claude Projects (product)** | Rung 1 — everything in the window | **Automatic**, at the window boundary: enables RAG mode, "expand capacity by up to 10x", via a *project knowledge search tool*; shows a visual indicator; converts back down if knowledge drops below the threshold (P) | The transition is a product decision, not a user setting. Paid plans only |
| **OpenAI API** | File inputs, or `file_search` over vector stores | You pick; `file_search` is rung 3 as a managed service | Chunking defaults are published: ~800-token chunks, 400-token overlap, up to 20 chunks into context (S/P-adjacent — see §7.1) |
| **Gemini API** | Rung 1 at a 1M window, plus caching | Context caching, now implicit-only on the newest models (§2.3) | The long-window-first philosophy: make rung 1 big enough that fewer workloads need rung 3 |

### 1.3 The thing to copy from path D

Anthropic's Projects behaviour is the single most copyable design in this section, and it is
four lines of logic:

```python
def choose_path(corpus_tokens: int, window: int, reserve: int = 32_000) -> str:
    """The Projects ladder, as a function. reserve = room for system + question + answer."""
    if corpus_tokens + reserve <= window:
        return "in_context"      # rung 1, with a cache breakpoint under the corpus
    return "retrieval"           # rung 3, via a search tool the model calls
```

Two properties make it work in production, and both are easy to leave out:

1. **It is hysteretic.** The product converts *back* to in-context when the corpus shrinks (P).
   A one-way switch leaves you permanently on the more expensive, lower-fidelity path after a
   single large upload that was later deleted.
2. **The model is told which mode it is in.** In retrieval mode the model has a search *tool*
   and knows the corpus is not all present. Silently truncating a corpus while the prompt still
   implies completeness is the single most common cause of confidently wrong aggregate answers
   (`06` §16, `08` §10.2).

Add a third property the products do not give you, because you have tenants and they have users:

3. **Report the mode in the trace.** Two answers to the same question, one in-context and one
   retrieved, are not comparable eval samples. Stratify by mode or your eval averages across a
   mechanism change (`08` §13, appendix F).

---

## 2. Caching mechanisms, compared as priced systems

> **In plain words.** All three major providers cache the same thing — the KV tensors for a
> prompt prefix the model has already processed — but they price it differently, invalidate it
> differently, and disagree about whether you should control it. Reading the three side by side
> tells you which parts of a prompt-caching design are physics and which are policy.
>
> **Real-world example.** You move a workload from one provider to another and your bill
> doubles even though the per-token rates are lower. The cause is usually a cache-write
> multiplier you didn't account for, or a prompt shape that hit an implicit breakpoint on the
> old provider and misses it on the new one.

### 2.1 Physics vs policy

What is physics (identical everywhere, because it falls out of how attention works):

- **It is a prefix cache, not a content cache.** A token's KV depends on every token before it.
  Change byte 10 of a 200K prompt and tokens 11…200,000 must be recomputed. There is no
  provider where "the middle of my prompt changed a little" is cheap.
- **Order of concatenation decides everything.** Tools, then system, then messages, on
  Anthropic's API (P). Stable content first, volatile content last. This is not advice; it is
  the only arrangement in which a cache can hit.
- **Caches are keyed by model.** A model switch is a cold cache, which is why a
  cheap-model cascade silently forfeits reuse (`23` §4, appendix L §5).

What is policy (differs, and changes):

| Dimension | Anthropic (Claude API) | OpenAI | Gemini API |
|---|---|---|---|
| **Opt-in model** | **Explicit.** You place up to 4 `cache_control` breakpoints | **Implicit by default**, with *explicit* breakpoints added on GPT-5.6 and later | **Implicit only** on 2.5 and newer; explicit cache objects not supported in the Interactions API or on Gemini 4 Argon onward |
| **Implicit breakpoint placement** | n/a (explicit) | End of the latest eligible message (5.6+); every 2,048 tokens on 5.5; model-dependent intervals earlier | Automatic |
| **Minimum cacheable prefix** | Model-dependent, ~512–4,096 tokens | 1,024 visible input tokens (5.6+) | 4,096 tokens (Gemini 4 Argon, 3.8 Flash, 3.6 Flash, 3.1 Pro Preview); 2,048 (2.5 Flash/Pro) |
| **Cache read price** | ~0.1× input (e.g. $0.25/MTok on a $10/MTok model) | 0.1× uncached input; 0.05× on GPT-6.1 Sol | Discount passed on automatically |
| **Cache write price** | A write multiplier on first call (TTL-dependent) | 1.25× the uncached input rate (5.6+); **no** separate write charge on 5.5 and earlier | No separate write charge for implicit |
| **Lifetime** | Short default, with a longer paid TTL | `prompt_cache_options.ttl: "30m"` (5.6+, only supported value, also the default); earlier: `in_memory` ≈ 5–10 min idle up to 1 hr, or `24h` ≈ 30 min typical up to 24 hr | Not user-controlled for implicit |
| **Reuse refreshes the entry** | Yes | Yes — "without another cache-write charge" | Yes |
| **Multi-tenant accounting** | Per-request, you aggregate | `prompt_cache_key` — on 5.6+ it is for *accounting separation* and anti-probing, not hit-rate; on earlier models it is the routing key that gets you hits at all | n/a |
| **ZDR interaction** | Retention policy is an org setting | Default retention depends on it: non-ZDR orgs default `24h`, ZDR orgs default `in_memory` | — |
| **Observability** | `usage.cache_read_input_tokens` / `cache_creation_input_tokens`; a cache-diagnosis beta reports *why* a prefix missed | `usage` cached-token counts (rounded down to a multiple of 128 before 5.6; exact at the eligible boundary on 5.6+), a caching dashboard, and a diagnostics tool | `usage.total_cached_tokens` |

All rows P, read 2026-10-16. The write multipliers and TTLs are the rows that move most often.

### 2.2 The break-even, as arithmetic

Caching is worth it when the saved reads outweigh the write premium. With `W` = write
multiplier, `R` = read multiplier, `n` = number of requests reusing the prefix within the TTL:

```
uncached  = n
cached    = W + R·(n − 1)
break-even n* = (W − R) / (1 − R)
```

| Provider shape | W | R | n* |
|---|---|---|---|
| OpenAI 5.6+ | 1.25 | 0.1 | **1.28** — the second request already pays for it |
| OpenAI ≤5.5 | 1.0 | model rate | **1** — free; it's on whether you like it or not |
| Anthropic short TTL | 1.25 | 0.1 | **1.28** |
| Anthropic long TTL | 2.0 | 0.1 | **2.1** — needs a third request within the longer window |

The practical reading: **two requests against the same prefix inside the TTL and caching has
already paid.** Which is why the whole industry moved toward implicit. The interesting
engineering is no longer "should I cache" but "why is my hit rate 11%", and that question is
answered by hunting silent invalidators, not by tuning the cache (appendix L §8, `06` §12).

### 2.3 The 2026 trend: caches are becoming invisible

Three independent moves in the same direction, all primary:

- Gemini **removed** explicit cache objects from its newest surface (not supported in the
  Interactions API, nor on Gemini 4 Argon onward); implicit caching is the only path, and the
  guidance reduces to "put large common content at the beginning, send similar prefixes close
  together in time."
- OpenAI made `prompt_cache_key` *optional* on 5.6+ — it demoted the knob from
  "how you get cache hits" to "how you separate billing between tenants."
- Anthropic went the other way on *diagnosis* rather than control: a cache-diagnosis beta that
  tells you which request broke the prefix, rather than more knobs for placing breakpoints.

**What this means for your design.** Stop treating the cache as something you operate. Treat
prompt *shape stability* as the thing you operate, and the cache as a reward for getting it
right. The audit list is short and it is the same on every provider:

| Silent invalidator | Why it bites | Fix |
|---|---|---|
| A timestamp or request ID in the system prompt | Changes the prefix on every call | Move it after the last breakpoint, or into the user turn |
| Unsorted JSON in a tool schema or serialized context | Key order varies between processes | Canonical serialization (`sort_keys=True`) |
| A tool list assembled from a dict or a set | Iteration order varies | Sort by name; never let discovery reorder it |
| Editing the system prompt mid-session | Invalidates everything | Append a mid-conversation `{"role": "system"}` message instead, where supported (P: Anthropic, on specific models) |
| Adding or removing a tool mid-session | Tools render first; invalidates all | Tool search *appends* schemas rather than swapping them, which preserves the prefix (P) |
| Your own history trimmer | Cuts above the breakpoint | Cut below it, or hand trimming to the provider — never both (appendix L §5) |
| A model fallback on 429 | Caches are model-keyed | Accept the cold cache as the cost of the fallback; don't "warm" the fallback speculatively |

### 2.4 What is underneath: the serving layer

The provider-visible cache is a product wrapper around inference-server machinery you can run
yourself, and knowing the machinery explains the product's constraints. (Grade S/V — these are
open-source projects, so the mechanisms are checkable, but the performance figures circulating
about them are third-party.)

| Layer | Mechanism | Why the product behaves the way it does |
|---|---|---|
| **Paged attention** (vLLM) | KV cache in fixed-size blocks, like OS paging | Blocks are the unit of reuse, which is why minimum prefixes are quantized (1,024 / 2,048 / 4,096 tokens) and why cached-token counts round to block multiples |
| **Automatic prefix caching** (vLLM APC) | Hash the block contents; reuse blocks whose hash matches | Hash-based matching is exact-prefix matching. This is the physics behind "one byte invalidates everything after it" |
| **RadixAttention** (SGLang) | A radix tree of KV blocks with longest-prefix match and LRU eviction | Handles *branching* prefixes — many completions sharing a prefix then diverging. This is the shape a sub-agent fan-out makes, which is why fan-out from a shared prefix is cheaper than it looks |
| **Attention-layer compression** (MQA, GQA, MLA) | Fewer KV heads per layer | Shrinks the cache itself; a model-architecture decision you inherit, not a knob |
| **KV quantization** (INT8/FP8) | Store KV at lower precision | Why providers can offer long TTLs economically |

The one design consequence worth carrying: **a fan-out of N sub-agents that share a long system
prefix is a radix-tree branch, not N cold prompts.** On a provider with prefix caching, the
shared prefix is paid once. That changes the sub-agent cost model in `30` §6 and §11 in your
favour — and it is a reason to give every sub-agent *identical* leading bytes and vary only
the tail.

---

## 3. The three in-session primitives: clearing, compaction, memory

> **In plain words.** Three different things get called "memory" and they are not
> interchangeable. *Clearing* deletes stale turns. *Compaction* summarizes them. *Memory*
> writes them somewhere outside the conversation. Both Anthropic and OpenAI now ship all three
> server-side, which is new as of 2026 and changes the build-vs-buy answer.
>
> **Real-world example.** An agent has made 180 tool calls. The transcript is 400K tokens, most
> of it file contents it read once. Clearing drops those tool results to placeholders and keeps
> the reasoning. Compaction replaces the whole early transcript with a summary. Memory writes
> "the migration touches 4 files, listed here" to a file the agent re-reads tomorrow. You
> probably want all three, in that order of preference.

### 3.1 The three, side by side

| | **Clearing / context editing** | **Compaction** | **Memory** |
|---|---|---|---|
| What happens to the bytes | Removed, replaced by a short placeholder | Replaced by a model-written summary | Written to durable storage, removed from the window |
| Information loss | Recoverable — re-call the tool | **Lossy and unrecoverable** — the summary is now the record | None, if the write succeeded |
| Cost | Free (no model call) | A model call to write the summary | A tool call to write; a read at session start |
| Cache effect | Invalidates once, at the cut point | Invalidates the whole compacted region | Appends; cache-friendly |
| Survives the session | No | No | **Yes** |
| Anthropic API | `context_management.edits` with `clear_tool_uses_20250919` (optional `clear_tool_inputs`) or `clear_thinking_20251015`; beta `context-management-2025-06-27` (P) | `compact_20260112`, beta `compact-2026-01-12`, default trigger ~150K tokens (P) | Memory tool `memory_20250818` — the model reads/writes a memory directory; **you implement the backend** (P) |
| OpenAI API | — | `context_management` with `compact_threshold`; compaction item emitted into the same stream; you chain it forward (P) | — (product-level memory; §5) |
| Chapter | `30` §8.2 | `30` §8.3, `06` §7 | `25` §7, `30` §8.4 |

### 3.2 The one implementation detail that breaks everything

Both vendors' compaction has the same failure mode and it is worth stating as a rule, because
it fails *silently*:

> **Append the whole response, not the text.** Compaction emits a block/item into the response
> that the server needs on the next request to know what it replaced. Extract only
> `response.content[0].text` and append that, and you lose the compaction state. The request
> succeeds. The history re-expands. Your next call is over the window again, or the model
> contradicts a summary it can no longer see (P, both vendors).

This is the server-side analogue of the mistake in `25` §3: treating a summary as a message
rather than as a *replacement for* messages.

### 3.3 Why clearing before compaction, and compaction before memory

An order that follows from the table, not from taste:

1. **Clear stale tool results first.** It is free, it is reversible, and on a tool-heavy agent
   loop it is where the tokens actually are. `30` §11's budget model puts the saving around
   half the window for a 50-turn loop with `keep = 3`.
2. **Compact only when clearing is not enough.** You are paying a model call to permanently
   destroy information. On a long-horizon run, each compaction is a chance to lose the one
   constraint that mattered.
3. **Write to memory for anything that must outlive the session** — and write it *before* you
   need it, not when you are already at the threshold. A note written under compaction pressure
   is written by a model that is already short of context.

### 3.4 The part the vendors cannot do for you

Server-side clearing and compaction operate on the transcript. They know nothing about your
domain. Two things stay yours:

- **What is load-bearing.** The vendor's summarizer does not know that the tenant ID, the
  deadline and the user's stated constraint must survive verbatim. Structured note-taking — a
  file the agent maintains with the objective, the constraints, the tally — survives compaction
  because it is re-read, not summarized (`30` §8.4).
- **The audit record.** A compacted transcript is not an audit trail. If you need to answer
  "why did the agent do that in March", persist the pre-compaction transcript out of band. The
  window is a working surface, not a log (`appendix-l` §9, `17` §4.7).

---

## 4. Prompt compression: the layer nobody ships in the API

> **In plain words.** There is a decade of research on making a prompt shorter without changing
> what it means — dropping low-information tokens, or replacing a passage with a few learned
> vectors. None of the three major providers exposes it as an API feature. Knowing why it isn't
> there is more useful than knowing the method names, and the "why" is the most reusable lesson
> in this appendix.
>
> **Real-world example.** You have a 40K-token retrieved context and a $0.02/answer budget.
> Compression promises 3× fewer tokens. You run it and faithfulness drops two points on
> questions whose answer hinged on a negation — because "not" is a low-information token by
> every statistical measure and a load-bearing one by every semantic measure.

### 4.1 The families

| Family | Method | What survives | Where the cost moves | Portable across models? |
|---|---|---|---|---|
| **Hard / extractive** | Score tokens or sentences, delete the low scorers. LLMLingua (perplexity-based, a small LM scores), LLMLingua-2 (token classification with a BERT-scale encoder trained by distilling GPT-4 compressions), Selective Context | Natural-language text, just shorter | A small model runs before the big one | **Yes** — output is still text |
| **Soft / learned** | Replace the passage with trained vectors. Gist tokens, AutoCompressor, ICAE, xRAG | Embeddings, not text | Training time + an encoder at inference | **No** — tied to the target model's embedding space |
| **KV-cache compression** | Compress or evict the cache after prefill rather than compress the prompt | Attention state | Inside the serving stack | **No** — it's a server feature, not a prompt feature |
| **Abstractive (the one you'll actually use)** | Ask a cheap model to summarize or extract per chunk | Text you can read and cite | A model call per chunk | Yes |

Vendor-reported figures for the LLMLingua line (grade V): LLMLingua-2 reports end-to-end
latency reductions up to ~2.9× at 2×–5× compression, and 3×–6× faster compression than
LLMLingua itself. Treat the compression *ratios* as the claim and re-measure the *quality* on
your own data; the published evaluations are on public QA and summarization sets.

### 4.2 Why no provider ships it

Five reasons, and each one is a design lesson you can reuse:

1. **It fights the cache.** A compressor is a function of the input, so its output changes
   whenever the input changes. Compressed prefixes are *unstable* prefixes. The cache gives you
   a 10× discount for free on stable bytes; compression gives you 3× on bytes you just made
   unstable. On any workload with prefix reuse, caching dominates and the two don't compose.
2. **The discount already exists.** With cache reads at 0.1× and continuing price declines, a
   3× token reduction is worth less than a cache hit and costs you a model call plus a quality
   risk.
3. **Faithfulness is the wrong thing to gamble.** Compression deletes negations, hedges,
   units and qualifiers — exactly the tokens that distinguish a correct answer from a confident
   wrong one (`08` §10.2). A provider cannot ship a feature whose failure mode is "the model
   now says the opposite".
4. **Soft prompts aren't portable.** Learned compressors are tied to a model version. A
   provider shipping them would be shipping an artifact that breaks on every model release —
   and model releases are now monthly.
5. **The agent-native alternative is better.** Everything compression tries to achieve
   in-prompt, the agent patterns achieve by never loading the tokens: tool search instead of
   all schemas, code mode instead of intermediate results in context, sub-agents instead of one
   window, just-in-time retrieval instead of up-front stuffing (`30` §3–§9). A token never
   loaded needs no compression.

### 4.3 When compression is still the right call

It is not never. The conditions are narrow and checkable:

| Condition | Why it flips the argument |
|---|---|
| **No prefix reuse at all** — every request has a unique long context | There is no cache hit to forfeit. Compression's 3× is the only discount available |
| **You are rate-limited on tokens, not billed on them** | A TPM ceiling is not softened by a cache discount; it is softened by fewer tokens (appendix L §4's two ceilings) |
| **A fixed context window you cannot raise** | Compression buys fit, not cost |
| **The content is genuinely redundant** | Boilerplate, repeated headers, serialized logs. Deduplication and formatting fixes often beat a learned compressor, for free |
| **You can measure faithfulness on the affected slice** | Without this, you cannot ship it responsibly at all |

And the ordering rule: **do the free things first.** Strip HTML cruft, drop duplicate chunks,
trim JSON whitespace, remove repeated system boilerplate from each chunk, use token-efficient
tool result formats (`30` §4.2). In practice those recover more tokens than a compressor, with
no quality risk and no extra model call.

### 4.4 If you do it anyway, measure it like a retrieval change

Compression is a *recall* problem wearing a cost costume. Evaluate it exactly as appendix F
evaluates every other recall layer:

```python
def compression_sweep(full_context: str, gold_spans: list[str]):
    """The only honest way to evaluate a compressor: hold the question set fixed,
    vary only the compression ratio, and watch the two curves cross."""
    for ratio in [1.0, 0.75, 0.5, 0.33, 0.2]:
        ctx = compress(full_context, ratio)
        a = answer(ctx)
        yield {
            "ratio": ratio,
            "tokens": count_tokens(ctx),
            "answer_correctness": grade_correctness(a),            # 08 §10.3
            "faithfulness": grade_faithfulness(a, ctx),            # 08 §10.2
            "span_survival": fraction_present(gold_spans, ctx),    # appendix F
        }
```

`span_survival` is the diagnostic that tells you *why* correctness fell: if the gold span was
deleted, no amount of prompting fixes it. Report all four columns. A compression result quoted
as "3× fewer tokens, same accuracy" with no faithfulness column and no span-survival column is
not a result.

---

## 5. Product memory: dreaming, entries, and personal context

> **In plain words.** The consumer assistants all built long-term memory, and they all
> converged on the same four-stage pipeline: extract candidate facts from conversations,
> consolidate them in the background, retrieve the relevant ones into each new conversation,
> and age out the stale ones. The differences are in *when* consolidation runs and *what shape*
> the stored unit is — and those two choices drive every user complaint about memory.
>
> **Real-world example.** A user corrects a preference ("I moved to Berlin"). A memory system
> that stores append-only facts now holds two contradictory cities and retrieves whichever
> embeds closer to the question. A system that consolidates in the background can notice the
> contradiction and supersede the old entry. This is the whole design problem, in one example.

### 5.1 The common pipeline

| Stage | What it does | The failure if you skip it |
|---|---|---|
| **1. Extract** | Pull candidate durable facts out of a conversation | Nothing is remembered, or the whole transcript is kept |
| **2. Consolidate** | Deduplicate, resolve contradictions, supersede stale entries, merge near-duplicates | **Semantic duplication** — the same fact stored six ways, crowding out everything else. The most-reported complaint about every one of these systems |
| **3. Retrieve** | Select which memories enter *this* conversation | Either everything is injected (token bloat, distraction) or the right fact is never recalled |
| **4. Decay** | Age out, or down-weight, what no longer holds | Memory becomes an archive of who the user used to be |

Stage 2 is the one that separates a working memory system from a growing pile, and it is the
one most self-built systems omit (`25` §7, §9).

### 5.2 What each product does

| | **ChatGPT** | **Claude** | **Gemini** |
|---|---|---|---|
| **Stored unit** | A synthesized memory state the model maintains, plus (legacy) discrete saved memories | **Individual categorized entries** Claude reads and updates during conversations (V) | Details drawn from earlier chats — preferences, projects, facts (V) |
| **Consolidation** | **"Dreaming"** — a background process that curates memory by referencing chat history, synthesizing a memory state for freshness, continuity and relevance (V, openai.com) | In-conversation read/update of entries (V) | Background, over past chats (V) |
| **History of the mechanism** | Discrete saved memories + a "reference chat history" setting (2024 → mid-2026), replaced by the dreaming architecture rolled out from 2026-06-04; the earlier pair survives as a legacy option (V) | Rolled out broadly during 2026; the API-side memory tool (`memory_20250818`) is the developer-facing analogue (P/V) | Past chats renamed to "memory" in March 2026, with import from other AI apps added (V) |
| **User control** | Editable memory summary; memory on/off | View/edit/delete entries in settings; sensitive categories (health, race, ethnicity, gender identity, religion, political belief) excluded unless explicitly opted in (V) | Memory on/off; managed per account |
| **Enterprise control** | Available on Team and Enterprise; **off by default on Enterprise**, admin-controlled (V) | Available across plans including Team and Enterprise (V) | Admins can disable memory and personalization profiles org-wide; not available on work/school/supervised accounts in some configurations (V) |
| **Cross-surface** | Across ChatGPT surfaces | Carries across Claude chat and Claude Cowork (V) | One memory set across web, mobile, desktop, CLI and Workspace apps (V) |

### 5.3 "Dreaming" is the idea worth stealing

Strip the name and it is a specific architectural claim: **consolidation should be asynchronous
and amortized, not synchronous and per-turn.** OpenAI's stated motivation is explicit about
why — staleness, correctness and scalability across hundreds of millions of users and
multi-year horizons, with a reported ~5× reduction in the compute needed to serve it to free
users (V).

That maps onto a design most teams get wrong:

| Where consolidation runs | Cost | Quality | When it's right |
|---|---|---|---|
| **Per turn, in the request path** | Pays on every turn, adds latency | Sees one conversation at a time; can't notice cross-session contradictions | Never, at scale |
| **At session end** | Pays once per session | Sees one session | Small deployments; a reasonable starting point |
| **Asynchronous, batched across sessions** ("dreaming") | Amortized; batchable at 50% on batch lanes | Can deduplicate and supersede across the whole history | The scaled answer |

The implementation is a queue plus a batch job, and the batch lane is exactly what
`appendix-l` §3 calls the lowest priority class. Memory consolidation is the canonical batch
workload: nobody is waiting for it, it is idempotent, and it gets cheaper the more of it you
do at once.

### 5.4 The governance surface you have to build

Everything in §5.2's enterprise row is a requirement list, not a feature list. If you ship
memory into a B2B product, these are table stakes because your buyers have seen them:

| Control | Why a buyer asks for it | Where it lands in this repo |
|---|---|---|
| Org-level off switch, default off | Compliance review will not approve "on by default" for a system that retains user statements | `16`-scope, appendix L §7 |
| Per-user view / edit / delete | GDPR right to rectification and erasure applies to derived facts, not just raw logs | `25` §10 |
| Category exclusions (health, politics, protected characteristics) | Inferring protected attributes is a legal exposure even when the user volunteered them | `17` §4 |
| Tenant isolation of the memory store | A memory leak across tenants is a data breach, not a bug | `25` §10, `16`-scope |
| Memory in the audit trail | "Why did it say that?" is unanswerable if the injected memories are not logged with the trace | `08` §13, appendix L §9 |
| Injection hardening | **A stored memory is untrusted input that gets replayed into every future prompt.** This is the highest-value prompt-injection target in the system | `17` §5, `28` |

The last row deserves emphasis because it is the one that is actually new. A classic injection
attack lives for one turn. An injection that gets *written to memory* executes on every future
session, in the user's own context, with the authority of "something the user told us". Treat
the write path to memory as a privileged operation: validate extracted facts against a schema,
never store raw model-authored instructions, and render memories into the prompt as clearly
delimited *data* (`17` §5.4).

---

## 6. Agent-memory frameworks and the benchmark war

> **In plain words.** There is now a product category selling "memory as a service" for agents,
> and its published benchmark numbers are not usable. Vendors re-run each other's systems with
> their own tuning and publish the configuration where they win. The numbers differ by sixty
> points on the same benchmark. This section is mostly about how to evaluate the category,
> because that skill transfers to every other vendor claim you will be handed.
>
> **Real-world example.** You are asked to pick a memory layer. One vendor's site reports ~92%
> on LoCoMo. An independent reimplementation of the same system on the same benchmark reports
> ~29% (S). Both "numbers" are about the same software. Neither tells you what will happen on
> your data.

### 6.1 The frameworks

| Framework | Model of memory | Distinguishing mechanism | Licence posture (S) |
|---|---|---|---|
| **Mem0** | Extracted facts with entity linking and temporal reasoning | Single-pass extraction, multi-signal retrieval | Apache-2.0 core, self-hostable |
| **Zep** (engine: **Graphiti**) | A **temporal knowledge graph** | Facts carry **validity windows**; provenance preserved; hybrid semantic + keyword + graph traversal | Apache-2.0 core |
| **Letta** (ex-MemGPT) | Tiered agent state the agent manages itself | Persistent agent state across tiers; the agent edits its own memory | Apache-2.0 core |
| **cognee** | Graph + vector hybrid over ingested data | Pipeline-oriented ingestion | Apache-2.0 core |

The one genuinely distinct idea in the list is Graphiti's **validity window**: a fact is stored
with the interval over which it held, so "the user lives in Berlin" does not silently overwrite
"the user lived in Yerevan" — it supersedes it with a date. That is the bi-temporal modelling
pattern from database design, applied to memory, and it is the clean answer to §5's
contradiction problem. You can implement it in a table without adopting a framework
(`25` §7.3, `databases/` for the bi-temporal pattern itself).

### 6.2 The benchmarks, and why they are contested

| Benchmark | What it tests | Why it gets gamed |
|---|---|---|
| **LoCoMo** (Snap) | Recall across long multi-session dialogues, built from persona-grounded conversations with temporal event graphs | Small, public, and the *harness* (how much context each system is allowed, which questions are scored, how answers are judged) is not fixed by the benchmark |
| **LongMemEval** | Long-term memory across chat histories | Configurations are not fully disclosed by the vendors reporting on it |
| Newer sets (BEAM, DolphinBench, STATE-Bench and others) | Different memory capabilities | Too new to have independent replications |

The public state of the dispute, as reported (grade S, and the disagreement itself is the
finding): on LoCoMo one vendor claimed ~84%, a competitor re-ran it and got ~58%, and the first
vendor counter-claimed ~75%. Separately, a self-reported ~92% was independently reimplemented
at ~29%. **Every number in that sentence is about real software on one public benchmark.**

### 6.3 The transferable lesson

This is not a memory-vendor problem. It is the standard shape of a contested benchmark, and
`08` §13 already gives you the tools. The checklist to apply to *any* vendor number:

| Question to ask | Why it decides whether the number means anything |
|---|---|
| **Who ran the baseline?** | A vendor re-running a competitor tunes its own system and accepts the competitor's defaults. That asymmetry is worth tens of points |
| **What was the token budget per system?** | A memory system allowed 30K of retrieved context is not competing with one allowed 4K. Anthropic's own multi-agent finding — token usage explaining ~80% of performance variance (V, §9) — means an unmatched token budget *is* the result |
| **Who judged the answers, with what prompt?** | LLM-judge choice and prompt move scores by more than most architectural differences (`08` §11) |
| **Is the harness published and runnable?** | If you cannot re-run it, it is marketing |
| **What is the variance across seeds?** | A single run on a small set has a confidence interval wider than the claimed gap (`08` §12) |
| **Does the benchmark resemble your traffic?** | LoCoMo is persona-grounded social chat. If your traffic is support tickets over a product catalogue, a win on LoCoMo predicts very little |

**The practical procedure.** Don't try to resolve the dispute. Build the smallest honest
replacement for it: 50–100 multi-session conversations from your own traffic, with written
answer keys, run against (a) no memory, (b) the dumbest possible memory — last-N-turns plus a
flat fact list — and (c) each candidate framework, all at the *same* retrieved-token budget.
That eval costs a couple of days and it is the only number that will survive the procurement
meeting. In practice (b) is a much stronger baseline than the category's marketing implies, and
if it wins you have saved yourself an entire dependency.

### 6.4 Build vs buy, decided on three questions

| If… | Then |
|---|---|
| Your memory is "a few hundred facts per user, mostly preferences" | **Build.** A table, an extraction call, a consolidation job, a retrieval query. `25` §7 is the whole design |
| Facts change over time and the history of the change matters (prices, roles, addresses, entitlements) | **Buy or copy the bi-temporal model.** Validity windows are the hard part and Graphiti has thought about them |
| You need multi-hop reasoning over entities the user mentioned across months | **Consider a graph.** Then read appendix H first, because the cost arithmetic there applies unchanged |
| You are pre-product-market-fit | **Build the dumb version.** Memory is the feature most often shipped before anyone has established that users want continuity |

---

## 7. The labs' own RAG, and its defaults

> **In plain words.** Each provider ships a retrieval system, and each one published or leaked
> enough of its defaults that you can read them as a set of opinions about chunking, retrieval
> and reranking from teams with enormous traffic. Where their defaults disagree with this
> repo's chapters, the disagreement is informative.
>
> **Real-world example.** OpenAI's `file_search` defaults to 800-token chunks with **400 tokens
> of overlap** — a 50% overlap ratio, far above the 10–20% most tutorials recommend. That is a
> team with a lot of traffic deciding that duplicated storage is cheaper than a span split
> across a boundary. It is also a team that cannot see your documents, so it picked the setting
> that fails least badly on unknown input.

### 7.1 OpenAI `file_search`: a managed pipeline with published defaults

| Stage | Default (S, widely reported and consistent across sources) | What the choice tells you |
|---|---|---|
| Parse + chunk | ~800-token chunks, ~400-token overlap, static strategy, configurable via `chunking_strategy` (`max_chunk_size_tokens`, `chunk_overlap_tokens`) | 50% overlap is a **boundary-insurance** setting for unknown documents. Your own corpus, where you know the structure, should do better with structure-aware chunking (`02` §4) |
| Embed | `text-embedding-3-large` at **256 dimensions** | Aggressive Matryoshka truncation — a deliberate storage/latency trade at the cost of some ceiling accuracy (`01` §7) |
| Retrieve | Query rewriting; complex queries decomposed into **multiple parallel searches**; **both keyword and semantic** search | Three things this repo argues for, shipped as defaults: rewriting (`05` §3), decomposition (`05` §5), hybrid (`04` §3) |
| Rerank | Results reranked before generation | A reranker is not optional at this scale (`04` §7) |
| Inject | Up to **20 chunks** into context | ~16K tokens of context at the default chunk size |

**What to copy:** the *order*. Rewrite → decompose → hybrid search → rerank → cap the injected
set. That is the pipeline in `04`+`05`, and the fact that a managed service converged on it is
the strongest available evidence that it is not over-engineering.

**What not to copy:** the 400-token overlap and the 256-dimension embedding. Both are
compromises for arbitrary user uploads. On a corpus you control, structure-aware chunking at
10–20% overlap and full-dimension embeddings are usually better, and you can prove it on your
own data in an afternoon (appendix D).

### 7.2 Claude: the retrieval the model drives

Anthropic's shape is different, and the difference is philosophical. There is no managed vector
store in the Messages API. Instead:

| Surface | Mechanism | The implied opinion |
|---|---|---|
| **Projects (product)** | A **project knowledge search tool** the model calls, enabled automatically past the window boundary (P) | Retrieval is a *tool the agent uses*, not a preprocessing step that happens to it |
| **Citations** | `citations: {enabled: true}` per document block; responses split into text blocks carrying `cited_text` and char/page spans (P) | Provenance is a provider-level primitive, not something you reconstruct by string-matching |
| **Files API + code execution** | Upload, then let the model grep/read/process in a sandbox | Rung 3 by *code*, not by embeddings — "code mode" applied to documents (`30` §5) |
| **Agent Skills** | Folder with a `SKILL.md`; the description sits in context, the body is read on demand (P) | **Progressive disclosure as the default for instructions**, which is the same mechanism as retrieval applied to prompts (`30` §7) |

The citations design is the most directly copyable. Most teams implement citations by asking
the model to emit `[1]` markers and then matching them back to chunks — which fails whenever
the model paraphrases or renumbers. The provider-level version returns the *span* and lets you
render it. If you build your own, build it as spans with character offsets, not as markers
(`06` §10, `appendix-f` on citation recall).

### 7.3 The pattern across all three

| What they all do | What none of them do |
|---|---|
| Hybrid retrieval (keyword + semantic), not pure vector | Expose the chunking to the model |
| Query transformation before search | Let you see the reranker's scores |
| Rerank before injection | Give you per-chunk ACLs — **authorization inside retrieval is still yours** (`04` §12) |
| Cap the injected context | Guarantee recall; every one of them silently returns less than everything |
| Make retrieval *a tool call* on agentic surfaces | Tell you when retrieval missed |

That last pair is the gap you have to fill yourself, and it is the one that matters in
enterprise: **a managed retrieval service cannot enforce your permission model, and it cannot
tell you when it failed to find the answer.** Both of those have to live in your layer
(abstention in `07` §8, recall accounting in appendix F).

### 7.4 Cursor's index: the best-documented production RAG over code

A coding assistant is a RAG system under a latency budget nobody else accepts, over a corpus
that changes every keystroke. The published mechanics (grade S/V — third-party write-ups and
Cursor's own docs and job postings) are worth reading as an engineering sample:

| Mechanism | How it works | Why it generalizes |
|---|---|---|
| **Merkle-tree sync** | The client hashes the project into a Merkle tree; the server keeps its own; the trees are compared periodically and only changed subtrees are re-indexed | **The incremental-reindex pattern.** Any corpus that changes continuously needs change detection cheaper than re-embedding. A content-hash tree gives you "what changed" in log time (`03` §9 on incremental indexing) |
| **Semantic chunking at syntax boundaries** | Files split into functions, classes, logical blocks — not fixed windows | The structure-aware chunking argument from `02` §4, in a domain where boundaries are unambiguous |
| **Domain-trained embeddings** | A custom code embedding model, reportedly trained on agent sessions | Embeddings trained on *the task* (what an agent actually looked up next) rather than on generic similarity (`01` §9) |
| **Turbopuffer as the vector store** | Object-storage-backed vector index | The "cheap cold storage, hot cache" shape from `03` §6 — chosen for cost at scale, not for recall ceilings |
| **Obfuscated storage + privacy mode** | The server stores embeddings and file hashes, not source; in privacy mode embeddings are computed without retaining source | The answer to "we can't send you our code": **index what you must, retain what you can defend.** This is the architecture an enterprise security review actually asks for (`17` §4, `16`-scope) |

The transferable insight is the privacy posture, not the vector store. Cursor's design lets a
security reviewer say "the vendor holds hashes and vectors, not our source", and that sentence
is what unblocks enterprise adoption. If you are selling RAG into regulated buyers, design the
*storage disclosure statement* first and let it constrain the architecture — not the reverse.

---

## 8. Cursor-class systems: the gateway as a product surface

> **In plain words.** A product that serves millions of model calls a day across several
> providers does not call provider SDKs from its application code. It builds one internal
> gateway that every feature talks to, and that gateway owns provider semantics, routing,
> failover, admission control and cost. `23` and appendix L teach the mechanisms; this section
> reads one shipped example and names which mechanisms a real product actually ran into.
>
> **Real-world example.** A provider has a partial outage. In an architecture where features
> call provider SDKs directly, every feature degrades differently and you ship six hotfixes. In
> a gateway architecture, one routing table changes and the product keeps working at slightly
> lower quality. That difference is the entire reason the layer exists.

### 8.1 What Cursor's own description of the layer says it owns

From Cursor's published material on the model-routing and inference team (grade V — their own
description of their system; the GPU and request-path details below are grade S from
third-party write-ups):

| Responsibility | Why it lands in the gateway and not the app | Repo chapter |
|---|---|---|
| **One abstraction over every provider's API semantics**, so onboarding a model is a config change | Provider SDKs disagree about streaming, tool-call shapes, thinking blocks, cache controls, error taxonomies. Normalizing once is the only way a feature team ships against "a model" | `23` §2–§3 |
| **Cross-provider failover**, so no single provider outage degrades users visibly | Failover needs a view of *all* providers' health, which no single call site has | `23` §5, appendix L §6 |
| **Routing backpressure and admission control**, so a traffic spike doesn't cascade into providers | The 429 you cause by over-sending costs you more than the request you shed | appendix L §3–§4 |
| **Model selection / routing per request** | A fast fine-tuned model for autocomplete, a frontier model for agentic edits — the choice is a property of the *request class*, not the feature | `23` §4 |

### 8.2 The two-tier model split is the design, not an optimization

The widely-reported shape (S) — a small fine-tuned model for line-level completion, a frontier
model for agentic edits — is the single most important architectural decision in a product like
this, and it is a *latency* decision before it is a cost decision:

| Tier | Budget | What it implies for context |
|---|---|---|
| **Autocomplete** | Tens of milliseconds to first token | Context must be assembled locally, with no network retrieval. Fill-in-the-middle over the open buffer plus a few neighbours. **No RAG in the hot path** |
| **Agentic edit** | Seconds, with visible progress | Full retrieval, tool calls, sub-agents, the whole ladder |

The mistake this prevents: trying to serve both from one path. A retrieval step that is
excellent for an agentic edit is fatal for autocomplete, and a context assembler tuned for
10 ms produces garbage for a multi-file refactor. **Two request classes, two context
pipelines, one gateway in front** — and the gateway is what lets you route between them
(`12`-scope on TTFT budgets, appendix L §3 on priority classes).

### 8.3 The request path, and where context gets built

The reported end-to-end path (S) is worth laying out because it shows *where* each piece of
this repo's machinery sits in a real product:

```
 editor (local)
   ├─ local index: Merkle tree of file hashes, semantic chunks      ← §7.4
   ├─ local context assembly for the fast tier                     ← §8.2
   └─ client-side encryption
        ↓
 edge (CDN/edge routing)  ← TLS termination, geo routing, DDoS
        ↓
 API gateway ← auth, tenant identity, quota                        ← appendix L §4, `29`
        ↓
 inference gateway  ← provider abstraction, routing, failover,
                      admission control, budget ledger             ← `23`, appendix L
        ↓
 provider APIs / own GPU fleet
```

Two observations that matter more than the diagram:

1. **Context assembly happens at the edge of the system, not the middle.** The client knows
   which file is open, what the cursor is near, what the user just typed. Pushing that to a
   server means sending more and knowing less. Any product with a rich local state should
   assemble context locally and send a *selection*, not a corpus.
2. **The budget ledger has to sit at the gateway**, because that is the only place that sees
   every call a single user turn fans out into. A per-request cost cap enforced per call is not
   a cap; a turn that fans out to twelve calls spends twelve times it (appendix L §2's shared
   ledger, §4's ceilings).

### 8.4 Fronting a tool like this with your own gateway

The enterprise pattern, and the reason OpenAI-compatible endpoints matter: because these
products accept an OpenAI-compatible base URL, an enterprise can put its *own* gateway in front
of the vendor's model access with one configuration change (S, and widely used). What the
customer gateway is actually for:

| What the enterprise gateway adds | Why the vendor can't provide it |
|---|---|
| Central key custody; no per-developer provider keys | The vendor doesn't own your key policy |
| Prompt and completion logging for review and DLP | Your retention rules, your jurisdiction |
| Per-team budgets and quota | Your cost centres |
| Model allowlists (e.g. no models without a DPA) | Your procurement constraints |
| Routing to a self-hosted or regional deployment | Your residency requirements |

This is appendix L §11's "enterprise reference configuration" seen from the buyer's side, and
it is the most common real-world use of an AI gateway in 2026: not clever routing, but
**interposing policy on a tool your engineers already adopted.** If you are asked to "get
Cursor approved", the deliverable is a gateway config and a data-flow document, not a model
evaluation.

### 8.5 The research frontier: routing and cache as one problem

The open research direction worth knowing the shape of (grade S, recent preprints): joint
optimization of **model routing and KV-cache management** in a unified gateway. The intuition
is direct from §2.4 — if the gateway decides which model serves a request, and caches are
model-keyed, then routing decisions destroy cache locality. A router that optimizes per-request
cost while ignoring cache state can be *more* expensive than a dumb sticky router.

The practical rule you can apply today without any research: **make routing sticky per
conversation.** Route the first turn however you like; pin subsequent turns of the same
conversation to the same model unless quality or availability forces a change. This is the
single highest-value routing rule in a cached world, and most routing layers get it wrong by
re-deciding per request (`23` §4.5).

---

## 9. Orchestration: the one public disagreement worth reading

> **In plain words.** Two teams published opposite advice about multi-agent systems within
> weeks of each other, both from direct production experience, both correct. Reconciling them
> gives you a decision rule you can actually apply, and it is a better rule than either post
> states on its own.
>
> **Real-world example.** You build a research agent that fans out to five sub-agents. It works
> beautifully. You reuse the same architecture for a code-editing agent that fans out to five
> sub-agents editing five files. It produces a codebase where five agents each solved the
> problem a different way. Same architecture, opposite outcome.

### 9.1 The two positions

| | **Anthropic — "How we built our multi-agent research system"** | **Cognition — "Don't build multi-agents"** |
|---|---|---|
| Architecture | Orchestrator–worker: a lead agent spawns parallel sub-agents, each with its **own context window** | Single-threaded agent; if you must split, share the **full trace**, not messages |
| Reported result | The multi-agent setup beat a single-agent baseline by **90.2%** on their internal research eval (V) | Fragmented context produces conflicting implicit decisions, and conflicting decisions produce bad results |
| Cost | **~15× the tokens** of a single-agent chat interaction (V) | — |
| The key measurement | **Token usage explained ~80% of performance variance** (V) | — |
| Task class | Research: read-only, parallel, independently verifiable | Coding: write-heavy, interdependent, order-sensitive |

### 9.2 The reconciliation

Both posts are about the same variable and neither names it: **whether the sub-tasks write to
shared state.**

| | Reads only | Writes to shared state |
|---|---|---|
| **Independent sub-tasks** | **Fan out.** This is Anthropic's case. Each sub-agent gets a clean window, results merge, and the merge is a summarization problem you already know how to solve | **Serialize, or partition the state first.** Two agents writing the same file is a lost-update race with a language model as the writer |
| **Interdependent sub-tasks** | Fan out, then *re-read* in one agent before concluding — the second agent needs the first's findings, so the dependency is in the merge | **Single agent.** This is Cognition's case. There is no cheap way to give two agents a consistent view of a state they are both mutating |

The decision rule, in one line: **fan out over reads, serialize over writes, and never fan out
over a state the branches mutate.**

### 9.3 The 80% finding is the one to internalize

"Token usage explained ~80% of performance variance" (V) is the most useful number in the
multi-agent literature, and it is usually quoted as a cost warning. It is better read as a
*measurement warning*:

- If tokens spent predict four-fifths of quality, then **any comparison between architectures
  at unequal token budgets measures the budget, not the architecture.** The multi-agent win is
  partly a win for "spent 15× more compute".
- Which means the honest baseline for a multi-agent system is not "single agent, default
  settings". It is **single agent, at the same token budget** — higher effort, more turns, more
  retrieval. Sometimes it wins, and when it does you have saved an orchestration layer.
- And it means the memory-framework benchmarks in §6.2 are uninterpretable without matched
  budgets, which is exactly what §6.3 asks you to control for. The same finding invalidates
  both sets of marketing.

### 9.4 What the labs do about sub-agent context, concretely

The mechanisms, as shipped (P, Anthropic API), and what each one is for:

| Mechanism | What it isolates | The cost |
|---|---|---|
| **Sub-agents** | A whole context window per sub-task; the parent sees only the summary | The parent loses detail it cannot recover; the summary is a lossy interface you must design (`30` §6) |
| **Code mode / programmatic tool calling** | Intermediate tool results never enter any window — they flow between function calls inside a sandbox | Debuggability; a failure inside the script is harder to attribute (`30` §5) |
| **Tool search with deferred loading** | Tool *schemas* stay out until needed, and are **appended** so the cache survives | A search round trip; and the model can only use what it can find, so descriptions become load-bearing (`30` §3) |
| **Skills** | Instruction bodies stay out until the task calls for them | A read step; and a skill nobody loads is a skill that does nothing (`30` §7) |
| **Task budgets** | A token ceiling the model is *aware* of, so it paces itself and finishes rather than being truncated (P, beta) | Advisory, not enforced. `max_tokens` is the enforced ceiling and the model cannot see it |

The last row is a primitive this repo's chapters predate and it is worth noting as a genuine
2026 addition: the difference between a budget the model knows about and a limit that cuts it
off. An agent told "you have 64K tokens for this task" writes a conclusion at 60K. An agent
cut off at 64K produces a truncated thought. If you build your own loop, inject the remaining
budget into the prompt — it is the cheapest quality improvement available on long-horizon runs
(`22` §7, appendix L §2).

---

## 10. Long context vs retrieval, 2026 state

> **In plain words.** Every frontier model now advertises a 1M-token window. The question
> "does that kill RAG" has an answer, and the answer has not changed in two years even though
> the windows grew 5×: **the advertised window is a capacity, not a performance guarantee**,
> and the economics of re-sending a corpus per question are what actually decide it.

`06` §11 argues this case in full. What is new as of 2026, and what is still contested:

| Claim | Grade | Reading |
|---|---|---|
| All frontier models ship ~1M context (Claude Opus/Sonnet/Fable tiers at 1M; Gemini at ~1M; OpenAI flagships in the same range) | **P** | Capacity is no longer the differentiator |
| Retrieval accuracy degrades well before the advertised limit, and models do not perform at 1M as well as at 200K | **S**, but consistent across many independent evaluations and the long-context benchmark literature (LongBench v2, and the "Long Context vs RAG" evaluations) | **Design against the effective window, not the advertised one.** Measure yours (`06` §4, lab M.1) |
| Needle-in-a-haystack scores near 100% while realistic multi-fact retrieval is far lower | **S** | NIAH is a *smoke test*, not an evaluation. A model that passes it can still fail to aggregate across ten documents |
| Long-context prompting can beat RAG on correctness at an order-of-magnitude-plus higher per-query token cost | **S** — the specific multipliers circulating (10×–30× and up) are third-party and vary enormously with the setup | Treat the *direction* as reliable and the *multiplier* as something you compute for your own corpus. §10.1 shows how |

### 10.1 Compute it yourself, in four lines

The decision does not need a benchmark. It needs your corpus size, your question volume and
your cache hit rate:

```python
def per_question_input_tokens(corpus: int, retrieved: int, overhead: int,
                              cached_fraction: float,
                              read_rate: float = 0.1) -> tuple[float, float]:
    """Effective billed input tokens per question, long-context vs retrieval."""
    long_ctx = overhead + corpus * (cached_fraction * read_rate + (1 - cached_fraction))
    rag      = overhead + retrieved          # plus the index cost, amortized separately
    return long_ctx, rag

# 400K-token corpus, 4K retrieved, 1K overhead:
#   no cache:            401,000  vs  5,000   → 80×
#   90% cache hits:       77,000  vs  5,000   → 15×
#   100% cache hits:      41,000  vs  5,000   → 8×   (the 0.1x floor on the whole corpus)
```

The arithmetic has one non-obvious consequence worth stating plainly: **caching does not make
long-context competitive with retrieval on cost — it makes it competitive with *itself***.
Even at a perfect hit rate you pay 0.1× of the whole corpus on every question, which on a
corpus two orders of magnitude larger than the retrieved span is still ten times the retrieval
bill. Caching changes long-context from "unaffordable" to "affordable when the quality is worth
8×", and that is a real and useful change — but the shape of the answer is the same as it was
at 200K windows.

### 10.2 The honest decision table

| Situation | Use |
|---|---|
| Corpus fits the effective window, few questions per document, answers need global synthesis | **Long context.** Cache it. Don't build an index for a one-off |
| Corpus fits, many questions per document, answers are local lookups | **Long context + cache**, and measure against retrieval before adding an index |
| Corpus exceeds the window, or exceeds it per-tenant | **Retrieval.** No choice |
| Corpus fits but answers require aggregation over the *whole* corpus ("main themes across 40K tickets") | **Neither, alone.** This is appendix H's global-query case: precomputed summaries, or map-reduce |
| Latency budget under a second | **Retrieval.** Prefill on a 400K prompt is not a sub-second operation, cached or not |
| Per-chunk permissions | **Retrieval.** Authorization has to happen *inside* retrieval; you cannot filter a prompt you already sent (`04` §12) |

The last row is the one that ends the argument in enterprise settings, and it is rarely in the
blog posts. A 1M window does not help you if half the corpus is invisible to the asking user.

---

## 11. What to copy, what to skip

> **In plain words.** The point of reading other people's systems is to steal the decisions that
> generalize and leave the ones that are properties of their scale or their business model.

### 11.1 Copy these

| Pattern | From | Why it generalizes |
|---|---|---|
| **The hysteretic ladder** — in-context under the boundary, retrieval over it, and *back down* when the corpus shrinks | Claude Projects (§1.2) | Costs you nothing, prevents permanent degradation after a transient upload |
| **Tell the model which mode it is in** | Claude Projects (§1.3) | The difference between "I don't have that document" and a confidently wrong aggregate |
| **Asynchronous, batched memory consolidation** | ChatGPT dreaming (§5.3) | Removes consolidation from the request path and makes it batch-lane cheap |
| **Validity windows on stored facts** | Graphiti (§6.1) | The clean answer to contradicting memories, implementable in a table |
| **Rewrite → decompose → hybrid → rerank → cap** | OpenAI `file_search` (§7.1) | A managed service's defaults converging on this repo's pipeline is strong evidence |
| **Citations as spans, not markers** | Claude citations (§7.2) | Survives paraphrase; makes citation recall measurable (appendix F) |
| **Merkle-tree change detection** | Cursor (§7.4) | Any continuously-changing corpus needs re-index detection cheaper than re-embedding |
| **Index what you must, retain what you can defend** | Cursor privacy mode (§7.4) | The sentence that unblocks enterprise procurement |
| **One internal gateway owning provider semantics** | Cursor's inference gateway (§8.1) | The only way feature teams ship against "a model" instead of against six SDKs |
| **Two request classes, two context pipelines** | Cursor's fast/frontier split (§8.2) | A 10 ms path and a 10 s path cannot share a context assembler |
| **Sticky routing per conversation** | §8.5 | Caches are model-keyed; re-deciding per request forfeits them |
| **Fan out over reads, serialize over writes** | Anthropic vs Cognition (§9.2) | Resolves the multi-agent question for your task class |
| **Budget the model can see** | Task budgets (§9.4) | An agent that knows its remaining budget finishes; one that is cut off doesn't |
| **Progressive disclosure for instructions** | Agent Skills (§7.2) | Retrieval, applied to prompts. The description is in context; the body is loaded on demand |

### 11.2 Skip these

| Pattern | Why it doesn't transfer |
|---|---|
| **800/400 chunking and 256-dim embeddings** | Defaults for *unknown* documents. You know your corpus; structure-aware chunking beats them (appendix D) |
| **Explicit cache-breakpoint micro-optimization** | The industry is moving to implicit (§2.3). Invest in prefix *stability*, not breakpoint placement |
| **Learned / soft prompt compression** | Model-version-tied, cache-hostile, and the agent patterns dominate it (§4.2) |
| **Multi-agent fan-out as a default architecture** | ~15× tokens for a win that is partly a token-budget win (§9.3). Earn it with a matched-budget baseline |
| **A memory framework before a memory eval** | The published numbers are contested by sixty points (§6.2). Build the 100-conversation eval first |
| **A graph before appendix H's arithmetic** | Index cost can be ~1,000× vector RAG and it often loses on ordinary questions |
| **Your own trimmer on top of the provider's** | Two independent trimmers produce prompts that are neither cacheable nor predictable (appendix L §5) |

### 11.3 The reference design this appendix implies

Putting §0's ladder together with everything above, for a document-heavy enterprise assistant:

```
Ingest        structure-aware chunking (02) + a Merkle/content-hash manifest for incremental reindex (7.4)
Storage       vectors + BM25 + per-chunk ACL columns (03, 04 §12)
Mode select   hysteretic: in-context + cache under the effective window, retrieval over it (1.3)
Retrieve      rewrite → decompose → hybrid → rerank → cap (7.1), ACL filter inside the query (04 §12)
Prompt        stable prefix (tools → system → corpus/instructions) with one breakpoint;
              volatile content after it (2.1)
Agent loop    tool search for schemas, code mode for bulk processing, sub-agents for read fan-out (9.4)
Long horizon  clear tool results → compact → structured notes, in that order (3.3)
Memory        extraction → async batched consolidation with validity windows → scoped retrieval (5.3, 6.1)
Gateway       one abstraction, sticky per conversation, shared budget ledger per user turn (8)
Observability mode in the trace, cache hit rate, per-stage recall, faithfulness (appendix F, G, L §9)
```

Every line of that is one of the chapters. The contribution of this appendix is only the
*order*, and the evidence that four separate companies arrived at approximately that order
independently.

---

## 12. Anti-patterns

| Anti-pattern | What goes wrong | The fix |
|---|---|---|
| **"We have a 1M window, we don't need retrieval"** | You pay 0.1× of the whole corpus per question even at a perfect cache hit rate, and accuracy sags before the limit | §10.1's arithmetic on your own corpus; measure the effective window (lab M.1) |
| **Treating a file handle as a cache** | Upload-once APIs save bandwidth, not input tokens. The bill doesn't move | Cache the prefix *and* use the file handle (§1.1) |
| **Caching a prompt with a timestamp in it** | 0% hit rate, and the write multiplier means you now pay *more* than no caching | Audit the invalidator list in §2.3; verify with `cache_read_input_tokens` |
| **Two trimmers** | Your trimmer and the provider's cut at different points; prefixes never stabilize | Pick one. If you keep yours, cut below the breakpoint (appendix L §5) |
| **Appending only the text of a compacted response** | Compaction state is lost, history re-expands, the model contradicts a summary it can't see — **silently** | Append the whole response content (§3.2) |
| **Compacting before clearing** | You pay a model call to destroy information that clearing would have removed for free and reversibly | Clear → compact → persist (§3.3) |
| **Memory as an append-only fact log** | Contradictions accumulate; retrieval returns whichever duplicate embeds closer; users report "it remembers things I corrected" | Consolidation with supersession and validity windows (§5.1, §6.1) |
| **Memories injected as instructions** | A stored injection executes on every future session with the user's authority | Render memories as delimited data; schema-validate the write path (§5.4) |
| **Quoting a vendor's benchmark number** | On the same public benchmark, the same software has been reported sixty points apart | Build the matched-budget eval in §6.3 |
| **Comparing architectures at unequal token budgets** | Token spend explains ~80% of the variance, so you measured the budget | Matched-budget baselines (§9.3) |
| **Fanning out agents over a shared mutable state** | Five agents solve the problem five different ways; last write wins | Fan out over reads only (§9.2) |
| **Prompt compression on a cached workload** | Compression makes the prefix unstable, forfeiting a 10× discount to win 3× | §4.2; do the free token hygiene instead |
| **Routing per request in a cached world** | Every re-decision is a cold cache | Sticky per conversation (§8.5) |
| **One context pipeline for a 10 ms path and a 10 s path** | Either autocomplete is slow or refactors are context-starved | Two request classes (§8.2) |
| **Server-side retrieval as your authorization boundary** | Managed retrieval does not know your permission model | ACLs inside the query, always yours (§7.3) |

---

## 13. Interview questions

1. **"A customer uploads a 5,000-page document set. Walk me through what happens."**
   Name the ladder (§0) and pick rungs with arithmetic, not taste: does it fit the *effective*
   window; if yes, cache it and compute §10.1's ratio before building an index; if no, retrieval
   with hysteresis back down (§1.3), and tell the model which mode it is in. Mention that
   per-chunk permissions force retrieval regardless of size.

2. **"Our cache hit rate is 11%. Where do you look?"**
   Not at the cache. At prefix stability: timestamps or request IDs above the breakpoint,
   unsorted JSON, a tool list built from a set, a mid-session system-prompt edit, your own
   trimmer cutting above the breakpoint, a model fallback. Verify with
   `cache_read_input_tokens`, and note that a write multiplier means a 11%-hit workload is
   *more* expensive than no caching (§2.2, §2.3).

3. **"Should we use prompt compression to cut our context bill?"**
   Lead with the question of prefix reuse. If the prefix is reused, caching dominates and
   compression actively fights it (§4.2). If every request is unique and long, compression is on
   the table — after free token hygiene, and only with a faithfulness and span-survival column
   in the eval (§4.4). Mention that the agent-native alternatives (tool search, code mode,
   just-in-time retrieval) beat it by never loading the tokens.

4. **"Design memory for our assistant."**
   Four stages (§5.1), and lead with stage 2 because it is the one everyone skips: consolidation
   with supersession, run asynchronously and batched (§5.3), with validity windows so corrections
   supersede rather than accumulate (§6.1). Then the governance list — org off switch defaulting
   off, per-user delete, category exclusions, tenant isolation, memories in the trace — and the
   injection argument: the memory write path is privileged because it replays into every future
   session (§5.4).

5. **"Vendor X reports 92% on LoCoMo. Do we buy it?"**
   The number has been independently reimplemented sixty points lower, and two vendors dispute
   each other's methodology on the same set (§6.2). Then the checklist: who ran the baseline, were
   token budgets matched, who judged, is the harness runnable, what's the variance, does the
   benchmark resemble our traffic (§6.3). Propose the 100-conversation internal eval with a
   last-N-turns baseline — and note that the dumb baseline often wins.

6. **"Multi-agent or single agent for this?"**
   Ask whether the sub-tasks write to shared state. Reads and independent: fan out (Anthropic's
   case, ~90% win at ~15× tokens). Writes to shared state: single agent or partition first
   (Cognition's case). Then the measurement point: token spend explains ~80% of the variance, so
   the baseline must be a single agent at the *same* budget (§9).

7. **"Why does every AI product build its own gateway?"**
   Provider semantics normalization, cross-provider failover, admission control and backpressure,
   per-request-class routing, and a shared budget ledger per *user turn* rather than per call
   (§8.1, §8.3). Then the enterprise mirror image: because these tools accept OpenAI-compatible
   base URLs, the customer puts its own gateway in front for key custody, DLP logging, budgets,
   model allowlists and residency — which is what "get Cursor approved" actually means (§8.4).

8. **"What's the difference between context editing, compaction and memory?"**
   Reversible deletion, lossy summarization, durable externalization — with different costs, cache
   effects and failure modes (§3.1). Both Anthropic and OpenAI ship compaction server-side now, and
   both have the same silent failure: appending only the response text loses the compaction state
   (§3.2). Use them in the order clear → compact → persist (§3.3).

---

## 14. Lab exercises

**M.1 — Measure your effective window (Core).**
Take a document of ~400K tokens and a set of 30 questions whose answers you have placed at known
depths (10%, 30%, 50%, 70%, 90%). Ask each question with the document at 50K, 100K, 200K and 400K
of surrounding filler. *Predict* the depth-by-length accuracy surface before running it. Report
accuracy per (length, depth) cell and name your effective window: the largest length at which
accuracy at every depth is within 2 points of the 50K result. Compare your surface to a
needle-in-a-haystack run on the same model and explain the gap (§10, `06` §5).

**M.2 — The caching break-even, measured (Core).**
Build a 60K-token stable prefix and a 200-question workload. Run it three ways: no caching, cached
with the short TTL, cached with the long TTL. Record `cache_read_input_tokens`,
`cache_creation_input_tokens` and billed cost per question. *Predict* the break-even `n*` from
§2.2 first, then find where the measured curves cross. Then break it deliberately: insert
`datetime.now()` into the system prompt and show the hit rate collapse and the resulting *increase*
in cost over the uncached run.

**M.3 — The hysteretic ladder (Core).**
Implement `choose_path` from §1.3 over a growing corpus. Add 20 documents one at a time, crossing
the window boundary; then delete 15. Assert: the mode switches up at the boundary, switches back
down, the model is told which mode it is in, and the mode is recorded in the trace. Then show the
bug you prevented: run the same sequence with a one-way switch and report the cost difference over
the final 50 questions.

**M.4 — Clear vs compact vs notes, on one agent run (Stretch).**
Take a 50-turn tool-heavy agent loop. Run four configurations: baseline, clearing (`keep = 3`),
compaction, and clearing + structured notes. Report for each: final window size, summed input
tokens, cache hit rate, turn at which the window would have overflowed, and task success on 20
tasks. *Predict* the ranking first. The expected surprise is in the cache column (§3, `30` §11).

**M.5 — A memory eval you can defend (Stretch).**
Build 60 two-session conversations from your own domain where session 2 depends on a fact from
session 1, including 10 where the fact is *corrected* in session 1. Evaluate four systems at an
identical retrieved-token budget: no memory, last-N-turns, a flat fact list, and a fact list with
supersession. Report recall of the correct fact and the rate at which a superseded fact is
retrieved instead. *Predict* which system wins the corrections slice before running it (§5, §6.3).

**M.6 — Compression, honestly (Stretch).**
Pick 40 questions whose answers depend on a negation, a unit or a numeric qualifier. Compress their
retrieved contexts at ratios 1.0 / 0.75 / 0.5 / 0.33 / 0.2 with any extractive compressor. Report
the four columns from §4.4 — tokens, correctness, faithfulness, span survival. Then compute what a
cache hit would have saved on the same workload and state which lever you would ship.

**M.7 — Fan-out over reads vs writes (Stretch).**
Build two tasks: (a) "summarize what these 8 documents say about X" (read-only) and (b) "apply this
rename across these 8 files" (write-heavy). Run each with a single agent and with an 8-way fan-out,
at *matched* token budgets. Report success rate, token spend and — for (b) — the number of
mutually inconsistent edits. *Predict* both outcomes from §9.2 before running.

**M.8 — Sticky vs per-request routing (Stretch).**
Simulate 500 multi-turn conversations across two models with a prefix cache. Route (a) per request
by cheapest-model heuristic, (b) sticky per conversation. Report cache hit rate and total cost.
*Predict* whether the "cheaper" router is actually cheaper (§8.5).

---

## 15. Sources

**Primary — vendor API and product documentation** (read 2026-10-16). Anthropic: prompt caching,
context editing (`clear_tool_uses_20250919`, `clear_thinking_20251015`), compaction
(`compact_20260112`), the memory tool (`memory_20250818`), Files API, citations, tool search and
deferred loading, programmatic tool calling, task budgets, mid-conversation system messages, cache
diagnosis, and the published model/context/pricing tables, at `platform.claude.com/docs`. Claude
Help Center, *What are projects?* and *Retrieval augmented generation (RAG) for projects*
(`support.claude.com`) — the source for the automatic RAG-mode switch, the "up to 10x" capacity
claim, the project knowledge search tool, the visual indicator and the conversion back to
in-context. OpenAI: *Prompt caching* and *Compaction* guides at `platform.openai.com/docs/guides/`
— the source for implicit vs explicit breakpoints, the 1,024-token minimum, the 0.1× read / 1.25×
write multipliers, `prompt_cache_options.ttl`, `prompt_cache_retention` (`in_memory`, `24h`), the
ZDR retention defaults, and `context_management.compact_threshold`. Google: *Context caching*,
Gemini API docs (`ai.google.dev/gemini-api/docs/caching`) — implicit-caching minimums per model,
and the removal of explicit cache objects from the Interactions API and Gemini 4 Argon onward.

**Vendor-published engineering** — Anthropic, *How we built our multi-agent research system* (2025)
and the context-engineering and tool-writing posts; OpenAI, *Dreaming: Better memory for a more
helpful ChatGPT* (`openai.com/index/chatgpt-memory-dreaming/`, rollout from 2026-06-04) and
*Memory and new controls for ChatGPT*; Cognition, *Don't build multi-agents*; Cursor's published
description of its model-routing and inference work; Google's Gemini-at-work and Gemini Enterprise
personalization/memory documentation.

**Research** — Jiang et al., *LLMLingua* (2023) and Pan et al., *LLMLingua-2: Data Distillation for
Efficient and Faithful Task-Agnostic Prompt Compression* (2024); Mu et al., *Learning to Compress
Prompts with Gist Tokens* (2023); Chevalier et al., *AutoCompressor* (2023); Ge et al., *ICAE*
(2024); surveys: *Contextual Compression in Retrieval-Augmented Generation for LLMs* (2024), *A
Comprehensive Survey on Long Context Language Modeling* (2025), *KV Cache Compression, But What Must
We Give in Return?* (2024). Long context vs retrieval: *Long Context vs. RAG for LLMs: An Evaluation
and Revisits* (2025), *LongBench v2* (2024), *When Retrieval Succeeds and Fails* (2025). Memory
benchmarks: *LoCoMo* (Snap), *LongMemEval*. Serving: vLLM (paged attention, automatic prefix
caching) and SGLang (RadixAttention) documentation and papers.

**Secondary** — third-party write-ups of Cursor's indexing (Merkle-tree sync, semantic chunking,
Turbopuffer, privacy mode) and request path; the Mem0 / Zep benchmark dispute, including Zep's
*Is Mem0 Really SOTA in Agent Memory?* and the independent reimplementation reported at ~29.3% on
LoCoMo against a ~92% self-report; and the reported `file_search` chunking defaults (≈800-token
chunks, 400-token overlap, `text-embedding-3-large` at 256 dimensions, up to 20 chunks). Everything
in this paragraph is a hypothesis to test, not a number to quote.

**Dates matter here more than anywhere else in this repo.** Four of the mechanisms described above
did not exist twelve months before the collection date, and two (Gemini explicit caching, OpenAI's
`prompt_cache_key` as a hit-rate lever) were deprecated or demoted inside that window. Re-read the
primary docs before relying on a parameter name.
