# 21 — LangGraph deep dive: agent orchestration as a state machine

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (§12, "the 2026 shape: from
> pipeline to loop" — this chapter is that loop, fully specified), [`13-agents-and-tool-calling.md`](13-agents-and-tool-calling.md)
> (function-calling schemas, parallel tool calls, and the tool-error-handling contract that
> `ToolNode` in §9 here is a specific, opinionated implementation of),
> [`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md) (every cycle a graph takes is
> another full context window billed — §12's recursion limits and §13's multi-agent fan-out are
> that cost model applied), [`../python-mastery/29-async-patterns-and-pitfalls.md`](../python-mastery/29-async-patterns-and-pitfalls.md)
> (nodes run concurrently when the graph fans out, and §3's reducers exist because concurrent writes
> to shared state are the same hazard as any other race).
>
> **Feeds into:** [`14-agent-evaluation.md`](14-agent-evaluation.md) (a graph's checkpoint history in
> §5 is the trace format that multi-turn agent eval is built on), [`16-multi-tenancy-and-isolation.md`](16-multi-tenancy-and-isolation.md)
> (§5's `thread_id` is a tenant/session boundary the moment you put it behind an API, and getting
> that boundary wrong is a cross-user data leak), [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md)
> (§6's human-in-the-loop interrupts are the primary technical control for "don't let the agent take
> the irreversible action unsupervised").
>
> **THESIS:** LangChain gives you a chain — a fixed, directed path from prompt to output. An agent is
> not a chain; it is a **loop with state that must survive the loop**, and a loop with unpredictable
> branching, retries, and pauses cannot be expressed as a sequence of `.pipe()` calls without the
> control flow leaking into string-typed exception handling and ad-hoc `while` loops around an LLM
> call. LangGraph exists to make the loop the primitive instead of the workaround: **a graph, over an
> explicit and reducer-merged state object, compiled into a runnable that checkpoints itself after
> every node.**
>
> Three design decisions follow from that one sentence and explain nearly everything else in this
> document. Because state is explicit and typed, concurrent branches can write to it without
> clobbering each other — *if* you declare a reducer, and *if* you don't, they do. Because the graph
> checkpoints itself after every node, a long-running or human-gated workflow can crash, get
> interrupted for approval, or simply be paused for three days, and resume from exactly where it left
> off with `thread_id` as the only handle. And because the compiled graph is the same object whether
> it has one node or fifty, whether it is a single ReAct loop or a supervisor coordinating six
> specialist subgraphs, the mental model does not change as the system grows — only the number of
> nodes does. Everything below is the mechanics of those three sentences, and the discipline of
> knowing when *not* to reach for a graph at all.

---

## Contents

1. [Why LangGraph exists: the gap between a chain and a loop](#1-why-langgraph-exists-the-gap-between-a-chain-and-a-loop)
2. [Core concepts: StateGraph, nodes, edges, compilation](#2-core-concepts-stategraph-nodes-edges-compilation)
3. [State management in depth: schemas, reducers, and merge semantics](#3-state-management-in-depth-schemas-reducers-and-merge-semantics)
4. [Conditional edges and routing](#4-conditional-edges-and-routing)
5. [Checkpointing and persistence: the killer feature](#5-checkpointing-and-persistence-the-killer-feature)
6. [Human-in-the-loop: interrupt, resume, approve](#6-human-in-the-loop-interrupt-resume-approve)
7. [Streaming: values, updates, messages, events, custom](#7-streaming-values-updates-messages-events-custom)
8. [Subgraphs: composing graphs out of graphs](#8-subgraphs-composing-graphs-out-of-graphs)
9. [Tool calling with LangGraph: ToolNode and the agent loop](#9-tool-calling-with-langgraph-toolnode-and-the-agent-loop)
10. [The ReAct pattern and create_react_agent](#10-the-react-pattern-and-create_react_agent)
11. [Error handling and retry](#11-error-handling-and-retry)
12. [Preventing infinite loops](#12-preventing-infinite-loops)
13. [Multi-agent architectures](#13-multi-agent-architectures)
14. [Durable execution: LangGraph versus workflow engines](#14-durable-execution-langgraph-versus-workflow-engines)
15. [LangGraph Platform, Server, and Studio](#15-langgraph-platform-server-and-studio)
16. [Production patterns: config, testing, observability, deployment](#16-production-patterns-config-testing-observability-deployment)
17. [Anti-patterns](#17-anti-patterns)
18. [Interview questions, with weak and strong answers](#18-interview-questions-with-weak-and-strong-answers)
19. [Lab exercises](#19-lab-exercises)

---

## 1. Why LangGraph exists: the gap between a chain and a loop

Start with what a chain actually is, mechanically, because the limitation is not a matter of taste —
it is structural. A LangChain "chain" (whether built with the legacy `Chain` classes or the modern
LangChain Expression Language, `prompt | model | parser`) is a composition of functions where the
output of one is the input of the next, evaluated exactly once, in exactly one direction. You can
branch it with `RunnableBranch`, you can fan out and merge with `RunnableParallel`, you can retry a
step with `.with_retry()`. What you fundamentally cannot do inside that abstraction is **go
backwards** — have step five decide that step two needs to run again with new information — without
stepping outside the composition and writing a hand-rolled `while` loop around the whole thing. And
the moment you write that `while` loop, you have re-invented, badly, the four things a real orchestration
layer needs to provide: a place to keep state across iterations, a way to decide when to stop, a way
to persist progress so a crash doesn't lose the whole conversation, and a way to inspect what
happened at each step. LangChain agents pre-LangGraph (the `AgentExecutor` class) did exactly this —
an internal loop, opaque, with a fixed shape (think, act, observe, repeat) and no exposed seams for
you to customize the control flow, checkpoint mid-loop, or add a human approval gate. It worked for
demos and broke down the moment a real production requirement showed up: "pause before this tool
runs and let a human approve it," "if the third retry also fails, escalate to a different model," "run
these two lookups in parallel and merge the results before deciding what to do next."

The conceptual gap is this: **a chain has no cycles and no persisted state across steps beyond what
you thread through function arguments; an agent loop has both, definitionally.** An agent is a system
that observes, decides, acts, and re-observes, an unknown number of times, where each iteration can
depend on everything that came before and where the loop itself is a resource that must be
observable, interruptible, and resumable. That is not a chain with an extra edge — it is a different
kind of object, and the natural representation for "a set of steps with arbitrary transitions between
them, holding a piece of state that gets updated as execution proceeds" is a graph, not a pipe. Nodes
are computation. Edges are control flow. State is the thing that flows along the edges and
accumulates. This is not a new idea — it is the same automaton/state-machine formalism used for
protocol design, for game AI behavior trees' more general cousin, and for workflow engines like
Temporal and AWS Step Functions (§14 draws that comparison out fully) — LangGraph's contribution is
applying it specifically to the shape of problems an LLM application has: a state object that mostly
holds a message history, nodes that are mostly "call an LLM" or "call a tool," and a runtime built
from the ground up to checkpoint after every node because LLM calls are slow and expensive enough
that losing progress on a crash is not acceptable.

It is worth being precise about what LangGraph is *not*, because the marketing blur between "LangChain"
and "LangGraph" causes real confusion in interviews and in production decisions. LangGraph does not
require LangChain — you can build a LangGraph application with plain Python functions and no
LangChain import at all, and increasingly that is how experienced teams use it: LangChain for the
model-provider abstraction and a handful of well-tested utilities (message types, `ToolNode`,
`create_react_agent`), LangGraph for everything about control flow. Conversely you can use LangChain's
model and tool abstractions with a hand-rolled loop and no LangGraph at all — plenty of production
systems did exactly this before LangGraph existed and some still do. What LangGraph specifically buys
you, and the reason it has become the default answer to "how do I orchestrate agents" in 2025–2026,
is the combination of four things none of which is individually hard to build but which are annoying
to build *well* and *together*: an explicit state schema with declarative merge semantics (§3), a
conditional-routing model that is just Python functions (§4), a checkpointer abstraction that
persists the entire state after every node with zero extra code in your nodes (§5), and a first-class
notion of pausing mid-graph for a human and resuming later with updated state (§6). Take any one of
those away and you are back to hand-rolling it.

The other framing worth internalizing before anything else: **LangGraph is a low-level orchestration
library, not a high-level agent framework.** This matters because it inverts the usual
tradeoff. Frameworks like the original AutoGPT-style agents or CrewAI's default "crew" abstraction
give you agent behavior for free but very little control over exactly how the loop runs — you accept
their control flow and hope it matches your use case. LangGraph gives you almost nothing for free (a
`StateGraph` with no nodes does nothing) but total control over the control flow, at the cost of you
having to specify it. The `create_react_agent` prebuilt (§10) is the one significant exception —
a fully wired ReAct loop you can call in one line — and it exists precisely so that the common case
doesn't require hand-drawing a graph, while every graph it produces is still just a `StateGraph`
you can inspect, extend, and fall back to hand-writing the moment the prebuilt's assumptions stop
fitting. A senior engineer's answer to "should I use LangGraph" is never "yes because agents," it is
"yes, once the control flow needs cycles, persistence across a pause, or more than one path through
the logic depending on runtime state" — and the honest complement of that answer is §17's anti-pattern
list, because a two-step "retrieve then generate" pipeline gains nothing from being a graph and loses
the straight-line readability of a chain.

The history here is a useful data point precisely because it is LangChain's own history: the original
`AgentExecutor` — LangChain's pre-graph agent abstraction — is now formally in maintenance mode, with
LangChain's own migration guides pointing every agent use case at LangGraph. That is not a marketing
pivot; it is the maintainers of the library that shipped the opaque-loop version concluding, from
direct exposure to what production users actually hit, that the opaque loop's fixed shape and closed
control flow were the wrong abstraction the moment real requirements (approval gates, custom retry,
multi-agent coordination) showed up — the same conclusion this section reaches from the abstraction's
structure. Anyone still maintaining `AgentExecutor`-based code should read that migration path as
exactly the signal `create_react_agent` is designed to make painless: the same tools, the same model
binding, wired into a graph that inspects and extends rather than a black box.

---

## 2. Core concepts: StateGraph, nodes, edges, compilation

Every LangGraph application is built from four kinds of object: a **state schema**, **nodes**,
**edges**, and the **compiled graph**. Get comfortable with the vocabulary before the mechanics,
because interviewers will use it precisely and expect you to.

**State** is a single object — conventionally a `TypedDict`, though Pydantic models and plain
dataclasses are also supported — that represents everything the graph knows at a point in time. It is
not per-node local state; it is the one shared blackboard every node reads from and writes to.

```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    remaining_steps: int
```

**Nodes** are plain Python (or async) functions with the signature `(state) -> dict`. A node receives
the *current* state and returns a **partial update** — only the keys it wants to change. It does not
return the full state, and it must not mutate the input state object in place; the runtime treats the
return value as a delta to merge in (exactly how the merge happens is the subject of §3).

```python
def call_model(state: AgentState) -> dict:
    response = llm.invoke(state["messages"])
    return {"messages": [response]}          # a delta, not the full list
```

**Edges** connect nodes and come in three flavors. A **normal edge** is an unconditional hop from one
node to the next — `graph.add_edge("retrieve", "generate")` means "after retrieve finishes, always run
generate." A **conditional edge** attaches a router function to a node's outgoing transitions —
`graph.add_conditional_edges("agent", route_fn, {"tool": "tools", "end": END})` means "after agent
finishes, call `route_fn(state)`, and go to whichever node the returned key maps to" (full treatment
in §4). And there are two **sentinel nodes**, `START` and `END`, that are not real nodes but markers:
every graph needs at least one edge out of `START` (where does execution begin) and at least one path
that reaches `END` (or the graph never terminates and eventually blows the recursion limit, §12).

```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(AgentState)
graph.add_node("agent", call_model)
graph.add_node("tools", tool_node)

graph.add_edge(START, "agent")
graph.add_conditional_edges(
    "agent",
    lambda state: "tools" if state["messages"][-1].tool_calls else "end",
    {"tools": "tools", "end": END},
)
graph.add_edge("tools", "agent")   # the cycle: tool result goes back to the LLM
```

That five-line skeleton — agent node, tools node, a conditional edge that checks for tool calls, and
an edge from tools back to agent — *is* the ReAct loop. Everything in §9 and §10 is filling in the
details of exactly this shape. Notice the cycle: `tools -> agent` and the conditional edge that can
route `agent -> tools` again is what a chain fundamentally cannot express, and it is the entire reason
this is a graph library and not a pipe library.

**Compilation** is the step that turns the graph *definition* into a runnable. `graph.compile()`
validates the graph (every node reachable from `START`, no dangling edges to nodes that were never
added, at least one path to `END`), and returns a `CompiledStateGraph` that implements the same
`Runnable` interface as everything else in LangChain — `.invoke()`, `.stream()`, `.ainvoke()`,
`.batch()`. This is a deliberate design choice: once compiled, a graph is indistinguishable from any
other LangChain runnable to code that calls it, which is what lets you nest a compiled graph as a node
inside a larger graph (§8) without the caller needing to know it's a graph at all.

```python
app = graph.compile()
result = app.invoke({"messages": [HumanMessage("What's 3 + 4?")]})
```

Compilation is also where you attach the two things that make a graph production-grade rather than a
toy: a **checkpointer** (`graph.compile(checkpointer=MemorySaver())`, §5) and static
**interrupt points** (`graph.compile(interrupt_before=["human_review"])`, §6). Both are compile-time
concerns because they change what the runtime does between node executions, not what any individual
node computes — a clean separation that pays off the first time you need to add human review to a
graph you didn't design with it in mind: you change the `compile()` call, not the nodes.

One more piece of vocabulary that trips people up: **`MessageGraph`** is a special-cased,
now-largely-legacy variant of `StateGraph` where the entire state *is* a list of messages rather than
a dict containing a messages key among others. It exists because early LangGraph agents were purely
conversational and a bare message list was the whole state. It has been superseded in current practice
by `StateGraph` with a `TypedDict` that has a `messages: Annotated[list, add_messages]` field among
possibly several other fields — strictly more general, since real agents need to carry things beyond
the chat transcript (a plan, a scratchpad, a budget counter, retrieved documents, tool-call error
counts). If an interviewer asks "when would you use `MessageGraph`," the honest senior answer is "for
a pure chatbot with no auxiliary state I might reach for it for brevity, but in practice I default to
`StateGraph` because almost every real agent grows a second field within a week, and migrating a
`MessageGraph` to a dict-based state later is needless churn."

### 2.1 The execution model underneath: Pregel and super-steps

It is worth naming the actual execution model explicitly, because it explains several things that
otherwise look like arbitrary API choices: LangGraph's runtime is built on a **Pregel-style bulk
synchronous parallel (BSP)** model, the same graph-computation model Google published for large-scale
graph processing in 2010 and that Apache Giraph and Spark GraphX later implemented. A Pregel-style
computation proceeds in discrete **super-steps**: at each super-step, every node scheduled to run
executes concurrently against the state as of the *previous* super-step's completion, produces its
update, and only once every node in that super-step has finished does the runtime merge all their
updates (via reducers, §3) and advance to the next super-step. This is precisely why two nodes
scheduled in the same super-step (via a fan-out conditional edge, §4.4, or `Send`, §13.3) never see
each other's updates mid-flight — they are isolated from each other by construction, the same
isolation guarantee BSP gives distributed graph algorithms, and it is why a reducer is the *only*
correct way to combine their outputs rather than, say, one node reading state the other just wrote.
A node is never re-entered mid-execution by another concurrently-running node; the unit of atomicity
is the super-step, not the individual node. Knowing this model by name is a strong, low-effort signal
in an interview, because it reframes "why do I need a reducer" from an API quirk into a direct
consequence of a well-known, principled concurrency model rather than an arbitrary LangGraph-specific
rule.

### 2.2 Synchronous and async nodes

Node functions can be defined either synchronously (`def node(state): ...`) or as coroutines
(`async def node(state): ...`), and a single graph can freely mix both — LangGraph inspects each
node's signature at `add_node` time and calls it appropriately. `.invoke()`/`.stream()` run sync nodes
directly and will happily run a graph containing async nodes by driving an event loop internally;
`.ainvoke()`/`.astream()` are the fully-async entry points and are the ones to use when the calling
service (a FastAPI handler, for instance) is itself async, so that a slow node awaiting an HTTP call
yields the event loop to other concurrent requests instead of blocking a worker thread. The practical
rule: if any node in the graph makes a network call (nearly all of them do — LLM calls, tool calls),
prefer `async def` nodes and the `a`-prefixed graph methods in any service handling concurrent
requests, for the same reasons `../python-mastery/29-async-patterns-and-pitfalls.md` gives for
preferring async I/O generally — a sync node blocking on a slow HTTP call inside an otherwise-async
service is a thread-pool exhaustion hazard waiting for enough concurrent load to trigger it.

---

## 3. State management in depth: schemas, reducers, and merge semantics

State is the part of LangGraph that looks trivial and is not. The schema is "just a `TypedDict`," but
the *merge semantics* — what happens when two things write to the same key — is where most of the
subtlety, and most of the production bugs, live.

### 3.1 The default merge: replace

Without any annotation, LangGraph's default behavior for a state key is **overwrite**: if a node
returns `{"count": 5}`, the new value of `state["count"]` is `5`, full stop, whatever it was before is
gone. This is the right default for scalar and "latest wins" fields — a status enum, a current plan,
a cached retrieval result — and it is the wrong default for anything you intend to *accumulate*, which
is why messages need special treatment.

```python
class State(TypedDict):
    status: str          # plain replace: node returns {"status": "done"}, old value discarded
    plan: dict            # plain replace: node returns {"plan": {...}}, old value discarded
```

### 3.2 Reducers: declaring how updates merge

A **reducer** is a function `(current_value, update_value) -> new_value` that you attach to a state
key via `Annotated[Type, reducer_fn]`. Instead of the default replace, LangGraph calls your reducer
every time any node returns an update for that key, threading the old value and the new partial value
through it.

```python
import operator
from typing import Annotated

class State(TypedDict):
    messages: Annotated[list, add_messages]     # LangGraph's message-aware reducer
    scratch: Annotated[list[str], operator.add]  # stdlib list concatenation as a reducer
    total_cost: Annotated[float, operator.add]   # stdlib float addition as a reducer
```

`operator.add` is the simplest possible reducer and it is enough for a huge fraction of
accumulation needs: a node returns `{"scratch": ["found X"]}`, the reducer computes
`old_scratch + ["found X"]`, and the result is appended rather than replaced. The same trick works for
numeric accumulators — `total_cost` sums every node's contribution instead of the last node winning.

`add_messages` is LangGraph's purpose-built reducer for chat history and it does more than
concatenate. It appends new messages by default, *but* if an incoming message has the same `id` as an
existing message in the list, it treats that as an **update-in-place** rather than an append — this is
how you overwrite a message (for example, patching a tool call's arguments after a validation step, or
replacing a placeholder streaming message with its final content) without reducer gymnastics. It also
normalizes plain dicts and LangChain message objects into a consistent message type. This dual
behavior — append by default, replace-by-id when asked — is the single most-cited example in the
LangGraph docs and in interviews of "why do reducers matter," so know it cold:

```python
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage

old = [AIMessage(content="draft", id="msg-1")]
new = [AIMessage(content="final", id="msg-1")]
add_messages(old, new)   # -> [AIMessage(content="final", id="msg-1")]  — replaced, not appended
```

### 3.3 Why reducers matter: the concurrent-write hazard

The reason this is not a stylistic nicety is concurrency. When a node fans out into multiple parallel
branches (either via multiple edges out of one node, or via the `Send` API in §13.3 for dynamic
fan-out), each branch runs concurrently and each branch's return value is merged into the shared state
*independently*, in whatever order they complete. If two branches both return `{"messages": [...]}`
without a reducer, the second one to be merged silently discards the first one's contribution — a bug
that is invisible in a serial test and appears only under real concurrency, exactly the class of bug
`../python-mastery/29-async-patterns-and-pitfalls.md` catalogs for asyncio generally. A reducer is what
makes "two branches append independently" a well-defined, order-independent operation instead of a
race. This is precisely why `add_messages` and `operator.add` are commutative and associative
(concatenation and addition both are) — a reducer that isn't commutative will produce results that
depend on branch completion order, which is its own subtle bug class worth naming explicitly if asked
"can a reducer be badly designed": yes, if it is order-sensitive over concurrent inputs, you have
reintroduced the race one level up.

### 3.4 Custom reducers

Anything beyond append-and-replace is a custom reducer, and they are ordinary functions:

```python
def merge_documents(current: list[dict], new: list[dict]) -> list[dict]:
    """Deduplicate retrieved documents by id, keeping the highest-scored copy."""
    by_id = {d["id"]: d for d in current}
    for doc in new:
        if doc["id"] not in by_id or doc["score"] > by_id[doc["id"]]["score"]:
            by_id[doc["id"]] = doc
    return sorted(by_id.values(), key=lambda d: -d["score"])

class State(TypedDict):
    documents: Annotated[list[dict], merge_documents]
```

This is the pattern for the "multiple retrieval branches feeding one fused candidate set" shape from
[`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) §5 implemented as
graph state rather than as an in-memory fusion function — if you're building a retrieval-augmented
agent where several tool calls each return candidate documents, this reducer *is* the fusion step,
running automatically every time any branch contributes.

### 3.5 Partial updates versus full replacement, and why nodes never see a mutable reference

A node's return value is always interpreted as a **partial update**: return only the keys you touched.
Returning `{}` is legal and means "no state change" (useful for a node that only has side effects, like
logging). This has a corollary that catches people the first week: mutating `state["messages"]` in
place inside a node and returning nothing does *not* update the graph's state, because the runtime
never sees the mutation — it only sees the returned dict. Nodes should be treated as pure functions of
their input, and the discipline is the same one you already apply to Redux reducers or React state
updates: never mutate, always return a new value (or, for a reducer-backed key, a value to be merged).

### 3.6 Input and output schemas: private versus public state

A graph's `state_schema` is what every node reads and writes internally, but you frequently want the
caller-facing contract to be narrower than the internal working state — you don't want callers to have
to pass in your internal retry counters, and you don't want to leak your scratchpad in the response.
`StateGraph` supports separate `input` and `output` schemas for exactly this:

```python
class InputState(TypedDict):
    question: str

class OutputState(TypedDict):
    answer: str

class InternalState(TypedDict):
    question: str
    answer: str
    retrieved_docs: list[dict]     # private: never in input or output
    retry_count: int               # private

graph = StateGraph(InternalState, input=InputState, output=OutputState)
```

Callers can only pass `question` and only see `answer` come back; `retrieved_docs` and `retry_count`
exist for nodes to coordinate through but are invisible at the boundary. This is the LangGraph
equivalent of a private field in a class — it lets internal implementation (how many retries did this
take, what did we retrieve) evolve without breaking the calling contract, which matters enormously
once a graph is a node inside someone else's graph (§8) or is exposed behind an API that other teams
depend on.

### 3.7 Nested and structured state

State fields are not restricted to flat scalars and lists — a field can be a nested Pydantic model or
dict, and this is the natural way to carry structured intermediate artifacts (a partially-built plan, a
form being filled across turns, an evolving risk score). The tradeoff to be explicit about in an
interview: a nested structure with no reducer is fully replaced on every write that touches it, so if
two nodes each want to update *different* sub-fields of the same nested object concurrently, you must
either give the whole nested object a custom reducer that merges at the field level, or flatten those
sub-fields into separate top-level state keys so each gets its own (possibly trivial) reducer. Flat
state with more keys is usually the simpler and more debuggable choice; nested state earns its keep
when the fields are always written together by the same node and read together downstream, in which
case a single un-reduced dict replace is exactly the semantics you want.

### 3.8 TypedDict versus Pydantic versus dataclass for the state schema

`TypedDict` is the default in nearly every LangGraph example for a reason worth understanding rather
than imitating blindly: it has zero runtime overhead (a `TypedDict` is a plain `dict` at runtime; the
"typing" is purely a static-analysis annotation that tools like mypy check but Python itself never
enforces), which matters because state is read and merged on every single super-step. `StateGraph`
also accepts a Pydantic `BaseModel` as the schema, which buys real **runtime validation** — a node that
returns a partial update with the wrong type for a field raises immediately at the state-merge
boundary instead of silently propagating a malformed value three nodes downstream until something
finally chokes on it:

```python
from pydantic import BaseModel, Field

class AgentState(BaseModel):
    messages: Annotated[list, add_messages] = Field(default_factory=list)
    retry_count: int = 0
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)   # validated range, not just a type
```

The tradeoff is exactly the one you'd expect from any dynamic-validation layer: Pydantic validates
every merged update, which is real per-super-step CPU cost, and it will raise a hard `ValidationError`
that propagates as an uncaught node exception (§11.3) if any node ever returns a value outside the
declared constraints — behavior you want for a field like `confidence` where an out-of-range value is
a real bug worth surfacing loudly, and behavior you don't want for a scratch field being iterated on
during early development where strict validation just slows down experimentation. Dataclasses sit
between the two: no validation, but real attribute access (`state.messages` instead of
`state["messages"]`) if that ergonomic matters to the team. The senior-engineer answer to "which one
should I use" is a real tradeoff, not a default: `TypedDict` for the common case and anything
performance-sensitive, Pydantic specifically for state fields where a malformed value is a
correctness bug you want caught at the point of writing rather than the point of eventual failure —
most commonly, structured output from an LLM call that a downstream node trusts implicitly.

### 3.9 Managing unbounded message growth: trimming and summarization

`add_messages`'s append-forever default (§3.2) is exactly right for correctness — nothing is lost —
and exactly wrong for a long-running thread's token budget: a support conversation that runs for two
hundred turns eventually has a `messages` list that alone exceeds the context window before the model
even sees the system prompt or the current question, a direct instance of
[`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md)'s budget problem playing out
inside a single state field. There are three standard mitigations, and they compose rather than
compete.

**Trimming at call time, not in state.** The simplest and most common: leave the full history in
checkpointed state (you may need it for audit, for time travel, for a human reviewing the whole
conversation) but pass only a trimmed window to the actual model call inside the node, using
LangChain's `trim_messages` utility or an equivalent hand-rolled window:

```python
from langchain_core.messages import trim_messages

def call_model(state: State) -> dict:
    trimmed = trim_messages(
        state["messages"],
        max_tokens=8000,
        strategy="last",
        token_counter=llm,
        include_system=True,
    )
    return {"messages": [llm.invoke(trimmed)]}
```

This keeps state as the full, honest record while bounding what any single model call actually pays
for — the checkpoint stays large, but the token bill doesn't grow with thread age.

**Summarization nodes that compact history into state.** For threads long enough that even "the last N
messages" loses important earlier context, a periodic summarization node replaces a prefix of the
message list with a single summary message, using `add_messages`'s replace-by-id capability (§3.2) or
an explicit `RemoveMessage` marker to delete superseded messages from state outright:

```python
from langchain_core.messages import RemoveMessage

def summarize_if_long(state: State) -> dict:
    if len(state["messages"]) <= 20:
        return {}
    to_summarize = state["messages"][:-10]
    summary = summarizer_llm.invoke([
        SystemMessage("Summarize this conversation concisely, preserving key facts and decisions."),
        *to_summarize,
    ])
    return {
        "messages": [RemoveMessage(id=m.id) for m in to_summarize] + [AIMessage(content=f"[Earlier summary: {summary.content}]")],
    }
```

Unlike call-time trimming, this actually shrinks what gets checkpointed going forward — the right
choice once the checkpoint's own storage cost (§5.6–5.7), not just the per-call token bill, starts
mattering, at the cost of the summarized detail being genuinely gone from the exact-record thread
history rather than merely omitted from one call.

**A separate, un-trimmed scratchpad for anything trimming must never lose.** Facts the graph relies on
structurally — a user's account ID, an approval decision, a running total — should live in their own
state fields rather than being buried in message text that a trimming or summarization pass might
compress away. This is the same private-state discipline from §3.6 applied specifically to guard
against your own context-management code: trim and summarize the conversational transcript freely,
because it exists for the model's benefit, but never let a fact the *graph's own logic* depends on
exist only inside that transcript.

---

## 4. Conditional edges and routing

Routing is the mechanism by which a graph's execution path depends on runtime data rather than being
fixed at construction time, and it is expressed entirely as ordinary Python functions — no special DSL.

### 4.1 The routing function's signature and contract

A conditional edge's routing function takes the current state and returns a value used to look up the
next node (or nodes — see §4.4) in a mapping you provide:

```python
def route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    if state.get("needs_approval"):
        return "human_review"
    return "end"

graph.add_conditional_edges(
    "agent",
    route_after_agent,
    {"tools": "tools", "human_review": "human_review", "end": END},
)
```

The third argument — the mapping from return value to node name — is optional; if omitted, LangGraph
assumes the router's return value *is* the node name. Providing it explicitly is worth doing in
production code even when it feels redundant, because it decouples the router's internal vocabulary
("tools", "end") from the graph's actual node names, and because it gives you one place to see every
possible destination from a node without reading the router's implementation — a real readability win
once routers get past a two-way branch.

### 4.2 Intent routing

The most common production pattern is routing on classified intent — an upstream node (often a small,
fast model call, sometimes a rules-based classifier) tags the state with an intent, and the router
dispatches purely on that tag:

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    intent: str

def classify_intent(state: State) -> dict:
    result = classifier_llm.invoke(state["messages"][-1].content)
    return {"intent": result.intent}   # e.g. "billing", "technical", "escalate"

def route_by_intent(state: State) -> str:
    return state["intent"]

graph.add_node("classify", classify_intent)
graph.add_conditional_edges(
    "classify", route_by_intent,
    {"billing": "billing_agent", "technical": "tech_agent", "escalate": "human_handoff"},
)
```

This is the graph-native form of the same intent-routing problem query understanding solves for
retrieval; the difference is that here the router's output determines which *agent* runs, not which
*retrieval strategy* runs.

### 4.3 Error routing

Because a node's return value is just state, a node can catch its own exceptions and encode the
failure *as data* rather than letting it propagate, and the very next conditional edge can route on
that data:

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    error: str | None

def call_flaky_api(state: State) -> dict:
    try:
        result = flaky_client.call(state["messages"][-1].content)
        return {"messages": [AIMessage(content=result)], "error": None}
    except ApiError as e:
        return {"error": str(e)}

def route_on_error(state: State) -> str:
    return "fallback" if state.get("error") else "continue"

graph.add_conditional_edges("call_api", route_on_error, {"fallback": "fallback_node", "continue": "next_step"})
```

This pattern — catch inside the node, route on the caught state outside it — is preferable to letting
LangGraph's built-in `retry` policy (§11) handle every failure, because not every failure should be
retried: a malformed request should be routed to a repair node, not retried verbatim five times against
the same malformed input.

### 4.4 Multiple conditional edges from one node, and fan-out

A single node can have more than one `add_conditional_edges` call is *not* the pattern — you attach
exactly one router per node, but that router can return any of an arbitrary number of destination
keys, including a list of destinations for parallel fan-out:

```python
def fan_out_to_tools(state: State) -> list[str]:
    # dispatch to every applicable tool node in parallel, not just one
    return [call.name for call in state["messages"][-1].tool_calls]

graph.add_conditional_edges("agent", fan_out_to_tools, {
    "search": "search_tool", "calculator": "calc_tool", "lookup": "lookup_tool",
})
```

Returning a list of keys causes LangGraph to schedule every named destination node to run in the same
super-step, concurrently — the mechanism underlying parallel tool calls (§9.3) and the map step of a
map-reduce graph (§13.3's `Send` API is the more general, dynamic version of this same idea, useful
when the number of parallel branches is only known at runtime rather than being a fixed set of named
nodes).

### 4.5 How much logic belongs in a router versus a node

The discipline worth stating explicitly, because §17 calls out its violation as an anti-pattern: a
router should be **cheap and pure** — read state, return a string, nothing else. Any router that calls
an LLM, hits a database, or has a side effect has smuggled a node's worth of work into a place the
graph's execution model doesn't checkpoint separately and doesn't retry separately. If deciding where
to go requires real computation (classification, a lookup), do that computation in a node, store the
result in state, and let the router be the one-line `return state["decision"]` that reads it back out.
This separation is also what makes a graph's structure legible from `add_conditional_edges` calls
alone — a graph you can reason about by reading its edges is a graph whose routers do no work.

### 4.6 `Command` as a routing mechanism in its own right

Everything above describes routing as a separate function attached via `add_conditional_edges`. An
alternative, increasingly common style is to let a node determine its own next hop directly, by
returning a `Command(goto=..., update=...)` instead of a plain dict — collapsing "compute the update"
and "decide where to go" into one return value from one function, with no separate router registered
on the graph at all:

```python
from langgraph.types import Command
from typing import Literal

def agent(state: State) -> Command[Literal["tools", "__end__"]]:
    response = llm_with_tools.invoke(state["messages"])
    if response.tool_calls:
        return Command(goto="tools", update={"messages": [response]})
    return Command(goto=END, update={"messages": [response]})

graph.add_node("agent", agent)
graph.add_edge(START, "agent")
# no add_conditional_edges call at all — "agent" declares its own destination
```

This is not a different capability from a conditional edge — it is the same routing decision expressed
inside the node rather than beside it — and the choice between the two styles is genuinely a matter of
where you want the decision to be legible. A separate `add_conditional_edges` call keeps every
possible destination visible at the point the graph is *assembled*, which is valuable when several
people maintain the graph's wiring and want to see the full topology without reading every node's
body; inline `Command` routing keeps the decision next to the logic that produces the data it depends
on, which is valuable when the routing condition is tightly coupled to what the node just computed (as
it typically is in multi-agent handoff, §13.2–§13.5, where a worker's own `Command(goto="supervisor")`
is simpler to read at the worker's definition than as a separate router that would otherwise have to
recompute or re-inspect the same result). Mixing both styles in one graph is normal and not a
code smell by itself — what matters, per §4.5, is that wherever the decision lives, it stays cheap and
readable at that location.

---

## 5. Checkpointing and persistence: the killer feature

If there is one section of this document to have flawless recall of in an interview, it is this one.
Every other feature of LangGraph is a reasonable design choice a competent team could have converged
on independently; checkpointing is the feature that actually explains why LangGraph won adoption over
hand-rolled agent loops, because building a correct, crash-safe, resumable version of it yourself is
weeks of work that has nothing to do with your actual product.

### 5.1 What gets checkpointed, and when

A **checkpoint** is a full snapshot of the graph's state, taken automatically after every super-step
(every node execution, or every set of concurrently-executing nodes at the same graph "layer"). You do
not opt into this per node — attaching a checkpointer at compile time makes *every* node's completion a
durable point:

```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()
app = graph.compile(checkpointer=checkpointer)
```

`MemorySaver` keeps checkpoints in an in-process dict — useful for development and tests, useless the
moment the process restarts. Production deployments use a persistent backend:

```python
from langgraph.checkpoint.sqlite import SqliteSaver

with SqliteSaver.from_conn_string("checkpoints.db") as checkpointer:
    app = graph.compile(checkpointer=checkpointer)
```

```python
from langgraph.checkpoint.postgres import PostgresSaver

DB_URI = "postgresql://user:pass@host:5432/langgraph"
with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    checkpointer.setup()          # creates the checkpoint tables on first use
    app = graph.compile(checkpointer=checkpointer)
```

`PostgresSaver` (and the async `AsyncPostgresSaver`) is the standard production choice: it survives
process restarts, is shared across horizontally-scaled API instances, and gives you SQL access to the
checkpoint history for debugging and analytics. `SqliteSaver` is the right middle ground for a
single-process service or a local tool that still needs persistence across restarts. There is also a
Redis-backed checkpointer maintained as a separate package for teams that already run Redis and want
sub-millisecond checkpoint writes.

### 5.2 Threads: the unit of persistence

A checkpoint is not global — it belongs to a **thread**, identified by `thread_id`, which you pass in
the `config` on every invocation:

```python
config = {"configurable": {"thread_id": "user-42-session-7"}}
app.invoke({"messages": [HumanMessage("Hi")]}, config)
app.invoke({"messages": [HumanMessage("What did I just say?")]}, config)   # same thread: full history available
```

Every checkpoint under a `thread_id` forms a linear (or, after time-travel edits, branching) history —
conceptually identical to a git log for that conversation. This is what makes LangGraph naturally
multi-tenant: `thread_id` is the isolation boundary, and it is your application's job (per
[`16-multi-tenancy-and-isolation.md`](16-multi-tenancy-and-isolation.md)) to derive it from something
you actually trust — a session ID tied to an authenticated user, never a client-suppliable value taken
at face value, because a `thread_id` collision or forgery is a cross-user state leak, not a cosmetic
bug.

### 5.3 Resuming from a checkpoint

The single most important consequence of thread-based persistence: invoking the graph again with the
*same* `thread_id` and *no new input relevant to where it left off* resumes exactly where the last
invocation ended, because `app.invoke(None, config)` (passing `None` as input) tells LangGraph "don't
add anything new, just continue running from the last checkpoint." This is the mechanism §6's
human-in-the-loop pauses rely on: the graph stops at an interrupt, the process can literally exit, and
hours later a new process with the same checkpointer and `thread_id` calls `app.invoke(None, config)`
and the graph continues as if no time had passed.

### 5.4 Checkpoint IDs and time travel

Every checkpoint has both a `thread_id` and a `checkpoint_id`; `get_state(config)` returns the latest
checkpoint for a thread, and `get_state_history(config)` returns every checkpoint ever taken for that
thread, oldest first. Passing a specific `checkpoint_id` in the config lets you **replay from any
historical point** — not just the latest:

```python
history = list(app.get_state_history(config))
earlier_checkpoint = history[3]        # some checkpoint from three steps back
replay_config = {"configurable": {"thread_id": "user-42-session-7", "checkpoint_id": earlier_checkpoint.config["configurable"]["checkpoint_id"]}}
app.invoke(None, replay_config)        # re-runs forward from that historical state
```

This is "time travel": you can fork execution from any point in a thread's history, which is
invaluable for debugging ("what would have happened if the tool had returned X instead of Y") and for
building edit-and-retry UIs (a user edits an earlier message, and you resume from the checkpoint just
before that message rather than replaying the whole conversation).

### 5.5 Updating state without running a node: `update_state`

You can also directly patch a thread's state between invocations, which is how you implement "the
human corrected a value" without the correction going through a node at all:

```python
app.update_state(config, {"approved": True}, as_node="human_review")
```

The `as_node` argument matters: it tells the checkpointer to record this update as if it came from the
named node, which keeps routing that inspects "did `human_review` run" consistent, and it determines
which node's outgoing edges fire next when execution resumes.

### 5.6 What a checkpoint backend actually stores

It is worth knowing the shape of the persisted data, not just the API, because it comes up whenever
someone asks "how expensive is this" or "can I query it directly." A `PostgresSaver` (and the other
production backends) persist to roughly two logical tables: a **checkpoints** table, one row per
super-step per thread, storing a serialized snapshot of the full state at that point plus metadata
(the `checkpoint_id`, the parent `checkpoint_id` it followed, a `source` tag distinguishing a normal
step from an input or an update), and a **writes** table, one row per node's *pending or completed*
write within a super-step, which is what lets LangGraph implement at-least-once node retry
semantics — if a super-step has three nodes running concurrently and one fails after the other two
succeeded, their successful writes are already durably recorded and are not re-executed on retry, only
the failed one is. This is a meaningfully different (and cheaper) guarantee than re-running the entire
super-step from scratch, and it is the concrete mechanism behind the retry behavior described in §11.1
— retries are scoped to the node, not the super-step, because the writes table already has the
sibling nodes' results. State snapshots are serialized (by default via a fast msgpack-based scheme
LangGraph calls its serde layer, with a pluggable `JsonPlusSerializer` for compatibility) rather than
raw `pickle`, specifically so that a Postgres row is portable across process restarts and language
versions rather than tied to Python's pickle protocol version, and so state schemas that don't
round-trip cleanly through JSON (raw file handles, open database connections, un-serializable
closures) surface an explicit serialization error at checkpoint time rather than corrupting a pickle
stream silently — a real, concrete reason state should hold data, not live resources.

### 5.7 Other checkpoint backends, and pruning

Beyond `SqliteSaver` and `PostgresSaver`, a `MongoDBSaver` and a Redis-backed saver exist as
community/first-party-adjacent packages for teams already standardized on those stores, and the
interface contract (`put`, `get_tuple`, `list`) is stable enough that writing a custom backend against
an existing internal data store is a reasonable few-days project rather than a fork of LangGraph
itself, should your infrastructure make that the right call. Whichever backend, per §16.4, checkpoint
data is not self-pruning — a `PostgresSaver` table grows with every super-step of every thread
forever unless the operator adds retention. A simple, concrete policy: delete checkpoints for threads
with no activity in N days (or copy them to cold storage first, if a "resume this six-month-old
support ticket" requirement exists), keyed off the checkpoint metadata's timestamp, run as an ordinary
scheduled job against the checkpoint tables directly — there is no LangGraph-provided TTL mechanism as
of this writing, so this is squarely an operator responsibility, easy to overlook until the table's
size starts showing up in a slow-query report.

### 5.8 Why this is the killer feature

Compare the alternative: a hand-rolled agent loop that keeps its state in a Python variable in a
request handler. The moment that process restarts — a deploy, a crash, an autoscaler recycling the
pod — every in-flight conversation is gone, silently, with no way to know it happened. Rebuilding
persistence yourself means designing a state schema, a serialization format, a storage backend, and a
resume protocol, and getting the failure semantics right (what if the process dies *during* a
checkpoint write?) is genuinely hard. LangGraph's checkpointers give you all of that as a compile-time
flag, backed by battle-tested implementations, and the same mechanism doubles as your human-in-the-loop
primitive, your debugging tool, and your crash-recovery story. This is also the foundation of §14's
durable-execution claim: a workflow that checkpoints after every step and can resume from the last
checkpoint on any failure has, almost for free, most of what people mean by "durable execution" in the
Temporal/Step-Functions sense.

---

## 6. Human-in-the-loop: interrupt, resume, approve

Production agents that take consequential actions — sending an email, executing a trade, deleting a
record, spending money — need a point where a human can review and approve before the action
happens. LangGraph's checkpointing (§5) makes this a first-class, not-bolted-on capability, because
pausing mid-graph is just "stop after this checkpoint and don't automatically continue."

### 6.1 Static interrupts: `interrupt_before` / `interrupt_after`

The simplest form is declared at compile time, naming nodes the graph should pause before or after:

```python
app = graph.compile(
    checkpointer=checkpointer,
    interrupt_before=["execute_trade"],
)

config = {"configurable": {"thread_id": "t1"}}
app.invoke({"messages": [...]}, config)      # runs up to, but not including, execute_trade, then stops

# ... a human reviews the pending state via app.get_state(config) ...

app.invoke(None, config)                      # resumes, runs execute_trade
```

`interrupt_before` is used far more often than `interrupt_after` in practice, because the point of
interrupting is almost always "let me see what's about to happen before it happens," not "let me see
what just happened" (which `get_state` after a normal `invoke` already gives you without an
interrupt). Static interrupts are unconditional — the graph *always* stops there, every time — which
is right for "every trade needs sign-off" and wrong for "only trades over $10,000 need sign-off,"
which is what dynamic interrupts (§6.2) are for.

### 6.2 Dynamic interrupts: the `interrupt()` function

The more flexible and now-preferred mechanism is calling `interrupt()` *inside* a node, conditionally,
which pauses execution at that exact point only when the node decides to:

```python
from langgraph.types import interrupt, Command

def execute_trade(state: State) -> dict:
    if state["trade_amount"] > 10_000:
        decision = interrupt({
            "action": "approve_trade",
            "amount": state["trade_amount"],
            "symbol": state["symbol"],
        })
        if decision != "approved":
            return {"status": "rejected"}
    result = broker.execute(state["symbol"], state["trade_amount"])
    return {"status": "executed", "result": result}
```

Calling `interrupt(payload)` raises a special exception internally that LangGraph catches: it
checkpoints the state *as of that point in the node*, surfaces `payload` to the caller as the
invocation's result (with a distinguished `__interrupt__` marker), and halts. Critically, resuming
does not restart the node from its top — it resumes the node function from exactly the `interrupt()`
call, with `decision` bound to whatever value you resume with:

```python
result = app.invoke({"trade_amount": 15_000, "symbol": "ACME"}, config)
# result contains an interrupt payload: {"action": "approve_trade", "amount": 15000, "symbol": "ACME"}

app.invoke(Command(resume="approved"), config)   # decision == "approved" inside the node, execution continues
```

This "resume the function from the interrupt call" behavior relies on the node function being
re-executed from the start on resume with the interrupt call short-circuited to return the resume
value — which has an important corollary: **any side effect before the `interrupt()` call in that
node will run again on resume**, so a node that calls `interrupt()` should either put the side effect
after the interrupt, or make the pre-interrupt work idempotent. This is one of the sharper edges of the
feature and a very fair interview question ("what's a gotcha with dynamic interrupts").

### 6.3 The `Command` primitive

`Command` is the general mechanism for resuming a graph *and simultaneously updating state or
redirecting control flow*, not just supplying an interrupt's resume value:

```python
from langgraph.types import Command

# resume, and also patch some state as part of resuming
app.invoke(Command(resume="approved", update={"approver": "alice@corp.com"}), config)

# jump execution directly to a named node, bypassing normal edges (used heavily in multi-agent handoff, §13.2)
return Command(goto="supervisor", update={"result": "done"})
```

`Command(goto=...)` returned directly from a node is the mechanism multi-agent handoff patterns use in
§13 to let a worker node route control back to a supervisor (or to another worker) without the
supervisor needing a conditional edge that already knows every possible destination — the worker
decides its own next hop and encodes it in the return value.

### 6.4 Approval workflow, end to end

Putting it together, a realistic approval gate:

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    pending_action: dict | None
    approved: bool | None

def propose_action(state: State) -> dict:
    action = plan_action(state["messages"])
    return {"pending_action": action}

def human_gate(state: State) -> dict:
    decision = interrupt({"review": state["pending_action"]})
    return {"approved": decision == "approve"}

def execute(state: State) -> dict:
    if not state["approved"]:
        return {"messages": [AIMessage(content="Action cancelled.")]}
    result = run_action(state["pending_action"])
    return {"messages": [AIMessage(content=f"Done: {result}")]}

graph.add_node("propose", propose_action)
graph.add_node("gate", human_gate)
graph.add_node("execute", execute)
graph.add_edge("propose", "gate")
graph.add_edge("gate", "execute")
```

A UI layer sits between the first `invoke` (which returns the interrupt payload) and the second
`invoke` (with `Command(resume=...)`), typically as a webhook or polling loop that surfaces
`pending_action` to a reviewer and waits for their click before calling back in.

### 6.5 LangGraph Studio's breakpoints are the same mechanism, visually

It is worth being explicit that LangGraph Studio's "add a breakpoint on this node" feature (§15) is not
a separate capability with its own implementation — it is a thin visual layer over exactly
`interrupt_before`/`interrupt_after` (or the compiled graph's dynamic `interrupt()` calls, which Studio
also honors and surfaces as a paused run in its UI). Knowing this means a graph you author entirely in
code, with no Studio-specific configuration, is fully steppable and breakpoint-able the moment someone
opens it in Studio — there is no separate "make it debuggable in Studio" step, because debuggability
*is* the checkpoint-and-interrupt mechanism you already built for production human-in-the-loop, viewed
through a different client.

### 6.6 Editing state at a breakpoint before resuming

A realistic reviewer action is not always a binary approve/reject — often the human wants to *correct*
the pending action before it runs (fix a wrong parameter, adjust an amount) and then continue. This
combines `update_state` (§5.5) with a plain resume, and is worth writing out because it is a distinct
code path from the resume-with-a-decision pattern in §6.4:

```python
# reviewer edits the pending action directly, in place, before resuming
app.update_state(
    config,
    {"pending_action": {**current_pending_action, "amount": corrected_amount}},
    as_node="propose",
)
app.invoke(None, config)   # resumes from the interrupt with the corrected state already in place
```

The `as_node="propose"` argument matters here for the same reason it did in §5.5: it makes the
correction look, to any node inspecting execution history, like `propose` itself produced the corrected
value, which keeps downstream logic that might branch on "did propose run" or audit logs that record
"who/what set this field" consistent with the graph's actual semantics rather than introducing a
provenance gap between "what propose said" and "what the human actually approved."

---

## 7. Streaming: values, updates, messages, events, custom

Streaming in LangGraph is not one mode — it is five, each answering a different question about "what
do you want to see as the graph runs," and picking the wrong one is the most common cause of "why is my
UI not updating the way I expect."

### 7.1 `stream_mode="values"`

Emits the **full state** after every super-step. Simple to reason about, but wasteful for large state
— you get the entire `messages` list retransmitted after every single node, not just the delta.

```python
for chunk in app.stream({"messages": [...]}, config, stream_mode="values"):
    print(chunk["messages"][-1])
```

### 7.2 `stream_mode="updates"`

Emits only the **partial update each node returned**, keyed by node name — the delta, not the whole
state. This is the mode most production UIs actually want, because it tells you exactly which node
just ran and exactly what changed.

```python
for chunk in app.stream({"messages": [...]}, config, stream_mode="updates"):
    for node_name, update in chunk.items():
        print(f"{node_name} -> {update}")
```

### 7.3 `stream_mode="messages"`

Streams **individual LLM tokens** as they are generated inside any node, tagged with metadata about
which node and which chat model produced them — this is the mode behind a token-by-token typing effect
in a chat UI, and it requires the underlying model call inside the node to itself support streaming
(most chat model integrations do, transparently).

```python
for msg_chunk, metadata in app.stream({"messages": [...]}, config, stream_mode="messages"):
    print(msg_chunk.content, end="", flush=True)
```

### 7.4 `stream_mode="events"` (`astream_events`)

The most granular mode: a stream of fine-grained lifecycle events (`on_chain_start`, `on_chat_model_stream`,
`on_tool_start`, `on_tool_end`, and so on) across every runnable inside the graph, LangChain-wide, not
LangGraph-specific. This is what you reach for when you need to build a detailed execution trace UI —
"show me every tool call, every model call, every retry, in order, with timing" — and it is verbose
enough that most applications filter it down by event type or tag rather than consuming it raw.

```python
async for event in app.astream_events({"messages": [...]}, config, version="v2"):
    if event["event"] == "on_tool_start":
        print(f"calling tool: {event['name']} with {event['data'].get('input')}")
```

### 7.5 `stream_mode="custom"`

Lets a node emit **arbitrary application-defined payloads** mid-execution via `get_stream_writer()` —
useful for progress updates that aren't naturally a state delta or a token ("scraping page 3 of 12,"
"still waiting on the search API").

```python
from langgraph.config import get_stream_writer

def long_running_node(state: State) -> dict:
    writer = get_stream_writer()
    for i, page in enumerate(pages_to_scrape):
        writer({"progress": f"page {i+1}/{len(pages_to_scrape)}"})
        scrape(page)
    return {"scraped": True}
```

### 7.6 Streaming from nested graphs, and combining modes

You can pass a list of modes (`stream_mode=["updates", "messages"]`) to get an interleaved stream
tagged by mode, and streaming propagates through subgraphs automatically as long as the subgraph is
invoked as a normal node — by default a subgraph's *internal* node updates are not surfaced (you only
see the subgraph-as-a-whole's update), and you opt into seeing inside it with `subgraphs=True`:

```python
for chunk in app.stream(inputs, config, stream_mode="updates", subgraphs=True):
    print(chunk)   # now includes updates from nodes inside any subgraph, tagged by namespace
```

The practical rule of thumb worth stating in an interview: `updates` for a production UI driving
incremental state (what changed, cheaply); `messages` for a chat UI that wants token-level typing;
`events` for building an observability/trace view; `custom` for progress bars over long-running
non-LLM work; `values` mostly for debugging, rarely in production because of its retransmission cost.

### 7.7 The five modes, side by side

| Mode | What each chunk contains | Typical consumer | Cost profile |
|---|---|---|---|
| `values` | Full state snapshot after every super-step | Debugging, quick scripts | Retransmits entire state every step — expensive for large `messages` lists |
| `updates` | Only the delta each node returned, keyed by node name | Production UI showing incremental state changes | Cheap — proportional to what actually changed |
| `messages` | Individual LLM tokens, tagged with node/model metadata | Chat UI token-by-token typing effect | Proportional to tokens generated, fine-grained |
| `events` | Fine-grained lifecycle events across every runnable (`on_chain_start`, `on_tool_end`, etc.) | Observability/trace UI, custom span construction | Verbose — usually filtered by event type before use |
| `custom` | Arbitrary payloads a node emits via `get_stream_writer()` | Progress bars over non-token-shaped work | As cheap or expensive as you make each payload |

The column worth internalizing beyond the API mechanics is the cost profile — `values` is the one mode
where the cost of streaming grows with the *size of your state*, not with the size of what changed,
which is precisely why it is the wrong default the moment `messages` is more than a handful of turns
long: a twenty-turn conversation streamed under `values` retransmits roughly $O(n^2)$ total message
data across the whole run (the full history, resent after every single node), where `updates` is
$O(n)$ by construction.

---

## 8. Subgraphs: composing graphs out of graphs

A **subgraph** is a compiled `StateGraph` used as a single node inside a larger, parent graph. This is
the composition mechanism that keeps large agent systems from becoming one enormous, unreadable graph
definition, and it mirrors ordinary software composition: a subgraph is a function with a well-defined
input/output contract that the parent doesn't need to know the internals of.

### 8.1 The two ways to nest a graph

If the subgraph's state schema shares key names with the parent's (or is a strict subset), you can add
the compiled subgraph directly as a node — LangGraph handles passing the relevant keys in and merging
the relevant keys back automatically:

```python
sub_app = subgraph_builder.compile()          # a fully independent, compiled StateGraph

parent_graph = StateGraph(ParentState)
parent_graph.add_node("research_subgraph", sub_app)   # used directly as a node
parent_graph.add_edge(START, "research_subgraph")
parent_graph.add_edge("research_subgraph", "synthesize")
```

If the subgraph's schema is genuinely different from the parent's — different field names, a narrower
or differently-shaped state entirely — you wrap it in an ordinary node function that does the
translation explicitly, invoking the subgraph yourself and mapping its output back into the parent's
schema:

```python
def run_research_subgraph(state: ParentState) -> dict:
    sub_result = sub_app.invoke({"query": state["user_question"]})
    return {"research_notes": sub_result["findings"]}

parent_graph.add_node("research", run_research_subgraph)
```

The explicit-wrapper form is the one to reach for whenever the subgraph was designed independently
(perhaps by a different team, perhaps reused across several parent graphs with different state shapes)
— it keeps the translation logic visible and testable in one place instead of relying on
implicit key-matching that breaks silently if either schema changes.

### 8.2 State mapping and isolation

A subgraph run via the direct-nesting form shares checkpointing with the parent under the hood — a
single `thread_id` covers the whole nested execution, and `get_state_history` with `subgraphs=True`
lets you see checkpoints from inside the subgraph too. This matters for human-in-the-loop: an
`interrupt()` called inside a subgraph node pauses the *entire* parent invocation, not just the
subgraph, and resuming resumes the whole nested structure from that exact point. Subgraph state is
otherwise isolated — fields private to the subgraph's own schema are not visible to the parent unless
explicitly mapped out, which is the same input/output-schema discipline from §3.6 applied one level up.

### 8.3 When to use a subgraph versus one large graph

Reach for a subgraph when a coherent chunk of the workflow has its own natural retry/error boundary,
its own team ownership, or its own reuse case — a "research" subgraph that does query decomposition,
multi-source retrieval, and fusion is a good subgraph because it's independently testable and reusable
across multiple parent agents (a customer-support agent and an internal-analyst agent might both embed
it). Do not reach for a subgraph purely to make a large graph "look" more organized — nesting adds a
state-mapping seam that must be maintained, and a flat graph of fifteen well-named nodes is more
debuggable than three subgraphs of five nodes each if there is no real reuse or ownership boundary
driving the split. The test to apply, matching this repo's general anti-pattern discipline: would this
chunk of the graph ever be invoked, tested, or deployed independently of the rest? If yes, subgraph. If
the honest answer is "no, it's just always run inline as step 4 of 9," it's a node, or a few nodes, not
a subgraph.

---

## 9. Tool calling with LangGraph: ToolNode and the agent loop

Tool calling is where LangGraph and the underlying model provider's function-calling API meet, and
`ToolNode` is LangGraph's prebuilt, opinionated implementation of the "execute whatever tools the model
asked for" half of the loop.

### 9.1 The loop, restated precisely

The canonical agent loop is: the LLM is given a message history and a list of tool schemas; it either
returns a plain text response (done) or a response containing one or more `tool_calls` (name,
arguments, and a call `id`); if there are tool calls, each is executed and its result is appended to
the message history as a `ToolMessage` carrying the matching call `id`; the augmented history goes back
to the LLM; repeat until a plain text response comes back or a limit is hit (§12). This is precisely
the graph from §2's skeleton: an `agent` node that calls the LLM, a `tools` node that executes calls, a
conditional edge that checks for `tool_calls` on the last message, and a back-edge from `tools` to
`agent`.

### 9.2 `ToolNode`

`ToolNode` is a prebuilt node that takes a list of tool functions (typically decorated with
LangChain's `@tool`), inspects `state["messages"][-1].tool_calls`, executes each named tool with its
arguments, and returns the results as `ToolMessage` objects with matching `tool_call_id`s — the entire
"tools" half of the loop in one prebuilt:

```python
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode, tools_condition

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return weather_api.lookup(city)

@tool
def search(query: str) -> str:
    """Search the web for a query."""
    return search_api.query(query)

tools = [get_weather, search]
tool_node = ToolNode(tools)
llm_with_tools = llm.bind_tools(tools)

def call_model(state: AgentState) -> dict:
    return {"messages": [llm_with_tools.invoke(state["messages"])]}

graph = StateGraph(AgentState)
graph.add_node("agent", call_model)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)   # prebuilt router: checks for tool_calls
graph.add_edge("tools", "agent")
app = graph.compile()
```

`tools_condition` is a matching prebuilt router that inspects the last message for tool calls and
returns `"tools"` or `END` — pairing it with `ToolNode` is literally the entire ReAct loop's control
flow in two prebuilt objects, which is exactly why `create_react_agent` (§10) can wrap this whole
pattern into a single call.

### 9.3 Parallel tool calls

When a model returns multiple `tool_calls` in one response (most current frontier models support this
natively — the model decided it needs both `get_weather("Paris")` and `search("Paris events")` to
answer one question), `ToolNode` executes all of them concurrently by default and returns all their
`ToolMessage` results in one update, relying on `add_messages`'s append semantics (§3.2) to fold them
all into the history correctly, keyed by their distinct `tool_call_id`s so the model can tell which
result answers which call.

### 9.4 Tool error handling

`ToolNode`'s default behavior on a tool raising an exception is to catch it and return a `ToolMessage`
containing the error text, rather than letting the exception propagate and crash the graph — this
lets the *next* LLM call see "tool X failed with error Y" and decide how to react (retry with different
arguments, try a different tool, apologize to the user) instead of the whole conversation dying. This
default can be disabled (`ToolNode(tools, handle_tool_errors=False)`) when you want failures to
propagate to LangGraph's node-level retry policy (§11) instead — the right choice depends on whether
the failure is something the *model* can reason about and route around (bad arguments, a business-rule
rejection) versus something purely infrastructural that a mechanical retry should handle (a transient
network timeout), and a mature system often wants both: infrastructural retries at the node level,
model-visible errors at the tool level for anything the model's next turn could plausibly fix.

```python
tool_node = ToolNode(
    tools,
    handle_tool_errors="Tool call failed. Check your arguments and try again.",
)
```

Passing a string instead of a bool sets a fixed fallback message rather than the raw exception text —
useful when you don't want internal error details (stack traces, internal hostnames) leaking into a
user-facing conversation.

### 9.5 `return_direct` and short-circuiting the loop

Some tools should end the turn immediately rather than sending their result back through another model
call — a tool that already produces a complete, user-ready answer (a canned-response lookup, a
final-form document generator) gains nothing from a redundant model call that just repeats or
lightly rephrases the tool's own output, and that extra round trip is pure latency and cost. Marking a
tool `return_direct=True` tells `ToolNode` (and `create_react_agent`, which checks the same flag) to
route straight to `END` after that specific tool executes, bypassing the normal tool-result-back-to-model
hop entirely:

```python
@tool(return_direct=True)
def escalate_to_human(reason: str) -> str:
    """Escalate this conversation to a human agent. Ends the conversation turn."""
    ticket_id = create_support_ticket(reason)
    return f"I've escalated this to our support team. Your ticket number is {ticket_id}."
```

This is a narrow tool but a real one: without it, a model that just produced a perfectly good final
answer via a tool call will frequently paraphrase or second-guess it in the following turn, occasionally
introducing an inconsistency between the tool's authoritative output and the model's rephrasing of it
— exactly the kind of avoidable failure mode worth eliminating structurally rather than trying to
prompt away.

### 9.6 Parallel tool calls in practice

When a model returns several `tool_calls` in one turn, `ToolNode` executes them concurrently by
default using the same async machinery as any other node (§2.2) — this is a real concurrency
consideration, not just a convenience, the moment two tools called together share a rate-limited
downstream dependency:

```python
@tool
async def get_stock_price(symbol: str) -> float:
    async with rate_limiter:                 # shared semaphore across all concurrent tool calls
        return await market_data_api.price(symbol)

tool_node = ToolNode([get_stock_price, get_weather, search])
```

If the model asks for `get_stock_price` on five symbols in one turn, all five run concurrently through
`ToolNode`, and without a shared rate limiter (or a `ToolNode`-level concurrency cap, where supported)
they can burst well past a downstream API's per-second limit in a way a sequential loop never would
have — the same lesson `../python-mastery/29-async-patterns-and-pitfalls.md` gives for bounded
concurrency generally, applied to the specific case of tool execution fan-out that the model, not your
code, decided the width of.

---

## 10. The ReAct pattern and create_react_agent

**ReAct** (Reason + Act, Yao et al. 2022) is the pattern of interleaving explicit reasoning with tool
invocation: the model reasons about what it needs, acts (calls a tool), observes the result, and
reasons again — the loop from §9.1 is a ReAct loop whether or not the model's reasoning is exposed as
visible "thought" text (modern function-calling models mostly do this reasoning implicitly rather than
emitting a literal "Thought:" line, but the control-flow shape — reason, act, observe, repeat — is
unchanged from the original paper).

### 10.1 `create_react_agent`

Because the ReAct loop's graph shape is so standard, LangGraph ships it as a one-line prebuilt:

```python
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

agent = create_react_agent(
    model=llm,
    tools=[get_weather, search],
    checkpointer=MemorySaver(),
    prompt="You are a helpful assistant. Use tools when you need current information.",
)

result = agent.invoke(
    {"messages": [HumanMessage("What's the weather in Paris and what's happening there this weekend?")]},
    {"configurable": {"thread_id": "t1"}},
)
```

`create_react_agent` returns a fully compiled `CompiledStateGraph` — not a black box distinct from
everything else in this document, but literally the `agent`/`tools` two-node graph from §9.2,
pre-wired, with a default state schema (`messages` plus, in current versions, support for a
`response_format` for structured final output and hooks for pre/post-model logic). Because it's a real
compiled graph, everything from earlier sections still applies: pass a checkpointer and you get
persistence (§5), pass `interrupt_before=["tools"]` and every tool call needs approval (§6), stream it
with any `stream_mode` (§7).

### 10.2 When ReAct is enough, and when it isn't

ReAct is the right default for the enormous class of problems that are genuinely "figure out what
information or action you need, get it, repeat" — customer support lookups, research assistants,
most tool-augmented Q&A. It starts to strain in three recognizable situations, each of which is a
signal to drop to a hand-written graph instead of the prebuilt. First, when the task has a **known,
fixed multi-stage shape** that isn't a loop at all — "always retrieve, then always summarize, then
always classify" doesn't need the model to decide what to do next at every step, and forcing it through
a ReAct loop spends tokens and latency on decisions that were never actually in doubt. Second, when you
need **structural control the prebuilt doesn't expose** — a custom routing condition beyond "does the
last message have tool calls," a human-approval gate placed somewhere other than before every tool
call, or two different models used for different phases. Third, and most commonly in production, when
you need **multiple cooperating agents** (§13) rather than one agent with many tools — the prebuilt is
single-agent by construction, and a supervisor/worker topology is a hand-assembled graph of
`create_react_agent`-produced subgraphs, not a configuration flag on the prebuilt itself. The honest
senior framing: `create_react_agent` is where you start for anything tool-using and single-agent, and
you should be able to articulate, concretely, the first requirement in your actual project that made
you drop to a hand-rolled `StateGraph` — if the answer is "no requirement yet, I just wanted more
control," that is itself a yellow flag for over-engineering (§17).

### 10.3 Customizing the prebuilt before abandoning it

Before reaching for a fully hand-rolled graph, it is worth knowing how much `create_react_agent` can
flex without giving up the prebuilt entirely, because several of the reasons people drop to
`StateGraph` are actually addressed by parameters the prebuilt already exposes. A custom
`state_schema` lets you carry the extra fields (§3) a real agent accumulates beyond `messages` — a
retry counter, a budget field — while still using the prebuilt's wiring:

```python
class CustomAgentState(TypedDict):
    messages: Annotated[list, add_messages]
    user_tier: str          # e.g. drives which tools are available, checked inside a tool

agent = create_react_agent(
    model=llm,
    tools=tools,
    state_schema=CustomAgentState,
)
```

`pre_model_hook` and `post_model_hook` let you splice logic immediately before or after the model call
inside the prebuilt's loop without rewriting the loop itself — the natural place to put §3.9's
trimming or summarization step (a `pre_model_hook` that trims `state["messages"]` before every model
call) or a guardrail check on the model's output (`post_model_hook` inspecting the response before it
re-enters the loop):

```python
def trim_before_call(state: CustomAgentState) -> dict:
    return {"llm_input_messages": trim_messages(state["messages"], max_tokens=8000, strategy="last", token_counter=llm)}

agent = create_react_agent(model=llm, tools=tools, pre_model_hook=trim_before_call)
```

And `response_format` (a Pydantic model passed to `create_react_agent`) gets you structured final
output — the agent's last turn is coerced to match the schema — without hand-writing the router logic
that would otherwise be needed to force a final structured-output call after the ReAct loop ends. The
practical implication: try these knobs first. If the actual gap is one of §10.2's three
structural reasons — a genuinely non-looping stage sequence, a routing condition no hook can express,
or a second cooperating agent — no amount of prebuilt configuration closes it and dropping to
`StateGraph` is the right call; if the gap is "I need one more field" or "I need to trim history," the
prebuilt already has a documented seam for exactly that.

---

## 11. Error handling and retry

Production graphs fail in three qualitatively different ways, and conflating them is the most common
error-handling mistake: **transient infrastructure failures** (a timeout, a 503, a dropped connection)
that a mechanical retry fixes; **model or tool logic failures** (bad arguments, an ambiguous request,
a business-rule violation) that no amount of identical retrying fixes and that need either a repair
step or a different approach; and **unrecoverable failures** (an auth error, a malformed graph, a bug)
that should surface loudly rather than being silently swallowed by either of the first two mechanisms.

### 11.1 `RetryPolicy` on nodes

LangGraph attaches retry behavior at the node level, at graph-construction time, for exactly the
transient-failure case:

```python
from langgraph.types import RetryPolicy

graph.add_node(
    "call_external_api",
    call_external_api_fn,
    retry=RetryPolicy(
        max_attempts=3,
        initial_interval=0.5,
        backoff_factor=2.0,
        retry_on=(ConnectionError, TimeoutError),   # narrow: don't retry every exception type
    ),
)
```

`retry_on` matters more than it looks: the default retries on a broad set of exceptions, but a node
that calls an LLM should almost never retry on, say, a content-policy refusal or a malformed-schema
`ValidationError` from the same request — retrying an identical request against a deterministic
rejection wastes attempts and latency for a certain repeat failure. Narrowing `retry_on` to genuinely
transient exception types is the difference between a retry policy that helps and one that just delays
an inevitable failure by `max_attempts × backoff`.

### 11.2 Fallback strategies

For failures a mechanical retry can't fix, the pattern is a fallback node reached via a conditional
edge (§4.3) rather than an in-node retry — the graph tries the primary path, catches and records the
failure as state, and routes to an alternate node:

```python
def call_primary_model(state: State) -> dict:
    try:
        return {"messages": [primary_llm.invoke(state["messages"])], "used_fallback": False}
    except (RateLimitError, ServiceUnavailableError):
        return {"used_fallback": True}

def call_fallback_model(state: State) -> dict:
    return {"messages": [fallback_llm.invoke(state["messages"])]}

graph.add_conditional_edges(
    "call_primary_model",
    lambda s: "fallback" if s.get("used_fallback") else "continue",
    {"fallback": "call_fallback_model", "continue": "next_step"},
)
```

This is the same "capacity-based fallback to a cheaper or different provider" pattern any production
LLM system needs, expressed as graph structure instead of a nested `try/except` — the benefit over a
plain `try/except` inside one function is that the fallback path is a first-class node with its own
retry policy, its own checkpoint, and its own visibility in traces and streamed `updates`.

### 11.3 What happens when a node throws uncaught

If a node raises an exception that isn't caught by its `RetryPolicy` (or has none), the graph
invocation raises that exception up to the caller — the checkpointer will have the last *successful*
checkpoint persisted, but not one for the failed node's attempt. This is deliberate: an uncaught
exception is a signal that the failure was not anticipated by the graph's design, and LangGraph's
answer is "surface it, don't guess a state to leave things in." Resuming after fixing the underlying
cause (a bad API key, a downstream outage) is exactly `app.invoke(None, config)` — the graph re-enters
at the failed node, since that's the point the last checkpoint precedes.

### 11.4 Graceful degradation as an explicit state field

A pattern worth naming: carrying a `degraded: bool` (or a more granular `degradations: list[str]`)
field in state that any node can set when it falls back to a lower-quality path (cached data instead of
live, a smaller model, a skipped enrichment step), and surfacing it in the final response or in
observability rather than pretending the degraded output is identical in quality to the happy path.
This mirrors [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) §7.4's
discipline of measuring quality under each degraded mode rather than assuming a fallback is "fine" —
the same principle applies to agent graphs: a fallback that silently ships a worse answer without
recording that it did is a debugging trap for whoever looks at production quality metrics next month
and can't explain a quality dip that correlates with an upstream outage nobody logged.

### 11.5 Circuit breakers and dead-lettering for a chronically failing dependency

`RetryPolicy` and a per-call fallback (§11.1–11.2) both assume the failure is transient at the scale of
one invocation. Neither protects you from a dependency that is down for the next twenty minutes,
during which every single thread that touches it pays the full retry-and-timeout cost before falling
back — multiplying an outage's user-facing latency impact across every concurrent conversation instead
of containing it. The fix is a circuit breaker: state (tracked outside any one thread, typically in a
shared cache like Redis rather than in per-thread checkpointed state, since the failure is a property
of the dependency, not of any one conversation) that trips after N consecutive failures and, once
tripped, makes the node skip straight to its fallback for a cooldown window without even attempting the
call:

```python
def call_flaky_service(state: State) -> dict:
    if circuit_breaker.is_open("flaky_service"):
        return {"messages": [AIMessage(content=cached_fallback_response())], "degraded": True}
    try:
        result = flaky_service.call(state["messages"][-1].content)
        circuit_breaker.record_success("flaky_service")
        return {"messages": [AIMessage(content=result)], "degraded": False}
    except ServiceError:
        circuit_breaker.record_failure("flaky_service")
        return {"messages": [AIMessage(content=cached_fallback_response())], "degraded": True}
```

For failures that are neither transient nor recoverable by any fallback the graph itself can execute —
a malformed request the graph genuinely cannot process, an action a human must resolve by hand — the
right disposition is a **dead letter**: route to a terminal node that records the failed thread's
state and reason to a queue or table for manual triage, and end the graph invocation cleanly, rather
than letting it either loop indefinitely trying variations that will never work or surface a raw
exception to whatever called it. This mirrors the same dead-letter-queue discipline any message-driven
system needs for poison messages, applied to a graph thread instead of a queue message.

---

## 12. Preventing infinite loops

A graph with a cycle can, definitionally, run forever, and an LLM that decides "let me try that tool
call again" is a more common cause of runaway loops than most people expect going in — the model isn't
malicious, it's just occasionally bad at recognizing that a tool call already failed for a reason that
retrying identically won't fix.

### 12.1 `recursion_limit`

The blunt, always-present backstop is `recursion_limit`, a config value capping the total number of
super-steps a single invocation may take before LangGraph raises `GraphRecursionError`:

```python
config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 25}
app.invoke({"messages": [...]}, config)
```

The default (25 at time of writing) is a super-step count, not a "conversation turn" count — a single
ReAct iteration (agent call, then tool call) is two super-steps, so 25 is roughly a dozen tool-call
round trips, which is generous for most tasks and exactly why hitting it in production is usually a
sign of a genuine loop rather than a legitimately long task. Treat a `GraphRecursionError` in
production logs as a bug report, not background noise to catch and ignore.

### 12.2 Detecting and breaking cycles deliberately

`recursion_limit` is a backstop, not a strategy — a well-designed graph should terminate for a
*reason* well before the limit, and that reason should be visible in state. The standard pattern is a
counter incremented by whichever node is the cycle's re-entry point, checked by the router that decides
whether to loop again:

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    tool_call_attempts: int

def call_model(state: State) -> dict:
    return {"messages": [llm_with_tools.invoke(state["messages"])]}

def route(state: State) -> str:
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "end"
    if state["tool_call_attempts"] >= 5:
        return "give_up"          # explicit termination, not a recursion error
    return "tools"

def run_tools(state: State) -> dict:
    result = tool_node.invoke(state)
    return {**result, "tool_call_attempts": state["tool_call_attempts"] + 1}
```

The `give_up` branch matters as much as the counter — it lets the graph terminate gracefully with a
message like "I wasn't able to complete this after several attempts" instead of a raw
`GraphRecursionError` bubbling to a user-facing 500.

### 12.3 Conversation turn limits and cost-based termination

For long-lived multi-turn threads (a support chat that could run for hours), a per-turn recursion
limit isn't enough — you also want a **cumulative** limit across the whole thread, which the
recursion limit (scoped to a single `invoke` call) does not give you. The pattern is a running counter
in state, incremented every turn, checked by an early node that can terminate or escalate the thread
once a ceiling is hit:

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]
    turn_count: int
    total_cost_usd: Annotated[float, operator.add]

def check_limits(state: State) -> dict:
    if state["turn_count"] > 50 or state["total_cost_usd"] > 5.00:
        raise ThreadLimitExceeded(state["thread_id"])
    return {"turn_count": state["turn_count"] + 1}
```

Tying termination to `total_cost_usd` rather than only to a step count is the right generalization for
any graph where nodes have wildly different costs (a cheap classifier node versus an expensive
multi-document synthesis node) — a step-count limit treats them as equivalent when the actual resource
you're protecting is spend, per [`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md).

### 12.4 Detecting a stuck loop directly, not just counting iterations

A counter catches "too many iterations" but not the more specific and diagnosable failure "the model
is repeating the *same* action expecting a different result" — a model calling `search("X")`, getting a
result it doesn't like, and calling `search("X")` again verbatim rather than trying a different query.
This is detectable directly, and detecting it lets you intervene earlier and more informatively than
waiting for a generic iteration cap:

```python
import hashlib

def call_model(state: State) -> dict:
    response = llm_with_tools.invoke(state["messages"])
    if response.tool_calls:
        call_signature = hashlib.sha256(
            str(sorted((c["name"], str(c["args"])) for c in response.tool_calls)).encode()
        ).hexdigest()
        recent = state.get("recent_call_signatures", [])
        if call_signature in recent:
            return {
                "messages": [AIMessage(content="I've tried this exact request before without success. Let me try a different approach or ask for clarification.")],
            }
        return {"messages": [response], "recent_call_signatures": (recent + [call_signature])[-5:]}
    return {"messages": [response]}
```

Hashing the tool name and arguments together and checking it against a short rolling window of recent
signatures catches an identical-repeat loop within one or two iterations rather than after a generic
recursion or turn limit finally trips, and it gives the model itself a nudge to change strategy — often
enough on its own to break the loop — before any harder termination mechanism has to intervene. This is
a genuinely different failure signature from "too many steps" and worth instrumenting separately in
production logs, because a spike in repeated-call detections localizes to a specific tool being
confusing or a specific prompt failing to communicate a tool's actual contract, which a raw iteration
count cannot tell you.

---

## 13. Multi-agent architectures

A single ReAct agent with many tools is not a multi-agent system — it's one agent with a wide toolbox,
and the distinction matters because the failure modes and design questions are different. Multi-agent
is warranted when the *reasoning itself* should be partitioned — different specialists with different
system prompts, different tool sets, or even different models, coordinating on a shared task — not
merely when there are many capabilities to expose.

### 13.1 Multi-agent versus multi-node

Every LangGraph graph is multi-node; not every multi-node graph is multi-agent. The distinguishing
property of a genuine multi-agent design is that more than one node in the graph is itself an
LLM-driven decision-maker with its own scope of authority and its own view of the state (often a
narrower, translated view, per §3.6/§8.2) — a "planner" node and an "executor" node that each call an
LLM with a different prompt and different tools is multi-agent; a single agent node followed by five
deterministic post-processing nodes is not, no matter how many nodes the graph has. This distinction is
worth stating precisely in an interview because "multi-agent" is overused as a buzzword for "graph with
several LLM calls in it" — the useful question to ask back is "does each of these nodes have its own
decision-making scope, or is it all one agent's reasoning spread across steps?"

### 13.2 The supervisor pattern

The dominant multi-agent topology: a **supervisor** node (an LLM call whose job is purely "given the
task and progress so far, which specialist should act next, or are we done") routes to one of several
**worker** nodes, each typically a `create_react_agent`-style subgraph with its own tools, and each
worker routes back to the supervisor when it finishes its piece rather than to a fixed next node:

```python
from langgraph.types import Command
from typing import Literal

members = ["researcher", "coder", "reviewer"]

def supervisor(state: State) -> Command[Literal["researcher", "coder", "reviewer", "__end__"]]:
    response = supervisor_llm.invoke([
        SystemMessage(f"You route between: {members}. Choose one, or FINISH if the task is done."),
        *state["messages"],
    ])
    next_ = response.content.strip()
    if next_ == "FINISH":
        return Command(goto=END)
    return Command(goto=next_, update={"messages": [response]})

def researcher(state: State) -> Command[Literal["supervisor"]]:
    result = researcher_agent.invoke({"messages": state["messages"]})
    return Command(goto="supervisor", update={"messages": result["messages"]})

graph = StateGraph(State)
graph.add_node("supervisor", supervisor)
graph.add_node("researcher", researcher)
graph.add_node("coder", coder)
graph.add_node("reviewer", reviewer)
graph.add_edge(START, "supervisor")
# note: no add_conditional_edges needed for the workers' return path — Command(goto=...) handles it
```

This is exactly the `Command(goto=...)` primitive from §6.3 doing double duty: each worker both
updates shared state (its contribution to the message history) and declares its own next hop, which is
what lets you add a fourth worker later by adding one node and one line in the supervisor's routing
prompt, without touching every worker's edges. The prebuilt `langgraph-supervisor` package wraps this
exact pattern (supervisor LLM plus a list of worker agents, each a compiled graph) into a
higher-level constructor for the common case, the same way `create_react_agent` wraps the single-agent
loop.

### 13.3 Hierarchical agents and dynamic fan-out with `Send`

Supervisors can themselves be supervised — a top-level supervisor routing to mid-level supervisors
that each coordinate their own workers — for systems large enough that one flat routing prompt would
have to choose among a dozen-plus specialists, which is both a prompt-quality problem (routing accuracy
degrades as the option set grows) and an organizational one (different teams own different
sub-supervisors). For cases where the *number* of parallel workers isn't known until runtime — "spawn
one research worker per sub-question, and there could be two or eight of them" — the `Send` API
provides dynamic fan-out that ordinary conditional edges (fixed set of named destinations, §4.4) can't:

```python
from langgraph.types import Send

def dispatch_subquestions(state: State) -> list[Send]:
    return [Send("research_worker", {"question": q}) for q in state["subquestions"]]

graph.add_conditional_edges("planner", dispatch_subquestions)
```

Each `Send` schedules a separate, concurrent invocation of `research_worker` with its own input,
and their results are merged back into shared state through whatever reducer the receiving key
declares (§3.4's `merge_documents`-style reducer is exactly the tool for merging N workers' findings).
This is LangGraph's map-reduce primitive, and it is the mechanism behind "decompose into subquestions,
research each in parallel, synthesize" agent designs.

### 13.4 Agent handoff and shared versus private context

A recurring design decision in any multi-agent graph: how much of the shared message history does each
worker see? Giving every worker the full history is simplest and works until the history gets long
enough that a worker's context window fills with other workers' irrelevant back-and-forth, or until a
worker's own scratchpad reasoning (not meant for other agents, let alone the user) leaks into the
shared thread. The alternative — narrower, translated state per worker via the wrapper pattern from
§8.1 — costs you a translation layer but keeps each worker's context focused and keeps internal
reasoning private, mirroring the input/output-schema discipline from §3.6 applied to agent-to-agent
communication instead of caller-to-graph communication. There is no universally correct answer; the
question to actually ask on a specific system is whether a worker's internal reasoning trace has ever
caused confusion or a wrong decision when another worker (or the supervisor) saw it verbatim — if yes,
narrow the interface.

### 13.5 The handoff-tool pattern: peer-to-peer agents without a central supervisor

The supervisor pattern is hierarchical — control always flows through one coordinating node. An
alternative, decentralized topology (the pattern behind the `langgraph-swarm` library) lets agents
hand off to each other **directly**, as peers, by exposing the handoff itself as a tool call: instead
of a supervisor deciding "researcher should go next," the researcher agent itself calls a
`transfer_to_coder` tool when it decides its part is done, and that tool's implementation is nothing
more than a `Command(goto="coder", update=...)`:

```python
from langchain_core.tools import tool
from langgraph.types import Command
from langgraph.prebuilt import InjectedState
from typing import Annotated

def make_handoff_tool(*, agent_name: str):
    @tool(f"transfer_to_{agent_name}")
    def handoff(state: Annotated[dict, InjectedState]) -> Command:
        """Hand off the conversation to another agent when your part of the task is done."""
        return Command(
            goto=agent_name,
            update={"messages": state["messages"]},
            graph=Command.PARENT,   # hop out of this worker's own subgraph, into the parent graph
        )
    return handoff

researcher_agent = create_react_agent(
    model=llm,
    tools=[search_tool, make_handoff_tool(agent_name="coder")],
)
```

The interesting mechanism here is `Command.PARENT`: because each worker is typically its own
`create_react_agent` subgraph (§8), a `Command(goto=...)` returned from *inside* that subgraph would,
by default, only be able to route to nodes within the same subgraph. `graph=Command.PARENT` tells
LangGraph to resolve the destination against the *parent* graph's node names instead, which is exactly
what lets a tool call from inside the researcher's own ReAct loop redirect control all the way up to a
sibling agent. This pattern trades the supervisor's single point of routing judgment (and its single
point of routing failure) for a fully distributed one — each agent decides for itself when it's done
and who should take over — which fits naturally when the handoff logic is genuinely local ("I'm a
researcher, when I have findings I always hand to the coder") rather than requiring global judgment
across many possible next steps, and it removes one LLM call's worth of latency per hop, since there
is no separate supervisor turn between workers. It costs you the supervisor's centralized visibility
and the ability to add cross-cutting routing logic (rate limiting a specific worker, escalation rules)
in one place rather than duplicated across every worker's tool set — the same centralization tradeoff
that shows up choosing between a service mesh's centralized control plane and point-to-point service
calls.

### 13.6 Shared scratchpad versus per-agent memory

A second, orthogonal design axis to §13.4's shared-versus-private state question: does the multi-agent
system have one collective `messages` list every agent reads and appends to (the shared-scratchpad
model, simplest to reason about, used in every example above), or does each agent maintain its own
private conversational memory with only *summaries or final results* passed between agents (an
isolated-memory model, more like independent microservices exchanging structured payloads rather than
sharing a mutable log)? The isolated-memory model scales better to many agents and large individual
histories, because no agent's context window has to hold every other agent's exploratory
back-and-forth, but it requires explicit summarization logic wherever one agent's output feeds
another's input — you're back to writing the interface contract by hand, which is exactly the
input/output-schema translation from §8.1's explicit-wrapper subgraph pattern. The practical guidance:
start with the shared scratchpad for two or three agents doing a genuinely collaborative task where
seeing each other's reasoning is valuable context; move to isolated memory with explicit handoff
payloads the moment either an agent's context window is dominated by another agent's irrelevant
exploration, or the number of agents makes "everyone sees everything" an obvious quadratic cost
problem.

---

## 14. Durable execution: LangGraph versus workflow engines

"Durable execution" means a workflow's progress survives the process that's running it — a crash,
a redeploy, or a deliberate pause does not lose work already done, and resuming continues from the
last durable point rather than from scratch. LangGraph's checkpointing (§5) gives you a real, if
partial, version of this, and knowing precisely where it matches and where it falls short of
purpose-built workflow engines like Temporal or AWS Step Functions is a strong interview signal,
because the naive answer ("LangGraph is basically Temporal for agents") overclaims in a way anyone who
has operated Temporal in production will immediately catch.

### 14.1 What LangGraph gives you, mechanically

Every super-step boundary is a checkpoint (§5.1); a crash between checkpoints loses at most the
in-flight node's work, not the whole thread; resuming is `app.invoke(None, config)` against the same
`thread_id`, requiring no special crash-recovery code in the application. For the common failure mode
in agent systems — a process dies mid-conversation because of a deploy or an OOM — this is
substantial, real durability, and it is the reason a team building agents today does not need to
separately stand up a workflow engine just to survive routine infrastructure churn.

### 14.2 Where it stops short of Temporal-class guarantees

Temporal's execution model guarantees that a workflow's code re-executes deterministically on replay —
every activity call is recorded, and on recovery the workflow function re-runs from the top with
already-completed activities returning their recorded results instantly, which lets Temporal recover
from a failure at *any* point inside a workflow function, not just at pre-declared boundaries.
LangGraph's checkpoint granularity is the **node**, not arbitrary lines of code — a node that makes
three sequential API calls and crashes after the second has no checkpoint between call one and call
two; on resume, the whole node re-runs from its start (subject to the same idempotency caveat as
§6.2's interrupt gotcha). If sub-node durability matters for a particular node — expensive or
side-effecting steps within it — the answer is to split that node into smaller nodes so the
checkpoint boundary lines up with the durability boundary you actually need, not to expect LangGraph to
infer it. Temporal also gives you exactly-once *activity execution* semantics via its task-queue and
history mechanism, worker-fleet-wide durable timers (sleep for 30 days, cheaply, across worker
restarts), and cross-language workflow definitions; none of that is what LangGraph is trying to be —
LangGraph is an in-process (or thin-server, §15) graph runtime with pluggable persistence, not a
distributed workflow orchestration platform with its own execution history service and worker fleet
model.

### 14.3 Exactly-once semantics, honestly

Neither LangGraph nor most naive Temporal usage gives you exactly-once *side effects* for free — a node
that calls `charge_credit_card()` and crashes after the charge succeeds but before its return value is
checkpointed will, on resume, re-run the node and charge the card again, exactly as Temporal's docs are
explicit that a non-idempotent activity retried after a partial failure has the same hazard. The fix in
both worlds is the same and is not framework-provided: idempotency keys on the side-effecting call
itself (pass a stable request ID derived from `thread_id` plus a node-local sequence number to the
payment API, and let the payment API's own idempotency guarantee do the actual work), because no
orchestration layer can make a non-idempotent external system idempotent from the outside. This is the
single most important caveat to raise unprompted if an interviewer asks about durable execution and
crash safety — a shallow answer stops at "checkpointing prevents lost work"; a strong answer adds "and
it does not by itself prevent duplicated work for non-idempotent side effects, which needs
idempotency keys regardless of orchestration layer."

### 14.4 When to actually reach for Temporal instead of (or alongside) LangGraph

Reach for Temporal (or Step Functions, or a comparable engine) when the durability requirement extends
well beyond a single conversational thread's lifetime — multi-day or multi-week workflows with durable
timers, workflows that must survive the LangGraph application's own deployment being retired entirely
rather than just restarted, or workflows whose activities are called from multiple languages/services
that don't share a checkpoint store. A pattern seen in mature production systems is LangGraph *inside*
a Temporal activity — Temporal owns the long-horizon durability and cross-service orchestration, and
inside one activity, a LangGraph graph handles the LLM-specific reasoning loop for as long as that
particular activity needs to run. That's not redundancy; it's each tool doing the part it's actually
good at.

### 14.5 The comparison, stated plainly

| Property | LangGraph | Temporal | AWS Step Functions |
|---|---|---|---|
| Checkpoint/recovery granularity | Per super-step (per node) | Per event in workflow history — any line | Per state transition |
| Durable timers (sleep for days/weeks, cheaply) | No native equivalent | Yes, first-class | Yes, via `Wait` states |
| Cross-language workflow definitions | No (Python/JS client libraries, same process model) | Yes — workers in any supported language share history | Yes — states call any Lambda/service, language-agnostic |
| Exactly-once activity execution | No (idempotency is the caller's job, §14.3) | No (same caveat — non-idempotent activities can still double-run) | No (same caveat) |
| Built for | LLM reasoning loops: message state, tool calls, streaming | General-purpose long-running business processes | AWS-native service orchestration |
| Human-in-the-loop primitive | First-class (`interrupt`/`Command`, §6) | Possible via signals, not a first-class primitive | Possible via callback tasks, more manual |
| Native LLM/agent ergonomics (message reducers, token streaming, tool-call prebuilts) | Yes — purpose-built | None — you build it on top | None — you build it on top |

Reading the table honestly: the two are not competitors trying to solve the same problem with different
APIs, they specialize in opposite directions from a shared checkpoint-and-resume idea. LangGraph
narrows the general workflow-durability problem down to exactly the shape an LLM reasoning loop needs
and gets first-class agent ergonomics in exchange for giving up cross-language support, durable timers,
and the finer replay granularity a general workflow engine provides. Temporal (and Step Functions)
solve the general problem and require you to build every LLM-specific convenience — message reducers,
token streaming, tool-call execution — yourself on top, because they were never built with a chat
message in mind as a first-class type. The "use both" pattern in §14.4 is not a compromise; it's
recognizing that "orchestrate a long-lived, cross-service business process" and "run an LLM reasoning
loop for one bounded task" are genuinely different problems that happen to both want checkpointing,
and a system that needs both should not contort one engine to do both jobs badly.

---

## 15. LangGraph Platform, Server, and Studio

It is important to separate what is open-source and free (the `langgraph` Python/JS library covered in
every section above) from what is a hosted commercial product (LangGraph Platform), because "do you
need the Platform to use LangGraph" is a real decision point and the answer is no.

### 15.1 The library versus the platform

The **LangGraph library** (`pip install langgraph`) is everything discussed so far: `StateGraph`,
checkpointers, `ToolNode`, prebuilts. It is fully open source, runs anywhere you can run Python, and
requires no LangChain-operated service. Plenty of production deployments are exactly this: a
compiled graph, a `PostgresSaver`, wrapped in a FastAPI (or comparable) service the team already
operates. **LangGraph Platform** is a separate, hosted (or self-hosted-license) offering built on top
of the library, adding three things the library alone doesn't provide: a managed **LangGraph Server**
that exposes a compiled graph as a REST/streaming API with authentication, horizontal scaling, and a
managed persistence layer, out of the box; **LangGraph Studio**, a visual debugger/IDE for stepping
through graph executions, inspecting state at each node, and editing-and-replaying from any checkpoint
without writing the `get_state_history` calls from §5.4 by hand; and integrated deployment tooling
(`langgraph.json` configuration, `langgraph deploy`, revisioning) for shipping graphs as versioned,
independently-scalable services.

### 15.2 What the Server actually solves

Standing up a production API around a LangGraph application yourself is not hard for a single graph
with one team's traffic, but it accumulates real work as it scales: authenticating and authorizing
requests, running the checkpointer's storage as a managed, backed-up service, exposing streaming
(§7) over HTTP correctly (SSE or websockets, with reconnect semantics), handling many concurrent
threads' worth of interrupts (§6) as pending, queryable state, and giving non-engineers (support
staff approving a human-in-the-loop action, a PM inspecting why an agent gave a bad answer) a way to
see and interact with a specific thread's state without shipping them a Python REPL. LangGraph Server
and Studio are, honestly, a reasonable buy-versus-build call at a certain team size and traffic level —
exactly the same calculus as buying a managed Kafka instead of running your own, and the correct
interview answer if asked "would you use LangGraph Platform" is a real tradeoff statement (operational
burden saved, versus vendor dependency and cost, versus the specific compliance/data-residency
constraints of the org), not a reflexive yes or no.

### 15.3 `langgraph.json` and local development parity

Platform deployments are configured through a `langgraph.json` manifest that names the graph(s) to
serve, the Python dependencies, and environment variables — and critically, the same manifest and the
same `langgraph dev` CLI command run the identical graph locally (backed by an in-memory or local
Postgres checkpointer) that Studio will visualize and that the Platform will eventually serve, so
"what runs in Studio during development" and "what runs in production" are the same compiled graph
object, not a separate simplified version — an explicit design goal that avoids the classic
dev/prod-parity failure mode of local mocks diverging from what actually ships.

### 15.4 Assistants: a graph plus configuration, versioned

A concept specific to the Platform's API worth knowing by name: an **Assistant** is not a different
kind of graph — it is a compiled graph plus a saved, named `configurable` payload (§16.1's
configuration mechanism), versioned independently of code deploys. The same underlying graph
("customer-support-agent") can back multiple assistants — "support-agent-v1-conservative" pinned to a
cheaper model with a strict system prompt, "support-agent-v1-experimental" pointed at a newer model
for a canary cohort — without duplicating a single line of graph code, because the only thing that
differs between them is the configuration record the Server looks up and injects at invocation time.
This is the Platform-level expression of the same principle §16.1 argues for at the code level:
behavior should be data, not a code fork, and Assistants is what makes that data independently
versioned, diffable, and rollback-able through the Platform's own API rather than through a
redeploy.

### 15.5 Background runs, cron, and webhooks

Because the Server exposes a graph as a stateful API rather than a request/response function, it
naturally supports invocation modes a plain synchronous API cannot: a **background run** starts a
graph invocation and returns immediately with a run ID the caller polls or subscribes to later — the
right shape for anything long enough that holding an HTTP connection open for the whole duration is
impractical (a multi-minute deep-research agent, for instance); a **cron-scheduled run** invokes a
given assistant against a given thread on a recurring schedule with no separate scheduler
infrastructure to operate (a daily "check for updates and summarize" agent); and a **webhook**
configured on a run fires an HTTP callback to your own service when that run reaches an interrupt or
completes, which is the production mechanism behind §6's human-in-the-loop UIs — rather than polling
`get_state` in a loop waiting for an interrupt to appear, your approval-review service registers a
webhook and gets pushed a notification the instant one occurs.

---

## 16. Production patterns: config, testing, observability, deployment

### 16.1 Configuration management

Graphs should treat model choice, temperature, tool availability, and feature flags as **runtime
configuration**, not hardcoded values baked into node closures, using LangGraph's `configurable`
mechanism (the same `config["configurable"]` dict that already carries `thread_id` and
`recursion_limit`):

```python
from langgraph.graph import StateGraph
from langchain_core.runnables import RunnableConfig

def call_model(state: State, config: RunnableConfig) -> dict:
    model_name = config["configurable"].get("model_name", "gpt-4o")
    temperature = config["configurable"].get("temperature", 0.0)
    llm = get_model(model_name, temperature=temperature)
    return {"messages": [llm.invoke(state["messages"])]}
```

This lets you A/B test models, roll out a new prompt to 5% of threads, or let a specific enterprise
customer pin an older model version, all without redeploying the graph — the graph's *structure* is
fixed at compile time, but its *behavior* per invocation is data.

### 16.2 Testing LangGraph applications

Test at three levels, matching the layers LangGraph itself exposes. **Node-level unit tests** call a
node function directly with a hand-constructed state dict and assert on the returned partial update —
no graph, no LLM call if the node's LLM client is injected and mockable, fast and specific about which
node broke. **Graph-structure tests** compile the graph and assert on its topology directly (which
nodes exist, which edges connect which, that a given router returns an expected destination for a
crafted state) without ever invoking a real model — catches wiring mistakes (a renamed node that a
conditional edge's mapping still points to the old name) that only show up at runtime otherwise.
**End-to-end scenario tests** invoke the compiled graph with a `FakeListLLM` or a recorded
cassette of real model responses (via a library like VCR-style HTTP replay) over one full turn or
conversation, asserting on the final state and, ideally, on the sequence of nodes visited (extractable
from `stream_mode="updates"`'s chunk keys) — this is what catches "the model's tool call routing
regressed" bugs that node-level tests structurally cannot see, and it is the graph-shaped analogue of
[`08-evaluation-methodology.md`](08-evaluation-methodology.md)'s golden-set discipline, run against
deterministic replayed model output rather than live model calls so the test suite doesn't flake on
model non-determinism or burn API budget in CI.

### 16.3 Observability with LangSmith

LangSmith (LangChain's tracing/observability product, usable independently of LangGraph Platform) is
the default way to get a trace tree — every node execution, every model call inside it with full
prompt/completion, every tool call and its latency, and the exact state at each checkpoint — with zero
code changes beyond setting `LANGCHAIN_TRACING_V2=true` and an API key, because LangGraph's runnables
already emit the same callback events every LangChain runnable does. For a team not using LangSmith,
`stream_mode="events"` (§7.4) is the raw material to build an equivalent trace view against your own
observability stack (OpenTelemetry spans per node and per model call is the natural mapping), matching
the general discipline from [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md)
of "a span per branch per stage" applied to a graph's nodes instead of a retrieval cascade's branches.
Concretely, wrapping node execution in a span is a thin decorator, and the payoff is that a graph's
trace shows up in whatever backend your organization already standardized on (Datadog, Honeycomb,
Jaeger) instead of requiring a second, LangSmith-specific observability surface:

```python
from opentelemetry import trace

tracer = trace.get_tracer("langgraph.agent")

def traced(node_name, fn):
    def wrapped(state):
        with tracer.start_as_current_span(node_name) as span:
            span.set_attribute("langgraph.thread_id", state.get("thread_id", "unknown"))
            result = fn(state)
            span.set_attribute("langgraph.state_keys_updated", ",".join(result.keys()))
            return result
    return wrapped

graph.add_node("agent", traced("agent", call_model))
```

For a `create_react_agent`-built graph where you don't control individual node registration, the same
outcome is reached by hooking `astream_events` (§7.4) once, at the top, and emitting one span per
`on_chain_start`/`on_chain_end` pair and one per `on_tool_start`/`on_tool_end` pair — strictly more
code than the LangSmith environment-variable toggle, and the right tradeoff exactly when the
organization's existing tracing backend, alerting, and on-call tooling are already built around
OpenTelemetry and a second, parallel observability tool for one class of service is a net operational
cost rather than a convenience.

### 16.4 Deployment strategies

Three shapes cover most production deployments. **Embedded**: the compiled graph runs in-process
inside your existing API service (a FastAPI route calls `app.invoke`/`app.astream` directly) — simplest
operationally, right when the graph is one component of a larger service that already has its own
deployment pipeline. **Standalone service**: the graph is its own deployable unit behind a thin API
layer, scaled independently from other services — right once the graph's resource profile (long-running
streaming connections, bursty LLM-bound concurrency) diverges enough from the rest of the stack that
sharing a deployment unit causes noisy-neighbor problems. **LangGraph Platform** (§15): right when the
team wants to buy rather than build the API/auth/scaling/Studio layer and the constraints (data
residency, vendor dependency) are acceptable. Whichever shape, the same checkpointer backend
(`PostgresSaver` almost always, in production) should be treated as a real piece of stateful
infrastructure — backed up, monitored for growth (checkpoint tables grow with every super-step of every
thread, and old threads need a retention/pruning policy same as any other operational data store), and
never swapped for `MemorySaver` outside local development, a mistake easy to make by leaving a default
in place past a prototype.

---

## 17. Anti-patterns

**Over-graphing a linear pipeline.** A three-step "retrieve, augment, generate" flow with no branching,
no retry logic that needs its own node, and no human gate gains nothing from being a `StateGraph` — it
gains a compile step, a state schema to maintain, and a less-readable control flow than
`retrieve(query) |> augment |> generate` would have been as a plain function composition. The tell:
if you can draw the graph and every node has exactly one outgoing edge and there is no cycle anywhere,
you have built an expensive way to write a function call sequence. Reach for `StateGraph` when a real
branch, cycle, or pause enters the picture, not preemptively.

**State explosion.** A state schema that has grown to twenty-plus loosely-related fields, several of
them nested and mutually exclusive ("only populated if `mode == 'x'`"), is a sign the graph is doing
too many unrelated things under one state object. The fix is usually either splitting into subgraphs
(§8) with narrower, purpose-fit state each, or recognizing that what looks like one graph is actually
two different workflows sharing accidental code, not accidental state.

**Putting decision logic in edges instead of nodes.** Per §4.5, a router that calls an LLM or performs
real computation has hidden a node's worth of work somewhere the graph's retry policy, checkpointing,
and streaming don't apply to it the way they would to a real node — that work is neither individually
retryable nor individually visible in `stream_mode="updates"`. Anything beyond "read a field, return a
string" belongs in a node whose output the router then reads.

**Not using reducers, or using the wrong default.** The most common concrete bug from §3.3: a state
field intended to accumulate (a list of tool results across parallel branches, a running cost total)
declared without a reducer, silently losing all but the last writer's contribution the first time two
branches genuinely run concurrently — invisible in sequential testing, visible only under real
production concurrency, and easy to misdiagnose as a flaky model rather than a state-merge bug.

**Treating `MemorySaver` as good enough for production.** Because it satisfies the same interface as
`PostgresSaver`, it is trivially easy to ship a service that "works in every test" and loses every
in-flight conversation on the first deploy, because nobody swapped the checkpointer before shipping.

**Building a multi-agent system where one agent with more tools would do.** Splitting reasoning across
several LLM-driven nodes multiplies latency (each hop is at least one more model call) and multiplies
the surface area for coordination bugs (a supervisor routing to the wrong worker, two workers stepping
on shared state) for a gain that, per §13.1, only exists if the specialists genuinely need different
prompts, tools, or models — "we have five tools so we made five agents" conflates a wide toolbox with a
genuine multi-agent need.

**No recursion or cost ceiling, or a ceiling that only catches the runtime error rather than causing a
graceful exit.** Relying on the default `recursion_limit` to eventually kill a runaway loop means every
runaway loop ends in an ungraceful `GraphRecursionError` surfaced to whatever called the graph, rather
than the graph itself recognizing, per §12.2, "I've tried this five times, time to give up cleanly."

**Ignoring the interrupt-resume idempotency gotcha (§6.2).** A node with side effects before an
`interrupt()` call that assumes the node runs exactly once will duplicate those side effects on every
resume — a bug that only appears in the human-in-the-loop path, which is disproportionately the path
guarding the most consequential actions in the system.

**Never pruning checkpoint storage.** Per §5.7, checkpoint tables grow forever by default; a team that
never revisits this discovers it as a slow-query incident or a surprised database-size alert months
into production rather than as a planned operational task, and by then the fix (backfilling a
retention policy against a much larger table) is more expensive than it would have been to design in
from the start.

**Deploying a state-schema change without a migration plan for in-flight threads.** Adding a required
field to a `TypedDict` state schema is free for *new* threads and silently breaks resuming *existing*
threads whose last checkpoint predates the field — the checkpointer will happily deserialize the old
state, the field will simply be absent, and the first node that unconditionally reads `state["new_field"]`
raises a `KeyError` on resume. The fix is the same discipline any schema change needs: make new
fields optional with a sensible default (`state.get("new_field", default)` in every reading node, or a
migration step that back-fills the field on the first read of an old checkpoint) until every thread
that predates the change has completed or been explicitly migrated — a graph redeploy is a schema
migration the moment it touches state, whether or not it is treated like one.

---

## 18. Interview questions, with weak and strong answers

**1. What problem does LangGraph solve that LangChain chains don't?**
Weak: "LangGraph is for agents, chains are for simple stuff." Strong: names the structural gap
directly — chains have no cycles and no persisted cross-step state without hand-rolling it; an agent
loop needs both, plus pause/resume, which is why LangGraph exists as a graph-and-checkpoint runtime
rather than an extension of the pipe operator (§1).

**2. What is a reducer, and why does it matter?**
Weak: "It's how you combine state." Strong: explains the default is overwrite, that `Annotated[Type,
fn]` attaches a custom merge function, that `add_messages` both appends and replaces-by-id, and gives
the concrete concurrency hazard — two parallel branches writing the same un-reduced key silently lose
one branch's contribution, a bug invisible under sequential testing (§3.2–3.3).

**3. Walk me through what happens when you call `graph.compile()`.**
Weak: "It builds the graph." Strong: validates every node is reachable from `START` and every declared
edge target exists, wires in the checkpointer and any static interrupts passed at compile time, and
returns a `CompiledStateGraph` implementing the standard LangChain `Runnable` interface, which is what
lets a compiled graph be used as a subgraph node elsewhere (§2, §8).

**4. How does checkpointing actually work — what's saved, and when?**
Weak: "It saves progress." Strong: a full state snapshot after every super-step, keyed by `thread_id`
and `checkpoint_id`, via a pluggable `BaseCheckpointSaver` backend (`MemorySaver` for dev,
`SqliteSaver`/`PostgresSaver` for production); explains that resuming is `invoke(None, config)` against
the same thread, and that `get_state_history` exposes every checkpoint for time-travel (§5.1–5.4).

**5. How would you implement a human-approval step before an irreversible action?**
Weak: "Add a confirmation dialog in the frontend." Strong: distinguishes static `interrupt_before`
(unconditional) from dynamic `interrupt()` inside a node (conditional on runtime data, e.g. amount
threshold), explains that the graph literally halts and checkpoints at that point, that resuming uses
`Command(resume=...)`, and flags the idempotency gotcha — any side effect in that node before the
`interrupt()` call re-runs on resume (§6.1–6.2).

**6. What's the difference between `stream_mode="values"` and `"updates"`?**
Weak: "One streams more than the other." Strong: `values` retransmits the full state after every
super-step; `updates` emits only each node's partial-update delta keyed by node name — and states the
practical consequence: `values` is wasteful for large state (e.g., long message histories re-sent every
step) and `updates` is what most production UIs actually want (§7.1–7.2).

**7. When would you use a subgraph instead of just adding more nodes to one graph?**
Weak: "To make it more organized." Strong: gives the concrete test — would this chunk ever be tested,
reused, or owned independently? If yes, subgraph, because it gets its own input/output schema boundary
and can be composed into multiple parents; if the honest answer is "no, it's just always inline," it's
plain nodes, because nesting adds a state-mapping seam with no matching reuse benefit (§8.3).

**8. How does tool calling actually work end to end in a LangGraph ReAct agent?**
Weak: "The LLM calls a tool." Strong: `.bind_tools()` attaches schemas to the model call; the model's
response may carry `tool_calls` with names, arguments, and call IDs; `ToolNode` executes each and
returns `ToolMessage`s carrying matching `tool_call_id`s; `add_messages` appends them; the augmented
history returns to the model; `tools_condition` is the router checking for tool calls to decide whether
to loop again or terminate (§9.1–9.2).

**9. What happens if a tool raises an exception?**
Weak: "The graph crashes." Strong: `ToolNode` catches it by default and returns the error as a
`ToolMessage` so the *next* model turn can see and react to the failure, versus disabling that
(`handle_tool_errors=False`) when you want the failure to hit the node's own `RetryPolicy` instead —
and the judgment call of which failures are model-recoverable versus purely infrastructural (§9.4).

**10. How do you prevent an agent from looping forever?**
Weak: "There's a recursion limit." Strong: names `recursion_limit` as the backstop but explains it
should rarely be what actually terminates a healthy graph — the real design is an explicit counter in
state checked by the router, with a graceful `give_up` branch, plus (for long-lived threads) a
cumulative cost or turn ceiling that a per-invocation recursion limit can't provide on its own
(§12.1–12.3).

**11. Explain the supervisor multi-agent pattern.**
Weak: "One agent bosses the others around." Strong: a supervisor node is itself an LLM call whose sole
job is choosing the next worker (or finishing); workers are typically their own `create_react_agent`
subgraphs; the `Command(goto=..., update=...)` primitive lets each worker declare its own return hop to
the supervisor without the supervisor needing to hardcode every worker's exit edge, which is what makes
adding a new worker a local change (§13.2).

**12. What's the difference between multi-agent and just having many tools on one agent?**
Weak: "Multi-agent has more agents." Strong: the distinguishing property is separate *decision-making
scope* — different prompts, tools, or models each reasoning independently — versus one agent choosing
among many tools with one reasoning process; conflating "many tools" with "needs multiple agents" is
called out explicitly as an anti-pattern because of the latency and coordination cost multi-agent adds
(§13.1, §17).

**13. Is LangGraph "durable execution" in the same sense as Temporal?**
Weak: "Yes, it checkpoints so it's durable." Strong: yes at the thread/super-step granularity — a crash
loses at most the in-flight node — but no at Temporal's granularity, which can recover deterministically
from *any* point inside a workflow function via replay, not just declared checkpoint boundaries; and
critically, neither gives you exactly-once side effects for free — non-idempotent external calls need
their own idempotency keys regardless of orchestration layer (§14.1–14.3).

**14. What's the difference between the LangGraph library and LangGraph Platform?**
Weak: "Platform is the paid version." Strong: the library (`StateGraph`, checkpointers, prebuilts) is
fully open source and sufficient for production on its own; Platform adds a managed Server (auth,
scaling, managed persistence), Studio (visual debugging/time-travel over `get_state_history`), and
deployment tooling — a buy-versus-build decision, not a required upgrade (§15.1–15.2).

**15. How do you test a LangGraph application without burning API budget or flaking on model
non-determinism?**
Weak: "Mock the LLM calls." Strong: layers it — node-level unit tests with hand-built state and a
mocked model client; graph-structure tests asserting on topology and router outputs with no model calls
at all; end-to-end tests against recorded/replayed model responses (`FakeListLLM` or cassette replay)
asserting on final state and the node-visitation sequence from `stream_mode="updates"` (§16.2).

**16. Your state has a `documents: list[dict]` field fed by three parallel retrieval branches. What
goes wrong if you don't give it a reducer, and how do you fix it?**
Weak: "Add `operator.add`." Strong: without a reducer, whichever branch's update merges last silently
overwrites the other two's contributions instead of failing loudly, because the default merge is
replace, not append; `operator.add` fixes the "keep everything" case but a real fix here needs
deduplication by document ID with score-based tie-breaking (§3.4's `merge_documents`), because a plain
concatenation reducer would let the same document appear three times from three branches (§3.3–3.4).

**17. Someone on your team wants to wrap a two-node "retrieve then generate" flow in LangGraph. Do
you push back?**
Weak: "Sure, it's a good pattern." Strong: yes — no cycle, no pause, no runtime-dependent branch means
no property LangGraph provides over a plain function composition is actually being used, and the graph
adds a state schema and a compile step for no behavioral gain; push back with the concrete question
"what will make this need to loop, branch, or pause?" and if there's a real near-term answer, that's
when to introduce the graph, not before (§17).

**18. How would you migrate a static `interrupt_before=["execute"]` compile-time interrupt to only
trigger for transactions over $10,000?**
Weak: "Add an if-statement somewhere." Strong: move from the static, compile-time interrupt list to a
dynamic `interrupt()` call inside the `execute` node itself, gated by the amount check, because static
`interrupt_before` is unconditional and has no way to see runtime state before deciding whether to
pause — the dynamic form is strictly more expressive and is why it's the currently preferred mechanism
(§6.1–6.2).

**19. What's the risk in a node that calls `interrupt()` partway through, after already calling a
payment API?**
Weak: "None, LangGraph handles it." Strong: on resume, LangGraph re-executes the node function from its
start with the `interrupt()` call short-circuited to the resume value — any code before that call,
including the payment API call, runs again, which double-charges unless that call is idempotent; the
fix is either moving side effects after the interrupt or making them idempotent via a request key
(§6.2, §14.3).

**20. How do you decide between `create_react_agent` and hand-writing a `StateGraph`?**
Weak: "Use the prebuilt when possible." Strong: start with the prebuilt for anything single-agent and
tool-using; drop to hand-written when the task has a fixed non-looping shape the prebuilt's
decide-every-step loop wastes tokens on, when you need a routing condition or interrupt placement the
prebuilt doesn't expose, or the moment a second cooperating agent enters the design — and can name
which of those three was the actual trigger on their own project rather than "just wanted more control"
(§10.2).

**21. What is `Send`, and how is it different from a normal conditional edge?**
Weak: "It's for parallel stuff." Strong: a normal conditional edge dispatches to a fixed, named set of
destinations known at graph-construction time (even if which one is chosen is dynamic); `Send`
dispatches a *dynamic number* of parallel invocations of the same node with different per-invocation
input, determined only at runtime — the map step of a map-reduce graph where the fan-out width isn't
known until the planner node runs (§13.3).

**22. Your production checkpointer table is growing without bound. What's the operational fix?**
Weak: "Add more disk." Strong: treats the checkpoint store as real stateful infrastructure needing a
retention policy — old, completed threads pruned or archived on a schedule, the same operational
discipline as any other data store growing with every request — and notes this is a `PostgresSaver`
operational concern that `MemorySaver` never surfaces in development, which is part of why testing only
against `MemorySaver` misses real production concerns (§16.4).

**23. Why does LangGraph require a reducer for concurrent writes instead of just running concurrent
nodes one at a time?**
Weak: "For performance, I guess." Strong: names the actual execution model — LangGraph's runtime is a
Pregel-style bulk synchronous parallel system, where every node in a super-step runs against a common
prior state and their updates are merged only once the whole super-step completes; that isolation is
what makes concurrent execution safe and fast, but it also means the runtime has no ordering
information to fall back on when two nodes touch the same key, which is exactly why a commutative,
associative reducer is the only well-defined way to combine them (§2.1, §3.3).

**24. When would you choose a Pydantic `BaseModel` over a `TypedDict` for your state schema?**
Weak: "Pydantic is more modern." Strong: `TypedDict` has zero runtime cost and is the right default;
Pydantic buys real validation at the state-merge boundary — catching a malformed value the instant a
node writes it rather than three nodes later when something finally chokes on it — which is worth the
per-super-step validation cost specifically for fields where an out-of-range or wrongly-typed value is
a correctness bug, most often structured LLM output a downstream node trusts implicitly (§3.8).

**25. You deploy a change that adds a new required field to your state schema. What breaks, and how do
you avoid it?**
Weak: "Nothing breaks, it's just a new field." Strong: existing in-flight threads' last checkpoint
predates the field, so resuming them deserializes state without it, and the first node that
unconditionally reads it raises a `KeyError`; the fix is treating it as a real schema migration — new
fields optional with defaults, or an explicit backfill on first read of an old checkpoint — until every
thread that predates the change has drained (§17).

**26. What is `Command.PARENT`, and what problem does it solve?**
Weak: "It routes to a parent node." Strong: in a handoff-tool multi-agent design (§13.5) where each
agent is its own `create_react_agent` subgraph, a `Command(goto=...)` returned from inside that
subgraph would by default only resolve against node names in that same subgraph; `graph=Command.PARENT`
tells LangGraph to resolve the destination against the parent graph instead, which is what lets one
agent's tool call hand off directly to a sibling agent one level up rather than being trapped inside
its own subgraph's node namespace.

**27. What actually gets persisted to the writes table versus the checkpoints table, and why does the
distinction matter for retries?**
Weak: "They're both just checkpoint data." Strong: the checkpoints table holds one full state snapshot
per super-step; the writes table holds each node's individual pending or completed write within a
super-step, and that separation is what lets a failed node in a multi-node super-step be retried alone
— its siblings' already-durable writes in the writes table are not re-executed, only the failed node's
work is redone, which is a materially cheaper and more precise retry guarantee than re-running the
whole super-step (§5.6).

---

## 19. Lab exercises

**Lab 1 — Build the ReAct loop from scratch, then compare it to `create_react_agent`.**
*Goal:* internalize §2, §9, and §10 by building the two-node agent/tools graph yourself before ever
calling the prebuilt. *Steps:* implement `AgentState`, `call_model`, a hand-written router (not
`tools_condition`), and `ToolNode` with two real tools; get it running and streaming with
`stream_mode="updates"`; then replace your hand-written graph with a single `create_react_agent` call
against the same tools and diff the behavior on five test prompts. *Artifact:* both implementations,
plus a short note on any behavioral difference you found (default system prompt, message formatting).
*Success criterion:* you can explain, without looking anything up, exactly which prebuilt is doing what
your hand-written version did line by line. *Time:* ~2 hours.

**Lab 2 — Reducer failure, reproduced on purpose.**
*Goal:* make §3.3's concurrent-write hazard a thing you've seen fail, not just read about. *Steps:*
build a graph that fans out to three nodes writing to a shared `results: list` field with no reducer;
run it enough times to observe non-deterministic loss of results depending on completion order; add
`operator.add`, confirm all three survive; then engineer a case where `operator.add` over-counts
(duplicate results from overlapping branches) and fix it with a dedup reducer like §3.4's
`merge_documents`. *Artifact:* a short script demonstrating all three states (broken, naively fixed,
correctly fixed) with printed output for each. *Success criterion:* you can point at the exact line
that caused data loss and explain why the reducer's algebra (commutative? idempotent?) matters.
*Time:* ~1.5 hours.

**Lab 3 — Checkpointing and crash recovery, for real.**
*Goal:* prove to yourself that `PostgresSaver` (or `SqliteSaver` if Postgres isn't handy) actually
survives a process restart, per §5. *Steps:* build a multi-turn graph with a checkpointer backed by a
file-based SQLite database; run it partway through a conversation in one Python process; kill the
process (not a graceful shutdown — `kill -9` or equivalent); start a fresh process pointed at the same
database and same `thread_id`; resume and confirm full history is intact. Then use
`get_state_history` to time-travel to an earlier checkpoint and fork a different continuation from it.
*Artifact:* a script demonstrating the kill-and-resume, plus one demonstrating time travel to a forked
branch. *Success criterion:* you can explain exactly what would have been lost with `MemorySaver`
instead, in concrete terms (which turns, which state). *Time:* ~2 hours.

**Lab 4 — A human-in-the-loop approval gate with the idempotency bug, then fixed.**
*Goal:* experience §6.2's gotcha directly rather than taking it on faith. *Steps:* write a node that
calls a (mocked, logging) "send_email" side effect *before* an `interrupt()` call inside the same node;
trigger the interrupt, resume it, and count how many times the mock email function actually fired.
Confirm it's more than once. Fix it by moving the side effect after the interrupt, and separately by
adding an idempotency key derived from `thread_id` and a step counter, and confirm both fixes
independently solve it. *Artifact:* the buggy version with its duplicate-send log, and both fixed
versions. *Success criterion:* a one-paragraph explanation of why LangGraph's resume semantics make
this an inherent risk rather than a bug in your code specifically. *Time:* ~1.5 hours.

**Lab 5 — Streaming modes, side by side.**
*Goal:* stop guessing which `stream_mode` to use and actually see the difference, per §7. *Steps:* take
one graph with at least three nodes including one that streams LLM tokens; run the identical invocation
under `values`, `updates`, `messages`, and `events`, capturing and printing the raw chunks for each;
measure the total bytes transmitted under `values` versus `updates` for a conversation with a 20-message
history. *Artifact:* a table of stream_mode versus what each chunk actually contains versus total bytes
for the same run. *Success criterion:* you can state, with the measured numbers, why `updates` and not
`values` is the production default for a chat UI. *Time:* ~1.5 hours.

**Lab 6 — A supervisor with three workers, then break it.**
*Goal:* build the multi-agent pattern from §13.2 and find its actual failure modes rather than assuming
it's robust. *Steps:* implement a supervisor and three worker subgraphs (each a small
`create_react_agent`) using `Command(goto=...)` for handoff; get a task requiring at least two workers'
contributions routed and completed correctly; then deliberately give the supervisor an ambiguous task
and observe what it does when routing is genuinely unclear (infinite ping-pong between two workers? A
wrong worker chosen confidently?); add the turn-limit pattern from §12.3 scoped to supervisor
hand-offs specifically, and confirm it terminates the ping-pong gracefully. *Artifact:* a trace of the
failure mode you found, and the fix. *Success criterion:* you found a real failure mode
experimentally, not the one described in this document — supervisor routing failures are
prompt-and-task-specific and yours will differ. *Time:* ~3 hours.

**Lab 7 — Recursion and cost ceilings, measured.**
*Goal:* turn §12's abstract advice into a graph that actually protects itself, per real numbers rather
than the default. *Steps:* instrument a ReAct agent with a per-node cost estimate (token count times
your model's per-token price) accumulated via an `operator.add`-reduced `total_cost_usd` field; craft a
prompt that reliably induces a long tool-calling loop (a tool that returns a subtly-wrong result the
model keeps retrying against); observe the default `recursion_limit` behavior (an ungraceful error);
then implement the graceful `give_up` branch from §12.2 and the cumulative cost ceiling from §12.3, and
confirm both trigger before the recursion limit does. *Artifact:* the induced-loop prompt, the raw
`GraphRecursionError` from the unprotected version, and the graceful termination message from the fixed
version. *Success criterion:* the fixed graph never surfaces a raw `GraphRecursionError` to a caller
for this scenario. *Time:* ~2 hours.

**Lab 8 — Durable-execution edge case: the non-idempotent side effect.**
*Goal:* make §14.3's "orchestration doesn't fix non-idempotent side effects" claim concrete. *Steps:*
build a node that calls a mocked non-idempotent "charge card" API; force a crash immediately after the
mocked charge succeeds but before the node returns (simulate with a raised exception right after the
mock call); resume the thread from its last checkpoint per Lab 3's method; observe the charge fire
twice. Then add an idempotency key (stable per `thread_id` + step) to the mock API and confirm a second
identical call is a no-op. *Artifact:* the double-charge reproduction and the idempotency-key fix.
*Success criterion:* you can explain why this would reproduce identically under Temporal without an
idempotent activity, i.e., that the fix is orchestration-agnostic. *Time:* ~1.5 hours.

**Lab 9 — Config-driven model swapping without redeploying the graph.**
*Goal:* implement §16.1's configuration discipline for real. *Steps:* take any graph with at least one
model-calling node; parameterize model name and temperature through `config["configurable"]` rather
than a hardcoded client; write a small harness that runs the same input against three different
`configurable` overrides (two models, two temperatures) without touching the compiled graph object; log
which config produced which output. *Artifact:* the harness and its comparison output. *Success
criterion:* you never re-imported or re-compiled the graph between configurations — only the config
dict changed. *Time:* ~1 hour.

**Lab 10 — The anti-pattern audit.**
*Goal:* apply §17 to a real graph rather than treating it as an abstract checklist. *Steps:* take
either the multi-agent system from Lab 6 or any LangGraph code you have from work, and audit it against
every item in §17 explicitly — is any router doing real computation instead of reading state; is any
state field un-reduced but written from a concurrent branch; is `MemorySaver` present anywhere outside
a test file; is there a graph in the codebase with no cycle and no conditional edge that could be a
plain function. *Artifact:* a short written audit, one line per anti-pattern, "present / not present /
n/a" with a one-sentence justification for each finding. *Success criterion:* you found at least one
real instance of something in the checklist — if the codebase is genuinely clean on every item, that
itself is worth stating and justifying explicitly, since it's the less common outcome. *Time:* ~1 hour.

**Lab 11 — State-schema migration across a live deploy.**
*Goal:* reproduce and fix §17's schema-migration anti-pattern rather than taking it on faith. *Steps:*
start a thread on a graph with a two-field state schema and checkpoint it with `PostgresSaver` or
`SqliteSaver`; stop the process partway through, before completion; change the code to add a new
required field with no default, redeploy, and attempt to resume the old thread — observe the
`KeyError` (or Pydantic `ValidationError` if using a `BaseModel` schema, per §3.8) on resume. Fix it two
ways independently: (a) make the field optional with a default and have every reading node use
`.get()`, and (b) write an explicit migration node that runs first and backfills the field if absent,
then confirm both let the old thread resume cleanly while a brand-new thread also works correctly.
*Artifact:* the failure reproduction and both fixes, with a one-paragraph note on which fix you'd
actually ship and why. *Success criterion:* the same code change (the new field) no longer breaks the
pre-existing thread under either fix. *Time:* ~2 hours.

**Lab 12 — Trace a multi-agent run end to end without LangSmith.**
*Goal:* build the OpenTelemetry-based observability path from §16.3 for real, so you understand what
LangSmith is giving you for free before defaulting to it. *Steps:* take the supervisor system from Lab
6; wrap every node (supervisor and all workers) with the `traced()` decorator pattern from §16.3,
emitting a span per node with `thread_id`, node name, and updated-keys attributes; run a task requiring
at least three handoffs; export spans to a local Jaeger or console exporter and confirm you can see the
full supervisor-to-worker-to-supervisor hop sequence as a single trace tree, correctly nested. Then
compare the same run's LangSmith trace (if you have access) and note what LangSmith shows that your
manual instrumentation didn't capture (typically: full prompt/completion payloads, token counts, cost).
*Artifact:* a screenshot or exported trace of both, and a short comparison. *Success criterion:* you
can explain concretely what LangSmith is buying you beyond what a few dozen lines of OpenTelemetry
wrapping gets you for free, which is the actual basis for a build-versus-buy decision on observability
tooling. *Time:* ~2.5 hours.
