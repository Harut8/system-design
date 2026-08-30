# 20 — LangChain architecture and internals

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the retrieval→generation pipeline
> as dataflow — LangChain is one concrete runtime for that dataflow, and this chapter is largely
> "here is how that abstract graph gets executed by a specific piece of software"),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (§6's chunking
> theory is what LangChain's text splitters implement; read the theory first so you can tell where the
> implementation falls short of it), [`03-indexing-and-vector-stores.md`](03-indexing-and-vector-stores.md)
> (LangChain's `VectorStore` interface is a thin adapter over exactly the systems that chapter covers),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (§5's RRF fusion is
> what `EnsembleRetriever` implements, and §7's cross-encoder rerank is what `ContextualCompressionRetriever`
> wraps — this chapter names the classes, that chapter has the theory),
> [`../python-mastery/29-async-patterns-and-pitfalls.md`](../python-mastery/29-async-patterns-and-pitfalls.md)
> (the `Runnable` protocol's `batch`/`abatch` are exactly the bounded-concurrency patterns that chapter
> covers, applied to a framework's internals instead of your own code).
>
> **Feeds into:** [`07-generation-and-structured-output.md`](07-generation-and-structured-output.md)
> (planned — `with_structured_output` and output parsers are the generation-side mechanism that
> chapter will formalize), [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md)
> (planned — LangChain's callback tree in §10 here is a proprietary pre-cursor to the OTEL GenAI
> semantic conventions that chapter covers; know both so you can explain why the industry moved),
> [`13-agents-and-tool-calling.md`](13-agents-and-tool-calling.md) (planned — §9's agent-loop mechanics
> and §8's tool-schema mechanics are the concrete substrate that chapter's abstract treatment of
> planning and idempotency sits on top of), [`19-build-vs-buy.md`](19-build-vs-buy.md) (planned —
> §10's LangSmith discussion and §14's alternatives survey are direct inputs to that build-vs-buy
> decision).
>
> **THESIS:** LangChain is not a retrieval system, a reasoning system, or an agent framework. It is a
> **composability layer** — a single protocol (`Runnable`) that every model, prompt, retriever, tool,
> and parser implements, so that `invoke`, `batch`, `stream`, and their async twins work identically
> regardless of which concrete class you're holding. The pipe operator is not syntax sugar for
> readability; it is operator overloading that builds a data structure (`RunnableSequence`) which
> *itself* implements the same protocol, which is the actual reason composition doesn't require special
> cases at every level.
>
> That protocol is the only part of LangChain worth learning at the internals level, because it is the
> only part that is hard to reimplement correctly yourself — getting streaming, batching, async, and
> callback propagation to compose transparently across arbitrary chain depth is genuinely fiddly
> plumbing. Everything built on top of it (specific chains, specific retrievers, specific memory
> classes) is a convenience wrapper you could write in an afternoon, and a large fraction of the
> framework's criticism — correctly — targets exactly those wrappers rather than the protocol
> underneath them. A senior engineer's job in an interview is to demonstrate they know which is which:
> defend the `Runnable` abstraction on its merits, concede the legacy `Chain` classes' problems without
> flinching, and be able to say precisely when the composability the protocol buys is worth its
> abstraction tax and when it manifestly is not.

---

## Contents

1. [Why LangChain exists](#1-why-langchain-exists)
2. [The component model: what LangChain actually ships](#2-the-component-model-what-langchain-actually-ships)
3. [The Runnable protocol](#3-the-runnable-protocol)
4. [LCEL: composition as an algebra over Runnables](#4-lcel-composition-as-an-algebra-over-runnables)
5. [Prompts: templates, placeholders, and structured output](#5-prompts-templates-placeholders-and-structured-output)
6. [Document loading and transformation](#6-document-loading-and-transformation)
7. [The retriever abstraction](#7-the-retriever-abstraction)
8. [Tools and function calling](#8-tools-and-function-calling)
9. [Agents: from AgentExecutor to LangGraph](#9-agents-from-agentexecutor-to-langgraph)
10. [Callbacks, tracing, and LangSmith](#10-callbacks-tracing-and-langsmith)
11. [Memory, and why it's being deprecated](#11-memory-and-why-its-being-deprecated)
12. [The package split: core, community, partner, and langchain itself](#12-the-package-split-core-community-partner-and-langchain-itself)
13. [Anti-patterns and the framework-lock-in criticism](#13-anti-patterns-and-the-framework-lock-in-criticism)
14. [LangChain versus the alternatives](#14-langchain-versus-the-alternatives)
15. [Interview-critical questions](#15-interview-critical-questions)
16. [Mental models — the compressed set](#16-mental-models--the-compressed-set)
17. [Lab exercises](#17-lab-exercises)

---

## 1. Why LangChain exists

Strip away everything else and an LLM API is a function: text (and maybe some structured messages)
goes in, text comes out, over HTTP, with a schema that differs by vendor in ways that seem trivial
until you're the one reconciling them. Before any framework, a team building an LLM application in
2022 was writing, by hand, for every single feature:

- A retry loop with backoff, because rate limits and transient 5xxs are not edge cases at this API's
  volume, they're Tuesday.
- A streaming handler, because the difference between "wait eight seconds then see a wall of text"
  and "see tokens appear" is a product requirement, not a nice-to-have, and streaming means
  server-sent-events parsing, partial-JSON handling, and a different code path than the non-streaming
  call.
- A way to inject conversation history and retrieved context into a prompt without string-concatenating
  themselves into an unmaintainable mess of f-strings.
- A way to get the model to call functions/tools, before any provider had first-class support for it —
  which meant prompting the model to emit a JSON blob, regex-extracting it, validating it, and retrying
  when it emitted almost-JSON.
- A way to swap providers (OpenAI to Anthropic, or a self-hosted model behind an OpenAI-compatible
  gateway) without rewriting every call site, because provider outages happen and procurement
  decisions change.
- Some way to see what actually got sent to the model when the output is wrong, which without
  tooling means print statements scattered through a codebase that nobody remembers to remove.

None of these problems are hard in isolation. All of them are duplicated, with small but consequential
variations, across every team building an LLM application, and duplicated *again* inside one team as
the number of call sites grows past a handful. That duplication is the actual argument for a
framework — not "LLMs are complicated" (the API surface of a single call is genuinely simple) but "the
scaffolding around a call site multiplies badly, and every team was independently reinventing it with
different bugs."

LangChain's answer, concretely, is: standardize the interface every component exposes (so retries,
streaming, and async are implemented **once**, in the base class, and inherited by every model,
retriever, and tool that anyone writes against that interface), and provide a large catalog of
pre-built integrations (so "call this vector database" or "call this document parser" is an import,
not a client library you learn from scratch). The first part — the standardized interface — is the
`Runnable` protocol covered in §3, and it is the part with real engineering substance. The second part
— the integration catalog — is `langchain-community` and the partner packages (§12), and it is where
most of the framework's line count lives, and also where most of its quality-control problems live,
because a catalog of hundreds of community-maintained wrappers around hundreds of third-party
services cannot possibly be uniformly excellent.

It's worth being precise about what LangChain is *not* solving, because conflating these causes a lot
of the framework's bad press. It does not make retrieval better (that's embedding models, chunking,
and fusion — `00`–`04`). It does not make an agent more likely to complete a task correctly (that's
prompt engineering, tool design, and evaluation — `08`, `13`, `14`). It does not make inference
cheaper or faster (that's the model, the provider, and caching — `11`, `12`). What it buys you is: the
same four verbs (`invoke`, `batch`, `stream`, plus their async forms) work on a chat model, a
retriever, a prompt template, an output parser, and any pipeline you build out of them, so the code
that calls a three-stage RAG pipeline looks exactly like the code that calls a single chat model, and
the runtime concerns (retries, streaming, concurrency, tracing) are handled by the protocol rather
than reimplemented at every call site. That is a real, if narrow, engineering win, and it is the win
this chapter is about.

**Why this matters specifically for a platform-engineering role rather than a product-engineering
one.** A product team shipping one chat feature against one provider rarely needs everything this
chapter covers — §13 says so plainly, and an interviewer respects that answer more than blanket
enthusiasm. A *platform* team's job is different by definition: it serves multiple internal consumers,
each of which may want a different model, a different retrieval strategy, or a different tool set, and
it is on the hook for uniform tracing, cost attribution, and reliability policy across all of them
without every consumer reimplementing retry logic and streaming handling from scratch. That is exactly
the shape of problem the `Runnable` protocol, `configurable_alternatives`, and a shared LangSmith (or
OTEL) tracing surface were built to address — not because LangChain is the only way to build such a
platform, but because "many callers, several providers, one consistent operational surface" is the
specific shape where a standardized composition protocol earns back its abstraction cost fastest. An
interview for a role that explicitly requires LangChain is very likely probing for exactly this
judgment: not "do you know the API," but "do you know when the platform-level abstraction is the right
tool, and can you say so with the same precision you'd use to say when it isn't."

---

## 2. The component model: what LangChain actually ships

LangChain organizes the LLM-application problem into a small number of component families, each
defined as an abstract base class in `langchain_core` with a common method surface, plus dozens to
hundreds of concrete implementations spread across `langchain`, `langchain-community`, and the
partner packages. Knowing the taxonomy matters because interview questions about "how does LangChain
do X" almost always reduce to "which base class does X and what's its contract."

**Models.** Three distinct base classes, easy to conflate but genuinely different contracts:
`BaseLLM` (`str` in, `str` out — the "text completion" shape, largely legacy now that essentially
every production model is chat-tuned), `BaseChatModel` (a list of `BaseMessage` in, an `AIMessage`
out — the shape essentially all real work uses today), and `Embeddings` (`embed_query(text) -> list[float]`
and `embed_documents(texts) -> list[list[float]]`, deliberately two methods rather than one, because
`01-embeddings-and-representation.md` §3's asymmetric-embedding distinction — query vs. document
encoding differing by model — is baked into the interface at the framework level, not left to the
caller to remember).

**Prompts.** `BasePromptTemplate` and its chat-oriented sibling `ChatPromptTemplate` turn a template
plus a dict of variables into either a string or a list of messages. Templates are Runnables (§3), so
`prompt.invoke({"question": "..."})` returns a `PromptValue`, and `PromptValue.to_messages()` /
`.to_string()` let the same template feed either a chat model or a text-completion model.

**Output parsers.** `BaseOutputParser[T]` takes the raw model output (a string, or an `AIMessage`) and
turns it into a typed Python value: `StrOutputParser` (a no-op that unwraps `AIMessage.content`),
`JsonOutputParser`, `PydanticOutputParser`, `CommaSeparatedListOutputParser`, and so on. Output
parsers are the pre-native-tool-calling way to get structured data out of a model, and §5 explains why
`with_structured_output` has mostly superseded them.

**Chains.** Historically a family of classes (`LLMChain`, `SequentialChain`, `RouterChain`, ...) each
hand-implementing a specific fixed pipeline shape with its own `_call`/`_acall` methods and its own
input/output key conventions. LCEL (§4) replaced essentially all of them with a general composition
mechanism, and the legacy `Chain` base class survives mainly in `langchain.chains` for a handful of
things (like some retrieval-QA convenience constructors) that haven't been fully migrated, and in a
lot of pre-2024 tutorials that are now actively misleading if followed literally.

**Memory.** `BaseMemory`'s contract — `load_memory_variables(inputs) -> dict` and
`save_context(inputs, outputs) -> None` — is an imperative, mutate-a-stateful-object interface that
sits awkwardly next to the pure-function `Runnable` contract everything else follows. §11 covers why
this mismatch is exactly why memory is being phased out in favor of LangGraph's checkpointer.

**Indexes** is LangChain's umbrella term (from the original docs' information architecture, less used
as a formal term now) for four things that are each their own base class: `BaseLoader` (§6, produces
`Document` objects from a source), `TextSplitter` (§6, turns long documents into chunks), `VectorStore`
(§7, `add_documents` / `similarity_search` / `as_retriever()`), and `BaseRetriever` (§7, the
query-in-documents-out interface that everything downstream of retrieval consumes, regardless of
whether a vector store, a keyword index, or an LLM-driven multi-query fan-out sits behind it).

**Agents and Tools.** `BaseTool` (§8) wraps a Python callable with a name, description, and
JSON-Schema-describable argument shape so a model can be told it exists and asked to call it. Agents
(§9) are the control-flow layer that decides, at each step, whether to call a tool or produce a final
answer — historically `AgentExecutor`, increasingly LangGraph.

**Callbacks.** `BaseCallbackHandler` (§10) is the cross-cutting observability hook: every component,
at every stage of every call, fires lifecycle events (`on_llm_start`, `on_chain_end`, `on_tool_error`,
...) that a registered handler can act on — logging, tracing to LangSmith, computing token counts,
streaming tokens to a UI.

The organizing fact that makes this taxonomy more than a glossary: **every single one of these base
classes — `BaseChatModel`, `BasePromptTemplate`, `BaseOutputParser`, `BaseRetriever`, `BaseTool`,
`VectorStoreRetriever`, and the `RunnableSequence`/`RunnableParallel`/etc. that compose them —
inherits from `Runnable`.** That is not an implementation detail; it is the single fact that makes
LCEL possible, and it is where this chapter goes next.

---

## 3. The Runnable protocol

`Runnable[Input, Output]`, defined in `langchain_core.runnables.base`, is a generic abstract class
with this method surface (elided to what matters for understanding, not the full signature list):

```python
class Runnable(Generic[Input, Output], ABC):
    def invoke(self, input: Input, config: Optional[RunnableConfig] = None) -> Output: ...
    async def ainvoke(self, input: Input, config: Optional[RunnableConfig] = None) -> Output: ...

    def batch(
        self, inputs: list[Input], config: Optional[RunnableConfig] = None,
        *, return_exceptions: bool = False,
    ) -> list[Output]: ...
    async def abatch(self, inputs: list[Input], config=None, *, return_exceptions=False) -> list[Output]: ...

    def stream(self, input: Input, config: Optional[RunnableConfig] = None) -> Iterator[Output]: ...
    async def astream(self, input: Input, config=None) -> AsyncIterator[Output]: ...

    def transform(self, input: Iterator[Input], config=None) -> Iterator[Output]: ...
    async def atransform(self, input: AsyncIterator[Input], config=None) -> AsyncIterator[Output]: ...

    async def astream_events(self, input: Input, config=None, version="v2") -> AsyncIterator[StreamEvent]: ...
```

Only `invoke` (and its async twin, `ainvoke`) is genuinely abstract — every concrete `Runnable` must
implement it. Everything else has a **default implementation on the base class that is expressed in
terms of `invoke`**, which is the load-bearing design decision of the whole protocol: it means any
class that implements `invoke` correctly *for free* gets a batch method, a stream method, and an async
version of both, with reasonable (if not optimal) default behavior. A component only needs to override
the defaults when it can do meaningfully better than them — and the two cases where that matters a lot
are chat models (which can stream token-by-token instead of yielding one blob) and sequences (which
need `transform` to actually be a pipeline of generators rather than a sequential wait-for-each-step).

**The default `stream` is a for-loop of one.** If a class doesn't override `stream`, the base
implementation is, in effect:

```python
def stream(self, input, config=None):
    yield self.invoke(input, config)
```

This is correct — the caller gets an iterator, so code written against the streaming interface still
works — but it is not actually streaming; the whole output arrives as a single chunk once `invoke`
returns. This matters concretely: if you build a chain and one component in the middle is a plain
`RunnableLambda` wrapping a synchronous, non-generator Python function, that component *cannot*
stream partial output no matter what's upstream or downstream of it, because its `stream` inherits the
call-invoke-then-yield-once default. Streaming a partial *result* through a sequence is possible only
because the LLM call at the end of a typical RAG chain overrides `stream` to actually yield
token-by-token, and `RunnableSequence.transform` (below) is written to pass such generators through
without buffering.

**The default `batch` runs invocations concurrently, not sequentially — via a thread pool for sync,
via `asyncio.gather` (with a semaphore) for async.** The signature exposes exactly the concurrency-
control lever you'd expect from `../python-mastery/29-async-patterns-and-pitfalls.md`'s bounded-
concurrency material:

```python
model.batch(
    prompts,
    config={"max_concurrency": 8},   # bound the fan-out; None means unbounded
)
```

`return_exceptions=True` changes `batch`'s failure semantics from fail-fast (one exception aborts the
whole batch and propagates) to per-item (`list[Output | Exception]`, with the caller responsible for
checking each element) — the same all-or-nothing-versus-partial-failure tradeoff that any bounded
fan-out primitive has to expose, and worth stating explicitly in an interview because it's an easy
detail to get wrong when reasoning about a batch job that can't afford to lose 999 good results
because 1 request 429'd.

**`transform` is what makes streaming survive composition, and it's the part of the protocol that
takes real engineering to get right.** Its signature — an iterator of inputs in, an iterator of
outputs out — looks unremarkable until you notice what it lets `RunnableSequence` do: instead of
calling `step1.invoke()` then `step2.invoke()` then `step3.invoke()` in sequence (which is what a naive
composition would do, and which would force the whole pipeline to wait for the slowest fully-materialized
intermediate value), `RunnableSequence.transform` calls `step1.transform(input_iter)` to get an
iterator, passes *that iterator* into `step2.transform(...)`, and so on, so that as soon as `step3`
(say, the final LLM call) starts yielding tokens, they're already flowing to the caller — the sequence
never blocks waiting for an intermediate step to "finish" in the way `invoke` would force it to,
*provided every step in the chain has a `transform` capable of incremental output*. A `PromptTemplate`
still can't meaningfully stream (its output — the fully-formatted prompt — has no meaningful "partial"
form), so its `transform` just passes through: consume the single-item input iterator, invoke, and
yield the single result immediately. The pipeline still ends up "instant intermediate steps, then
streamed final output," which is exactly the shape you want for the common `prompt | model |
output_parser` chain: the prompt formats instantly, the model streams tokens, and `StrOutputParser`'s
`transform` — since it overrides it — passes each token through as it arrives rather than buffering
until the model call is fully done.

**`astream_events` is the introspection API, and it exists because `stream`'s output type is just
"the pipeline's output," with no visibility into which step produced what.** It yields a flat sequence
of tagged events — `on_chain_start`, `on_chat_model_stream`, `on_retriever_end`, `on_tool_start`, and
so on — each carrying a `run_id`, a `parent_ids` list establishing which step it's nested under, and a
payload. This is the mechanism a UI uses to show "searching documents..." then "generating answer..."
during a single `.astream_events()` call over a whole RAG chain, and it's built directly on top of the
callback system in §10 — every event is, structurally, a callback firing that gets collected into an
async queue and re-yielded to the caller instead of (or alongside) being sent to a tracer.

**Input and output schemas are derived, not declared.** `runnable.get_input_schema()` and
`.get_output_schema()` return dynamically generated Pydantic models, introspected from the concrete
class's type parameters and, for sequences, from the first and last step respectively. This is what
lets `langserve` (LangChain's now-largely-superseded FastAPI-integration package) auto-generate an
OpenAPI spec and a Swagger UI for an arbitrary LCEL chain with zero manual schema-writing — the schema
is derived from the chain's own composition, which is only possible because every step's shape is
knowable from the protocol rather than being an opaque function.

**`.bind()` and `.with_config()` look similar and are easy to conflate, and the distinction is a fair
interview question in its own right.** `.with_config(...)` attaches `RunnableConfig` values (tags,
`run_name`, `max_concurrency`, and so on) that apply to *how the runnable is invoked* — the
orchestration-level knobs from the block below — and returns a `RunnableBinding` wrapping the same
underlying object with those defaults pre-set, so you don't have to pass `config=...` at every call
site. `.bind(**kwargs)` instead attaches extra keyword arguments that get merged into the *underlying
call itself* — `model.bind(stop=["\n"])` fixes a stop sequence into every subsequent `.invoke()`, and
`bind_tools` (§8) is implemented as exactly this: `self.bind(tools=[...])`. Put concretely: `.with_config()`
changes how the framework runs the call (tracing, concurrency, naming); `.bind()` changes what gets
sent to the underlying model or function (its actual arguments). Both return the same kind of object
(`RunnableBinding`) and both are non-mutating — the original `Runnable` is untouched, and the bound
version is a new object layered on top of it, which is what makes stacking `.bind_tools(...).with_config(...)`
(or the reverse order) safe and composable rather than order-sensitive in a way that would surprise you.

**`RunnableConfig` is the second argument threaded through every one of these methods, and it's worth
enumerating what actually lives in it, because it's the mechanism half of this chapter's later
sections (callbacks in §10, configurable fields below, memory in §11) turn out to be built on:**

```python
class RunnableConfig(TypedDict, total=False):
    tags: list[str]
    metadata: dict[str, Any]
    callbacks: Callbacks
    run_name: str
    max_concurrency: Optional[int]
    recursion_limit: int
    configurable: dict[str, Any]
    run_id: Optional[uuid.UUID]
```

`tags` and `metadata` are free-form annotations attached to every run in the resulting trace (§10) —
this is how a LangSmith view gets filtered by `tenant_id` or `experiment_arm` without touching the
chain's logic. `recursion_limit` bounds how deep a chain (or, far more relevantly, a LangGraph graph)
is allowed to recurse before raising, which is the safety valve against an agent loop (§9) that never
reaches a terminal state. `run_id`, when supplied explicitly rather than auto-generated, lets a caller
correlate a `Runnable` invocation with an ID minted elsewhere in the surrounding system (a request ID
from an upstream service, for instance) — useful for joining a LangSmith trace to an application log
line by a shared identifier rather than guessing from timestamps.

A detail worth knowing because it causes real bugs: **config does not automatically propagate through
a plain Python function call the way it propagates through `Runnable.invoke`.** If a `RunnableLambda`
wraps a function that internally calls `some_other_runnable.invoke(x)` without forwarding the `config`
argument it was given, that inner call starts a *new*, unparented run — it won't show up nested under
the outer run in a trace, and it won't inherit `max_concurrency` or callbacks from the caller. The fix
is mechanical but easy to forget: any function used inside a `RunnableLambda` that itself calls another
`Runnable` should accept and forward `config`:

```python
def enrich(input: dict, config: RunnableConfig) -> dict:
    extra = some_other_runnable.invoke(input["query"], config=config)   # forward it
    return {**input, "extra": extra}

RunnableLambda(enrich)
```

Losing this thread is one of the more common causes of "why is this one step missing from my trace" —
not a framework bug, a config-forwarding omission at exactly the seam between "framework-managed
composition" and "your own imperative Python code."

**Configurable runnables let you defer a design decision from build time to call time.**

```python
from langchain_core.runnables import ConfigurableField

model = ChatOpenAI(model="gpt-4o-mini", temperature=0).configurable_fields(
    temperature=ConfigurableField(
        id="llm_temperature",
        name="LLM Temperature",
        description="The sampling temperature for the completion.",
    )
)

# default behavior, temperature=0
model.invoke("Write a haiku about caching")

# override at call time, no code change to the chain that built `model`
model.invoke(
    "Write a haiku about caching",
    config={"configurable": {"llm_temperature": 0.9}},
)
```

`.configurable_alternatives` goes further and lets the *entire component* be swapped at call time:

```python
from langchain_core.runnables import ConfigurableField
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-4o-mini").configurable_alternatives(
    ConfigurableField(id="llm_provider"),
    default_key="openai",
    anthropic=ChatAnthropic(model="claude-sonnet-4-5"),
)

chain = prompt | model | StrOutputParser()

chain.invoke({"question": "..."})                                        # OpenAI
chain.invoke({"question": "..."}, config={"configurable": {"llm_provider": "anthropic"}})  # Anthropic
```

This is the single clearest example of the composability payoff the THESIS is about: a chain built
once, against an abstract `Runnable`, can be pointed at a different concrete model at request time —
useful for A/B testing providers, per-tenant model routing, or a fallback tier — without touching the
chain's definition. Under the hood, both calls return a `DynamicRunnable` subclass
(`RunnableConfigurableFields` / `RunnableConfigurableAlternatives`) whose `invoke` reads
`config["configurable"]`, resolves the concrete underlying `Runnable` for this call, and delegates to
it — it is not magic, it's a dispatch table keyed by a config dict, but it's a dispatch table the
framework maintains for you rather than one you'd write by hand at every call site.

---

## 4. LCEL: composition as an algebra over Runnables

LCEL — the LangChain Expression Language — is not a separate language; it's the name for the pattern
of composing `Runnable` objects using Python's own operators and a handful of purpose-built wrapper
classes. The entire mechanism hangs off one dunder method:

```python
class Runnable(Generic[Input, Output], ABC):
    def __or__(self, other: Runnable[Output, Other]) -> RunnableSequence[Input, Other]:
        return RunnableSequence(self, other)

    def __ror__(self, other: Runnable[Other, Input]) -> RunnableSequence[Other, Output]:
        return RunnableSequence(other, self)
```

`prompt | model | output_parser` desugars, left to right, to
`prompt.__or__(model).__or__(output_parser)`, which builds a `RunnableSequence` whose `steps` list is
`[prompt, model, output_parser]` (LangChain flattens nested sequences rather than nesting them, so
chaining more `|` doesn't build a linked list of two-step sequences — it appends to one flat list).
The resulting `RunnableSequence` **is itself a `Runnable`**, which is the entire trick: nothing else
in the framework needs a special case for "a chain of things" versus "one thing," because a chain of
things is one thing as far as every consumer of the protocol is concerned. That's why you can pipe a
`RunnableSequence` into another step, pass it to `.batch()`, wrap it in `RunnableWithMessageHistory`,
or hand it to `AgentExecutor` as if it were a single model call — it satisfies the same interface.

**`RunnableSequence.invoke`** is close to what you'd write by hand: loop over `self.steps`, threading
each step's output into the next step's input, with `RunnableConfig` propagated (and a new child
`run_id` minted per step for the callback tree in §10). The value of *not* writing this by hand is
entirely in what it gets you beyond the naive loop: `batch` runs each input through the same sequence
concurrently across inputs, not just concurrently across steps; `stream` runs it via `transform`
chaining (§3) so the final step's incremental output flows through immediately; and every step's
entry and exit fires a callback event, giving you the full trace, all without a single line of
bookkeeping code in your chain definition.

**`RunnableParallel`** — historically named `RunnableMap` — runs multiple `Runnable`s against the
*same* input and returns a dict of their outputs. It's rarely constructed by name; instead, LangChain
auto-coerces any plain Python `dict` appearing inside a `|` chain into a `RunnableParallel`, which is
why the canonical RAG chain looks like this:

```python
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

prompt = ChatPromptTemplate.from_template(
    "Answer the question using only the context below.\n\nContext:\n{context}\n\nQuestion: {question}"
)

def format_docs(docs):
    return "\n\n".join(d.page_content for d in docs)

rag_chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | prompt
    | model
    | StrOutputParser()
)

rag_chain.invoke("What triggers a HNSW graph rebuild?")
```

That dict literal is coerced into a `RunnableParallel({"context": retriever | format_docs, "question":
RunnablePassthrough()})`. When the chain is invoked with a single string question, `RunnableParallel`
runs *both* branches concurrently against that same string input: the `context` branch retrieves
documents and formats them, the `question` branch is `RunnablePassthrough()` — the identity function —
which just hands the original question straight through unmodified. The two branch outputs are
assembled into `{"context": "...", "question": "..."}`, which becomes the single dict input to
`prompt`, whose template placeholders it fills. This is the idiom worth being able to draw on a
whiteboard: **the retriever and the passthrough of the raw question run in parallel, not in sequence,**
because retrieval doesn't need to wait on anything and the question needs to reach two different
consumers (the retriever, and the prompt template directly) unmodified.

`RunnablePassthrough.assign(...)` is the variant that *adds* keys to a dict without discarding the
rest of it — useful when you want to keep the full input for later steps while adding a computed
field:

```python
chain = (
    RunnablePassthrough.assign(context=lambda x: retriever.invoke(x["question"]) )
    | prompt
    | model
)
# input {"question": "..."} becomes {"question": "...", "context": [...]} before reaching `prompt`
```

**`RunnableLambda`** wraps an arbitrary Python callable (sync or async, one positional argument plus
an optional `config` keyword) so it can sit inside a chain:

```python
from langchain_core.runnables import RunnableLambda

def word_count(text: str) -> int:
    return len(text.split())

chain = prompt | model | StrOutputParser() | RunnableLambda(word_count)
```

The `|` operator's `__ror__` handles the case where a plain function (not yet a `Runnable`) is the
left-hand operand of a pipe by coercing it automatically, so `format_docs | prompt` (without wrapping
`format_docs` in `RunnableLambda` explicitly) also works — `coerce_to_runnable` is applied at
composition time to functions, dicts, and even bare values. It's worth knowing this coercion happens
implicitly, because it's exactly the kind of "magic" that makes a stack trace confusing the first time
you see a `RunnableLambda` frame you never wrote wrapping a plain function you did.

A subtlety worth stating in an interview: **`RunnableLambda` only streams if the wrapped function is
itself a generator.** `inspect.isgeneratorfunction` (or the async equivalent) is checked at wrap time;
if your function `return`s a value, its `Runnable` wrapper's `stream` falls back to the invoke-once
default from §3, silently breaking streaming for anything downstream that depended on incremental
input. This is a real, easy-to-hit production bug: someone inserts a `RunnableLambda(my_postprocessing_fn)`
after the model call in a chain that used to stream token-by-token to a UI, and the UI silently starts
waiting for the whole response before showing anything, because `my_postprocessing_fn` isn't a
generator and therefore neither is its wrapper.

**`RunnableBranch`** is LCEL's conditional-routing primitive, and it's the direct LCEL replacement for
the legacy `RouterChain`/`MultiPromptChain` pattern:

```python
from langchain_core.runnables import RunnableBranch

branch = RunnableBranch(
    (lambda x: "refund" in x["topic"], refund_chain),
    (lambda x: "billing" in x["topic"], billing_chain),
    general_chain,  # default, no condition — must be last
)
```

Each `(condition, runnable)` pair is checked in order against the input; the first whose condition
returns truthy has its runnable invoked; if none match, the final unconditioned entry (the default) is
used. It's a linear scan, not a dispatch table, so ordering matters and a `RunnableBranch` with dozens
of branches is a code smell worth calling out — that's when a dict-keyed dispatch (a plain
`RunnableLambda` that looks up a `Runnable` by key and invokes it) or an actual `RunnableParallel`-and-
select pattern reads better and runs faster.

**`.with_fallbacks()` and `.with_retry()` are the two reliability combinators every production chain
should know about, and both are ordinary `Runnable` wrappers rather than special-cased chain
behavior.**

```python
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

primary = ChatOpenAI(model="gpt-4o", timeout=10)
backup = ChatAnthropic(model="claude-sonnet-4-5", timeout=10)

resilient_model = primary.with_fallbacks([backup])
resilient_model.invoke("...")   # tries primary; on exception, tries backup with the same input
```

`with_fallbacks` returns a `RunnableWithFallbacks`, whose `invoke` tries `self.runnable` first and,
on any exception in `exceptions_to_handle` (by default, broad — most exception types), retries the
*same input* against each fallback in order until one succeeds or the list is exhausted. This is the
LCEL-native version of the provider-outage failover every production LLM service eventually needs, and
it composes: the fallback can itself be a full chain (`prompt | backup_model | parser`), not just a
bare model, as long as its input/output shape matches the primary's.

```python
resilient_chain = (prompt | primary | parser).with_retry(
    stop_after_attempt=3,
    wait_exponential_jitter=True,
    retry_if_exception_type=(RateLimitError, APITimeoutError),
)
```

`.with_retry()` wraps the chain with `tenacity`-based retry logic — configurable backoff, jitter, a
maximum attempt count, and an exception-type filter so you don't blindly retry a `ValidationError` that
will fail identically every time. The two combinators solve different failure classes and are commonly
stacked (`model.with_retry(...).with_fallbacks([...])`): retry handles *transient* failures against the
*same* provider (a rate limit that clears in a second), fallback handles *sustained* failures where
retrying the same provider is pointless (an extended outage) and a different provider is the only path
to success. Neither requires touching the chain's core logic — both are applied as a wrapper around an
already-built `Runnable`, which is the composability payoff paying off again: reliability policy is
attached at the edges, not woven through the business logic.

**Why LCEL replaced the legacy `Chain` classes.** Every pre-LCEL chain (`LLMChain`, `SequentialChain`,
`TransformChain`, ...) was a hand-written subclass of `Chain`, implementing its own `_call` and
(optionally, often incompletely) `_acall`, with bespoke `input_keys`/`output_keys` class attributes
instead of a generically derived schema. The consequences, all fixed by moving to a protocol-first
design: streaming had to be reimplemented per chain class (and mostly wasn't — `LLMChain` streamed only
because its underlying LLM call did, and composing two `LLMChain`s sequentially typically lost
streaming entirely because `SequentialChain` waited for each child's `_call` to fully return);
batching didn't exist as a first-class concept, so concurrent execution across a list of inputs was
left to the caller; and introspecting *what a chain actually does* meant reading its `_call` method's
source, because there was no generic way to ask a `Chain` object "what are your steps" the way
`sequence.steps` answers that today. LCEL's actual technical improvement over the legacy design is
narrow and specific — batch/stream/async came from one protocol instead of N reimplementations — and
that narrowness is worth stating precisely in an interview, because overclaiming what LCEL fixed
(better prompts, better retrieval, better reasoning — it does none of that) is a tell that someone
learned the marketing rather than the mechanism.

---

## 5. Prompts: templates, placeholders, and structured output

**`ChatPromptTemplate`** is the workhorse for anything chat-model-based, and its `from_messages`
constructor takes a list of `(role, template_string)` tuples or message-like objects:

```python
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a support agent for {product}. Be concise."),
    MessagesPlaceholder("history"),
    ("human", "{input}"),
])

prompt.invoke({"product": "Acme Widgets", "history": [], "input": "How do I reset my widget?"})
```

`.invoke(...)` returns a `ChatPromptValue`, not a plain list — a small wrapper whose `.to_messages()`
gives you the `list[BaseMessage]` a chat model expects and whose `.to_string()` gives a flattened text
rendering for logging or for feeding a text-completion model instead. `MessagesPlaceholder("history")`
is the mechanism that lets a variable-length list of prior turns be spliced into a fixed template
position — the value bound to `"history"` at invoke time must already be a `list[BaseMessage]` (typed
`AIMessage`/`HumanMessage`/`SystemMessage`/`ToolMessage` objects, not raw strings), which is exactly
the slot `RunnableWithMessageHistory` (§11) fills in automatically once wired up.

**Few-shot prompting** has a template class of its own, `FewShotChatMessagePromptTemplate`, and its
more interesting mode dynamically *selects* which examples to include based on similarity to the
current input rather than hard-coding a fixed set:

```python
from langchain_core.example_selectors import SemanticSimilarityExampleSelector
from langchain_core.prompts import FewShotChatMessagePromptTemplate, ChatPromptTemplate
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

examples = [
    {"input": "2+2", "output": "4"},
    {"input": "What color is the sky", "output": "Blue"},
    # ... dozens more
]

example_selector = SemanticSimilarityExampleSelector.from_examples(
    examples, OpenAIEmbeddings(), Chroma, k=2,
)

few_shot_prompt = FewShotChatMessagePromptTemplate(
    example_selector=example_selector,
    example_prompt=ChatPromptTemplate.from_messages([("human", "{input}"), ("ai", "{output}")]),
)
```

This is retrieval applied to prompt construction itself — the example selector embeds the incoming
query, does a similarity search over the example bank, and returns the `k` closest examples to inject.
It is architecturally identical to the RAG retrieval step in `03`/`04`; the only difference is the
retrieved objects are demonstration pairs, not passages destined for an answer. Worth pointing out in
an interview if asked "have you built RAG with LangChain" — few-shot example selection *is* a small
RAG pipeline, and recognizing the shared structure is a sign of actually understanding retrieval
rather than having memorized a chain constructor.

**Output parsers versus structured output — the distinction that matters most in this section.** An
output parser (`PydanticOutputParser`, `JsonOutputParser`, ...) is a post-hoc text-processing step: it
injects formatting instructions into the *prompt* (via `.get_format_instructions()`, typically a chunk
of text like "Return a JSON object matching this schema: {...}"), the model produces plain text that
is *hopefully* well-formed JSON matching that schema, and the parser then attempts to `json.loads` it
and validate it against a Pydantic model. This is fundamentally probabilistic — the model can and does
occasionally emit malformed JSON, trailing commentary before or after the JSON block, or JSON that's
syntactically valid but violates the schema (wrong types, missing required fields) — which is why
`OutputFixingParser` and `RetryWithErrorOutputParser` exist: wrappers that catch a parse failure, feed
the error back to the model, and ask it to fix its own output. That's a real, if crude, reliability
pattern, and it's the *only* pattern available for providers or models without native structured
output support.

`with_structured_output`, in contrast, uses the **provider's own enforcement mechanism** — OpenAI's
tool-calling / JSON-mode / strict JSON-schema modes, Anthropic's tool-use forced-to-one-tool mode — so
the constraint is enforced by the inference server, not recovered after the fact by your code:

```python
from pydantic import BaseModel, Field

class Ticket(BaseModel):
    severity: Literal["low", "medium", "high", "critical"] = Field(...)
    summary: str = Field(..., description="One-sentence summary of the issue")
    requires_escalation: bool

structured_model = model.with_structured_output(Ticket)
result: Ticket = structured_model.invoke("My production database is down and customers can't check out.")
```

Under the hood, `with_structured_output` converts the Pydantic model to a JSON Schema (via
`model.model_json_schema()` plus LangChain's own schema-cleanup pass, since provider JSON Schema
support is a strict subset of the full spec — no `$ref`s, limited `oneOf`, and so on, differing subtly
by provider), presents it to the model as a single forced tool call (`tool_choice` pinned to that one
tool, for providers where that's how structured output is implemented) or via a native
`response_format={"type": "json_schema", ...}` for providers with that first-class feature, and parses
the resulting tool-call arguments or JSON content directly into the Pydantic model — no format
instructions polluting your prompt, and validation failure becomes rare enough that most production
code doesn't even bother catching it, though it still can happen (schema mismatches, models with
weaker structured-output adherence) and shouldn't be assumed impossible.

The interview-ready framing: **output parsers are a workaround for providers without native structured
output; `with_structured_output` is what you use once the provider has it, which by 2026 is
essentially every frontier provider.** If you see `PydanticOutputParser` in a modern codebase talking
to OpenAI or Anthropic chat models, that's a strong signal the code predates the provider's native
support and is a candidate for simplification — one of the concrete, checkable anti-patterns in §13.

One more wrinkle worth knowing: `with_structured_output` has a `method` parameter
(`"function_calling"`, `"json_mode"`, or `"json_schema"` depending on provider capability) because
different providers expose structured output through different underlying mechanisms, and the
abstraction has to pick one per provider — which is a small, concrete instance of the "the abstraction
leaks because the providers aren't actually equivalent" criticism covered fully in §13.

---

## 6. Document loading and transformation

The `Document` object is deliberately minimal: `page_content: str` plus `metadata: dict[str, Any]`,
and (more recently) an optional `id: str`. Every loader's job is to produce a `list[Document]` (via
`.load()`) or, for anything large enough that materializing the whole list is wasteful, an
`Iterator[Document]` via `.lazy_load()`:

```python
from langchain_community.document_loaders import PyPDFLoader, DirectoryLoader

loader = DirectoryLoader("./docs", glob="**/*.pdf", loader_cls=PyPDFLoader, show_progress=True)
docs = loader.load()
docs[0].metadata   # {"source": "./docs/handbook.pdf", "page": 0}
```

Every loader is required to populate `metadata["source"]` at minimum — it's the one convention the
framework actually enforces, because without it a retrieved chunk has no traceable origin, which
breaks citation (`06-context-engineering.md`, planned) at the root. Beyond `source`, metadata
population is loader-specific and wildly inconsistent in quality across the `langchain-community`
catalog — some loaders extract page numbers, headers, or structural markers; many extract nothing
beyond the source path. This is worth checking, not assuming, for any loader you adopt: read its
source or run it against a known document and inspect what metadata actually comes back, because
`02-chunking-and-document-processing.md`'s entire metadata-propagation argument (structure discovered
at parse time is much more expensive to recover after chunking) is only as good as what the loader you
picked actually preserves.

**Text splitters** are LangChain's reference implementation of `02`'s chunking theory, and the
important thing to internalize is that the default splitter is simpler than it looks:

```python
from langchain_text_splitters import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=200,
    separators=["\n\n", "\n", ". ", " ", ""],
    length_function=len,   # character count by default, NOT token count
)
chunks = splitter.split_documents(docs)
```

`RecursiveCharacterTextSplitter` tries each separator in `separators` in order, recursively: it
attempts to split on the first separator, and for any resulting piece still longer than `chunk_size`,
it recurses using the *next* separator in the list, down to the empty-string separator (hard character
cut) as the fallback of last resort. This is "recursive" in the sense of *retrying with progressively
more aggressive separators*, not in the sense of understanding document structure — it has no idea
what a paragraph, a heading, or a code block is; it's pattern-matching on characters (`"\n\n"`, then
`"\n"`, then `". "`, then `" "`). `chunk_overlap` is applied by re-including the trailing
`chunk_overlap` characters of one chunk at the start of the next, a crude fixed-window overlap rather
than anything content-aware.

The parameter worth flagging as a default that will bite you in production: **`length_function=len`
counts characters, not tokens**, and most model context windows and cost accounting are token-based.
A 1000-character chunk is roughly 200–300 tokens for English prose, but that ratio moves meaningfully
for code, non-English text, or text with lots of punctuation/whitespace, so a `chunk_size` tuned by
eyeballing character counts can silently produce chunks that blow a token budget once passed through a
different tokenizer than the one you tested against. The fix is to pass an actual tokenizer:

```python
import tiktoken

encoding = tiktoken.encoding_for_model("gpt-4o")
splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    length_function=lambda text: len(encoding.encode(text)),
)
```

or use `RecursiveCharacterTextSplitter.from_tiktoken_encoder(...)`, a convenience constructor that
wires this up for you but still splits on the same character-level separators underneath — only the
*measurement* becomes token-aware, not the split points themselves.

For anything beyond flat prose, LangChain ships structure-aware splitters that are worth knowing exist
even if you don't reach for them by default: `MarkdownHeaderTextSplitter` (splits on heading levels
and attaches the heading path as metadata — the header-as-context pattern `02` recommends, implemented
directly), `RecursiveJsonSplitter`, and language-aware code splitters
(`RecursiveCharacterTextSplitter.from_language(Language.PYTHON)`, which swaps in separators tuned to a
given language's syntax — class/def boundaries before blank lines, roughly). None of these implement
semantic chunking (chunk boundaries chosen by embedding-similarity drift, `02` §7); that exists in
`langchain_experimental.text_splitter.SemanticChunker`, explicitly labeled experimental, and it's
worth knowing the theory in `02` well enough to evaluate whether that implementation's specific
breakpoint heuristic (percentile, standard-deviation, or interquartile threshold on consecutive-sentence
embedding distance) is actually doing what your corpus needs, rather than trusting the class name.

**Document transformers** are the third stage between loading and indexing, and they're where
post-split cleanup lives: `EmbeddingsRedundantFilter` embeds every chunk and drops near-duplicates
above a cosine-similarity threshold before they ever reach the index (a pre-ingest version of `02`
§10.4's near-duplicate suppression, applied at write time instead of retrieval time), and
`LongContextReorder` reorders a retrieved document list so the most relevant items sit at the start and
end of the context window rather than buried in the middle — a direct, mechanical response to the
"lost in the middle" positional-attention effect: models attend more reliably to the beginning and end
of a long context than to its center, so a naive rank-then-concatenate assembly can bury your best
passage exactly where it's least likely to be used. Both are ordinary `BaseDocumentTransformer`
implementations (`transform_documents(docs) -> docs`), composable the same way compressors are in §7:

```python
from langchain_community.document_transformers import EmbeddingsRedundantFilter, LongContextReorder
from langchain.retrievers.document_compressors import DocumentCompressorPipeline

pipeline = DocumentCompressorPipeline(transformers=[
    EmbeddingsRedundantFilter(embeddings=embedding_model, similarity_threshold=0.95),
    LongContextReorder(),
])
```

Metadata-extraction transformers — asking an LLM to generate a title, summary, or set of hypothetical
questions a chunk answers, then attaching them as searchable metadata — are the LangChain-flavored
implementation of `02`'s contextual-chunking material (prepending document-level context to a chunk
before embedding it). They cost one LLM call per chunk at ingest time, which is the same cost-versus-
retrieval-quality tradeoff `02` §9 already covers in depth; nothing about doing this inside a LangChain
`DocumentTransformer` changes that arithmetic, it just gives the operation a composable interface
consistent with everything else in this pipeline.

The load-bearing point for this section, stated plainly: **LangChain's document-loading and splitting
layer is glue code around genuinely hard problems (parsing, chunking) that are covered in depth in
`02`. Don't rewrite that theory here — know it there, and know here that the defaults you get by
importing `RecursiveCharacterTextSplitter` are reasonable, character-based, structure-blind, and
usually the wrong choice to ship unexamined into a production system with a real accuracy bar.**

---

## 7. The retriever abstraction

`BaseRetriever`'s actual contract is one method: `_get_relevant_documents(query: str, *, run_manager:
CallbackManagerForRetrieverRun) -> list[Document]` (plus its async twin). The public entry point is
`.invoke(query)` (a `Runnable` method — `BaseRetriever` inherits from `Runnable[str, list[Document]]`),
which wraps `_get_relevant_documents` with callback lifecycle management so retrieval steps show up in
traces automatically. This is a narrow, honest contract: a retriever is a function from a query string
to a ranked list of documents, full stop, and everything else in this section is a way of building
that function out of other pieces.

**`VectorStoreRetriever`** is the default, produced by calling `.as_retriever()` on any `VectorStore`:

```python
retriever = vectorstore.as_retriever(
    search_type="mmr",              # "similarity" | "mmr" | "similarity_score_threshold"
    search_kwargs={"k": 8, "fetch_k": 40, "lambda_mult": 0.5},
)
```

`search_type="mmr"` routes to the vector store's `max_marginal_relevance_search`, implementing exactly
the diversity-versus-relevance tradeoff `04` §11 covers — `fetch_k` candidates are pulled by similarity
first, then MMR re-selects `k` of them balancing relevance against redundancy via `lambda_mult`.
`"similarity_score_threshold"` adds a hard cutoff (`search_kwargs={"score_threshold": 0.75}`) that
drops candidates below a similarity floor — useful, but worth remembering `03`'s and `04`'s warnings
about similarity scores not being comparable across queries or embedding models, so a hard-coded
threshold tuned on one query distribution can silently over- or under-return on another.

**`ContextualCompressionRetriever`** wraps a base retriever with a `DocumentCompressor` applied *after*
retrieval, before the documents reach the caller — this is where reranking (`04` §7) and
extractive compression both live in LangChain's abstraction:

```python
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import CohereRerank
from langchain_cohere import CohereRerank as CohereRerankModel

compressor = CohereRerank(model="rerank-v3.5", top_n=5)
compression_retriever = ContextualCompressionRetriever(
    base_compressor=compressor, base_retriever=vectorstore.as_retriever(search_kwargs={"k": 40}),
)
```

`base_retriever` supplies the wide first-stage candidate set (the "cheap and wide" side of `04`'s
cascade), `base_compressor` narrows and reorders it (the "expensive and narrow" side) — `CohereRerank`
here is a `BaseDocumentCompressor` wrapping a cross-encoder API call, but the same slot accepts
`LLMChainExtractor` (asks an LLM to extract only the relevant sentences from each document — genuine
compression, not just reordering) or `EmbeddingsFilter` (drops documents below an embedding-similarity
threshold to the query, cheaper than an LLM call but cruder). `DocumentCompressorPipeline` chains
several of these, e.g., a cheap `EmbeddingsFilter` first to cut candidate count, then an
`LLMChainExtractor` on the survivors — itself a small cascade nested inside the retrieval cascade `04`
describes.

**`EnsembleRetriever`** combines multiple retrievers (typically a lexical one and a dense one — this is
LangChain's hybrid-retrieval primitive, the direct implementation of `04`'s §1 "union raises the
ceiling" argument):

```python
from langchain.retrievers import EnsembleRetriever, BM25Retriever

bm25_retriever = BM25Retriever.from_documents(docs, k=10)
dense_retriever = vectorstore.as_retriever(search_kwargs={"k": 10})

ensemble = EnsembleRetriever(
    retrievers=[bm25_retriever, dense_retriever], weights=[0.4, 0.6],
)
```

Its fusion is a weighted variant of reciprocal rank fusion: each branch's result list is converted to
rank-based scores (`1 / (rank + c)`, `c` defaulting to `60` — the same constant Elasticsearch's RRF
defaults to, per `04` §5), scaled by that branch's weight, summed across branches for any document
appearing in more than one, and resorted. It is, in other words, exactly `04` §5's RRF, with the
weighting knob `04` calls out as an optional refinement over unweighted RRF. Knowing this lets you
answer "how does LangChain do hybrid search" precisely instead of vaguely — it's rank fusion with a
configurable constant and per-branch weights, not some proprietary blending algorithm.

**`MultiQueryRetriever`** uses an LLM to generate several paraphrased or decomposed versions of the
input query, retrieves for each independently, and unions (deduplicating by content) the results:

```python
from langchain.retrievers.multi_query import MultiQueryRetriever

mq_retriever = MultiQueryRetriever.from_llm(retriever=vectorstore.as_retriever(), llm=model)
mq_retriever.invoke("How does index rebuild affect query latency?")
# internally generates ~3 paraphrases, retrieves for each, unions the results
```

This is query expansion as a retrieval-ceiling-raising technique — more independent "branches" into the
same corpus, in `04`'s framing — implemented by making the LLM call the query-generation step rather
than a fixed rule-based rewriter. The failure mode worth knowing: paraphrases generated by an LLM
without grounding in your corpus's actual vocabulary can drift away from the terms your documents
actually use, in which case query expansion can *add* noise branches rather than useful ones — this
is squarely the kind of claim `04` §13's ablation discipline says to measure, not assume.

**`SelfQueryRetriever`** solves a genuinely different problem: translating a natural-language query
that has an *implicit structured filter* embedded in it ("cheap flights to Tokyo departing after June"
implies `price: low`, `destination: Tokyo`, `depart_after: June`) into an explicit metadata filter
applied at the vector-store query level, plus a residual semantic query for the rest:

```python
from langchain.chains.query_constructor.base import AttributeInfo
from langchain.retrievers.self_query.base import SelfQueryRetriever

metadata_field_info = [
    AttributeInfo(name="genre", description="The movie genre", type="string"),
    AttributeInfo(name="year", description="The year the movie was released", type="integer"),
    AttributeInfo(name="rating", description="A 1-10 rating", type="float"),
]

self_query_retriever = SelfQueryRetriever.from_llm(
    llm=model, vectorstore=vectorstore,
    document_contents="Brief summary of a movie",
    metadata_field_info=metadata_field_info,
)
self_query_retriever.invoke("What are some highly rated (above 8.5) science fiction films from the 1990s?")
```

Under the hood, the LLM is prompted (with the `metadata_field_info` schema injected) to produce a
structured `StructuredQuery` (a residual semantic-search string plus a filter expression tree —
comparisons and boolean combinators), which a per-vector-store `Translator` then converts into that
store's native filter syntax (a Chroma `where` clause, a Pinecone metadata filter, and so on — this
translation layer is why `SelfQueryRetriever` support is enumerated per vector store rather than
universal). It is entirely dependent on the vector store *supporting* metadata filtering in the first
place (`03`'s filtered-search material) and on the LLM correctly inferring field names and value types
from natural language, which is a real reliability surface worth testing against your actual query
distribution rather than the two or three examples in a tutorial.

---

## 8. Tools and function calling

A LangChain `Tool` is a Python callable plus three things a model needs to decide whether and how to
call it: a `name`, a `description`, and an `args_schema` (a JSON-Schema-describable shape for its
arguments). The `@tool` decorator is the fast path — it infers all three from ordinary Python:

```python
from langchain_core.tools import tool

@tool
def get_current_weather(city: str, unit: Literal["celsius", "fahrenheit"] = "celsius") -> str:
    """Get the current weather for a city.

    Args:
        city: The city name, e.g. "Berlin".
        unit: Temperature unit to report in.
    """
    return f"18 degrees {unit} and cloudy in {city}"

get_current_weather.name          # "get_current_weather"
get_current_weather.description   # "Get the current weather for a city."
get_current_weather.args          # JSON Schema derived from the type hints + docstring Args section
```

The mechanism: the function's name becomes the tool name; the first line of the docstring becomes the
description; and the argument schema is built by introspecting the function's type hints via
`pydantic.create_model` (constructing an ad-hoc Pydantic model whose fields mirror the function
signature), with per-argument descriptions parsed out of a Google- or NumPy-style `Args:` docstring
section if present. This means **a tool's usability by the model is a direct function of how well you
type-hint and document the underlying Python function** — a bare `def foo(x, y): ...` with no
docstring and no type hints produces a schema so vague the model has little to work with, which is a
real, common cause of poor tool-selection accuracy that has nothing to do with the model and everything
to do with an under-specified tool definition.

For cases the decorator's inference can't handle cleanly — nested/complex argument shapes, or
attaching a schema to something that isn't a plain function — `StructuredTool` takes an explicit
Pydantic `args_schema`:

```python
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

class SearchArgs(BaseModel):
    query: str = Field(..., description="The search query")
    max_results: int = Field(5, description="Maximum number of results to return", le=20)

def search_docs(query: str, max_results: int = 5) -> list[dict]:
    ...

search_tool = StructuredTool.from_function(
    func=search_docs, name="search_docs", description="Search the internal knowledge base.",
    args_schema=SearchArgs,
)
```

**`bind_tools`** is how a chat model is told which tools exist for a given call, and it's implemented
as a thin, provider-specific schema translation plus a partial application:

```python
model_with_tools = model.bind_tools([get_current_weather, search_tool])
response = model_with_tools.invoke("What's the weather in Lisbon?")
response.tool_calls
# [{"name": "get_current_weather", "args": {"city": "Lisbon"}, "id": "call_abc123", "type": "tool_call"}]
```

`bind_tools` converts each `BaseTool` into the provider's expected wire format — for an OpenAI-style
API, `{"type": "function", "function": {"name": ..., "description": ..., "parameters": <json schema>}}`
— via a per-provider `convert_to_openai_tool` / `convert_to_anthropic_tool` style function, then calls
`.bind(tools=[...])`, which returns a `RunnableBinding`: a wrapper `Runnable` that stores extra kwargs
(here, `tools`) and merges them into every `invoke`/`stream`/`batch` call's underlying request, without
mutating the original `model` object. That's why `model_with_tools` and `model` are safely two separate
objects you can hold onto independently — `bind` (and `bind_tools`) is functional, not mutating.

The model's response, an `AIMessage`, carries a `.tool_calls` attribute — a list of dicts with `name`,
`args` (already parsed from the model's JSON output into a Python dict — you don't hand-parse this),
and `id` (which must be echoed back so the model can match a `ToolMessage` result to the specific call
that requested it, important when a single response requests multiple parallel tool calls). Executing
a tool call and feeding the result back is mechanical:

```python
from langchain_core.messages import ToolMessage

messages = [HumanMessage("What's the weather in Lisbon?")]
ai_msg = model_with_tools.invoke(messages)
messages.append(ai_msg)

tools_by_name = {t.name: t for t in [get_current_weather, search_tool]}
for call in ai_msg.tool_calls:
    tool_result = tools_by_name[call["name"]].invoke(call["args"])
    messages.append(ToolMessage(content=str(tool_result), tool_call_id=call["id"]))

final = model_with_tools.invoke(messages)   # model now has the tool's result in context
```

That loop — invoke, inspect `.tool_calls`, execute, append `ToolMessage`s, re-invoke — **is the agent
loop.** There is no additional mechanism; `AgentExecutor` and LangGraph's `create_react_agent` (§9) are
both, at their core, this exact loop wrapped in control-flow scaffolding (iteration limits, error
handling, state management, human-in-the-loop hooks). Being able to write this loop by hand, from
memory, cold, is one of the highest-signal things a senior candidate can demonstrate, because it proves
the "agent" isn't magic — it's a while-loop around a message list and a dict lookup.

Tool descriptions and schemas are literally what gets serialized into the request the provider bills
and rate-limits you on — a tool with a large `args_schema` (many fields, verbose descriptions) or a
long docstring-derived description adds tokens to every single call that includes it in `bind_tools`,
whether or not the model ends up calling it. At double-digit tool counts this is a real, measurable
cost and context-budget line item (`06-context-engineering.md`'s token budget, planned), not a
theoretical concern — worth checking `len(json.dumps(model_with_tools.kwargs["tools"]))` (or your
provider's token counter over the same payload) if you're debugging why a "simple" tool-using request
costs more than expected.

---

## 9. Agents: from AgentExecutor to LangGraph

**The legacy shape.** `AgentExecutor` pairs an "agent" — itself a `Runnable` that, given the current
scratchpad of intermediate steps, decides on either an `AgentAction` (call this tool with these
args) or an `AgentFinish` (here's the final answer) — with a list of tools, and runs the think-act-
observe loop from §8 for you:

```python
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant with access to tools."),
    ("human", "{input}"),
    MessagesPlaceholder("agent_scratchpad"),
])

agent = create_tool_calling_agent(model, tools=[get_current_weather, search_tool], prompt=prompt)
executor = AgentExecutor(agent=agent, tools=[get_current_weather, search_tool], max_iterations=6, verbose=True)
executor.invoke({"input": "What's the weather in Lisbon, and search our docs for 'weather API rate limits'"})
```

`create_tool_calling_agent` builds the "decide next action" `Runnable` out of `bind_tools` plus a
parser that turns tool-call-bearing `AIMessage`s into `AgentAction`s and plain-text `AIMessage`s into
`AgentFinish`; `AgentExecutor.invoke` is the loop from §8 with bookkeeping — it accumulates the
scratchpad, respects `max_iterations` and a wall-clock `max_execution_time`, and catches (configurably)
parsing errors from malformed tool calls so one bad step doesn't crash the whole run.

**Why it's being deprecated in favor of LangGraph.** The problems are structural, not implementation
bugs, and worth naming precisely rather than gesturing at "it's old":

1. **Control flow is fixed.** `AgentExecutor`'s loop is exactly one shape: call the agent, if it's an
   action execute the tool and loop, if it's a finish stop. There is no supported way to insert a
   human-approval gate before a specific tool call, run two tool calls with different retry policies,
   or branch the *loop structure itself* based on which tool was called — any of that requires
   subclassing or monkey-patching internals that were never designed to be extension points.
2. **No persistence or resumability.** If a process crashes mid-loop, or a human needs to review and
   approve a step before it continues (a hard requirement for anything touching money, production
   infrastructure, or irreversible external actions), `AgentExecutor` has nothing to offer — the
   scratchpad lives in a Python list in memory for the duration of one `.invoke()` call and nowhere
   else.
3. **Debuggability is poor precisely because the loop is implicit.** `verbose=True` prints a
   semi-structured trace, but there's no way to pause, inspect, and manually inject a different next
   step; the loop runs to completion or raises.

**LangGraph's answer** is to make the state machine explicit instead of hidden inside a library's loop
implementation: nodes are plain functions (or Runnables) that take and return a shared state object
(typically a `TypedDict` with a `messages` field using an `add_messages` reducer to append rather than
overwrite), edges — including *conditional* edges, functions that inspect the current state and return
the name of the next node — define control flow explicitly as a graph you can draw, and a
`checkpointer` persists state after every node execution, keyed by a `thread_id`, which is what makes
resumability, human-in-the-loop interrupts, and time-travel debugging possible as one mechanism rather
than three bespoke ones.

```python
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

agent = create_react_agent(
    model, tools=[get_current_weather, search_tool], checkpointer=MemorySaver(),
)

config = {"configurable": {"thread_id": "user-42"}}
agent.invoke({"messages": [HumanMessage("What's the weather in Lisbon?")]}, config=config)
# a later call with the same thread_id resumes with full prior message history restored from the checkpointer
agent.invoke({"messages": [HumanMessage("And what about Porto?")]}, config=config)
```

`create_react_agent` is a *prebuilt* graph — under the hood it constructs exactly two nodes (an
"agent" node that calls `model_with_tools.invoke` against the accumulated `messages`, and a "tools"
node — `ToolNode` — that executes whichever tool calls the agent node's output contains) connected by a
conditional edge (if the latest `AIMessage` has `.tool_calls`, go to "tools"; otherwise, end) and an
unconditional edge from "tools" back to "agent". That is, structurally, identical to the manual loop in
§8 — LangGraph's contribution isn't a smarter loop, it's making that loop's structure a first-class,
inspectable, extensible graph object, with persistence attached for free via the checkpointer.

For anything beyond the vanilla ReAct shape — a review step before a risky tool executes, parallel
tool branches with different failure handling, a supervisor node routing between specialist sub-agents
— you build the graph directly with `StateGraph`, `add_node`, `add_edge`, and `add_conditional_edges`
rather than reaching for the prebuilt:

```python
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]   # reducer: append, don't overwrite
    pending_approval: bool

def call_model(state: AgentState) -> dict:
    response = model_with_tools.invoke(state["messages"])
    return {"messages": [response]}

def require_approval(state: AgentState) -> dict:
    last = state["messages"][-1]
    risky = any(c["name"] == "delete_production_record" for c in last.tool_calls)
    if risky:
        interrupt("A tool call requires human approval before executing.")
    return {}

def route_after_model(state: AgentState) -> str:
    last = state["messages"][-1]
    if not last.tool_calls:
        return END
    if any(c["name"] == "delete_production_record" for c in last.tool_calls):
        return "require_approval"
    return "tools"

graph = StateGraph(AgentState)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode(tools))
graph.add_node("require_approval", require_approval)
graph.set_entry_point("agent")
graph.add_conditional_edges("agent", route_after_model, {"tools": "tools", "require_approval": "require_approval", END: END})
graph.add_edge("require_approval", "tools")
graph.add_edge("tools", "agent")

app = graph.compile(checkpointer=MemorySaver())
```

This is the shape `create_react_agent` cannot express: a third node interposed specifically for one
dangerous tool, a conditional edge that inspects *which* tool was requested (not just whether one was),
and `interrupt()` — LangGraph's human-in-the-loop primitive, which pauses execution at that exact point,
persists the paused state via the checkpointer, and resumes only when the caller explicitly provides a
resume value, potentially minutes or days later and potentially from a different process entirely. None
of this required subclassing or monkey-patching anything; it's a graph definition using the same nodes
and edges that `create_react_agent` builds automatically for the vanilla case. That's the concrete
answer to "why LangGraph" that goes beyond the marketing: **the state graph is not a nicer-looking
`AgentExecutor`, it is a different category of object — one you can insert an arbitrary node into,
which the closed loop's design never made room for.**

The interview-ready summary: **`AgentExecutor` is a closed loop you configure; a LangGraph graph is an
open control-flow structure you author, with persistence as a first-class citizen instead of an
afterthought — which is exactly the class of requirement ("can a human approve this step," "can this
survive a process restart," "can I branch based on which tool got called") that makes agentic systems
production-grade rather than demo-grade, and exactly the class of requirement `AgentExecutor`
structurally cannot satisfy without working against its own design.**

---

## 10. Callbacks, tracing, and LangSmith

Every lifecycle event of every component — a chain starting, a chat model producing a new token, a
retriever finishing, a tool erroring — fires a method on every registered `BaseCallbackHandler`. The
full interface (abbreviated) looks like this:

```python
from langchain_core.callbacks import BaseCallbackHandler

class TokenCounter(BaseCallbackHandler):
    def __init__(self):
        self.total_tokens = 0

    def on_llm_end(self, response, **kwargs):
        usage = response.llm_output.get("token_usage", {})
        self.total_tokens += usage.get("total_tokens", 0)

    def on_tool_error(self, error, **kwargs):
        print(f"Tool failed: {error!r}")

counter = TokenCounter()
chain.invoke({"question": "..."}, config={"callbacks": [counter]})
print(counter.total_tokens)
```

**Propagation is the mechanism worth understanding at the internals level.** Callbacks passed via
`config={"callbacks": [...]}` are wrapped in a `CallbackManager`, and every nested `Runnable` inside a
sequence, parallel block, or agent loop inherits that manager — each nested call mints a *child*
`run_id` parented to the caller's `run_id`, so the resulting event stream is a tree, not a flat list:
a `RunnableSequence`'s top-level run is the root, its three steps are children, and if one of those
steps is itself a retriever backed by an embedding call and a vector-store query, those are
grandchildren. This parent/child run tree is structurally the same idea as a distributed trace's span
tree (`../sre-observability/02-opentelemetry-deep-dive.md`), and it existed in LangChain before OTEL's
GenAI semantic conventions were mature — which is precisely why `10-llm-observability-and-tracing.md`
(planned) treats OTEL as the vendor-neutral direction the industry has converged on, and this proprietary
callback tree as the mechanism that got there first and that LangSmith still uses natively.

**LangSmith** is LangChain's commercial tracing/eval platform, and wiring a chain to it requires zero
code changes — it's environment variables:

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=ls__...
export LANGCHAIN_PROJECT=support-agent-prod
```

Setting `LANGCHAIN_TRACING_V2=true` causes LangChain to register a `LangChainTracer` callback handler
globally, so *every* `Runnable.invoke()` call anywhere in the process gets traced without touching a
single call site — a deliberate, convenient, and slightly dangerous default, because it means tracing
can be silently enabled (sending prompts and completions to a third-party service) by an environment
variable someone set for a different purpose, which is worth calling out explicitly in any
compliance-sensitive interview context (PII in prompts flowing to LangSmith by default once that
variable is set anywhere in the deployment environment).

Within a traced run, `run_name`, `tags`, and `metadata` passed via `config` let you annotate the trace
for later filtering — `config={"run_name": "rag-answer", "tags": ["prod", "tenant-42"], "metadata":
{"user_id": "u_123"}}` — which is how a LangSmith dashboard ends up sliceable by tenant, environment,
or experiment arm without any bespoke logging code.

**The other consumer of the exact same callback mechanism is `astream_events`** (§3): rather than
sending events to LangSmith, `astream_events` collects them into an async queue and re-yields them to
the caller in-process, which is the mechanism a chat UI uses to render "Searching..." → "Reading 4
documents..." → token-by-token generation from a single call, without the UI needing to know anything
about the chain's internal structure beyond the event names it cares about. Recognizing that tracing
and fine-grained streaming-with-progress are *the same underlying mechanism consumed two different
ways* is a good "internals" answer if asked how LangChain achieves either.

**Why the industry is converging on OTEL's GenAI semantic conventions instead of standardizing on
LangChain's callback tree.** The callback tree in this section predates OTEL having any GenAI-specific
vocabulary at all, and it works well as long as every component in your system is a LangChain
`Runnable` — but a real production stack rarely is: a raw provider SDK call made outside any chain, a
non-LangChain queue consumer, or a different team's service in the same request path emit nothing into
this tree, which makes LangSmith's view of the world necessarily partial at a system boundary.
OpenTelemetry's GenAI semantic conventions (`../sre-observability/34-schema-and-semantic-conventions-governance.md`
covers semconv governance generally) define a vendor-neutral span/attribute vocabulary for exactly this
domain — `gen_ai.request.model`, `gen_ai.usage.input_tokens`, span kinds for a chat completion versus a
tool execution — so that a trace assembled from a LangChain chain, a raw SDK call, and an entirely
different framework in the same request can land in the *same* trace backend under a *shared* schema,
rather than three incompatible proprietary formats. `10-llm-observability-and-tracing.md` (planned)
treats this as the load-bearing reason to instrument at the OTEL layer even in a LangChain-heavy
codebase: LangSmith's native tracing is excellent *within* LangChain's boundary and blind *outside* it,
while an OTEL-based callback handler (there is a community `langchain-core` callback handler that emits
OTEL spans instead of, or alongside, LangSmith runs) gives you one trace format across every system
that touches a request, LangChain or not.

---

## 11. Memory, and why it's being deprecated

LangChain's memory classes solve one problem: a chat application needs the *previous* turns of a
conversation available when generating the *next* one, and something has to store, retrieve, and
(often) compress that history so it doesn't grow the prompt without bound.

`ConversationBufferMemory` is the simplest and most dangerous default — it stores every turn verbatim
and replays the entire history into every prompt, which means token cost and context consumption grow
linearly with conversation length and eventually exceed the context window outright; it is a
reasonable choice for a demo and a production incident waiting to happen in anything with unbounded
session length. `ConversationBufferWindowMemory` bounds it crudely (keep only the last `k` turns,
discarding everything older regardless of relevance). `ConversationSummaryMemory` replaces old turns
with a running LLM-generated summary — bounded token growth, at the cost of an extra LLM call per turn
and the summarization itself being lossy in ways that are hard to predict (a summary that drops a
detail the user references three turns later is a real, recurring failure mode).
`ConversationSummaryBufferMemory` hybridizes the two: keep recent turns verbatim up to a token
threshold, summarize anything older. `ConversationEntityMemory` extracts and tracks named entities
across turns into a structured store rather than a flat transcript. `VectorStoreRetrieverMemory` goes
further and treats memory itself as a retrieval problem — past turns are embedded and stored, and only
the turns semantically relevant to the *current* message are retrieved and injected, rather than
replaying a linear window — which is architecturally the most interesting of the group precisely
because it reduces "memory" to the same retriever abstraction as §7, rather than inventing a separate
mechanism.

**The structural problem, independent of which specific memory class you pick:** `BaseMemory`'s
contract — `load_memory_variables(inputs) -> dict` (read) and `save_context(inputs, outputs) -> None`
(write, called *after* a chain runs, mutating the memory object's internal state) — is an imperative,
side-effecting interface bolted onto a framework whose other central abstraction (`Runnable`) is
built around pure functions: same input, same config, same output, with no hidden mutable state
consulted or updated behind the caller's back. A memory-backed `ConversationChain` doesn't compose
cleanly with LCEL for exactly this reason — the memory object's state isn't part of the input or the
config, so two composed chains sharing "the conversation so far" have to share a mutable Python object
by reference, which works but sits outside everything the `Runnable` protocol makes explicit and
traceable.

`RunnableWithMessageHistory` is the bridge LangChain shipped to reconcile the two: rather than a
special `Chain` subclass, it's a wrapper around *any* `Runnable`, parameterized by a
`get_session_history(session_id) -> BaseChatMessageHistory` factory function:

```python
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.chat_message_histories import RedisChatMessageHistory

def get_session_history(session_id: str) -> RedisChatMessageHistory:
    return RedisChatMessageHistory(session_id=session_id, url="redis://localhost:6379")

chain_with_history = RunnableWithMessageHistory(
    prompt | model, get_session_history,
    input_messages_key="input", history_messages_key="history",
)

chain_with_history.invoke(
    {"input": "What's the weather in Lisbon?"},
    config={"configurable": {"session_id": "user-42"}},
)
```

This treats history as an **I/O side-channel keyed by config**, not a mutable object threaded through
the chain's business logic: before invoking the wrapped chain, it loads history for `session_id` and
injects it at `history_messages_key`; after invoking, it appends the new turn back to that same store.
The wrapped chain itself (`prompt | model`) stays a pure `Runnable` with no knowledge that memory
exists — history is entirely the wrapper's concern, addressed by the same `config` mechanism that
carries callbacks and configurable fields, which is a cleaner architectural fit than any of the
`BaseMemory` subclasses it's meant to replace.

**Why this whole area is being superseded by LangGraph's `checkpointer` rather than iterated on
further.** A checkpointer persists the *entire graph state* (of which the message list is typically
just one field) after every node execution, keyed by `thread_id` — which gives you conversation memory
as a side effect of the same mechanism that gives you crash recovery, human-in-the-loop interrupts, and
time-travel debugging (replaying execution from any earlier checkpoint). Rather than choosing among
seven memory-class variants each solving a narrow slice of "what to keep and what to forget," a
LangGraph application keeps its full state and addresses the token-growth problem the same way any
other engineering system does — explicitly, in a node you write (a summarization node, a
sliding-window trim node using `langchain_core.messages.trim_messages`, or a semantic-retrieval node
over past state) — rather than delegating that judgment call to a memory class whose summarization or
windowing heuristic you don't control at the granularity a real product usually needs. The
consolidation is the point: one persistence mechanism (checkpointing) subsumes what used to be three
separate concerns (conversation memory, crash recovery, human-in-the-loop), and LangChain's own
documentation has been steering new work toward it since LangGraph's introduction rather than adding
new memory-class variants.

---

## 12. The package split: core, community, partner, and langchain itself

Before `v0.1` (January 2024), `langchain` was a single package containing everything — abstractions,
orchestration logic, and every third-party integration anyone had contributed. The split into four
categories was a direct response to problems that monolith caused in practice, and each category has a
distinct contract:

| Package | Contract | Dependency weight | Stability guarantee | Who maintains it |
|---|---|---|---|---|
| `langchain-core` | `Runnable` protocol, every base class, message types, LCEL primitives | minimal (Pydantic) | strongest in the ecosystem | LangChain team |
| `langchain` | provider-neutral orchestration built on `langchain-core` (legacy chains, `EnsembleRetriever`, agent helpers) | moderate | stable, but larger surface than core | LangChain team |
| `langchain-community` | the long tail of third-party integrations | heavy, mostly optional | variable, integration by integration | community, uneven review |
| partner packages (`langchain-openai`, ...) | one provider's adapter to `langchain-core` interfaces | scoped to that provider's SDK | first-party tested, versioned independently | LangChain + the provider |

**`langchain-core`** holds the `Runnable` protocol and every base class described in this chapter —
`BaseChatModel`, `BaseRetriever`, `BasePromptTemplate`, `BaseOutputParser`, `BaseTool`,
`BaseCallbackHandler`, `VectorStore`, `BaseChatMessageHistory` — plus the message types
(`HumanMessage`, `AIMessage`, `ToolMessage`, `SystemMessage`) and LCEL's composition primitives
(`RunnableSequence`, `RunnableParallel`, `RunnableLambda`, and so on). Its dependency footprint is
deliberately minimal (Pydantic, and not much else) and its release cadence is deliberately slow and
carries the strongest backward-compatibility guarantees in the whole ecosystem — it's the layer
everything else, including every partner package, is built against, so breaking it breaks everything
downstream simultaneously.

**`langchain`** holds orchestration logic that isn't tied to any one provider: legacy chain classes
still in maintenance, retrieval strategies like `MultiQueryRetriever`, `EnsembleRetriever`, and
`SelfQueryRetriever` from §7, agent-construction helpers like `create_tool_calling_agent`, and other
higher-level composition patterns built purely in terms of `langchain-core` interfaces — it imports
`langchain-core` but does not depend on any specific model provider's SDK.

**`langchain-community`** is the long tail: hundreds of document loaders, vector store integrations,
tool wrappers, and chat-message-history backends, each contributed against a specific third-party
service or library, with heavier and more numerous optional dependencies (most guarded behind
try/except import blocks so installing `langchain-community` doesn't force every user to install every
vector database's client library). Quality here is explicitly variable by design — it's a
community-maintained catalog, not a first-party-tested one, and its release cadence is faster and
looser than `langchain-core`'s specifically because that looseness used to leak into the core package's
stability before the split.

**Partner packages** (`langchain-openai`, `langchain-anthropic`, `langchain-google-genai`,
`langchain-pinecone`, `langchain-cohere`, and dozens more) are first-party-maintained, often jointly
by LangChain and the provider itself, versioned and released independently of one another — a thin
adapter implementing `langchain-core`'s interfaces (`BaseChatModel`, `Embeddings`, `VectorStore`, and
so on) against exactly one provider's SDK. `langchain-openai`'s `ChatOpenAI`, for instance, is where
provider-specific behavior like OpenAI's particular tool-calling wire format or its particular
streaming chunk shape actually lives, translated at this layer into the provider-neutral shapes
(`AIMessage`, `AIMessageChunk`) the rest of the framework consumes.

**Why the split happened, concretely.** Pre-split, a bug in a community-contributed vector-store
wrapper could block a `langchain-core`-level release train shared by the whole package, because
everything shipped together; installing `pip install langchain` pulled in optional dependencies for
services most users never touched, bloating install size and surface area for supply-chain risk; and
there was no way to tell, from the package boundary alone, whether a given class was a stable interface
you could build production infrastructure against or a community integration of unknown maintenance
status. The split fixed all three by making the boundary a load-bearing engineering decision rather
than an organizational afterthought: `langchain-core`'s stability guarantee is meaningful specifically
*because* it no longer ships in the same release as `langchain-community`'s churn, and a partner
package's first-party status is a real, checkable signal (who publishes it, how often, with what test
coverage) that a community-contributed wrapper simply cannot offer at the same confidence level. The
practical implication for a codebase: **prefer partner packages over the equivalent
`langchain-community` integration whenever both exist for a provider you actually use** (e.g.
`langchain-openai`'s `ChatOpenAI` over any community-maintained alternative), and treat anything
imported from `langchain-community` as "third-party code of unverified quality that happens to share
an interface," which is a materially different trust level than `langchain-core`.

---

## 13. Anti-patterns and the framework-lock-in criticism

**Anti-pattern 1: wrapping a single API call in unnecessary composition.** If a service makes exactly
one kind of LLM call, to exactly one provider, with no retrieval, no tools, and no need to swap models
at runtime, `client.chat.completions.create(...)` (or the Anthropic SDK equivalent) is fewer lines,
fewer dependencies, and a shallower stack trace than `ChatOpenAI() | StrOutputParser()`. LCEL earns its
keep through composability and the protocol's shared retry/stream/batch machinery; a chain with one
step realizes none of that and pays the import weight and abstraction indirection for nothing. This is
the single most common "why did you use LangChain here" interview trap, and the honest answer, when
it applies, is "I wouldn't."

**Anti-pattern 2: hand-rolled output parsing against a provider with native structured output.**
Covered in depth in §5 — `PydanticOutputParser` plus format-instruction prompt injection plus
`OutputFixingParser` retry loops is solving a problem `with_structured_output` already solved at the
API level for any modern OpenAI or Anthropic model. Seeing this pattern in a 2025+ codebase is a strong
signal of either stale tutorial-following or an actual constraint (a provider or self-hosted model
without native structured-output support) that's worth asking about rather than assuming.

**Anti-pattern 3: `ConversationBufferMemory` (or no memory management at all) in a long-lived
production conversation.** Unbounded verbatim history replay is a token-cost and context-window bug
that manifests as "the bot got worse/slower/more expensive the longer you talked to it" — entirely
predictable from the mechanism in §11, and entirely preventable with a windowed, summarized, or
checkpointer-managed alternative decided on deliberately rather than defaulted into.

**Anti-pattern 4: building custom control flow on top of `AgentExecutor` instead of moving to
LangGraph.** Any requirement for a human-approval step, persistence across restarts, non-linear
branching, or parallel tool execution with differentiated error handling is fighting `AgentExecutor`'s
fixed loop shape (§9) rather than working with it. The tell in a codebase is subclassing
`AgentExecutor` or monkey-patching its `_call` method to inject custom behavior — if you're overriding
internals not designed as extension points, that's the signal to re-platform onto an explicit graph
instead.

**Anti-pattern 5: `verbose=True` and print-statement debugging instead of tracing.** `verbose=True`
produces an unstructured text dump to stdout that's fine for a five-minute local experiment and useless
for diagnosing a production incident three hops into a chain, across concurrent requests, after the
fact. LangSmith or an OTEL-based callback handler (`10-llm-observability-and-tracing.md`, planned)
gives you the parent/child run tree from §10 as structured, queryable data — the difference between
"grep a log file and hope" and "look at the span for this specific request."

**Anti-pattern 6: depending on `langchain` (or worse, `langchain-community`) when your code only
touches `langchain-core` interfaces.** If a module only imports `BaseChatModel`, `Runnable`, and
message types to define an interface your code programs against, importing the much heavier
`langchain` package (or worse, a `langchain-community` loader) for that purpose pulls in dependency
weight and update churn your code doesn't need — import from `langchain-core` directly, and from the
specific partner package for the one provider you actually call.

**Anti-pattern 7: not knowing what's actually being sent to the model.** LCEL's composability, taken
to an extreme — deeply nested `RunnableSequence`s, `RunnableBranch`es selecting between other
sequences, several layers of `.assign()` — can make "what exact string or message list reached the
model on this specific call" genuinely hard to answer by reading the chain's definition. The fix isn't
avoiding composition; it's using the tools that exist precisely for this — `chain.get_prompts()` to
inspect a chain's prompt templates statically, callbacks or LangSmith to see the literal payload of a
specific run, and `astream_events` to watch a call unfold live — rather than either guessing or (worse)
falling back to `print()` statements sprinkled through library internals you don't own.

**The framework-lock-in criticism, and where it's valid versus where it isn't.** Valid: LCEL's
operator overloading builds objects (`RunnableSequence`, `RunnableBinding`, `RunnableConfigurableFields`)
that a Python debugger steps into as generic framework frames rather than your business logic, which is
a genuine, measurable increase in the cost of debugging a wrong answer compared to a linear script of
function calls — `pdb` or an IDE debugger stepping through `Runnable.invoke` → `RunnableSequence.invoke`
→ another `Runnable.invoke` is less legible than stepping through your own three functions. Also valid:
version churn across `0.0.x` → `0.1` → `0.2` → `0.3` broke import paths repeatedly (the memory module's
reorganization and various chain classes' deprecation are the two most-cited examples), and a team
maintaining production code against LangChain has genuinely paid a non-trivial migration tax more than
once. Also valid, and covered in §5's `with_structured_output` `method` parameter and elsewhere: the
provider-neutral abstraction leaks in exactly the places where providers really aren't equivalent —
tool-calling strictness, system-message handling, JSON-mode support — so the promise of
"write once, swap providers" is real but imperfect, and treating it as complete rather than
approximate is a mistake.

Invalid, or at least overstated: "LangChain is slow." True historically for some legacy `Chain`
implementations that did unnecessary synchronous work; false for LCEL, whose own overhead — a handful
of Python function calls and dict manipulations per step — is dwarfed by orders of magnitude by
network latency and model inference time on any real call. "LangChain is unnecessary for RAG,
period." True for a single-provider, single-pipeline production service with no plan to add a second
model, a second vector store, or a second retrieval strategy — the composability the framework sells
is genuinely unused in that shape of system, and a from-scratch implementation is smaller and more
debuggable. False, and this is the crux for a Senior *Platform* Engineer interview specifically, when
the actual job is building shared infrastructure multiple teams or products build on — multiple
providers, multiple retrieval strategies, a need for uniform tracing and evaluation across all of it —
which is exactly the situation the `Runnable` protocol's uniform interface, `configurable_alternatives`
swapping, and LangSmith-wide tracing were built for. **Knowing which side of that line a given system
sits on, and saying so plainly rather than defending or attacking the framework as a monolith, is the
answer that reads as senior.**

---

## 14. LangChain versus the alternatives

**LlamaIndex** started from the opposite end of the same problem space — RAG-specific indexing and
querying primitives first, general-purpose orchestration second — and it shows in the defaults: its
`VectorStoreIndex`, `QueryEngine`, and node-postprocessor abstractions are more RAG-ergonomic
out of the box (less boilerplate to get a working retrieval-augmented query engine running) than
assembling the equivalent from LCEL primitives. Where it's historically been thinner is general-purpose
multi-step tool-calling agent orchestration and the breadth of non-RAG integrations — ground LangChain
(and now LangGraph specifically) has more actively built out. For a system whose center of gravity is
genuinely "index a corpus, query it well," LlamaIndex's defaults are often less code for an equivalent
result; for a system whose center of gravity is "coordinate several tools, models, and control-flow
branches, of which retrieval is one piece," LangChain/LangGraph's broader composition primitives carry
more of the load. Both camps have absorbed lessons from the other over time, and the gap is narrower
than it was in 2023 — but the origin-story difference in what's ergonomic by default persists.

**Haystack** (deepset) is pipeline-first and configuration-declarative in a way LangChain isn't — a
Haystack pipeline can be defined as YAML, with nodes and connections specified declaratively rather
than composed via Python operator overloading, which some production teams prefer specifically because
it separates "the pipeline's shape" from "the code that runs it" more cleanly, at some cost in
Python-native flexibility for highly dynamic control flow. Its component ecosystem and community size
are smaller than LangChain's, and its agent/tool-calling story is less mature, but for a
classic search-and-generate production pipeline with a stable, well-understood shape, it's a
legitimate and often underrated alternative.

**Raw SDK calls** (the `openai` or `anthropic` Python packages directly) are the right choice
specifically when none of LangChain's composability is going to be exercised: one provider, one
pipeline shape, a team that would rather own 200 lines of retry/streaming/tool-loop code than learn a
framework's abstractions to get equivalent behavior. The tradeoff is real and symmetric: you get full
control and a shallow, legible stack trace, and you take on reimplementing (and testing, and
maintaining) retries, streaming, batching, and tool-call-loop handling yourself, once per provider you
ever add.

**Semantic Kernel** (Microsoft) occupies a similar niche to LangChain for .NET- and enterprise-Azure-
oriented teams, with a comparable plugin/tool model; worth knowing it exists and roughly what it's for,
less commonly the center of a Python-shop system-design interview.

| Dimension | LangChain / LangGraph | LlamaIndex | Haystack | Raw SDK |
|---|---|---|---|---|
| Center of gravity | general composition across models/tools/retrieval | RAG indexing and querying | declarative search/RAG pipelines | whatever you build |
| Agent/tool-calling maturity | high (LangGraph is the reference point most others compare to) | improving, historically thinner | modest | none — you write it |
| RAG ergonomics out of the box | moderate — composed from primitives | high — purpose-built | high — pipeline-native | none |
| Declarative pipeline definition | no (Python composition) | partial | yes (YAML) | no |
| Observability | LangSmith, native callback tree | LlamaTrace / integrations | integrations | whatever you instrument |
| Dependency weight | scoped if you import correctly (§12) | moderate | moderate | minimal |
| Debuggability (stack depth) | deeper (Runnable composition frames) | moderate | moderate | shallowest |
| Best fit | platform serving multiple teams/providers | corpus-centric query engine as the product | stable, well-understood search pipeline | single provider, single pipeline |

Treat this table as a starting orientation, not a scored bake-off — every row is a "usually" that a
specific version of a specific library can violate, and the only way to make it authoritative for your
own decision is `04` §13's discipline applied to frameworks instead of retrievers: pick the two or three
rows that actually matter for your system, and measure rather than infer them.

**The decision rule worth stating plainly:** reach for LangChain (or LangGraph specifically, for
agentic control flow) when the system genuinely needs to compose across multiple models, retrieval
strategies, or tools, and needs that composition to stay swappable and uniformly traceable as the
system grows — which is the recurring shape of platform work, not of a single well-scoped product
feature. Reach for LlamaIndex when the job is fundamentally "build an excellent index and query engine
over a corpus" and agentic orchestration is secondary. Reach for the raw SDK when there's exactly one
provider, one pipeline, and a team that values minimal dependencies and maximal debuggability over
composability nobody's going to exercise. None of these are permanent commitments — a system built on
raw SDK calls that later needs to support three providers and a tool-calling agent is a legitimate
candidate for a later migration to LangChain/LangGraph, and the reverse (ripping a single-provider
LangChain chain back out to raw calls once composability turns out to be unused) is just as legitimate
an engineering call to make later, on evidence, rather than a decision to get "right" up front on
faith.

---

## 15. Interview-critical questions

The following are the questions a senior candidate should expect to be asked cold, with the answer a
strong response gives — not a script to memorize, but the shape of reasoning that shows the mechanism
is actually understood rather than the marketing. A useful self-test before an interview: for each
question, answer it twice — once from memory, and once by actually opening the relevant
`langchain-core` source file and confirming the mechanism against the code. Where the two disagree,
that's the exact gap worth closing before the interview, not after it.

**1. What problem does LangChain actually solve, in one sentence?** It standardizes the interface
(`invoke`/`batch`/`stream`, sync and async) that every model, prompt, retriever, and tool exposes, so
retries, streaming, concurrency, and tracing are implemented once in a shared protocol instead of
reimplemented per component — see §1 and §3.

**2. What is the `Runnable` protocol, and which methods are actually abstract versus which have
default implementations?** Only `invoke`/`ainvoke` are abstract; `batch`, `stream`, and their async
forms have default implementations expressed in terms of `invoke` (§3). A component only overrides the
defaults when it can do something meaningfully better — chat models override `stream` for real
token-by-token output; sequences override `transform` to chain generators rather than materializing
each step's full output before starting the next.

**3. What does the `|` operator actually do?** It calls `Runnable.__or__`, which constructs a
`RunnableSequence` — a data structure holding an ordered list of steps that itself implements the
`Runnable` protocol (§4). It is operator overloading building an object, not special syntax.

**4. Why does streaming work through a chain like `prompt | model | parser` but silently stop working
if you insert a plain `RunnableLambda` in the middle?** Because `RunnableSequence.transform` chains
each step's `transform` (an iterator-in, iterator-out generator pipeline), and `RunnableLambda`'s
`transform` only does incremental streaming if the wrapped Python function is itself a generator
function; a normal `return`-based function falls back to invoke-once-and-yield, which breaks the
incremental flow for anything downstream of it (§3, §4).

**5. How does `.batch()` achieve concurrency, and how do you bound it?** The default implementation
runs invocations concurrently — a thread pool for sync `batch`, `asyncio.gather` with a semaphore for
async `abatch` — and `config={"max_concurrency": N}` bounds the fan-out (§3), the same bounded-
concurrency tradeoff as `../python-mastery/29-async-patterns-and-pitfalls.md`.

**6. What's the difference between an output parser and `with_structured_output`?** An output parser
is a post-hoc, best-effort text-processing step relying on prompt-injected format instructions and the
model's voluntary compliance; `with_structured_output` uses the provider's native enforcement (tool-
calling forced to one tool, or a JSON-schema response-format mode), so the constraint is enforced
server-side rather than recovered client-side (§5). Prefer the latter whenever the provider supports
it; the former is a fallback for providers that don't.

**7. Walk through what happens, mechanically, when a model with `bind_tools` decides to call a
tool.** `bind_tools` translates each `BaseTool` into the provider's wire-format tool schema and returns
a `RunnableBinding` that merges `tools=[...]` into every request; the model's response is an
`AIMessage` whose `.tool_calls` list contains already-parsed `name`/`args`/`id` dicts; the caller looks
up the tool by name, invokes it with `.invoke(args)`, wraps the result in a `ToolMessage(tool_call_id=...)`,
appends it to the message list, and re-invokes the model — this loop, done by hand, *is* the agent loop
(§8).

**8. Why is `AgentExecutor` being deprecated in favor of LangGraph?** Not because it's "old" — because
its control-flow shape is fixed (one loop shape, no supported extension points for human approval,
branching, or differentiated per-step error handling) and it has no persistence, so no crash recovery,
resumability, or human-in-the-loop interrupt is possible. LangGraph makes the loop's structure an
explicit, authorable graph with a `checkpointer` giving persistence as a first-class property rather
than an afterthought (§9).

**9. What is `create_react_agent` actually building?** A two-node graph — an "agent" node calling the
tool-bound model, a "tools" node (`ToolNode`) executing whatever tool calls result — connected by a
conditional edge on whether the latest `AIMessage` has `.tool_calls`. It is the manual loop from §8,
made into an explicit, inspectable, extensible graph object (§9).

**10. How do callbacks propagate through a nested chain, and why does that matter for tracing?**
Callbacks passed via `config["callbacks"]` are wrapped in a `CallbackManager` inherited by every nested
`Runnable`; each nested call mints a child `run_id` parented to its caller's, producing a tree of runs
structurally identical to a distributed trace's span tree (§10) — which is exactly what LangSmith
renders, and exactly the mechanism `astream_events` reuses to stream fine-grained progress events to a
UI.

**11. How does `EnsembleRetriever` combine a lexical and a dense retriever, precisely?** Weighted
reciprocal rank fusion — each branch's results are scored `1/(rank + c)` with `c` defaulting to 60,
scaled by a per-branch weight, summed for documents appearing in multiple branches, and resorted (§7);
it is the concrete implementation of `04`'s RRF fusion theory, not a separate proprietary algorithm.

**12. What does `SelfQueryRetriever` actually do, and what does it require of the underlying vector
store?** It uses an LLM, given a schema of filterable metadata fields (`AttributeInfo`), to translate a
natural-language query into a structured filter plus a residual semantic-search string, then a
per-store `Translator` converts that structured filter into the vector store's native filter syntax
(§7) — it requires the vector store to support metadata filtering in the first place (`03`'s filtered-
search material), and its reliability depends on the LLM correctly inferring field names and types from
natural language, which should be tested against your actual query distribution.

**13. Why doesn't LangChain's built-in memory compose cleanly with LCEL?** `BaseMemory`'s contract
(`load_memory_variables`/`save_context`) is an imperative, mutate-a-stateful-object interface, while
`Runnable` is built around pure input/config/output with no hidden mutable state — the two don't share
a calling convention, which is why `RunnableWithMessageHistory` exists as a config-keyed I/O side-
channel wrapper rather than a `BaseMemory` subclass being composed directly into a chain (§11).

**14. Why is LangChain's memory being superseded by LangGraph's checkpointer rather than iterated
on?** A checkpointer persists the entire graph state (of which message history is one field) after
every node, keyed by `thread_id`, unifying conversation memory, crash recovery, and human-in-the-loop
resumability into one mechanism, instead of choosing among several memory-class variants each solving
one narrow slice of "what to keep and forget" with a heuristic you don't fully control (§11).

**15. What's actually different between `langchain-core`, `langchain`, `langchain-community`, and a
partner package like `langchain-openai`?** `langchain-core` holds the `Runnable` protocol and every
base class, with minimal dependencies and the strongest stability guarantee; `langchain` holds
provider-neutral orchestration logic built on `langchain-core`; `langchain-community` is a
community-maintained catalog of third-party integrations with heavier, optional dependencies and
variable quality; partner packages are first-party adapters for exactly one provider, versioned
independently (§12). The split (v0.1, Jan 2024) exists because the pre-split monolith coupled
community-integration churn and dependency bloat to core-abstraction releases.

**16. When would you *not* use LangChain?** Single provider, single well-understood linear pipeline,
no plan to add a second model/retriever/tool, and a team that values a shallow stack trace and minimal
dependencies over composability that won't be exercised — the raw SDK is fewer moving parts and easier
to debug in that specific shape of system (§13, §14).

**17. What's a legitimate criticism of LangChain, versus an overstated one?** Legitimate: deep LCEL
composition produces generic framework frames in a debugger's stack trace instead of your own business
logic, and the provider-neutral abstraction genuinely leaks where providers differ (tool-calling
strictness, JSON-mode support). Overstated: "LangChain is slow" — true for some legacy `Chain`
implementations, false for LCEL, whose own per-step overhead is negligible next to network and
inference latency (§13).

**18. How would you debug "the model gave a wrong answer" in a five-step LCEL chain?** `chain.get_prompts()`
to inspect templates statically, a LangSmith trace (or an OTEL-backed callback handler) to see the
exact payload at each step of that specific run, and `astream_events` to watch a live call unfold step
by step — not `print()` statements scattered through library internals (§10, §13).

**19. What is `astream_events`, and what's it built on?** A version-2 event stream (`on_chain_start`,
`on_chat_model_stream`, `on_tool_end`, ...) each tagged with a `run_id` and `parent_ids`, giving
fine-grained visibility into which step of a chain produced what, without knowledge of the chain's
internal structure beyond event names — it's built directly on the same callback mechanism that feeds
LangSmith tracing, just re-yielded to the caller in-process instead of (or alongside) sent to a tracer
(§3, §10).

**20. If you had to explain LCEL's actual technical contribution over legacy `Chain` classes without
overselling it, what would you say?** Legacy chains each hand-implemented `_call`/`_acall` with bespoke
input/output keys, so streaming, batching, and async support were reimplemented (often incompletely)
per chain class, with no generic way to introspect a chain's steps. LCEL's contribution is narrow and
real: one protocol (`Runnable`), implemented once, gives every composed chain batch/stream/async and a
derivable input/output schema for free — it does not make retrieval, reasoning, or generation quality
better, and claiming it does is the tell that the answer came from marketing rather than the mechanism
(§4).

**21. What's the difference between `.with_retry()` and `.with_fallbacks()`, and when do you use
each?** `.with_retry()` retries the *same* underlying `Runnable` on transient failures (rate limits,
timeouts), with configurable backoff and an exception-type filter; `.with_fallbacks()` tries a
*different* `Runnable` entirely once the primary fails, appropriate for sustained failures (a provider
outage) where retrying the same target is futile. Production chains commonly stack both — retry first,
fall back only once retries are exhausted (§4).

**22. What actually breaks if a `RunnableLambda`-wrapped function calls another `Runnable` without
forwarding its `config` argument?** The inner call starts an unparented run — it won't nest under the
outer run in a trace, and it won't inherit `max_concurrency`, tags, or callbacks from the caller. It's
not a framework bug; it's a manual forwarding step the framework can't do on your behalf once your own
imperative code is in the loop (§3).

**23. How would you add a mandatory human-approval step before one specific tool call, and why can't
`AgentExecutor` do this cleanly?** With LangGraph: an explicit node between the "agent" and "tools"
nodes that inspects which tool was requested and calls `interrupt()` to pause and persist execution via
the checkpointer until a human supplies a resume value. `AgentExecutor`'s loop has exactly one shape —
decide, act, observe, repeat — with no node graph to interpose a step into; approximating this on top of
it means overriding internals that were never built as extension points (§9).

**24. Why might you prefer a raw SDK call over `with_structured_output` even when the provider supports
native structured output?** Rare, but real: extremely latency-sensitive paths where you want zero
added client-side schema-translation overhead and are willing to hand-parse a `response_format` payload
yourself, or a provider/version combination where the framework's schema-cleanup pass (stripping
`$ref`s, tightening `oneOf`) doesn't yet support a JSON Schema feature you need and calling the API
directly gives you the missing control. The general case still favors `with_structured_output` — this
is an exception worth being able to name, not a default (§5).

**25. If a chain's output is wrong, how do you determine whether the bug is in retrieval, the prompt,
or the model, using only LangChain's own tooling?** Pull the trace (§10) and look at each step's
recorded input/output independently: the retriever's run shows exactly which documents it returned (a
retrieval-quality question, routes to `04`'s evaluation discipline); the prompt-template step's output
shows the exact string or message list the model received (a prompt-construction bug shows up here);
the model step's output shows what the model actually generated given that exact input (a genuine
generation-quality question only reachable once the first two are ruled out). The trace tree turns
"where's the bug" from a guess into a per-stage inspection, which is precisely `04`'s stage-wise
attribution discipline applied to a LangChain trace instead of a hand-rolled pipeline.

---

## 16. Mental models — the compressed set

**Everything is a `Runnable`; the protocol, not any specific class, is what's worth learning at depth.**
A `BaseChatModel`, a `PromptTemplate`, a `BaseRetriever`, a `BaseTool`, and a `RunnableSequence` built
out of all four share one interface. Learn `invoke`/`batch`/`stream`/`transform` and their defaults
once, and every component's behavior follows from it rather than needing to be memorized per class.

**The pipe operator builds a data structure; it does not execute anything.** `a | b | c` produces a
`RunnableSequence` object with `steps = [a, b, c]`. Nothing runs until `.invoke()` (or `.batch()`/
`.stream()`) is called on the result.

**Streaming through a chain requires every relevant step to implement `transform` as a real generator
pipeline, not just the final step to support streaming in isolation.** One non-generator `RunnableLambda`
in the chain breaks the incremental flow for everything downstream of it.

**A tool's usefulness to a model is exactly as good as its type hints, docstring, and Pydantic
schema — there is no additional magic making a badly-specified tool legible to the model.**

**The agent loop is not a separate mechanism from `bind_tools` plus a while-loop.** `AgentExecutor` and
`create_react_agent` both wrap the identical invoke-inspect-tool_calls-execute-append-ToolMessage-
reinvoke loop from §8; the difference between them is control-flow flexibility and persistence, not the
loop itself.

**The callback tree and LangSmith tracing are the same mechanism as `astream_events`'s progress
stream, consumed two different ways.** Both are downstream of every component firing lifecycle events
into a `CallbackManager`; one sends those events to a tracing backend, the other re-yields them
in-process to a caller.

**Memory is being replaced by checkpointing because checkpointing is a strictly more general
mechanism — persist the whole state, address token growth explicitly in your own graph node — rather
than choosing among memory-class heuristics you don't fully control.**

**The package split encodes a trust gradient, not just a dependency-management convenience.**
`langchain-core` is the stable interface; partner packages are first-party-tested adapters;
`langchain-community` is a third-party catalog of unverified, individually-variable quality that
happens to share the same interface. Treat each accordingly.

**The abstraction is worth its cost exactly when composability is actually exercised — multiple
providers, multiple retrieval strategies, uniform tracing across a platform serving several
consumers — and is a net cost when it isn't.** This is the single framing that resolves almost every
"should we use LangChain" argument, and the one worth leading with in an interview rather than a
blanket defense or a blanket dismissal.

**`.bind()` changes what's sent to the model; `.with_config()` changes how the call is orchestrated;
neither mutates the object it's called on.** Both return a new `RunnableBinding` layered over the
original, which is why stacking them in either order is safe — a small but real instance of the
broader "every wrapper in this framework is a new object, not a mutation" discipline that makes
sharing a base `model` object across multiple differently-configured call sites safe by default.

**Every criticism of LangChain worth taking seriously is a criticism of a specific layer, not the whole
framework.** "Debugging is hard" is a criticism of deep LCEL composition's stack traces, not of the
`Runnable` protocol's contract. "It's unreliable" is usually a criticism of a `langchain-community`
integration's maintenance quality, not of `langchain-core`. "It's overkill" is a criticism of applying
platform-shaped tooling to a single-consumer, single-provider problem, not of the tooling itself.
Naming the layer precisely, every time, is what separates an engineer who has actually used the
framework from one repeating a take they read.

---

## 17. Lab exercises

These labs are ordered to build the same progression this chapter argues for: hand-build the mechanism
first (Lab 1), verify the framework's convenience wrapper does what it claims rather than assuming it
(Labs 2, 4), measure rather than guess at cost and reliability claims (Labs 3, 8), and finish by
exercising the exact judgment calls §13 and §14 are about — where the abstraction earns its keep, and
where it doesn't, on a system you specify rather than a general opinion (Lab 7). None of them require a
production LangSmith account; a local trace export or even careful print-based instrumentation is
enough for every lab except the ones that explicitly call for tracing infrastructure. Treat the
"Success criterion" line in each lab as the actual deliverable — a finished script that runs without
producing that specific, falsifiable answer has not completed the lab.

**Lab 1 — Build the agent loop by hand, then replace it with `create_react_agent`, and diff the
behavior.**
*Goal:* prove the "the agent loop is just a while-loop" claim in §8 and §15 Q7/Q9 by making both
versions actually run against the same tools and prompts.
*Steps:* implement the manual loop from §8 (invoke, check `.tool_calls`, execute, append `ToolMessage`,
re-invoke, with a hard iteration cap) against two or three real tools. Then build the identical
behavior with `create_react_agent` from `langgraph.prebuilt`. Run both against a shared set of test
prompts, including at least one that should trigger two sequential tool calls and one that should
trigger zero.
*Artifact:* both implementations, plus a short note on what LangGraph's version gave you for free
(state typing, the conditional-edge graph, checkpointing) that the hand-rolled loop didn't.
*Success criterion:* both implementations produce identical tool-call sequences on the shared test
prompts, and you can point to the exact line in each where "did the model ask for a tool" is checked.
*Time:* ~3 hours.

**Lab 2 — Break streaming, then find why, using only `transform`'s contract.**
*Goal:* internalize §3/§4's claim that one non-generator step kills streaming for everything
downstream of it.
*Steps:* build a working `prompt | model | StrOutputParser()` chain and confirm token-by-token
streaming via `.stream()`. Insert a `RunnableLambda` that does a trivial `return` (not `yield`)
transformation after the parser. Confirm streaming now buffers into one chunk. Fix it by rewriting the
lambda as a generator function, and confirm streaming is restored.
*Artifact:* the three chain variants (working, broken, fixed) plus the observed difference in
`list(chain.stream(...))`'s chunk count and timing.
*Success criterion:* you can state, from the `transform` contract alone (not from having read this
document), why the broken variant behaves the way it does.

**Lab 3 — Build the RAG chain three ways and time the difference.**
*Goal:* measure whether LCEL's actual overhead (§13's "LangChain is slow" claim) is real on your
hardware and provider.
*Steps:* implement the same retrieve-then-generate pipeline three ways: (a) raw SDK calls with your
own retrieval and prompt-formatting code, (b) an LCEL chain (`{"context": ..., "question": ...} |
prompt | model | parser`), (c) a LangGraph two-node graph. Run each 50 times against the same queries
and measure wall-clock latency, isolating the framework's own overhead from network/model latency by
also timing the bare model call alone as a baseline.
*Artifact:* a latency table (p50/p95) for all three, plus the bare-model-call baseline for comparison.
*Success criterion:* a number, not an opinion, on how much (if any) latency LCEL's own machinery adds
relative to the raw-call baseline — expect it to be small enough to be dominated by network/inference
time, and be able to say by how much.

**Lab 4 — Implement `EnsembleRetriever`'s fusion from scratch, then verify against the built-in.**
*Goal:* connect §7's "it's just weighted RRF" claim to `04`'s fusion theory by actually implementing
it.
*Steps:* given two ranked result lists (from a `BM25Retriever` and a `VectorStoreRetriever` over the
same corpus), implement weighted RRF by hand: `score(doc) = Σ weight_i / (rank_i(doc) + c)` for each
branch the document appears in, `c = 60`. Compare your fused ranking against `EnsembleRetriever`'s
output on the same two retrievers and the same query set.
*Artifact:* your implementation, plus a diff (ideally empty, or explained) against the built-in
retriever's output.
*Success criterion:* your hand-rolled fusion matches `EnsembleRetriever`'s ranking, proving the
"it's just RRF" claim rather than taking it on faith. Feed the result into `04` lab 3's fusion-method
comparison if you have that lab's harness already built.

**Lab 5 — Migrate a memory-based chain to `RunnableWithMessageHistory`, then to a LangGraph
checkpointer, and compare what each buys you.**
*Goal:* make §11's deprecation argument concrete rather than asserted.
*Steps:* build a small multi-turn chatbot three ways: (a) `ConversationBufferMemory` with a legacy
`ConversationChain`, (b) `prompt | model` wrapped in `RunnableWithMessageHistory` with a persistent
backing store (Redis or SQLite), (c) `create_react_agent` (or a hand-built `StateGraph`) with a
`SqliteSaver` checkpointer. For each, kill the process mid-conversation and restart it, and see what
survives.
*Artifact:* the three implementations, plus a table of what each preserves across a process restart
(nothing, nothing without extra wiring, full state respectively) and what each costs in lines of code.
*Success criterion:* you can explain, from having built all three, exactly what "checkpointing
subsumes memory" means operationally rather than as a slogan.

**Lab 6 — Trace a five-step chain in LangSmith (or an OTEL callback handler) and answer "what exactly
was sent to the model."**
*Goal:* practice §10's and §13's debugging discipline instead of `print()`-debugging.
*Steps:* build a chain with at least five composed steps (retrieval, a `RunnablePassthrough.assign`,
a prompt, a model, an output parser). Enable tracing. Deliberately introduce a bug that produces a
wrong answer (e.g., swap which key the prompt reads context from). Using only the trace (not the code),
identify which step received the wrong input.
*Artifact:* the trace (screenshot or exported JSON) with the faulty step identified, plus the one-line
code fix.
*Success criterion:* you found the bug from the trace tree's per-step input/output, not by re-reading
the chain definition — proving the observability tooling, not just careful code reading, does the
diagnostic work.

**Lab 7 — Write the "when NOT to use LangChain" memo for a specific system.**
*Goal:* force the §13/§14 decision framework into a defensible, specific written judgment rather than
a general opinion.
*Steps:* pick a real or realistic system (yours, or one from this repo's project ladder in
[`README.md`](README.md) §5). Write a one-page memo answering: how many providers does it call, how
many retrieval strategies, does it need runtime model swapping, does it need uniform tracing across
multiple consumers, and does it need agentic control flow with persistence. Conclude with a specific
recommendation — LangChain/LangGraph, LlamaIndex, or raw SDK — and name the one or two facts about the
system that would flip your recommendation if they changed.
*Artifact:* the memo.
*Success criterion:* a reviewer who disagrees with your conclusion can point to a specific fact you
cited as wrong or a specific tradeoff you didn't weigh — the memo is falsifiable, not just an opinion
restated confidently.

**Lab 8 — Measure tool-calling reliability as a function of schema quality.**
*Goal:* turn §8's claim — "a tool's usefulness to a model is exactly as good as its type hints and
docstring" — into a measured result instead of an assertion.
*Steps:* write the same tool three ways: (a) a bare function with no docstring and untyped `**kwargs`,
wrapped in `StructuredTool` with a minimal hand-written schema; (b) the `@tool`-decorated version with
full type hints but a one-line docstring; (c) the `@tool`-decorated version with full type hints, a
complete `Args:` docstring section, and per-field `Field(..., description=...)` constraints. Build a
test set of 30–50 natural-language requests that should trigger this tool with specific argument
values, run each variant with `bind_tools([variant])` against the same model, and score both
*tool-selection accuracy* (did it call the tool at all when it should have) and *argument accuracy*
(did the extracted args match the expected values, field by field).
*Artifact:* a three-row table of tool-selection accuracy and per-field argument accuracy, plus the
three schema definitions side by side.
*Success criterion:* a measured accuracy gap between (a) and (c) large enough to make "write good
docstrings and type hints" a data-backed engineering requirement for anyone adding a tool to your
system, not a style preference.

**Lab 9 — Reproduce `EnsembleRetriever`'s failure mode when one branch is authorization-filtered and
the other isn't.**
*Goal:* connect §7's retriever composition to `04` §12's authorization-invariant material, in the
specific place LangChain's convenience wrapper can quietly violate it.
*Steps:* build an `EnsembleRetriever` from a `BM25Retriever` (no notion of per-user authorization) and
a `VectorStoreRetriever` whose `search_kwargs` include a metadata filter for the current principal.
Confirm — deliberately — that a document only the second branch's filter should exclude still surfaces
in the fused result if the BM25 branch was built over the full unfiltered corpus. Then fix it by
building the BM25 index per-principal (or filtering its output before fusion) and re-run the same
check.
*Artifact:* a failing test demonstrating the leak through the unfiltered branch, and a passing test
after the fix.
*Success criterion:* you can state precisely, from having reproduced it, why "one retriever in an
ensemble is properly filtered" is not the same guarantee as "the ensemble is properly filtered" —
`04`'s per-branch authorization invariant applies to every branch independently, and `EnsembleRetriever`
does not enforce that for you.
