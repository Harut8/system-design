# 25 — memory and state management

> **Prerequisites:** [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md) (§3's reducers and §5's
> checkpointing are the mechanical substrate this chapter builds memory systems on top of — read that
> chapter for *how* state persists before this one for *what* to put in it and *how much*),
> [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md) (§8's three-kinds-of-state
> taxonomy — conversation, task, world — is the sibling cut of the same problem this chapter's §1
> re-derives from a memory-lifetime angle instead of a semantic-content angle; §6's blackboard pattern
> is the multi-agent shared-state mechanism §11 here builds on directly),
> [`20-langchain-architecture-and-internals.md`](20-langchain-architecture-and-internals.md) (the
> `Runnable` and message-type primitives §3 and §13 assume, and the legacy `Memory` abstraction §13
> explains the deprecation of), [`../databases/01-storage-engine-fundamentals.md`](../databases/01-storage-engine-fundamentals.md)
> (the WAL-durability, buffer-pool, and page-write tradeoffs behind every storage backend decision in
> §8 — a Postgres checkpoint table and a B-tree index are solving the same durable-write problem, and
> the vocabulary transfers exactly).
>
> **Feeds into:** `14-agent-evaluation.md` (planned — you cannot score a multi-turn agent trajectory
> without knowing which memory tier produced which fact in context, and §3–§5 here are the
> instrumentation prerequisite for that), `16-multi-tenancy-and-isolation.md` (planned — §10's per-
> tenant memory isolation is that chapter's subject matter applied one layer earlier, to the store
> instead of the compute), `17-safety-guardrails-and-prompt-injection.md` (planned — a long-term memory
> store that persists an attacker-injected "fact" across sessions is a *persistent* prompt injection,
> strictly worse than the single-turn kind that chapter otherwise assumes, and §7's fact-extraction
> validation is the first line of defense against it).
>
> **THESIS:** Memory is not one system. It is three systems — conversation history, agent state, and
> long-term memory — with different lifetimes, different consistency requirements, different storage
> answers, and different failure modes, and the single most expensive mistake a team makes in
> production is collapsing all three into one growing list of messages threaded through every LLM call
> and calling the whole pile "memory." That mistake is not a style preference; it has a bill attached.
> Every unnecessary token in context is billed, at every turn, for the rest of the conversation's life,
> and past roughly a few thousand tokens of irrelevant history the model's attention measurably degrades
> on the part that matters — the "lost in the middle" effect is not folklore, it is a reproducible
> retrieval curve. **The job of a memory system is not to remember everything. It is to forget
> correctly** — to decide, at every turn, which of everything that has ever happened is worth the
> tokens to re-state, and to make that decision cheaply, deterministically where possible, and legibly
> enough that a human debugging a bad response can tell *why* the model didn't know something it
> should have. Conversation history needs windowing and summarization because it grows without bound.
> Agent state needs a typed schema and a reducer discipline because it is read and written by concurrent
> steps that must not clobber each other. Long-term memory needs retrieval, not concatenation, because
> a user's entire history of interactions will never fit in a context window and mostly should not —
> only the handful of facts relevant to *this* turn should. Get this separation right and the rest of
> this chapter is implementation detail. Get it wrong and no amount of prompt engineering fixes a system
> that is, structurally, a context window slowly filling with noise.

---

## Contents

1. [The three kinds of memory](#1-the-three-kinds-of-memory)
2. [Why you can't just dump everything into the prompt](#2-why-you-cant-just-dump-everything-into-the-prompt)
3. [Conversation history management: buffers, windows, and summarization](#3-conversation-history-management-buffers-windows-and-summarization)
4. [Message trimming strategies](#4-message-trimming-strategies)
5. [Agent state in LangGraph: schemas, reducers, and checkpointing](#5-agent-state-in-langgraph-schemas-reducers-and-checkpointing)
6. [Workflow state machines](#6-workflow-state-machines)
7. [Long-term memory architectures](#7-long-term-memory-architectures)
8. [Memory storage backends: the decision matrix](#8-memory-storage-backends-the-decision-matrix)
9. [Semantic memory and retrieval: memory as RAG](#9-semantic-memory-and-retrieval-memory-as-rag)
10. [Memory in multi-tenant systems](#10-memory-in-multi-tenant-systems)
11. [Memory in multi-agent systems](#11-memory-in-multi-agent-systems)
12. [The context window budget](#12-the-context-window-budget)
13. [LangChain memory classes and why LangGraph replaced them](#13-langchain-memory-classes-and-why-langgraph-replaced-them)
14. [Production patterns](#14-production-patterns)
15. [Anti-patterns](#15-anti-patterns)
16. [Interview questions, with weak and strong answers](#16-interview-questions-with-weak-and-strong-answers)
17. [Lab exercises](#17-lab-exercises)

---

## 1. The three kinds of memory

Start by refusing the word "memory" as a single concept, because production systems that treat it as
one thing invariably build one data structure — usually a `list[Message]` — and try to make it serve
three incompatible purposes at once. Split it before writing a line of code.

### 1.1 Conversation history

Conversation history is the literal transcript: the sequence of user and assistant turns (and tool
calls and tool results interleaved among them) that make up one continuous dialogue. Its defining
properties:

- **Append-only and chronological.** New turns are added at the end; old turns are never rewritten,
  only trimmed or summarized away.
- **Lifetime bounded by the session, but often longer in practice.** A chat product's users routinely
  return to the same thread across days or weeks, so "session" is a product decision, not a technical
  ceiling — the technical ceiling is the context window, and it arrives long before most users would
  consider the conversation "over."
- **Read by the model on (almost) every turn**, which makes it the single biggest driver of per-turn
  cost in a long-running chat, and the primary target of every technique in §3–§4.
- **Storage shape:** an ordered list of typed messages (`HumanMessage`, `AIMessage`, `ToolMessage`,
  `SystemMessage` in LangChain's vocabulary), keyed by a conversation or thread identifier.

### 1.2 Agent state

Agent state is the **working memory of a single, in-flight execution** — everything the orchestration
layer needs to know to correctly resume or continue a task, that is not itself part of the dialogue a
human would read. Concretely: which step of a plan is next, what a tool call returned three steps ago,
whether a human-in-the-loop approval is pending, how many tokens or dollars this run has spent so far,
which branch of a conditional a router already took. This is exactly `22-agent-orchestration-patterns.md`
§8.1's "task state," looked at from the storage-and-lifetime angle instead of the semantic-content
angle:

- **Structured and typed**, not prose — a `TypedDict` or `dataclass` with named fields, not a message
  list. This is the property that makes it programmatically inspectable: "is `approved` true" is a
  field read, not a string search over a transcript.
- **Lifetime bounded by the task**, not the conversation. A single conversation can contain many tasks
  (a chat session where the user asks the agent to do five unrelated things in sequence), each with its
  own agent state that should not leak into the next task's state.
- **Written by every node/step, read by the router and by the next step** — it is the thing that makes
  a resumed execution behave identically to an uninterrupted one, which is why LangGraph's checkpointer
  (§5, and `21-langgraph-deep-dive.md` §5 in full) checkpoints exactly this object.
- **Storage shape:** a typed schema, versioned, small — the discipline in `22-agent-orchestration-patterns.md`
  §8.4 ("every field typed, versioned, minimal") applies without modification.

### 1.3 Long-term memory

Long-term memory is knowledge that outlives any single conversation or task: a user's stated
preferences ("always answer in bullet points," "I use Python 3.12 and pytest, not unittest"), facts
learned about them ("works at a fintech, cares about compliance"), or organizational knowledge that
should inform every future interaction regardless of which session it happens in.

- **Lifetime spans sessions**, deliberately — the entire point is that it survives the conversation
  that produced it and is available in a *different*, later conversation.
- **Written rarely, relative to conversation turns** — a fact is extracted and stored once, then read
  many times across many future sessions, which is the opposite read/write ratio of conversation
  history (written every turn, read as a whole).
- **Retrieved, not concatenated.** Unlike conversation history, which is naturally small enough (before
  it isn't, see §3) to include wholesale, long-term memory is, by definition, unbounded over a user's
  lifetime — you cannot include "everything this user has ever told the system," so long-term memory is
  architecturally a retrieval problem: embed it, index it, retrieve the top-K relevant items for *this*
  turn (§9), the same discipline `01`–`04` teach for document retrieval, pointed at a different corpus.
- **Storage shape:** varies by what is being remembered — key-value for discrete preferences, a vector
  store for semantically retrievable facts and past interactions, a graph for relationships between
  entities (§7.4).

### 1.4 Why the distinction is load-bearing, not academic

The three kinds differ on every axis that matters for system design:

| Axis | Conversation history | Agent state | Long-term memory |
|---|---|---|---|
| Lifetime | Session (often longer) | Single task execution | Cross-session, indefinite |
| Write frequency | Every turn | Every step | Rare (fact extraction events) |
| Read frequency | Nearly every model call | Every step | Selectively, per-turn retrieval |
| Shape | Ordered message list | Typed schema | Key-value / vector / graph |
| Growth | Unbounded, needs active management | Bounded by task length | Unbounded, needs retrieval not truncation |
| Failure mode if mismanaged | Context bloat, cost, attention loss (§2) | Lost place on crash, un-resumable tasks | Stale or contradictory facts served forever |
| Primary technique | Windowing, summarization (§3–4) | Reducers, checkpointing (§5) | Embedding, retrieval, decay (§9) |

A system that stores long-term facts inside the conversation-history message list (a common early
mistake: prepending "the user prefers concise answers" as a fake `SystemMessage` re-inserted every
session) has smuggled long-term memory into conversation history's storage shape and inherited its
worst property — it now grows without a retrieval mechanism to bound it, because nobody built the
retrieval step for something that was never architected as retrievable. Conversely, a system that
tries to reconstruct agent state (whether a human already approved a pending action) by re-reading and
re-parsing the conversation transcript has smuggled agent state into conversation history's format and
inherited *its* worst property — a fact that should be a boolean field read in O(1) is now a string
search over prose that an LLM might paraphrase differently each time it writes it. Every section below
assumes this split as settled; where a technique blurs the line (semantic memory built from
conversation turns, §9), the blurring is a deliberate, named architectural choice, not an accident of
using one data structure for everything because it was the one already lying around.

### 1.5 Quick-reference map

Every remaining section of this chapter is a deep dive into one row of this table — worth returning to
once §3–§14 have added the mechanism behind each cell:

| Memory kind | Primary technique(s) | Section |
|---|---|---|
| Conversation history | Windowing, token truncation, summarization | §3 |
| Conversation history | Structural trimming (`trim_messages`, tool-pair preservation) | §4 |
| Agent state | Typed schema, reducers, checkpointing | §5 |
| Agent state | Explicit state machines for long-running workflows | §6 |
| Long-term memory | Profiles, fact extraction, vector/graph storage | §7 |
| All three | Backend selection (SQLite/Postgres/Redis/vector/graph) | §8 |
| Long-term memory | Retrieval scoring, hybrid search, supersession | §9 |
| All three | Tenant isolation, retention, right-to-erasure | §10 |
| Agent state + long-term memory | Scoped sharing across agents | §11 |
| All three, jointly | Token budget allocation across categories | §12 |

---

## 2. Why you can't just dump everything into the prompt

The naive default — every message ever exchanged, concatenated in order, sent on every call — is wrong
for three independent reasons, and it is worth holding them separately because they call for different
countermeasures and a system that fixes only one of them still fails on the other two.

### 2.1 Context window limits are a hard ceiling, not a soft one

Every model has a maximum context length, and going over it is not a degraded-quality failure, it is a
`400`-class API error — the request is rejected outright. A conversation that grows unboundedly *will*
eventually hit this wall, and if the only mitigation the system has is "truncate when we're about to
exceed the limit," that truncation happens under time pressure, with no chance to choose *what* to
drop intelligently — it is usually "delete the oldest N messages" applied reactively, at exactly the
moment the system can least afford to lose context (a long, deep conversation). §3–§4 exist so that
truncation is a designed, tested, proactive policy applied every turn, not a panic response to an
error.

### 2.2 Cost is linear in tokens, and history repeats

Because most LLM APIs are stateless per call, sending the full conversation history on every turn
means turn *N* re-transmits and re-bills every token from turns 1 through *N-1*, every single time. A
100-turn conversation with an average 200 tokens per turn is not a 20,000-token cost — it is closer to
20,000 × 50 (the average history length re-sent per call), because turn 100 re-sends turns 1–99 in
full. This is quadratic in the number of turns, not linear, and it is the single most common reason a
chat product's per-conversation cost curve bends upward far faster than a naive "tokens per turn × number
of turns" estimate predicts. Prompt caching (out of scope here, covered in the planned
`12-serving-latency-and-caching.md`) mitigates the *repeated-prefix* cost but does nothing about the
*attention* cost in §2.3 — a cached prefix is still processed by the attention mechanism at generation
time, it is only the KV-cache computation that is amortized, not the model's need to attend over it.

Worked example, because the shape of the curve matters more than the label "quadratic": a support-chat
conversation growing at 150 input tokens per turn, sent at $3/million input tokens, with no history
management at all.

```python
def naive_history_cost(num_turns: int, tokens_per_turn: int, price_per_million: float) -> float:
    total_tokens = sum(tokens_per_turn * t for t in range(1, num_turns + 1))   # turn t re-sends t turns
    return total_tokens / 1_000_000 * price_per_million

for n in (20, 50, 100, 200):
    print(n, naive_history_cost(n, 150, 3.0))
# 20   0.0945
# 50   0.5738
# 100  2.2725
# 200  9.045
```

Doubling the conversation length from 100 to 200 turns does not double the cost, it roughly
quadruples it — the shape any capacity-planning estimate has to account for, and the reason a
back-of-envelope "average tokens per turn times expected turns" estimate systematically
under-predicts cost for any product with a fat tail of long-running conversations. A token-bounded
history (§3.3) flattens this curve to linear in the number of turns by construction, because
cost-per-turn is capped at the budget regardless of how long the conversation has already run.

### 2.3 Attention degrades with irrelevant context: "lost in the middle"

This is the argument that survives even if tokens were free. Empirically (Liu et al., "Lost in the
Middle: How Language Models Use Long Contexts," and reproduced widely since), model accuracy on a
fact-retrieval task is highest when the relevant fact is at the very beginning or the very end of the
context, and measurably lower when it is buried in the middle — a U-shaped performance curve as a
function of position, not a flat one. This means two conversations with the *same relevant fact*
present can produce different quality answers purely as a function of how much irrelevant material
surrounds it and where the fact happens to land. Concretely: a user's stated preference from turn 3 of
a 60-turn conversation is *less likely to be honored* at turn 60 if all 60 turns are sent verbatim than
if a summarization step (§3.3) had promoted that preference into a durable, prominent position (e.g., a
standing system-message-adjacent block) rather than leaving it to be found by chance in the middle of
a wall of text. The practical consequence: **more context is not strictly better, past the point where
it dilutes the signal the model needs for the current turn** — a shorter, curated context frequently
outperforms a longer, complete one, which is the opposite of the naive intuition that "more information
can only help."

### 2.4 The composite argument against naive history

Put together: naive full-history-every-turn is (a) going to fail outright once the window is exceeded,
(b) getting more expensive per turn as a quadratic function of conversation length, and (c) actively
degrading the quality of the specific answer you're generating *right now*, well before either of the
first two failure modes bites. None of §3's countermeasures are premature optimization — by the time a
production chat conversation reaches even 20–30 turns with tool calls interleaved, all three failure
modes are already in effect, just not yet visibly enough to trigger an incident. The fix has to be
designed in before the failure is visible, because the failure that becomes visible last (an outright
context-length error) is the one that was quietly costing money and quality the whole time.

---

## 3. Conversation history management: buffers, windows, and summarization

Every strategy below is a different answer to "which subset (or compressed form) of the transcript do
we actually send this turn," and production systems typically combine two or three of them rather than
picking one.

### 3.1 Buffer memory: the (correct, narrow) baseline

Buffer memory is "keep everything, send everything" — the naive default from §2, formalized as a named
strategy rather than an accident:

```python
from dataclasses import dataclass, field

@dataclass
class BufferMemory:
    """Full, unmodified history. Correct only when the conversation is
    provably short-lived and bounded — a single-shot tool, a form-filling
    flow with a known maximum number of turns. Never the default for an
    open-ended chat product."""
    messages: list[dict] = field(default_factory=list)

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def to_prompt(self) -> list[dict]:
        return list(self.messages)
```

It is not a strawman — it is the *right* choice for a bounded number of known-short interactions (a
five-question onboarding wizard, a single customer-support ticket resolution with a hard turn cap). The
mistake is using it as the default for anything open-ended, where "how many turns will this
conversation have" has no known upper bound.

### 3.2 Sliding window: bound by turn count

The simplest bound: keep only the last *N* messages (or *N* turns, i.e., 2*N* messages if every turn is
a user/assistant pair).

```python
class WindowMemory:
    def __init__(self, window_size: int = 10):
        self.window_size = window_size          # in messages, not turns
        self.messages: list[dict] = []

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def to_prompt(self) -> list[dict]:
        return self.messages[-self.window_size:]
```

This is cheap, deterministic, and has one serious failure mode: it drops information based on *age*,
not *relevance*. A user's identity, stated goal, or a critical constraint from turn 1 is gone by turn
12 with a window of 10, even though it may be exactly what turn 50 needs. A pure window is a reasonable
choice only when recency genuinely correlates with relevance — support chat where each ticket is
mostly self-contained, not a long collaborative session building on early decisions.

### 3.3 Token-based truncation: bound by budget, not count

Message count is a poor proxy for the thing that actually matters — tokens — because message length
varies enormously (a one-word acknowledgment and a 2,000-token pasted stack trace are both "one
message"). Token-based truncation bounds the actual quantity that determines cost and window fit:

```python
import tiktoken

class TokenBoundedMemory:
    def __init__(self, max_tokens: int = 3000, model: str = "gpt-4o"):
        self.max_tokens = max_tokens
        self.encoding = tiktoken.encoding_for_model(model)
        self.messages: list[dict] = []

    def _count(self, msg: dict) -> int:
        return len(self.encoding.encode(msg["content"])) + 4   # role/formatting overhead

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def to_prompt(self) -> list[dict]:
        kept, total = [], 0
        for msg in reversed(self.messages):          # walk from most recent backward
            cost = self._count(msg)
            if total + cost > self.max_tokens:
                break
            kept.append(msg)
            total += cost
        return list(reversed(kept))
```

This is strictly better than count-based windowing for cost and window-fit guarantees, but it inherits
the same relevance-blindness: it drops the *oldest* tokens first regardless of whether they were the
important ones. Token-based truncation answers "how much" correctly; it says nothing about "which."

### 3.4 Summary memory: compress the old, keep the new verbatim

Summarization addresses the relevance-blindness both prior strategies share: instead of *dropping* old
turns, *compress* them into a shorter form that preserves the information likely to matter later, and
keep only the most recent turns verbatim (verbatim because recent context is disproportionately likely
to be referenced precisely — "what did you just say" needs exact wording, not a paraphrase).

```python
from dataclasses import dataclass, field

@dataclass
class SummaryBufferMemory:
    """Recent turns kept verbatim; everything older is folded into a
    running summary, updated incrementally so summarization cost stays
    O(1) per turn rather than O(n) over the whole history each time."""
    llm: "ChatModel"
    max_verbatim_messages: int = 8
    summary: str = ""
    recent: list[dict] = field(default_factory=list)

    SUMMARY_PROMPT = (
        "Update the running summary of this conversation with the new "
        "messages below. Preserve: stated user preferences, decisions made, "
        "facts established, and open questions. Drop: pleasantries, "
        "resolved tangents, and anything superseded by a later message.\n\n"
        "Current summary:\n{summary}\n\n"
        "New messages:\n{new_messages}\n\n"
        "Updated summary:"
    )

    def add(self, role: str, content: str) -> None:
        self.recent.append({"role": role, "content": content})
        if len(self.recent) > self.max_verbatim_messages:
            to_fold = self.recent[: -self.max_verbatim_messages]
            self.recent = self.recent[-self.max_verbatim_messages :]
            self._fold(to_fold)

    def _fold(self, messages: list[dict]) -> None:
        new_text = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        prompt = self.SUMMARY_PROMPT.format(summary=self.summary or "(none yet)",
                                             new_messages=new_text)
        self.summary = self.llm.invoke(prompt).content

    def to_prompt(self) -> list[dict]:
        prefix = [{"role": "system", "content": f"Conversation summary so far:\n{self.summary}"}] \
            if self.summary else []
        return prefix + self.recent
```

The incremental-fold design matters: naively re-summarizing the *entire* history from scratch every
time the window slides is O(n) work per turn and O(n²) over the conversation's life — the exact
quadratic-cost problem from §2.2, just moved into the summarization call instead of the main model
call. Folding only the messages that are about to fall out of the verbatim window, into a summary that
already captures everything before them, keeps the summarization cost constant per turn regardless of
total conversation length.

### 3.5 What summarization loses, and how to bound the damage

Summarization is lossy by construction, and being honest about *what* it loses is what separates a
production-grade implementation from a naive one:

1. **Specific numbers, IDs, and exact phrasing degrade first.** An LLM summarizing "the user's order
   number is 8842-B" as "the user asked about an order" has destroyed the one fact most likely to be
   needed verbatim later. Mitigation: extract structured facts (§7.2) *before* summarizing prose, and
   store them in agent state or long-term memory as typed fields, not as sentences subject to further
   paraphrase.
2. **Summarization is itself an LLM call, with its own failure modes** — it can hallucinate a detail
   that was never said, or silently drop a caveat ("I said I *might* be free Tuesday, not that I *am*
   free Tuesday") in exactly the way LLMs compress nuance. Never summarize safety-relevant or
   legally-relevant statements (consent, financial commitments) without a verification step, or better,
   never let those pass through summarization at all — extract and store them as structured facts the
   moment they occur.
3. **Repeated summarization of a summary compounds error**, the same telephone-game problem
   `22-agent-orchestration-patterns.md` §6.4 names for multi-agent handoffs. The incremental-fold design
   above still summarizes the *previous summary plus new messages* each time, which means information
   present only in a summary three folds ago has already survived two additional lossy compressions.
   For anything that must not degrade, promote it out of the summary and into a durable field the
   moment it is learned (this is exactly why §7's long-term memory exists as a separate, non-lossy-by-
   design store).

### 3.6 Buffer vs summary vs hybrid: the decision

| Strategy | Cost growth | Relevance-aware | Exact wording preserved | When to use |
|---|---|---|---|---|
| Full buffer | Unbounded (fails eventually) | N/A | Yes | Bounded, short interactions only |
| Sliding window | Bounded, cheap | No (recency proxy only) | Yes, within window | Ticket-style chats, weak cross-turn dependence |
| Token truncation | Bounded, cheap | No | Yes, within budget | Same as above, tighter cost control |
| Summary buffer (hybrid) | Bounded + small LLM cost per fold | Partial (summarizer's judgment) | Only for recent window | Long, evolving conversations with cross-turn dependence |
| Summary-only (no verbatim tail) | Bounded, lowest | Partial | No | Archival / handoff between sessions, not live chat |

In practice, production chat systems default to the hybrid (§3.4) with a token-bounded verbatim tail
(combine §3.3's exact budget accounting with §3.4's folding), and promote anything safety- or
fact-critical out of the summary path entirely into structured long-term memory (§7) the moment it is
recognized — summarization is for *conversational continuity*, not for *facts that must survive
exactly*.

---

## 4. Message trimming strategies

Trimming and summarization solve the same problem with different tradeoffs: trimming is free (no LLM
call) and lossy by deletion; summarization costs a call and is lossy by compression. LangGraph's
`trim_messages` utility is the standard, batteries-included implementation of the trimming half, and is
worth knowing at the parameter level because interviewers use it as a concrete probe of whether you've
actually shipped this.

### 4.1 `trim_messages`: by token count

```python
from langchain_core.messages import trim_messages, SystemMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o")

trimmed = trim_messages(
    messages,
    strategy="last",                 # keep the most recent messages
    token_counter=model,             # delegate token counting to the model's own tokenizer
    max_tokens=3000,
    start_on="human",                # after trimming, the sequence must start on a HumanMessage
    include_system=True,             # always keep the SystemMessage regardless of budget
)
```

The `start_on` parameter is the detail that separates a correct implementation from one that
silently produces malformed input: many chat model APIs require (or strongly expect) that a trimmed
history still alternates correctly and doesn't open with a dangling `AIMessage` or `ToolMessage` whose
preceding `HumanMessage`/tool-call was cut off. `trim_messages` walks the boundary and drops one more
message if needed to land on a valid starting point, rather than trimming to the token budget and
leaving a structurally broken sequence — a bug class ("trimmed history sent a `ToolMessage` with no
matching `tool_call_id` in context") that is otherwise easy to introduce with a naive slice.

### 4.2 By message count

```python
trimmed = trim_messages(
    messages,
    strategy="last",
    token_counter=len,                # count messages, not tokens
    max_tokens=10,                    # "10 messages" despite the parameter name
    start_on="human",
)
```

Message-count trimming is §3.2's sliding window expressed through the same utility — useful when
messages are roughly uniform in size (short back-and-forth chat) and token accounting is overkill; the
wrong choice the moment tool results or pasted documents can appear in the history, because a single
9,000-token tool result now counts the same as a two-word acknowledgment.

### 4.3 By role: always keep the system message, trim the rest

`include_system=True` is the built-in version of a rule that needs to be explicit in any hand-rolled
trimmer: the system message defines the agent's entire behavioral contract (tools available, persona,
safety constraints) and must never be a casualty of a token-budget squeeze the way an old user turn can
be. A hand-rolled trimmer that naively drops from the front of the list will eventually drop the system
message on a long enough conversation — a bug that manifests as the agent gradually "forgetting" its
own instructions with no explicit deletion event to point to in a trace.

```python
def trim_keep_system(messages: list, max_tokens: int, count_fn) -> list:
    system = [m for m in messages if m.type == "system"]
    rest = [m for m in messages if m.type != "system"]
    system_cost = sum(count_fn(m) for m in system)
    budget = max_tokens - system_cost
    kept, total = [], 0
    for m in reversed(rest):
        c = count_fn(m)
        if total + c > budget:
            break
        kept.append(m)
        total += c
    return system + list(reversed(kept))
```

### 4.4 Custom trimming logic: pairs, tool calls, and pinned messages

The built-in strategies handle the common case; production systems frequently need custom logic for
two situations `trim_messages`'s generic strategy doesn't know about:

**Tool call/result pairs must trim together.** An `AIMessage` carrying a `tool_calls` entry and the
`ToolMessage`(s) answering it are structurally one unit — trimming the `AIMessage` but leaving the
`ToolMessage` (or vice versa) produces a request the model API will reject or silently mishandle.

```python
def trim_preserving_tool_pairs(messages: list, max_tokens: int, count_fn) -> list:
    # Walk backward, but if a ToolMessage is included, its originating
    # AIMessage (matched by tool_call_id) must be included too, even if
    # that pushes past the naive per-message budget.
    kept, total, needed_call_ids = [], 0, set()
    for m in reversed(messages):
        c = count_fn(m)
        must_keep = getattr(m, "tool_call_id", None) in needed_call_ids
        if not must_keep and total + c > max_tokens:
            continue if kept else None  # allow skipping non-required messages once budget is hit
            break
        if getattr(m, "tool_calls", None):
            needed_call_ids -= {tc["id"] for tc in m.tool_calls}
        kept.append(m)
        total += c
    return list(reversed(kept))
```

**Pinned messages survive any trim.** A user's explicit constraint ("never suggest solution X," "my
account tier is enterprise") is sometimes worth marking as pinned — excluded from the trim candidate
pool entirely, at the cost of consuming budget permanently. This is a narrow escape hatch, not a general
pattern: pin sparingly, because every pinned message is budget every future turn cannot reclaim, which
is exactly the failure mode §7 solves properly (promote it to long-term memory and retrieve it when
relevant, rather than permanently reserving conversation-history budget for it).

### 4.5 The tradeoff, stated precisely

Too aggressive a trim: the model answers turn 40 without the constraint stated at turn 3, produces a
response the user experiences as "it forgot," and the failure is silent — nothing errors, the answer is
just subtly or badly wrong, and it is expensive to debug because the trace shows a perfectly valid,
well-formed (trimmed) request; nothing looks broken unless you know to ask "was the relevant turn even
in context." Too conservative a trim: cost grows needlessly and, per §2.3, the model's attention on the
*current* turn's actually-relevant material degrades because it is diluted by material that didn't need
to be there. There is no context-free right answer — the right budget is a property of the specific
application's turn-to-turn dependency structure, which is why §12 treats it as a budget-allocation
problem to be measured and tuned, not a constant to copy from a blog post.

---

## 5. Agent state in LangGraph: schemas, reducers, and checkpointing

This section assumes `21-langgraph-deep-dive.md` §2–§3 (state schema, node signature, reducers) and §5
(checkpointing) as background and does not re-derive the mechanics — it focuses on the memory-specific
design decisions layered on top of that mechanism.

### 5.1 State as the single source of truth for "where are we"

The central design commitment: **agent state, not the message list, is the authoritative record of
control flow.** A message list can tell you *that* a tool was called; it cannot cheaply tell you
"is this task still awaiting approval" without parsing prose. A typed state field can:

```python
from typing import TypedDict, Literal, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]      # conversation history — append/merge by add_messages
    task_status: Literal["planning", "executing", "awaiting_approval", "done", "failed"]
    current_step: int
    plan: list[str]
    tool_call_budget_remaining: int
    approved_by: str | None
```

Note the deliberate split within one `TypedDict`: `messages` is conversation history (§1.1, using
`add_messages` as its reducer — append new messages, replace-by-id on edits, exactly
`21-langgraph-deep-dive.md` §3.2's mechanism); every other field is agent state (§1.2). They coexist in
one schema because LangGraph's state object is the vehicle for both, but they are conceptually and
operationally distinct — a router reads `task_status`, never greps `messages` for the string "approved."

### 5.2 Reducers as the concurrency contract for state

`22-agent-orchestration-patterns.md` §6.2 names the hazard generically ("shared mutable state under
concurrent writers"); LangGraph's answer is specifically the reducer, and it is worth restating why
this matters for *memory* specifically, not just state in general: whenever two branches of a graph run
concurrently and both need to contribute to the same piece of memory — two research sub-agents each
finding facts to add to a shared `facts_learned: list[dict]` field — the default (last-writer-wins,
`21-langgraph-deep-dive.md` §3.3) silently drops one branch's contribution the first time they complete
in the same super-step. A reducer makes the merge explicit and correct:

```python
import operator
from typing import Annotated

class ResearchState(TypedDict):
    messages: Annotated[list, add_messages]
    facts_learned: Annotated[list[dict], operator.add]   # concurrent writers' facts all survive
```

This is the mechanism by which agent state safely *becomes* a feeder into long-term memory (§7): facts
accumulated correctly during a task's execution, via a reducer that guarantees no branch's contribution
is silently lost, are exactly the candidates for extraction into persistent storage once the task
completes.

### 5.3 Checkpointing is state persistence, not conversation persistence

The distinction worth being precise about in an interview: LangGraph's checkpointer persists the
*entire state object* after every super-step — which happens to include `messages` because that field
lives in the same `TypedDict`, but the checkpointer has no special-cased notion of "conversation
history" as a thing separate from any other field. This has a direct, practical consequence for memory
system design: **the checkpoint is not, by itself, a substitute for the trimming and summarization
discipline of §3–§4.** A `messages` field that never gets trimmed will checkpoint a monotonically
growing list forever, and every checkpoint write serializes and persists the whole thing — checkpoint
storage cost, and the deserialization cost of resuming a thread, both grow with an untrimmed history
exactly as badly as the token cost in §2.2 does. A production graph applies trimming as a node in the
graph itself (commonly the last step before returning to the user, or the first step of the next
model-calling node) so that what gets checkpointed is already the bounded, curated history, not the
raw unbounded one:

```python
def trim_history_node(state: AgentState) -> dict:
    trimmed = trim_messages(state["messages"], strategy="last",
                             token_counter=model, max_tokens=4000, start_on="human")
    return {"messages": trimmed}   # combined with add_messages's replace-by-id semantics for a
                                    # full-list replacement, use RemoveMessage (§5.4) instead
```

### 5.4 Removing messages correctly: `RemoveMessage`

Because `add_messages` is an append/merge reducer, returning a shorter `messages` list from a node does
*not* shrink the accumulated state the way it would with the default overwrite reducer — `add_messages`
merges by message ID, so a shorter returned list simply fails to add anything new, it does not delete
what's already there. Deleting requires the explicit sentinel:

```python
from langchain_core.messages import RemoveMessage

def prune_old_messages(state: AgentState) -> dict:
    cutoff = len(state["messages"]) - 20
    to_remove = state["messages"][:cutoff] if cutoff > 0 else []
    return {"messages": [RemoveMessage(id=m.id) for m in to_remove]}
```

This is the mechanically correct way to implement §3.2's sliding window *inside* a LangGraph state
graph — a common bug is attempting to shrink history by returning a truncated list and being surprised
when the checkpointed state keeps growing anyway, because the reducer's merge semantics were not
accounted for.

### 5.5 Why LangGraph's state model supersedes LangChain's memory abstractions

The full argument is §13's, but the state-specific version of it belongs here: LangChain's pre-LangGraph
`Memory` classes (`ConversationBufferMemory` and siblings) were designed around a single `Chain`'s
input/output dict, with no concept of typed, reducer-merged, multi-field state — they could hold a
message list and, at best, a flat dict of "extra variables," with no way to express "these two fields
must merge via this specific concurrent-safe rule" or "this field is versioned and this one isn't."
LangGraph's `TypedDict` + reducer model is a strictly more expressive superset that happens to also
subsume everything the old `Memory` classes did (a `messages` field with `add_messages` *is*
`ConversationBufferMemory`, expressed as one field of a richer schema) — which is why the migration is
not "replace memory class X with memory class Y" but "stop having a separate memory abstraction at all;
memory is just fields of your graph's state."

---

## 6. Workflow state machines

Long-running agent workflows — a multi-day approval process, a document pipeline with several
human-gated stages, an incident-response runbook — are naturally modeled as state machines, and doing
so explicitly (rather than letting control flow emerge implicitly from a tangle of conditionals reading
loosely-related state fields) is what makes such a workflow debuggable, resumable, and testable.

### 6.1 States, transitions, and guards

```
                    submit                approve
   ┌───────┐  ────────────────►  ┌──────────────────┐  ────────────►  ┌──────────┐
   │ DRAFT │                     │ SUBMITTED_FOR_    │                 │ APPROVED │
   └───────┘                     │ REVIEW            │  ─── reject ──► └──────────┘
                                  └──────────────────┘        │              │
                                                               ▼              │ begin_execution
                                                         ┌──────────┐         ▼
                                                         │ REJECTED │   ┌───────────┐
                                                         └──────────┘   │ EXECUTING │
                                                                        └───────────┘
                                                                          │        │
                                                                complete  │        │  fail
                                                                          ▼        ▼
                                                                  ┌───────────┐ ┌────────┐
                                                                  │ COMPLETED │ │ FAILED │
                                                                  └───────────┘ └────────┘
```

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable

class WorkflowState(str, Enum):
    DRAFT = "draft"
    SUBMITTED_FOR_REVIEW = "submitted_for_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class Transition:
    from_state: WorkflowState
    to_state: WorkflowState
    guard: Callable[[dict], bool]        # returns True if the transition may fire
    name: str

TRANSITIONS = [
    Transition(WorkflowState.DRAFT, WorkflowState.SUBMITTED_FOR_REVIEW,
               guard=lambda ctx: ctx.get("draft_complete", False), name="submit"),
    Transition(WorkflowState.SUBMITTED_FOR_REVIEW, WorkflowState.APPROVED,
               guard=lambda ctx: ctx.get("reviewer_decision") == "approve", name="approve"),
    Transition(WorkflowState.SUBMITTED_FOR_REVIEW, WorkflowState.REJECTED,
               guard=lambda ctx: ctx.get("reviewer_decision") == "reject", name="reject"),
    Transition(WorkflowState.APPROVED, WorkflowState.EXECUTING,
               guard=lambda ctx: True, name="begin_execution"),
    Transition(WorkflowState.EXECUTING, WorkflowState.COMPLETED,
               guard=lambda ctx: ctx.get("execution_result") == "success", name="complete"),
    Transition(WorkflowState.EXECUTING, WorkflowState.FAILED,
               guard=lambda ctx: ctx.get("execution_result") == "error", name="fail"),
]

def next_state(current: WorkflowState, ctx: dict) -> WorkflowState | None:
    for t in TRANSITIONS:
        if t.from_state == current and t.guard(ctx):
            return t.to_state
    return None    # no transition fires: stay put, awaiting more input
```

The guard-as-a-pure-function design is what makes this testable without an LLM in the loop at all —
every transition is a unit test: given this context dict, does the guard fire, and does it fire
*exclusively* (two guards on the same `from_state` both returning `True` for the same context is a
modeling bug, not a runtime one, and is worth an assertion that catches it at transition-table
construction time rather than at whichever unlucky runtime call exercises the ambiguity first).

### 6.2 Representing "where are we" in agent state

The workflow's current state is itself a piece of agent state (§1.2, §5.1) — a single field, checked by
routing logic, updated by whichever node executes a transition:

```python
class WorkflowGraphState(TypedDict):
    messages: Annotated[list, add_messages]
    workflow_state: WorkflowState
    context: dict          # the guard-evaluation inputs: draft_complete, reviewer_decision, etc.

def route_on_workflow_state(state: WorkflowGraphState) -> str:
    nxt = next_state(state["workflow_state"], state["context"])
    return nxt.value if nxt else "await_input"
```

This is a direct, explicit encoding of "where are we in the process" as a single readable field — the
property `22-agent-orchestration-patterns.md` §8.4 calls out as the difference between a recoverable,
diffable state and an undifferentiated notes blob. A support engineer debugging a stuck workflow reads
one field and one transition table, not a conversation transcript looking for the sentence that implies
approval happened.

### 6.3 Persisting workflow state for long-running processes

A workflow that can legitimately take days (waiting on a human reviewer, waiting on an external system)
must survive the originating process exiting entirely — this is exactly `21-langgraph-deep-dive.md`
§5's checkpointing, applied to a state machine's `workflow_state` field instead of a chat's message
list, and it is the same `thread_id`-keyed mechanism:

```python
config = {"configurable": {"thread_id": f"approval-workflow-{ticket_id}"}}
app.invoke({"workflow_state": WorkflowState.SUBMITTED_FOR_REVIEW, "context": {}}, config)
# ... three days pass, a different process, a reviewer clicks "approve" in a UI ...
app.update_state(config, {"context": {"reviewer_decision": "approve"}}, as_node="human_review")
app.invoke(None, config)     # resumes; the router re-evaluates transitions with the new context
```

### 6.4 Crash recovery via checkpointing

The property this buys, stated plainly: if the process handling the "three days later, reviewer
clicks approve" event crashes immediately after writing the state update but before the workflow
finishes executing its next transitions, a fresh process reading the same `thread_id` resumes from the
last durable checkpoint — the update is not lost, because `update_state` itself is a checkpointed write
(`21-langgraph-deep-dive.md` §5.5), not an in-memory mutation waiting to be flushed by a later step.
This is the concrete difference between "a state machine implemented as an in-memory Python object in a
long-lived process" (loses all in-flight workflows on every restart, exactly `22-agent-orchestration-patterns.md`
§8.3's crash-recovery argument) and one built on a checkpointed graph.

---

## 7. Long-term memory architectures

### 7.1 User profiles: the simplest durable structure

The least sophisticated and most robust long-term memory primitive is a structured profile — a typed
record of facts and preferences, keyed by user, read in full (not retrieved selectively) because it is
small by design:

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class UserProfile:
    user_id: str
    preferences: dict[str, str] = field(default_factory=dict)   # e.g. {"response_style": "concise"}
    facts: list[dict] = field(default_factory=list)             # [{"fact": "...", "source_turn": ..., "confidence": ..., "learned_at": ...}]
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def upsert_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value
        self.updated_at = datetime.utcnow()
```

A profile is the right tool when the total volume of durable facts per user is small (tens, not
thousands) — it fits in a prompt wholesale, needs no retrieval step, and is trivially auditable (a
support engineer, or the user themselves under a data-access request, can read the entire thing). It
stops being the right tool the moment volume grows past "fits comfortably in context," at which point
§9's retrieval-based approach takes over — profiles and vector-retrieved memory are not competitors,
they are the small-scale and large-scale answers to the same problem, often used together (profile for
the handful of high-value, always-relevant facts; retrieval for the long tail).

### 7.2 Fact extraction: turning conversation into memory, deliberately

The critical discipline: long-term memory should be built from **extracted, structured facts**, never
from raw conversation turns stored verbatim and called "memory." Extraction is itself an LLM call, run
asynchronously (never on the critical path of the user-facing response), with a schema that forces
the model to commit to a small set of discrete claims rather than free text:

```python
from pydantic import BaseModel

class ExtractedFact(BaseModel):
    fact: str
    category: Literal["preference", "biographical", "constraint", "goal"]
    confidence: float          # the extractor's own calibration; low-confidence facts get a review queue
    source_message_id: str

EXTRACTION_PROMPT = """Extract durable facts about the user from this conversation turn that
would be useful to remember in FUTURE, unrelated conversations. Do not extract anything that
is only relevant to the current task. Return an empty list if nothing durable was said.

Turn: {turn_text}"""

def extract_facts(llm, turn_text: str, message_id: str) -> list[ExtractedFact]:
    structured_llm = llm.with_structured_output(list[ExtractedFact])
    return structured_llm.invoke(EXTRACTION_PROMPT.format(turn_text=turn_text))
```

The "would be useful in future, unrelated conversations" framing in the prompt is doing real work — it
is the extraction-time filter that keeps long-term memory from accumulating task-specific noise (agent
state, §1.2, masquerading as long-term memory because nobody drew the line at extraction time). Facts
below a confidence threshold go to a review queue rather than being written directly — extraction
errors compound silently across every future session that retrieves a wrong fact, unlike a
conversation-history error, which is scoped to the one conversation it occurred in.

### 7.3 Vector-store-based memory: embed, store, retrieve

For facts and past interactions too numerous to fit in a profile, embed them and retrieve the
semantically relevant subset per query — mechanically identical to `03-indexing-and-vector-stores.md`
and `04-retrieval-hybrid-and-reranking.md`'s document retrieval, pointed at a memory corpus instead of a
document corpus:

```python
class VectorMemoryStore:
    def __init__(self, embedder, vector_store):
        self.embedder = embedder
        self.store = vector_store    # any of 03's backends: pgvector, a dedicated ANN index, etc.

    def remember(self, user_id: str, fact: ExtractedFact) -> None:
        vector = self.embedder.embed(fact.fact)
        self.store.upsert(
            id=f"{user_id}:{fact.source_message_id}",
            vector=vector,
            metadata={"user_id": user_id, "fact": fact.fact, "category": fact.category,
                      "confidence": fact.confidence, "learned_at": time.time()},
        )

    def recall(self, user_id: str, query: str, k: int = 5) -> list[dict]:
        query_vector = self.embedder.embed(query)
        results = self.store.search(query_vector, k=k, filter={"user_id": user_id})
        return [r.metadata for r in results]
```

The `filter={"user_id": user_id}` is not optional — it is §10's tenant-isolation requirement applied at
the query level, and its absence is the single most common way a memory system leaks one user's facts
into another user's context (§10.1, §15).

### 7.4 Knowledge graphs for structured, relational memory

Vector retrieval answers "what facts are semantically similar to this query"; it does not natively
answer relational questions ("who does this user report to," "which of the user's projects depend on
which service") — questions that are about *structure between entities*, not similarity of text. A
lightweight knowledge graph fills that gap:

```python
class MemoryGraph:
    def __init__(self):
        self.triples: list[tuple[str, str, str]] = []   # (subject, predicate, object)

    def add(self, subject: str, predicate: str, obj: str) -> None:
        self.triples.append((subject, predicate, obj))

    def query(self, subject: str | None = None, predicate: str | None = None) -> list[tuple]:
        return [t for t in self.triples
                if (subject is None or t[0] == subject)
                and (predicate is None or t[1] == predicate)]

# extracted from conversation: "I work on the payments team, which depends on the fraud service"
graph.add("user:42", "works_on", "team:payments")
graph.add("team:payments", "depends_on", "service:fraud")
```

In production this is typically a graph database (Neo4j, or a relational schema with an edge table) —
`../databases/12-replication-and-distributed-storage.md` and the indexing chapters cover the storage
engine considerations. The architectural point that survives regardless of backend: a knowledge graph
and a vector store are answering different questions (relational traversal vs semantic similarity), and
a sophisticated long-term memory system typically runs both, choosing which to query based on the
question's shape (a "who/what depends on what" question routes to the graph; a "what do we know that's
*like* this" question routes to the vector store) — the retrieval-router pattern from
`05-query-understanding.md` (planned) applied to memory instead of documents.

### 7.5 Session-spanning memory: the retrieval-at-session-start pattern

The concrete mechanism that makes long-term memory *feel* like continuity to a user: at the start of a
new session, before the first turn is even answered, retrieve the facts most relevant to context
available at that point (the user's opening message, if there is one; otherwise their most recently
active projects/topics) and inject a compact digest — not the full fact list — into the system context:

```python
def build_session_context(user_id: str, opening_message: str, memory: VectorMemoryStore,
                           profile_store: dict[str, UserProfile]) -> str:
    profile = profile_store.get(user_id)
    relevant_facts = memory.recall(user_id, query=opening_message, k=5) if opening_message else []
    parts = []
    if profile and profile.preferences:
        parts.append("User preferences: " + "; ".join(f"{k}={v}" for k, v in profile.preferences.items()))
    if relevant_facts:
        parts.append("Relevant known facts: " + "; ".join(f["fact"] for f in relevant_facts))
    return "\n".join(parts)
```

### 7.6 Facts vs context: the distinction that keeps memory from becoming noise

The last discipline worth naming explicitly: **remembering a fact is not the same as remembering
context**, and long-term memory should store the former, never the latter. "The user prefers TypeScript
over JavaScript" is a fact — stable, reusable, true independent of any specific conversation. "The user
was debugging a race condition in their checkout flow on Tuesday" is context — true of a specific
moment, valuable within the conversation it occurred in, and actively *wrong* to resurface unprompted
in an unrelated session three weeks later ("last time we spoke you were debugging a race condition" in
a conversation now about something else reads as either impressively creepy or plainly irrelevant,
depending on the user, and is never a net positive). The extraction prompt in §7.2 is explicitly
filtering for facts, not context, for exactly this reason — context belongs in conversation history
(§1.1, naturally scoped to the session it occurred in) and should never be promoted into a store whose
entire purpose is cross-session persistence.

### 7.7 Wiring long-term memory into a LangGraph node, end to end

The pieces from §7.1–§7.6 compose into two ordinary graph nodes — a read node that runs before the
model call and a write node that runs after the turn completes — with nothing more exotic than
`21-langgraph-deep-dive.md` §2's node signature:

```python
class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    user_id: str
    retrieved_memory: str          # populated by recall_memory, consumed by the model-calling node

def recall_memory(state: ChatState, memory: VectorMemoryStore, profiles: dict) -> dict:
    last_user_msg = next(m.content for m in reversed(state["messages"]) if m.type == "human")
    context = build_session_context(state["user_id"], last_user_msg, memory, profiles)
    return {"retrieved_memory": context}

def call_model_with_memory(state: ChatState, model) -> dict:
    system = SystemMessage(content=f"{BASE_SYSTEM_PROMPT}\n\n{state['retrieved_memory']}")
    response = model.invoke([system] + state["messages"])
    return {"messages": [response]}

def extract_and_store_memory(state: ChatState, llm, memory: VectorMemoryStore) -> dict:
    last_turn = state["messages"][-2:]                  # the human/AI pair just completed
    turn_text = "\n".join(f"{m.type}: {m.content}" for m in last_turn)
    facts = extract_facts(llm, turn_text, message_id=last_turn[-1].id)
    for fact in facts:
        if fact.confidence >= 0.7:
            memory.remember(state["user_id"], fact)
    return {}                                             # no state field to update; side effect only

graph = StateGraph(ChatState)
graph.add_node("recall_memory", recall_memory)
graph.add_node("call_model", call_model_with_memory)
graph.add_node("extract_memory", extract_and_store_memory)
graph.add_edge(START, "recall_memory")
graph.add_edge("recall_memory", "call_model")
graph.add_edge("call_model", "extract_memory")
graph.add_edge("extract_memory", END)
```

Two design choices here are worth being deliberate about in an interview, because both are easy to get
backwards. First, `extract_and_store_memory` runs *after* the response is already generated and
returned to the graph's output — memory writes are never on the critical path of the user-facing
latency, exactly §7.2's "run asynchronously" requirement, implemented here as "runs after, not blocking
before" (a fully async deployment would fire this node's work onto a background queue rather than
awaiting it inline, which the graph edge alone doesn't guarantee — the synchronous version above is
correct for clarity, not for a latency-sensitive production deployment). Second, `recall_memory` reads
based on the *user's incoming message*, not the full conversation so far — retrieval should be
query-driven (§9.1), and the query is what the user just asked, not an ambient summary of everything
said previously, which is a different retrieval problem with a different (weaker) relevance signal.

---

## 8. Memory storage backends: the decision matrix

Each of the three memory kinds from §1 has different access patterns, and the storage backend decision
should follow the access pattern, not familiarity or default tooling choice.

### 8.1 In-memory (dict, `MemorySaver`)

Zero durability — gone on process restart. Correct only for development, tests, and notebooks. The
anti-pattern (§15) is shipping this to production because "it worked in every test," which it will,
since tests don't restart the process mid-conversation.

### 8.2 SQLite

Single-node, file-backed, durable across restarts, zero operational overhead (no server process). The
right choice for a single-process service, a local developer tool, or a low-traffic internal tool where
horizontal scaling and concurrent-writer throughput are non-issues. `SqliteSaver`
(`21-langgraph-deep-dive.md` §5.1) is the LangGraph-native version; the same tradeoffs apply to any
hand-rolled memory store built on it. Do not reach for it once more than one process instance needs to
read/write the same store — SQLite's file-locking model is not built for that.

### 8.3 PostgreSQL

The default production answer for both agent state (checkpoints) and structured long-term memory
(profiles, extracted facts as rows), and increasingly for vector memory too via `pgvector`
(`03-indexing-and-vector-stores.md` §7 covers pgvector's tradeoffs against dedicated vector databases in
depth — the summary that matters here: pgvector is the right default when you already run Postgres and
memory volume per tenant is moderate; a dedicated vector store earns its keep past a scale or
recall/latency requirement pgvector's HNSW implementation stops comfortably meeting). Postgres gives
you: durability across restarts, horizontal read scaling, SQL access for analytics and debugging,
transactional consistency between a memory write and any other write in the same transaction (write a
fact and update a `last_interaction_at` timestamp atomically), and row-level security as a genuine
tenant-isolation mechanism (§10.2).

```sql
CREATE TABLE user_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    fact TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence FLOAT NOT NULL,
    embedding VECTOR(1536),               -- pgvector column, if co-locating semantic memory
    learned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by UUID REFERENCES user_facts(id)   -- versioning: never hard-delete a fact, supersede it
);
CREATE INDEX ON user_facts USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON user_facts (user_id, learned_at DESC);
```

### 8.4 Redis

Sub-millisecond access, TTL support natively, the right choice for session-scoped agent state that must
be read on the hot path of every request and does not need SQL-queryability or long-term durability
guarantees beyond "survives a restart of Redis itself with AOF/RDB persistence enabled." A common
production split: agent state (§1.2, task-scoped, short-lived) in Redis for latency; conversation
history and long-term memory in Postgres for durability and queryability; Redis as a read-through cache
in front of Postgres for the hot path of "load this session's recent messages," invalidated on write.

```python
import redis, json

r = redis.Redis(host="localhost", decode_responses=True)

def save_agent_state(task_id: str, state: dict, ttl_seconds: int = 86400) -> None:
    r.setex(f"agent_state:{task_id}", ttl_seconds, json.dumps(state))

def load_agent_state(task_id: str) -> dict | None:
    raw = r.get(f"agent_state:{task_id}")
    return json.loads(raw) if raw else None
```

The TTL is doing real work here: task-scoped agent state that is never explicitly cleaned up (a task
that errors out and is never retried, a task whose owning process crashed before marking it complete)
should still eventually be reclaimed, and Redis's native expiry is a simpler correctness mechanism for
that than a cron job scanning a Postgres table for staleness — though for anything requiring an audit
trail of *what* expired and *when*, Postgres with an explicit retention job (§14.1) is still the right
call, because an expired Redis key leaves no trace it ever existed.

### 8.5 Vector stores

Covered in full in `03-indexing-and-vector-stores.md`; the memory-specific framing is §9. The decision
between pgvector, a managed vector database (Pinecone, Weaviate, Qdrant), and a self-hosted dedicated
index (Milvus) for *memory* specifically follows the same tradeoffs as for document retrieval, with one
memory-specific wrinkle: per-tenant filtering (§10) is on the hot path of *every* memory query (you
never search across all users' memories at once), so a vector store's filtered-search performance under
your expected per-tenant cardinality matters more here than it typically does for a single shared
document corpus.

### 8.6 The decision matrix

| Backend | Durability | Latency | Concurrent writers | Query richness | Best for |
|---|---|---|---|---|---|
| In-memory dict | None | Lowest | Single process | Whatever Python allows | Dev, tests only |
| SQLite | Process-restart-safe | Low | Single writer at a time | Full SQL | Single-node services |
| PostgreSQL | Full, replicated | Low–moderate | High (MVCC) | Full SQL + pgvector | Default production choice |
| Redis | Configurable (AOF/RDB) | Lowest | High | Key/value, limited | Hot-path session/task state |
| Dedicated vector store | Full | Low at scale | High | ANN search, metadata filter | Large-scale semantic memory |
| Knowledge graph (Neo4j etc.) | Full | Moderate | Moderate | Graph traversal | Relational memory queries |

---

## 9. Semantic memory and retrieval: memory as RAG

### 9.1 The core reframing

Treat long-term memory as a document store and memory retrieval as RAG pointed at a different
corpus — this is not an analogy, it is a direct architectural reuse: the same embedding model, the same
ANN index, the same hybrid (BM25 + dense) retrieval from `04-retrieval-hybrid-and-reranking.md`, and the
same reranking step, applied to "memories" (extracted facts, past conversation summaries, past task
outcomes) instead of "documents." A team that builds a bespoke memory-retrieval pipeline without reusing
this machinery is, per `22-agent-orchestration-patterns.md` §8.2, re-deriving RAG badly.

```python
class MemoryRetriever:
    def __init__(self, embedder, index, reranker=None):
        self.embedder = embedder
        self.index = index
        self.reranker = reranker

    def retrieve(self, user_id: str, query: str, k: int = 20, top_n: int = 5) -> list[dict]:
        query_vec = self.embedder.embed(query)
        candidates = self.index.search(query_vec, k=k, filter={"user_id": user_id})
        if self.reranker:
            candidates = self.reranker.rerank(query, candidates, top_n=top_n)
        else:
            candidates = candidates[:top_n]
        return candidates
```

### 9.1.1 Hybrid retrieval for memory: why lexical matching still matters

`04-retrieval-hybrid-and-reranking.md` §2's argument for combining BM25 with dense retrieval transfers
to memory with a specific, common trigger: a user's stated fact frequently contains an exact identifier
— a proper noun, a product name, a ticket number, an internal system name — that dense embedding
similarity is known to under-weight relative to a sparse lexical match, because embedding models
optimize for semantic similarity, not exact-token recall. "The user works on Project Chimera" and a
later query "what does the user work on" embed close together; a query that happens to literally say
"Chimera" benefits disproportionately from a lexical match the dense index alone can miss if the
embedding space has drifted the term's representation toward something more generic:

```python
class HybridMemoryRetriever:
    def __init__(self, embedder, vector_index, bm25_index, reranker=None):
        self.embedder, self.vector_index, self.bm25_index, self.reranker = (
            embedder, vector_index, bm25_index, reranker)

    def retrieve(self, user_id: str, query: str, k: int = 20, top_n: int = 5) -> list[dict]:
        dense_hits = self.vector_index.search(self.embedder.embed(query), k=k, filter={"user_id": user_id})
        sparse_hits = self.bm25_index.search(query, k=k, filter={"user_id": user_id})
        fused = reciprocal_rank_fusion([dense_hits, sparse_hits])   # `04`'s RRF fusion, reused verbatim
        candidates = fused[:k]
        if self.reranker:
            candidates = self.reranker.rerank(query, candidates, top_n=top_n)
        return candidates[:top_n]
```

The reranking step is worth keeping even for a small per-user memory corpus (dozens to low hundreds of
facts) because a cross-encoder reranker's cost scales with the candidate set size, not the full corpus
size — cheap here specifically because §7.3's tenant-scoped filter has already cut the search space down
to one user's memories before reranking ever runs, unlike document-corpus reranking where the candidate
set from a shared index can be much larger before filtering.

### 9.2 Scoring: relevance is necessary but not sufficient

Pure semantic similarity is an incomplete relevance signal for memory in a way it usually isn't for
static document retrieval, because memories have a property most document corpora don't: they can be
*stale* or *superseded* in a way that similarity scoring is blind to. A fact learned eight months ago
("the user is using React 16") and one learned yesterday ("the user just migrated to React 19") can be
equally semantically similar to the query "what frontend framework does the user use," and a naive
top-K-by-cosine-similarity retrieval can surface the stale one, or both, with no signal to the model
that one supersedes the other. A composite score that blends similarity, recency, and confidence
corrects for this:

```python
import math

def composite_score(similarity: float, learned_at: float, confidence: float,
                     now: float, half_life_days: float = 90) -> float:
    age_days = (now - learned_at) / 86400
    recency_decay = 0.5 ** (age_days / half_life_days)     # exponential decay, half-life tunable
    return similarity * 0.6 + recency_decay * 0.25 + confidence * 0.15

def rank_memories(candidates: list[dict], now: float) -> list[dict]:
    scored = [(c, composite_score(c["similarity"], c["learned_at"], c["confidence"], now))
              for c in candidates]
    return [c for c, _ in sorted(scored, key=lambda x: x[1], reverse=True)]
```

The half-life should differ by fact category: a stated preference ("prefers dark mode") barely decays;
a technical-stack fact decays over months as tools change; a stated short-term goal ("finishing a
project by Friday") should decay to irrelevance within weeks. A single global half-life across all
categories is the common shortcut, and the common resulting bug is a stale, category-inappropriate fact
surfacing at high rank because the global half-life was tuned for a different category.

### 9.3 Superseding, not just decaying

Better than relying on decay alone: when a new fact contradicts an old one on extraction (§7.2), mark
the old one explicitly superseded (the `superseded_by` column in §8.3's schema) rather than trusting a
decay curve to demote it in time. Detecting contradiction is itself a retrieval-then-compare step at
write time — before storing a new fact, retrieve the top-1 most similar existing fact for that user and
ask an LLM (or apply a cheap category-specific rule: two facts in the "technical_stack" category about
the same subcategory, e.g. "frontend framework," are candidates for supersession) whether the new one
supersedes it.

### 9.4 Retrieval at write time vs read time

Both matter, and they answer different questions: write-time retrieval (§9.3) prevents storing
contradictions; read-time retrieval (§9.1) selects which of the (already-deduplicated,
already-superseded-marked) stored memories are relevant to *this* query. A system that only does one is
incomplete — write-time-only lets stale-but-not-yet-superseded facts still get retrieved by an imperfect
recency signal; read-time-only lets the store accumulate an ever-growing pile of contradictions that
retrieval has to sort out fresh, and expensively, on every single query instead of once at write time.

---

## 10. Memory in multi-tenant systems

### 10.1 Isolation is the load-bearing requirement, not an afterthought

Every memory query in a multi-tenant system must be scoped to the correct tenant boundary, and the
boundary itself needs to be chosen deliberately: per-user (each individual's own preferences and
facts), per-session (isolated even from the same user's other sessions — appropriate for, e.g., a
shared kiosk or a "private/incognito" mode), or per-organization (shared across everyone in a company
account — appropriate for institutional knowledge, wrong for personal preferences). Conflating these —
storing per-user facts in a per-organization-scoped store, or vice versa — is either a privacy leak
(user A's personal fact visible to user B in the same org) or a missed-context bug (a fact that should
be shared org-wide is siloed to whoever happened to state it).

```python
class ScopedMemoryKey:
    """Makes the isolation boundary an explicit, constructed value rather
    than an implicit convention every call site has to remember."""
    @staticmethod
    def per_user(org_id: str, user_id: str) -> str:
        return f"org:{org_id}:user:{user_id}"

    @staticmethod
    def per_session(org_id: str, user_id: str, session_id: str) -> str:
        return f"org:{org_id}:user:{user_id}:session:{session_id}"

    @staticmethod
    def per_org(org_id: str) -> str:
        return f"org:{org_id}:shared"
```

### 10.2 Enforcing isolation at the storage layer, not just the application layer

The `filter={"user_id": user_id}` pattern from §7.3 is correct but fragile if it is the *only*
enforcement point — every single call site across the codebase has to remember to apply it, and a
single missed filter (a new endpoint added by an engineer unfamiliar with the convention) is a
cross-tenant data leak. Postgres row-level security makes the isolation a database-enforced invariant
instead of an application-remembered one:

```sql
ALTER TABLE user_facts ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON user_facts
    USING (user_id = current_setting('app.current_user_id')::uuid);
```

```python
def with_tenant_context(conn, user_id: str):
    conn.execute("SET app.current_user_id = %s", (user_id,))
    # every subsequent query on this connection is transparently filtered by RLS,
    # even a query someone forgot to hand-write a WHERE user_id = ... clause for
```

For vector stores without native row-level security, the equivalent discipline is a mandatory,
non-optional filter parameter in the retrieval function's signature (no default that searches
unfiltered) plus an integration test that asserts cross-tenant queries return zero results — treat it
as a security-critical code path with security-critical test coverage, not an ordinary feature.

### 10.3 Data retention policies

Long-term memory that grows forever, per user, forever, is both a cost problem and — increasingly — a
compliance problem. A retention policy should specify, per memory category, how long a fact is kept
absent further reinforcement: a stated long-term preference might be retained indefinitely (until
explicitly changed); a project-specific fact might expire when the project context has clearly moved on
(no retrieval hits in N months, a reasonable proxy for "no longer relevant"); anything extracted at low
confidence and never subsequently corroborated should expire fastest.

```python
def apply_retention_policy(store, now: float):
    RETENTION_DAYS = {"preference": None, "biographical": 365, "goal": 30, "constraint": 180}
    for category, days in RETENTION_DAYS.items():
        if days is None:
            continue
        store.delete_where(category=category, learned_at_before=now - days * 86400,
                            not_reinforced_since=now - days * 86400)
```

### 10.4 GDPR and the right to be forgotten

A user's right to erasure means the system must be able to answer, concretely, "delete everything we
have stored about this user" — and this is a much harder query against a memory system than against a
typical relational application, precisely because memory is deliberately scattered across multiple
backends (§8) for good reasons: a Postgres `user_facts` table, a vector index's embeddings, a Redis
session cache, and possibly a knowledge graph's nodes and edges. A deletion implementation that only
covers the primary relational store while leaving embeddings live in a vector index is a compliance gap
that looks complete in a code review of the obvious table but isn't:

```python
def forget_user(user_id: str, postgres_conn, vector_store, redis_client, graph_store) -> dict:
    """Right-to-erasure: must touch every backend memory is written to, not
    just the one that's easiest to query. Returns a per-backend count for audit."""
    results = {}
    results["facts"] = postgres_conn.execute(
        "DELETE FROM user_facts WHERE user_id = %s", (user_id,)).rowcount
    results["vectors"] = vector_store.delete(filter={"user_id": user_id})
    results["redis_keys"] = len(redis_client.keys(f"*:user:{user_id}:*"))
    for key in redis_client.keys(f"*:user:{user_id}:*"):
        redis_client.delete(key)
    results["graph_nodes"] = graph_store.delete_nodes(entity=f"user:{user_id}")
    return results
```

The design lesson generalizes past GDPR specifically: **any time memory is written to more than one
backend, deletion (and export, for a data-access request) needs a single, tested, cross-backend
implementation that is exercised in CI** — not a runbook step someone remembers to do manually across
four consoles when a request eventually comes in, which is exactly the kind of process that is correct
in the design doc and silently incomplete the first time it is actually executed under time pressure.

---

## 11. Memory in multi-agent systems

### 11.1 The cost problem specific to multi-agent memory

`22-agent-orchestration-patterns.md` §6 covers the coordination mechanics (message passing, shared
state, the blackboard pattern) in full; the memory-specific problem layered on top is cost: naively
giving every agent in a multi-agent system the *full* conversation history and the *full* output of
every other agent multiplies token cost by the number of agents, on every turn, and the growth is
combinatorial in a peer-to-peer topology (`22-agent-orchestration-patterns.md` §5.3's warning about
peer-to-peer's coordination cost is this same fact restated for token budgets specifically). The
question "how do you share context between agents without exploding token costs" (a genuine, common
interview question, §16) has a real answer built from three pieces:

### 11.2 Scoped memory: agent-private vs shared

Not every agent needs every piece of context. A supervisor deciding which specialist to route to needs
a short digest of the task, not each specialist's full internal tool-call history; a specialist executing
a subtask needs the pieces of the shared blackboard (`22-agent-orchestration-patterns.md` §6.3) relevant
to its slice of work, not every other specialist's full working state.

```python
class ScopedAgentMemory:
    def __init__(self):
        self._shared: dict = {}                    # blackboard: visible to all agents
        self._private: dict[str, dict] = {}         # per-agent working memory: visible only to owner

    def write_shared(self, key: str, value) -> None:
        self._shared[key] = value

    def write_private(self, agent_id: str, key: str, value) -> None:
        self._private.setdefault(agent_id, {})[key] = value

    def context_for(self, agent_id: str) -> dict:
        return {**self._shared, **self._private.get(agent_id, {})}
```

This is the mechanical implementation of the discipline `22-agent-orchestration-patterns.md` §6.4
argues for: pass structured artifacts (specific shared-state keys) rather than each agent's full prose
transcript, which bounds what any given agent's context call actually contains to what it was scoped to
need, not everything every other agent has ever produced.

### 11.3 Summarized handoffs, with the same caveat as §3.5

When a full artifact genuinely is too large to hand to the next agent (a research agent's raw scraped
content, before the synthesis agent needs it), summarize — but apply §3.5's discipline: extract anything
structured (facts, numbers, citations) before summarizing prose, and never summarize the parts that are
likely to be checked or cited later, because a summarization-induced error at an agent handoff is
strictly worse than one in a user-facing chat: there is no human in the loop to notice the paraphrase
was subtly wrong before it propagates into a downstream agent's decision.

### 11.4 Shared long-term memory across agents

For agents in the same system that should benefit from the same long-term memory store (a customer
support multi-agent system where a routing agent, a resolution agent, and a follow-up agent should all
see the same user facts), share the retrieval layer (§9), not the raw memory objects — every agent
calls the same `MemoryRetriever.retrieve(user_id, query)` with its *own* query, scoped to what it
specifically needs, rather than one agent fetching everything up front and forwarding a "memory dump" to
every other agent regardless of relevance. This keeps the token cost of memory retrieval proportional to
the number of agents that actually need memory for their specific subtask, not multiplied by the number
of agents in the system regardless of need.

### 11.5 A supervisor allocating a per-worker memory budget

Putting §11.2–§11.4 together, a supervisor coordinating specialists can treat each worker's context as
its own budget (§12) to enforce, rather than trusting each worker to self-limit what it pulls from
shared memory:

```python
class SupervisorMemoryBudget:
    def __init__(self, retriever: "HybridMemoryRetriever", per_worker_tokens: int = 1500):
        self.retriever = retriever
        self.per_worker_tokens = per_worker_tokens

    def context_for_worker(self, user_id: str, worker_task: str, count_fn) -> str:
        candidates = self.retriever.retrieve(user_id, query=worker_task, k=15, top_n=8)
        kept, total = [], 0
        for c in candidates:                      # already ranked; take highest-ranked until budget fills
            cost = count_fn(c["fact"])
            if total + cost > self.per_worker_tokens:
                break
            kept.append(c["fact"])
            total += cost
        return "\n".join(kept)
```

The budget is deliberately per-worker, not a single shared pool split evenly across however many
workers a task happens to spawn — a supervisor that fans out to eight workers for a large task
shouldn't starve each one's memory context to an eighth of a fixed total; each worker's *task* is what
determines how much memory context it plausibly needs, and `worker_task` (not a shared, generic query)
is what should drive retrieval relevance for that worker specifically. This is the mechanical answer,
at the multi-agent layer, to the same question §12 answers at the single-agent layer: allocation should
follow what the current unit of work needs, not an arbitrary even split.

---

## 12. The context window budget

### 12.1 The five categories competing for the same tokens

Every production LLM call has, effectively, a fixed token budget (the context window, or a smaller
budget chosen deliberately below the window's ceiling for cost and attention reasons per §2.3), and five
categories compete for it: the system prompt (instructions, persona, tool schemas — usually fixed cost,
paid every call), conversation history (§3–§4, the largest variable-size consumer in a long chat),
retrieved context from RAG (§9, and the main `01`–`04` document-retrieval pipeline, if the application
does both document RAG and memory retrieval), tool results (can be arbitrarily large — a database query
result, a file's contents — and is the category most likely to blow a budget unpredictably if
unbounded), and working memory (agent state fields rendered into the prompt for the model to reason
over — a plan, a scratchpad).

### 12.2 A budget framework

```python
from dataclasses import dataclass

@dataclass
class ContextBudget:
    total: int = 128_000
    system_prompt_reserved: int = 2_000
    output_reserved: int = 4_000              # reserve room for the model's own response
    safety_margin: int = 1_000

    @property
    def available_for_content(self) -> int:
        return self.total - self.system_prompt_reserved - self.output_reserved - self.safety_margin

    def allocate(self, priorities: dict[str, float]) -> dict[str, int]:
        """priorities: category -> weight, e.g. {'history': 0.3, 'rag': 0.4,
        'tool_results': 0.2, 'working_memory': 0.1}. Weights should sum to 1.0."""
        pool = self.available_for_content
        return {category: int(pool * weight) for category, weight in priorities.items()}
```

Static weights are a reasonable starting point; the more effective version makes allocation dynamic per
turn, because the right split genuinely differs by what the current turn needs:

```python
def dynamic_allocation(budget: ContextBudget, turn_type: str) -> dict[str, int]:
    PROFILES = {
        "factual_lookup":  {"history": 0.15, "rag": 0.65, "tool_results": 0.10, "working_memory": 0.10},
        "long_conversation_followup": {"history": 0.55, "rag": 0.15, "tool_results": 0.15, "working_memory": 0.15},
        "tool_heavy_task": {"history": 0.20, "rag": 0.10, "tool_results": 0.55, "working_memory": 0.15},
    }
    return budget.allocate(PROFILES.get(turn_type, PROFILES["long_conversation_followup"]))
```

Classifying `turn_type` is itself a cheap, small classification step (a lightweight model call or even
a heuristic on the presence of a retrieval trigger, a pending tool call, or turn count) run before the
main call — its cost is trivial relative to the savings from not wasting budget on a category the
current turn doesn't need. A factual-lookup turn that allocates 55% of budget to conversation history it
barely needs is paying §2.3's attention-dilution cost for no benefit; a long-conversation-followup turn
that starves history to make room for RAG context nobody asked for produces the "it forgot what I just
said" failure from a different cause than under-trimming — over-allocating to the wrong category.

### 12.3 Truncating within a category when it still overflows

Even with an allocation, a single category can still exceed its slice (a tool result larger than its
budget) — apply category-appropriate truncation, not a blind cut: for tool results, truncate the
*content* while preserving structure (the first and last N rows of a large table, not a hard byte cutoff
midway through a JSON object that leaves invalid JSON); for RAG context, drop lowest-ranked documents
entirely rather than truncating every document's content, which usually stays more sensible than every
document losing its second half.

---

## 13. LangChain memory classes and why LangGraph replaced them

### 13.1 The classes, for the record

LangChain's pre-LangGraph `Memory` module offered a family of classes, each a named, pre-built version
of one strategy from §3:

```python
from langchain.memory import (
    ConversationBufferMemory,          # §3.1: full history, no bound
    ConversationBufferWindowMemory,    # §3.2: last-k messages
    ConversationSummaryMemory,         # §3.4, summary-only variant: everything summarized, nothing verbatim
    ConversationSummaryBufferMemory,   # §3.4: the hybrid — summary + verbatim recent tail
    ConversationEntityMemory,          # tracks facts about named entities mentioned in conversation
    VectorStoreRetrieverMemory,        # §9: retrieves relevant past exchanges via embedding similarity
)

memory = ConversationSummaryBufferMemory(llm=llm, max_token_limit=2000)
memory.save_context({"input": "hi"}, {"output": "hello"})
memory.load_memory_variables({})     # returns the current buffer+summary as a prompt variable
```

`ConversationEntityMemory` is worth naming specifically because it is the closest the legacy module got
to §7's long-term memory — it extracted and tracked facts about entities mentioned in conversation — but
it did so with a fixed, non-extensible extraction schema and no first-class notion of cross-session
persistence, retrieval scoring, or supersession (§9.3); it was a narrower, less controllable version of
what §7's hand-built fact-extraction pipeline does deliberately.

### 13.2 Why these are being deprecated

Three structural limitations, not a change of taste:

1. **They were designed around a single `Chain`'s flat input/output dict**, with no concept of a typed,
   multi-field, reducer-merged state object. Every class solved exactly one memory strategy in
   isolation; combining conversation summarization with separately-tracked agent state (§1.2) required
   bolting together multiple incompatible abstractions with no shared contract between them.
2. **No persistence or checkpointing story of their own.** A `ConversationBufferMemory` instance's
   contents lived in a Python object with whatever lifetime the surrounding application gave it — saving
   and resuming it across process restarts was left entirely to the application, with none of
   `21-langgraph-deep-dive.md` §5's checkpointer machinery (thread-scoped, crash-safe, time-travelable)
   available for free.
3. **No concurrency story.** A memory object mutated by concurrent chain executions had no reducer
   discipline (§5.2) — exactly the silent-data-loss hazard `21-langgraph-deep-dive.md` §3.3 describes,
   with no built-in mechanism to prevent it, because the abstraction predates LangGraph's graph-and-
   reducer model entirely.

### 13.3 How checkpointing plus state replaces every one of them

The migration is not class-for-class; it is a change of what memory *is*:

| Legacy class | LangGraph equivalent |
|---|---|
| `ConversationBufferMemory` | A `messages` field with the `add_messages` reducer, no trimming node |
| `ConversationBufferWindowMemory` | Same field, plus a `trim_messages`/`RemoveMessage` node (§4.1, §5.4) |
| `ConversationSummaryMemory` | A `summary: str` state field, updated by a summarization node, `messages` cleared via `RemoveMessage` |
| `ConversationSummaryBufferMemory` | Both fields together — §3.4's hybrid, as two fields of one schema |
| `ConversationEntityMemory` | A `facts_learned` state field with a reducer (§5.2), or promoted out to §7's dedicated long-term store |
| `VectorStoreRetrieverMemory` | §9's `MemoryRetriever`, called as an ordinary retrieval node before the model-calling node |

Every legacy class becomes, in the new model, "a field of state, populated and maintained by an ordinary
node, persisted for free by whichever checkpointer the graph is compiled with" — which is why LangChain's
own migration guides do not offer a drop-in replacement class; there is no class to replace, because
memory is no longer a special kind of object, it is state, exactly as §5.1 argues.

---

## 14. Production patterns

### 14.1 Memory compaction

Periodic, scheduled (not per-request) compaction keeps stores from growing unbounded even when
individual writes are well-behaved: fold old conversation summaries further (a monthly job that
re-summarizes a quarter's worth of session summaries into one yearly digest, for a user who has been
active a long time), deduplicate near-identical extracted facts, and apply the retention policy (§10.3)
as a batch job rather than checking it inline on every read.

```python
def compact_old_facts(store, user_id: str, older_than_days: int = 180) -> int:
    stale = store.query(user_id=user_id, learned_at_before=days_ago(older_than_days))
    clusters = cluster_by_similarity(stale, threshold=0.92)     # near-duplicate facts
    merged = 0
    for cluster in clusters:
        if len(cluster) > 1:
            canonical = max(cluster, key=lambda f: f["confidence"])
            for f in cluster:
                if f["id"] != canonical["id"]:
                    store.mark_superseded(f["id"], by=canonical["id"])
                    merged += 1
    return merged
```

### 14.2 Memory eviction policies

Two flavors, applied to different storage tiers: **LRU** for hot-path caches (Redis-backed agent state
or session cache, §8.4) where the policy is purely about cache capacity, not memory correctness — an
evicted entry is recoverable from the durable backing store, so eviction is cheap to get slightly wrong.
**Relevance-based** eviction for long-term memory itself (§9.2's composite score, applied not just to
ranking retrieval results but to deciding what to prune during compaction) — here eviction is a real
data-loss decision, not a cache-capacity one, and should be logged and, ideally, reversible for a
bounded window (soft-delete with a recovery period, not an immediate hard delete) precisely because a
wrongly-evicted long-term fact has no other copy to fall back to.

### 14.3 Memory versioning

State schemas change over an application's life — new fields added, old ones renamed or removed — and
`21-langgraph-deep-dive.md` §17's anti-pattern ("deploying a state-schema change without a migration
plan for in-flight threads") applies identically to long-term memory schemas, with a longer-lived blast
radius: an in-flight LangGraph thread is typically hours to days old; a user's long-term memory profile
is potentially years old. Version the schema explicitly and migrate lazily, on read:

```python
CURRENT_PROFILE_VERSION = 3

def migrate_profile(raw: dict) -> UserProfile:
    version = raw.get("_version", 1)
    if version < 2:
        raw["preferences"] = raw.pop("prefs", {})     # v1 -> v2 rename
        version = 2
    if version < 3:
        raw["facts"] = [{"fact": f, "confidence": 1.0, "learned_at": 0} for f in raw.get("facts", [])
                         if isinstance(f, str)]         # v2 -> v3: facts became structured, not strings
        version = 3
    raw["_version"] = version
    return UserProfile(**{k: v for k, v in raw.items() if k != "_version"})
```

Lazy, on-read migration (rather than a one-time batch migration of every stored profile) is usually the
lower-risk choice for a store with a long tail of rarely-accessed old records — it spreads the migration
cost across actual reads instead of requiring a single risky bulk job, at the cost of the migration code
needing to stay in the codebase until every record has plausibly been touched at least once (a
determinable fact, from the store's own `_version` field distribution, not a guess).

### 14.4 Testing with memory: deterministic vs stateful

Two distinct test regimes are needed, and conflating them produces either flaky tests or tests that
don't actually exercise memory behavior:

**Deterministic tests** — no LLM calls in the memory path itself — exercise the mechanical parts:
reducers merge correctly under simulated concurrent writes (§5.2), trimming respects `start_on` and
tool-call pairing (§4.4), retention policy deletes exactly the records past their TTL, RLS policies
actually block cross-tenant reads (§10.2). These should be the majority of memory test coverage, because
they are fast, free, and non-flaky.

```python
def test_reducer_survives_concurrent_writes():
    state = {"facts_learned": []}
    branch_a_update = {"facts_learned": [{"fact": "A"}]}
    branch_b_update = {"facts_learned": [{"fact": "B"}]}
    merged = apply_reducer(operator.add, state["facts_learned"],
                            branch_a_update["facts_learned"] + branch_b_update["facts_learned"])
    assert len(merged) == 2          # neither branch's contribution was lost
```

**Stateful (LLM-in-the-loop) tests** — a smaller, slower suite — exercise the judgment calls: does the
extraction prompt (§7.2) actually decide "this is durable" vs "this is task-specific context" correctly
on a labeled set of example turns; does the summarizer (§3.4) preserve the specific facts a golden test
set says it must preserve. These are evaluation-style tests (`08-evaluation-methodology.md`'s
methodology applies directly) — non-deterministic by nature, scored against a labeled set with a
pass-rate threshold, not a single assert.

### 14.5 Observability: measuring memory, not just building it

A memory system with no metrics is a memory system nobody can tell is degrading until a user complains
that "it forgot" — the same silent-failure risk §4.5 names for over-aggressive trimming, generalized to
every layer in this chapter. Instrument, at minimum, four numbers per turn, in the same span-per-call
discipline `21-langgraph-deep-dive.md` and the planned `10-llm-observability-and-tracing.md` already
establish for retrieval and generation: **history tokens sent** (is the trimming/summarization budget
actually being respected in production, not just in the unit test that checks the function in
isolation), **memory retrieval hit rate** (of the top-K memories retrieved, how many were actually
referenced in — or influenced — the model's response, a proxy for whether retrieval relevance is
holding up as the memory corpus grows), **extraction yield and rejection rate** (how many turns produce
an extracted fact, and what fraction of those are rejected by the confidence threshold — a sudden shift
in either is usually a prompt regression, not a change in what users are saying), and **checkpoint/state
size growth per thread** (per `21-langgraph-deep-dive.md` §5.7's pruning discussion, a rising trend here
predicts the slow-query incident before it happens rather than after).

```python
def log_memory_turn_metrics(trace_id: str, history_tokens: int, retrieved: list[dict],
                             cited_fact_ids: set[str], extracted_count: int, rejected_count: int) -> None:
    hit_rate = len(cited_fact_ids & {r["id"] for r in retrieved}) / max(len(retrieved), 1)
    emit_metric("memory.history_tokens", history_tokens, tags={"trace_id": trace_id})
    emit_metric("memory.retrieval_hit_rate", hit_rate, tags={"trace_id": trace_id})
    emit_metric("memory.extraction_yield", extracted_count, tags={"trace_id": trace_id})
    emit_metric("memory.extraction_rejected", rejected_count, tags={"trace_id": trace_id})
```

None of these require an LLM-as-judge call to compute — they are cheap, structural signals, which is
exactly why they belong in the always-on production path rather than in the sampled, more expensive
evaluation suite of §14.4's stateful tests. The evaluation suite answers "is memory *correct* on this
labeled set"; these metrics answer "is memory *behaving the same way in production* that it did when
it was last evaluated" — a distinction that matters because a prompt change, a model upgrade, or simply
a shift in what users talk about can silently move production behavior away from what the last
evaluation run validated, with no single test failing to announce it.

---

## 15. Anti-patterns

**Unlimited conversation history with no trimming or summarization strategy.** The default that §2
argues against in full — correct for a bounded number of turns, silently expensive and then broken past
that bound, with the break arriving as a production incident (a context-length error, or a cost report
that surprises finance) rather than a design review finding.

**No summarization strategy for long conversations, only truncation.** Truncation alone (§3.2–§3.3)
optimizes for cost and window-fit while remaining blind to relevance — correct as one *layer* of a
memory strategy, wrong as the *only* layer for any conversation with meaningful cross-turn dependency.

**Storing raw conversation as "long-term memory" instead of extracted facts.** The single most common
architectural mistake in §7: appending full transcripts (or even full per-session summaries) to a
"memories" table and retrieving them wholesale in future sessions. This inherits every problem §7.6
names — context masquerading as facts, unbounded per-user growth, no supersession mechanism, and
retrieval quality that degrades as the corpus of "memories" grows because most of it was never meant to
be reusable across sessions in the first place.

**Memory that grows unboundedly with no eviction, compaction, or retention policy.** Every store in §8
needs an answer to "what happens to this after a year of activity for a heavy user," decided at design
time, not discovered when a slow-query alert or a storage-cost anomaly forces the question.

**No isolation between users, or isolation enforced only at the application layer.** §10.2's argument
in full: application-layer-only filtering is one missed `WHERE` clause away from a cross-tenant leak,
and the missed clause is disproportionately likely to be in the newest, least-reviewed code path.

**Trusting the LLM to manage its own memory correctly, end to end, with no verification step.** Letting
a model decide unsupervised what to remember, what to forget, and what supersedes what — with no
extraction schema (§7.2), no confidence threshold, no contradiction check (§9.3) — produces a memory
store whose contents are exactly as reliable as an ungrounded LLM generation, because that is precisely
what they are. Memory correctness is a system property enforced by code around the model, not a
capability to delegate wholesale to the model itself.

**Treating the checkpointer as a complete memory solution.** Per §5.3, checkpointing persists whatever
state exists; it does not, by itself, bound conversation history growth, extract long-term facts, or
apply retention — a graph that checkpoints an ever-growing `messages` field faithfully checkpoints an
ever-growing cost and latency problem.

**Re-summarizing full history from scratch on every turn instead of folding incrementally.** The
quadratic-cost mistake named in §3.4 — easy to introduce by "simplifying" an incremental design into a
naive one that looks equivalent on a short conversation and only reveals its cost curve at scale.

**No confidence or provenance tracking on extracted facts.** A fact stored without a confidence score,
a source, and a timestamp cannot later be triaged, corrected, or superseded correctly (§9.3, §14.3) — it
is an opaque assertion the system has no way to reason about except to trust or discard wholesale.

**Ignoring the message-structure constraints when hand-rolling trimming.** Dropping a `ToolMessage`
without its matching tool-call `AIMessage` (or vice versa), or trimming past a `SystemMessage`, per
§4.1–§4.3 — bugs that are invisible in a short manual test and appear as a malformed-request API error
or a silently-forgotten system prompt in production traffic specifically shaped to trigger them.

---

## 16. Interview questions, with weak and strong answers

**1. What are the three kinds of memory in an agent system, and why does the distinction matter?**
Weak: "Short-term and long-term memory." Strong: names conversation history, agent state, and
long-term memory specifically, and grounds the distinction in different lifetimes, read/write ratios,
and storage shapes (§1.4) — and gives the concrete failure of collapsing them (a preference stored
inside the message list instead of a retrievable store, §1.4's closing example).

**2. Why can't you just send the entire conversation history on every call?**
Weak: "It costs too much." Strong: gives all three independent reasons — hard context-window ceiling,
quadratic cost growth from re-transmitting history every turn, and the "lost in the middle" attention
degradation that makes irrelevant context actively harmful to the current answer's quality, not merely
wasteful (§2.1–§2.4).

**3. How do you handle a conversation that's been going for 100+ turns?**
Weak: "Truncate old messages." Strong: describes the layered approach — a token-bounded verbatim tail
(§3.3–§3.4) combined with incremental summarization of everything older, with safety- or fact-critical
statements extracted into structured long-term memory (§7.2) *before* they can be lost to lossy
summarization, and flags that pure truncation alone is relevance-blind (§3.2) where summarization at
least attempts to preserve what mattered.

**4. What's the difference between conversation buffer memory, summary memory, and window memory?**
Weak: lists the three names. Strong: explains the mechanism and failure mode of each — buffer keeps
everything (unbounded), window keeps the last N by recency (relevance-blind), summary folds old turns
into a compressed running digest while keeping recent turns verbatim (bounded, relevance-aware, lossy on
exact detail) — and states when each is the right choice (§3.6's table), not just what each does.

**5. Walk me through `trim_messages` — what parameters matter and why?**
Weak: "It trims old messages." Strong: `strategy` (last vs first), `token_counter` (model-specific vs
raw message count), `max_tokens`, `include_system` (never trim the system prompt), and critically
`start_on` — the parameter that prevents a structurally invalid trimmed sequence (a dangling
`ToolMessage` with no matching call) from being sent to the model (§4.1).

**6. How would you trim history without breaking a tool call and its result apart?**
Weak: "Trim by message count, it usually works out." Strong: explains that tool-call/tool-result pairs
are structurally one unit tied by `tool_call_id`, that a generic trimmer isn't aware of this, and shows
the pattern of tracking required call IDs while walking backward so a required `ToolMessage`'s
originating `AIMessage` is never dropped independently (§4.4).

**7. What is a reducer, and how does it relate to memory specifically?**
Weak: "It's how LangGraph merges state." Strong: explains the default overwrite-on-conflict behavior,
why concurrent branches writing to the same un-reduced memory field (e.g. a shared `facts_learned` list)
silently lose contributions, and that `add_messages` and `operator.add` are the two most common memory-
relevant reducers, with the concrete failure mode being invisible in sequential testing (§5.2, and
`21-langgraph-deep-dive.md` §3.3).

**8. Why does LangGraph's checkpointer persist conversation history, and is that enough on its own?**
Weak: "Yes, checkpointing handles memory." Strong: explains that the checkpointer persists whatever is
in state, including an unbounded `messages` field if nothing trims it, and that a production graph must
still apply trimming/summarization as an explicit node so the *checkpointed* history stays bounded — the
checkpointer solves durability, not growth (§5.3).

**9. How do you delete a message from LangGraph state, and why can't you just return a shorter list?**
Weak: "Return the trimmed list from the node." Strong: explains that `add_messages` merges by ID rather
than replacing wholesale, so a shorter returned list only fails to add new messages — it does not remove
existing ones — and that `RemoveMessage(id=...)` is the explicit deletion sentinel required (§5.4).

**10. Design a system that remembers user preferences across sessions.**
Weak: "Store the conversation in a database and load it next time." Strong: separates the pieces —
async fact extraction with a structured schema and confidence score (§7.2), a small profile for
always-relevant preferences plus a vector store for the long tail of facts (§7.1, §7.3), a composite
relevance+recency+confidence retrieval score with explicit supersession rather than pure decay (§9.2–
§9.3), tenant isolation on every read (§10.2), and a retention/deletion story (§10.3–§10.4) — and states
explicitly that raw conversation is never stored as "the memory," only the facts extracted from it.

**11. How do you share context between agents without exploding token costs?**
Weak: "Give every agent the full conversation and let them figure it out." Strong: scoped memory —
private-per-agent versus shared-blackboard state (§11.2) so each agent's context is bounded to what its
subtask needs, structured artifact handoffs instead of prose summaries between agents
(`22-agent-orchestration-patterns.md` §6.4), and a shared *retrieval layer* for long-term memory queried
independently and narrowly by each agent rather than one agent broadcasting a full memory dump to every
other agent (§11.4) — names the combinatorial blowup of naive peer-to-peer full-context sharing
explicitly.

**12. What's "lost in the middle," and why does it matter for memory design?**
Weak: "Models get confused with long context." Strong: describes the empirical U-shaped
position-vs-accuracy curve, explains it means adding more (even relevant) context can *reduce* answer
quality if it dilutes the position of the truly relevant fact, and draws the design conclusion: curated,
promoted-to-prominent-position context (a summary, a retrieved fact placed near the query) beats a
longer but undifferentiated context every time relevance and position aren't aligned (§2.3).

**13. How would you design agent state for a multi-day, human-gated approval workflow?**
Weak: "Store the current status in a database." Strong: models it as an explicit state machine (states,
guarded transitions, §6.1), keeps "where are we" as a single typed field rather than something inferred
from a transcript, checkpoints it with a durable, thread-scoped backend so a human's approval three days
later resumes exactly where the flow left off even across process restarts, and calls out
`update_state`'s `as_node` parameter for correctly recording an out-of-band human action (§6.2–§6.4,
`21-langgraph-deep-dive.md` §5.5).

**14. What's wrong with using `ConversationBufferMemory` (or its siblings) in a new project today?**
Weak: "It's deprecated, use LangGraph instead." Strong: explains the structural reasons — no typed
multi-field schema, no reducer/concurrency story, no persistence of its own — and correctly states the
migration is conceptual (memory becomes state, §13.3's table) not a class-for-class swap.

**15. How do you decide between Postgres, Redis, and a vector store for a given piece of memory?**
Weak: "Postgres for everything, it's fine." Strong: applies the decision matrix (§8.6) — durability and
queryability needs point to Postgres; hot-path low-latency session/task state with natural TTL semantics
points to Redis; semantic retrieval over an unbounded, growing corpus of facts points to a vector store
(or pgvector if volume and scale don't yet justify a dedicated one) — and gives a concrete example of
splitting one system across two or three of these deliberately, not defaulting to one for everything.

**16. A user asks the agent to "forget everything about me." What actually has to happen?**
Weak: "Delete their row from the database." Strong: enumerates every backend memory was written to
(relational facts table, vector index embeddings, Redis session keys, any knowledge graph nodes/edges)
and states that a correct implementation is tested in CI against all of them, not a manual runbook,
because deletion that misses one backend is a compliance gap that looks complete in review (§10.4).

**17. How do you prevent a summarization step from silently dropping something important?**
Weak: "Use a good prompt." Strong: names the concrete failure modes — exact numbers/IDs degrading first,
hallucination or nuance-loss being an independent LLM-call risk, and compounding error from repeated
re-summarization of summaries — and the mitigation of extracting structured facts before summarizing
prose, never routing safety- or legally-relevant statements through a lossy summarization path at all
(§3.5).

**18. What's the difference between a "fact" and "context" in long-term memory, and why does conflating
them cause problems?**
Weak: "They're basically the same thing." Strong: a fact is stable and reusable across unrelated future
sessions; context is true of a specific moment and often actively wrong to resurface unprompted later —
gives the concrete example (resurfacing "you were debugging X on Tuesday" in an unrelated later session)
and states that the extraction step (§7.2) is explicitly the filter that keeps context out of a store
meant only for facts (§7.6).

**19. How would you test a memory system without making every test depend on a live LLM call?**
Weak: "Just call the real model in tests, it's the most accurate." Strong: splits deterministic tests
(reducer merge correctness, trimming boundary conditions, retention/TTL logic, RLS/tenant-isolation
enforcement — fast, free, non-flaky) from a smaller stateful suite that does exercise LLM judgment calls
(extraction quality, summary fidelity) scored against a labeled set with a pass-rate threshold, per
`08-evaluation-methodology.md`'s methodology, rather than one undifferentiated bucket of flaky
LLM-in-the-loop tests (§14.4).

**20. What's the risk of "trusting the LLM to manage its own memory"?**
Weak: "Models are pretty reliable now, it's fine." Strong: states plainly that a memory store built by
letting a model decide unsupervised what to remember and forget is only as reliable as an ungrounded
generation, because that's exactly what it is, and that correctness has to be enforced by code around
the model — a structured extraction schema with confidence scoring, an explicit contradiction/
supersession check, a retention policy — not delegated wholesale (§15).

**21. Your context window budget is 128k tokens. How do you allocate it across system prompt, history,
RAG, tool results, and working memory?**
Weak: "Split it evenly" or "just use as much as fits." Strong: reserves fixed cost for system prompt and
model output up front, then allocates the remainder by *turn type* rather than a single static split —
a factual-lookup turn weights RAG heavily and history lightly, a long-conversation-followup turn does
the reverse, a tool-heavy task weights tool results — and truncates within a category using
category-appropriate logic (structure-preserving for tool results, drop-lowest-ranked for RAG documents)
rather than a blind byte cutoff when a category still overflows its slice (§12.2–§12.3).

**22. Two branches of a graph both try to append to a shared `facts_learned` list concurrently. What
happens, and how do you fix it?**
Weak: "LangGraph handles that automatically." Strong: states plainly that without a declared reducer the
default is overwrite/last-writer-wins, so one branch's contribution is silently lost — invisible in
sequential testing, real under production concurrency — and that the fix is `Annotated[list[dict],
operator.add]` (or a custom merge function for anything needing deduplication), naming this as
specifically the concurrency contract memory-bearing state fields need (§5.2).

---

## 17. Lab exercises

**Lab 1 — Build all three summarization strategies and measure their information loss.**
*Goal:* stop taking §3's tradeoff table on faith and produce your own numbers. *Steps:* generate (or
collect) a 60-turn synthetic conversation containing five specific facts planted at known turns (a
name, a number, a stated preference, a constraint, a decision); run it through sliding-window (§3.2),
token-truncation (§3.3), and summary-buffer (§3.4) memory; at turn 60, ask a fixed set of five questions
recovering each planted fact and score exact-recovery rate per strategy. *Artifact:* a table of strategy
versus facts recovered versus total tokens spent. *Success criterion:* you can state, with your own
numbers, which strategy recovered which facts and why the ones it lost were lost. *Time:* ~2 hours.

**Lab 2 — Reproduce "lost in the middle" on a model you actually use.**
*Goal:* turn §2.3 from a cited claim into something you've personally measured. *Steps:* construct ten
prompts, each embedding one specific fact at a different relative position (0%, 10%, ..., 90%) within a
fixed-length padding of irrelevant text, and ask a question only answerable from that fact; run all ten
against the same model and plot accuracy against position. *Artifact:* the position-vs-accuracy plot
and the raw prompts/responses. *Success criterion:* you can describe the shape of your own curve and
explain one design decision it would change in a system you're building. *Time:* ~1.5 hours.

**Lab 3 — Reducer failure in a shared memory field, reproduced on purpose.**
*Goal:* make §5.2's concurrency hazard something you've seen fail, not just read about — a memory-
specific variant of `21-langgraph-deep-dive.md` §17's Lab 2. *Steps:* build a graph with three
concurrently-executing nodes each appending a fact to a shared `facts_learned` field with no reducer;
run it enough times to observe non-deterministic data loss; fix it with `operator.add`; then
deliberately engineer a duplicate-fact case and fix that with a dedup-aware custom reducer. *Artifact:*
a script demonstrating broken, naively-fixed, and correctly-fixed behavior with printed state after
each run. *Time:* ~1.5 hours.

**Lab 4 — Build a fact-extraction pipeline with confidence scoring and supersession.**
*Goal:* implement §7.2–§9.3 end to end rather than reading the pseudocode. *Steps:* write the structured
extraction prompt and schema; run it over ten multi-turn synthetic conversations, inspecting which
statements get extracted as durable facts versus correctly rejected as session-scoped context; add the
write-time contradiction check (§9.3) and demonstrate a later fact correctly superseding an earlier
contradictory one rather than both persisting side by side. *Artifact:* the extraction pipeline, its
prompt, and a before/after showing supersession working. *Success criterion:* you can point to at least
one case where your confidence threshold correctly caught a low-quality extraction and routed it to
review instead of writing it directly. *Time:* ~2.5 hours.

**Lab 5 — Cross-tenant memory leak, found and fixed.**
*Goal:* experience §10's isolation argument as a bug you found, not a rule you were told. *Steps:* build
a two-user memory store with vector retrieval and application-layer-only filtering (no RLS); write an
endpoint that forgets to apply the tenant filter (deliberately, to reproduce the realistic mistake); write
a test that catches the leak; then implement Postgres row-level security (or an equivalent
mandatory-filter wrapper for a non-SQL store) and show the same missing-filter code path can no longer
leak, because the enforcement moved to the storage layer. *Artifact:* the vulnerable version, the failing
test that caught it, and the fixed version with the same test passing. *Time:* ~2 hours.

**Lab 6 — Implement and time a full "forget this user" operation across three backends.**
*Goal:* prove §10.4's cross-backend deletion argument concretely rather than assuming one `DELETE`
statement is enough. *Steps:* stand up a user with data in a Postgres facts table, a vector index, and a
Redis session cache; implement `forget_user` touching all three; verify with a post-deletion query
against each backend that zero rows/vectors/keys remain; then deliberately remove the vector-store
deletion call and demonstrate the leftover embeddings a naive "delete the row" implementation would
have missed. *Artifact:* the complete deletion function, its test, and a one-paragraph note on which
backend would have been the actual gap in a rushed implementation. *Time:* ~1.5 hours.

**Lab 7 — Dynamic context budget allocation, measured against a fixed split.**
*Goal:* validate §12.2's claim that turn-type-aware allocation beats a static split, with your own data.
*Steps:* implement the fixed 25/25/25/25-style split and the dynamic, turn-type-classified allocation
side by side; run both against a mixed set of twenty turns (some factual-lookup, some
long-conversation-followup, some tool-heavy) from the same underlying conversation history and RAG
corpus; score both on an answer-quality rubric (or exact-fact-recovery, per Lab 1's method) per turn.
*Artifact:* a table of turn versus strategy versus quality score. *Success criterion:* identify at least
two turns where the static split visibly under-served the category that actually mattered, and confirm
the dynamic allocation corrected it. *Time:* ~2 hours.

**Lab 8 — Migrate a memory schema without breaking old, unmigrated records.**
*Goal:* build and prove §14.3's lazy on-read migration pattern. *Steps:* create a store of user profiles
at schema v1; write the v2 and v3 migrations (a field rename, a shape change from `list[str]` to
`list[dict]`); confirm that reading a v1 record through the current code path correctly upgrades it in
memory without a batch job having touched the underlying row, and that writing it back persists the
now-current version; then simulate reading a record from a *future* version your current code doesn't
know about and confirm it fails loudly rather than silently corrupting data. *Artifact:* the migration
chain, plus a test matrix of (stored version) x (expected behavior). *Time:* ~1.5 hours.
