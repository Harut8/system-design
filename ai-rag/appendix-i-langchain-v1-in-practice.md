# Appendix I — LangChain 1.x in practice: `create_agent`, middleware and structured output, under the hood

> **Why this appendix exists.** Chapters `07`, `20` and `21` teach the theory: constrained decoding,
> the Runnable protocol, LCEL, StateGraph, checkpointing. They were written around the 0.x API and
> only mention the 1.x API in version notes. In 2026 almost all new LangChain code is three calls:
> `init_chat_model`, `create_agent` and `response_format=`. Those calls hide a graph, a middleware
> stack and a structured-output state machine. Their defaults decide whether your agent stops, retries,
> falls back or leaks data.
>
> This page is the practice layer. It shows what each modern call compiles into, read from the source,
> which defaults bite, and the best and worst practices a staff engineer should know before approving
> a design. It does not list use cases. It explains how the code behaves when things go wrong.
>
> **Read from source, not docs.** Everything marked *(source)* was checked in `langchain==1.4.5`,
> `langchain-core==1.6.10`, `langgraph==1.2.15`, `langgraph-prebuilt==1.1.0` (October 2026).
> Everything marked *(probe)* was run against fake chat models. The scripts are in §10, so you can
> re-check them after an upgrade. Defaults change between minor versions, so pin and re-probe (§9).
>
> **Where the neighbouring detail lives:** the Runnable protocol and the 0.x package split in
> [`20-langchain-architecture-and-internals.md`](20-langchain-architecture-and-internals.md);
> StateGraph, reducers, checkpointers, `interrupt()` and stream modes in
> [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md); how constrained decoding works and how to
> design schemas in [`07-generation-and-structured-output.md`](07-generation-and-structured-output.md)
> §4–§5; tool authorization and idempotency in
> [`24-tool-calling-and-enterprise-integration.md`](24-tool-calling-and-enterprise-integration.md);
> retries and fallback as a gateway concern in [`23-multi-llm-model-gateway.md`](23-multi-llm-model-gateway.md);
> compaction strategy in [`30-context-engineering-for-agents.md`](30-context-engineering-for-agents.md).
> The wider stack (packages, reference stacks, other frameworks) is in
> [appendix J](appendix-j-production-ai-stack.md). Runtime controls, the Functional API, LangSmith, Deep Agents, the store and
> MCP adapters are in [appendix K](appendix-k-langgraph-runtime-and-platform-in-practice.md).

## Contents

1. [The 2026 surface on one page](#1-the-2026-surface-on-one-page)
2. [`create_agent`: what it compiles into](#2-create_agent-what-it-compiles-into)
3. [Middleware: six hooks, two kinds, one ordering rule](#3-middleware-six-hooks-two-kinds-one-ordering-rule)
4. [The prebuilt middleware, and the defaults that bite](#4-the-prebuilt-middleware-and-the-defaults-that-bite)
5. [Structured output: three paths and the state machine behind them](#5-structured-output-three-paths-and-the-state-machine-behind-them)
6. [Tools in 1.x: `ToolRuntime`, context, state and store](#6-tools-in-1x-toolruntime-context-state-and-store)
7. [Best practices: a production skeleton](#7-best-practices-a-production-skeleton)
8. [Worst practices: what fails in production, and why](#8-worst-practices-what-fails-in-production-and-why)
9. [When not to use `create_agent`, and version hygiene](#9-when-not-to-use-create_agent-and-version-hygiene)
10. [Probe it yourself](#10-probe-it-yourself)
11. [Interview questions](#11-interview-questions)
12. [Sources](#12-sources)

---

## 1. The 2026 surface on one page

> **In plain words.** LangChain 1.0 (October 2025) cut the `langchain` package down to a few
> agent-building calls and moved everything else into `langchain-classic`. If code imports
> `AgentExecutor`, `LLMChain`, `ConversationBufferMemory` or `create_react_agent`, it is legacy code.
>
> **Real-world example.** A team copies a 2024 tutorial with `AgentExecutor(max_iterations=6)` and
> `ConversationBufferMemory`. It works in the demo. It has no persistence, no approval step, and the
> memory grows without limit. The 1.x version of the same agent is one `create_agent` call with a
> checkpointer and two middleware.

| Job | Use in 2026 | Legacy (do not start new code with it) |
|---|---|---|
| Get a chat model | `init_chat_model("provider:model")` | Importing a provider class everywhere |
| Agent loop | `langchain.agents.create_agent` | `AgentExecutor`, `create_tool_calling_agent`, `langgraph.prebuilt.create_react_agent` (deprecated) |
| Change behaviour inside the loop | `middleware=[...]` (`AgentMiddleware`, `@before_model`, `@wrap_model_call`, …) | Subclassing the executor, custom prompt templates with `agent_scratchpad` |
| Typed final answer from an agent | `response_format=Schema` / `ToolStrategy` / `ProviderStrategy` | Parsing the last message with an output parser |
| Typed answer from one model call | `model.with_structured_output(Schema)` | `PydanticOutputParser` + format instructions in the prompt |
| Conversation memory | `checkpointer=` + `thread_id` | `ConversationBufferMemory`, `RunnableWithMessageHistory` |
| Cross-thread memory | `store=` (`BaseStore`) | Custom vector "memory" classes |
| Per-request facts (tenant, user, scopes) | `context_schema=` + `invoke(..., context=Ctx(...))` | Stuffing them into `config["configurable"]` or the prompt |
| Tool access to state, context, store | `runtime: ToolRuntime` parameter | `InjectedState`, `InjectedStore` (still work) |
| Message content across providers | `msg.content_blocks` (standard blocks: text, reasoning, tool_call, image, …) | Branching on provider-specific `content` lists |
| Long-horizon "deep" agent (planning, files, sub-agents) | `deepagents.create_deep_agent`, which is `create_agent` plus a middleware stack (*source*: `deepagents/graph.py` returns `create_agent(...)`) | Hand-rolled planner loops |

The key point: **`create_agent` is not a new runtime.** It is a function that builds a LangGraph
`StateGraph` and compiles it. Everything in `21` (checkpoints, interrupts, stream modes, subgraphs)
applies unchanged. What is new is the middleware layer and the structured-output logic, and that is
where the surprises are.

---

## 2. `create_agent`: what it compiles into

> **In plain words.** `create_agent` builds a graph with a `model` node, a `tools` node, and one extra
> node for each `before_*`/`after_*` hook in your middleware. Conditional edges decide after every
> step whether to run tools, call the model again, or stop.
>
> **Real-world example.** You add six middleware and the trace shows only four new nodes. That is
> expected. Retry and fallback middleware do not become nodes. They run *inside* the `model` node, so
> they never get their own checkpoint (§3.2).

### 2.1 The signature

```python
create_agent(
    model,                       # "provider:model" string → init_chat_model(), or a BaseChatModel
    tools=None,                  # BaseTool | callable | dict (provider built-in tools, e.g. web search)
    *,
    system_prompt=None,          # str | SystemMessage; prepended on every model call, not stored in state
    middleware=(),               # ordered; order matters (§3.3)
    response_format=None,        # Schema | ToolStrategy | ProviderStrategy (§5)
    state_schema=None,           # extend AgentState; prefer middleware.state_schema
    context_schema=None,         # type of the immutable per-run context (§6)
    checkpointer=None,           # per-thread persistence; required for interrupts / HITL
    store=None,                  # cross-thread persistence
    interrupt_before=None, interrupt_after=None,
    debug=False, name=None, cache=None, transformers=None,
) -> CompiledStateGraph
```

### 2.2 The graph it builds *(probe)*

With `ModelCallLimit`, `Summarization`, `HumanInTheLoop`, `ModelRetry`, `ModelFallback` and
`ToolCallLimit` middleware (in that order) plus one tool and a `response_format`, the compiled graph has
exactly these nodes and edges:

```
__start__ ─► ModelCallLimit.before_model ─► Summarization.before_model ─► model
                 │ (jump: end)                                              │
                 ▼                                                          ▼
              __end__ ◄── ToolCallLimit.after_model ◄───────────────────────┘
                              │
                              ▼
                    HumanInTheLoop.after_model ─► ModelCallLimit.after_model ─┬─► tools ─┬─► ModelCallLimit.before_model (loop)
                                                                             │          └─► __end__ (return_direct / structured output)
                                                                             ├─► ModelCallLimit.before_model (loop)
                                                                             └─► __end__
```

Notice three things.

1. **`ModelRetry` and `ModelFallback` are not nodes.** `wrap_model_call` hooks are composed into one
   function that runs inside the `model` node (*source*: `_chain_model_call_handlers`). The same holds
   for `wrap_tool_call` inside `tools`.
2. **`after_model` hooks run in reverse list order.** `ToolCallLimit` was last in the list and runs
   first after the model. `before_*` hooks run in list order (§3.3).
3. **There is no `max_iterations`.** The loop exits only when the model stops calling tools, a
   `return_direct` tool runs, a structured response is parsed, or a middleware jumps to `end`.

### 2.3 The routing rules *(source: `_make_model_to_tools_edge`, `_make_tools_to_model_edge`)*

After the model (or after the last `after_model` hook):

| Order | Condition | Goes to |
|---|---|---|
| 1 | A middleware set `jump_to` | `model` / `tools` / `end` as requested |
| 2 | No `AIMessage` in state (for example, history was cleared) | end |
| 3 | Last `AIMessage` has no tool calls | end (the normal exit) |
| 4 | There are tool calls with no `ToolMessage` yet, excluding structured-output tools | **one `Send("tools", [call])` per call** |
| 5 | A structured response was produced in this step | end |
| 6 | Tool calls exist but all are answered (a middleware injected `ToolMessage`s) | model |

After `tools`: end if every executed tool has `return_direct=True` or a structured-output tool
succeeded; otherwise back to the loop entry.

**Rule 4 matters most.** Each pending tool call becomes its own `Send`, so parallel tool calls run as
**concurrent tasks in one LangGraph super-step**. Two consequences:

- A model that emits `charge_card` twice in one turn charges twice at the same time. Idempotency keys
  are required, not optional (§6.3).
- Tools that return `Command(update=...)` to the same state key in one turn conflict unless that key
  has a reducer (`21` §3.3).

### 2.4 Two quiet defaults *(source)*

- **`recursion_limit=9_999`.** `create_agent` sets this with `.with_config(...)`. A plain LangGraph
  graph defaults to 25 steps, which used to stop runaway agents. Here it does not. **A runaway loop
  runs until your budget ends unless you add `ModelCallLimitMiddleware`.** Treat a call limit as
  required in production (§7).
- **Malformed tool calls are patched.** Before every model call, `_patch_invalid_tool_calls` adds a
  `ToolMessage(status="error", "...arguments were malformed or truncated")` for each
  `invalid_tool_call`. A truncated JSON argument (often caused by hitting `max_tokens`) therefore does
  not crash the run. The model sees the error and tries again. That is good, but it also hides the
  cause, so alert on `status="error"` tool messages, not only on exceptions.

---

## 3. Middleware: six hooks, two kinds, one ordering rule

> **In plain words.** Middleware is the 1.x way to change what the agent does without writing a
> graph. Some hooks are **nodes** (they run between steps and can save to state). Others are
> **wrappers** (they surround the model or tool call and can retry, swap or short-circuit it).
>
> **Real-world example.** Cutting old tool outputs from the prompt: do it in a wrapper and the
> checkpoint keeps the full history for audit, while the model sees a short prompt. Do it in a node and
> the history is deleted for good.

### 3.1 The six hooks

| Hook | Kind | Runs | Can do | Typical job |
|---|---|---|---|---|
| `before_agent` | node | once per `invoke` | update state, jump | load user profile, validate input |
| `before_model` | node | before every model call | update state (persisted), jump to `end` | trimming/summarizing history, call limits |
| `wrap_model_call` | wrapper | around every model call | change the request (model, tools, prompt, messages, response_format), call the handler 0..n times, return a different response | retry, fallback, dynamic model/tool selection, transient context edits, caching |
| `after_model` | node | after every model call | inspect `tool_calls`, update state, jump | approval (HITL), guardrails on output, tool-call limits |
| `wrap_tool_call` | wrapper | around every tool call | change args, retry, return `ToolMessage` or `Command` without running the tool | auth checks, tool retry, error-to-message, emulation in tests |
| `after_agent` | node | once at the end | update state | write-back to memory, audit record |

Decorator forms exist for one-off hooks (`@before_model`, `@after_model`, `@wrap_model_call`,
`@wrap_tool_call`, `@dynamic_prompt`). Use a class when the middleware has config, state fields or more
than one hook.

### 3.2 Node versus wrapper: what persists and what is checkpointed

This is the distinction most teams miss.

| | Node hook (`before_*`, `after_*`) | Wrapper hook (`wrap_*`) |
|---|---|---|
| Return value | a state update (`dict`) → merged through reducers → **checkpointed** | a response → only what the model/tool node returns is saved |
| Editing `messages` | **permanent**. `SummarizationMiddleware` returns `RemoveMessage(REMOVE_ALL_MESSAGES)` + summary + kept messages (*source*) | **transient**. `request.override(messages=...)` changes only this call's prompt |
| Crash in the middle | resumes from the previous checkpoint, the hook runs again | the whole `model`/`tools` node runs again, **including retries already made** |
| Visible in the graph | yes, as `"<Name>.before_model"` | no, it is inside `model` / `tools` |
| Can jump | yes, with `@hook_config(can_jump_to=[...])` and `{"jump_to": "end"}` | no; it shapes the response instead |

Rule of thumb: **do transient edits in wrappers and permanent edits in nodes.** If the audit trail must
keep every tool output (regulated domains, `29` §12), do not use a node hook that deletes messages. Use
`ContextEditingMiddleware` (a wrapper) to hide old tool results from the prompt instead.

Two mechanics to know:

- **`ModelRequest` is immutable in practice.** Use `request.override(model=..., tools=...)`. Direct
  assignment such as `request.tools = [...]` still works but raises `DeprecationWarning` (*source*:
  `ModelRequest.__setattr__`).
- **A wrapper that wants to write state** returns `ExtendedModelResponse(model_response, command=Command(update={...}))`.
  The command is applied through reducers after the model node. `goto` is not supported there.

### 3.3 The ordering rule

```
middleware = [A, B, C]

before_agent / before_model :  A → B → C           (list order)
wrap_model_call / wrap_tool_call :  A( B( C( call ) ) )   (first = outermost)
after_model / after_agent  :  C → B → A           (reverse list order)
```

It is the same onion model as HTTP middleware. Order changes behaviour, not just style:

| You want | Order | Why |
|---|---|---|
| Retry each model several times, then fall back | `[ModelFallback(...), ModelRetry(on_failure="error")]` | Fallback is outer, so it only sees an exception after the inner retries are used up |
| Retry the whole fallback chain | `[ModelRetry(...), ModelFallback(...)]` | Retry is outer and re-runs primary + fallbacks |
| Approve the *final* tool calls after other `after_model` checks | put `HumanInTheLoopMiddleware` **first** in the list | `after_model` runs in reverse, so the first entry runs last |
| Count tool calls before they are approved | put `ToolCallLimitMiddleware` **after** HITL in the list | it then runs before HITL |
| Summarize after the call-limit check | `[ModelCallLimit, Summarization, ...]` | do not pay for a summary on a call that will be refused |

Also: middleware names must be unique, and two instances of the same class raise
`AssertionError("Please remove duplicate middleware instances.")` unless you override `.name`
(*source*). For example, two `ToolCallLimitMiddleware` instances for two tools need distinct names.

### 3.4 State and context from middleware

- `state_schema` on a middleware class adds fields to the agent state. They are merged with
  `AgentState` at compile time. Mark internal counters `PrivateStateAttr` so they stay out of the
  public input and output schemas, as `ModelCallLimitState` does.
- Per-run counters use `UntrackedValue` (not checkpointed). Per-thread counters are normal fields
  (checkpointed). That is how `ModelCallLimitMiddleware` keeps `run_limit` and `thread_limit` apart
  (*source*).
- `runtime.context` is the typed `context_schema` object passed at `invoke(..., context=...)`. It is
  **not** saved in state or checkpoints. Use it for tenant, user and scopes (§6).

---

## 4. The prebuilt middleware, and the defaults that bite

> **In plain words.** The prebuilt middleware save weeks of work, but each has a default chosen for
> demos. Read the defaults below before you ship.
>
> **Real-world example.** A team adds `ModelRetryMiddleware` and `ModelFallbackMiddleware`. In a
> provider outage, users get the reply *"Model call failed after 3 attempts with APITimeoutError: …"*
> as the agent's final answer, and the fallback model is never called. The cause is one default
> (`on_failure="continue"`, below). *(probe, §10)*

| Middleware | Hook(s) | Default that bites *(source)* | Production setting |
|---|---|---|---|
| `ModelCallLimitMiddleware(thread_limit, run_limit, exit_behavior)` | `before_model`, `after_model` | `exit_behavior="end"` stops with an `AIMessage` like *"Model call limits exceeded: run limit (3/3)"*. **`structured_response` is `None`** (*probe*) | always set `run_limit`; callers must handle a missing `structured_response`; use `"error"` in batch jobs so a failure is loud |
| `ToolCallLimitMiddleware(tool_name, thread_limit, run_limit, exit_behavior)` | `after_model` | `exit_behavior="continue"` blocks extra calls with error `ToolMessage`s and **lets the model keep going** | per-tool limits on expensive or side-effecting tools; `"end"` for those |
| `ModelRetryMiddleware(max_retries=2, retry_on, on_failure, backoff_factor=2.0, initial_delay=1.0, max_delay=60, jitter=True)` | `wrap_model_call` | `on_failure="continue"` turns the final exception into an `AIMessage` with the error text. **The loop ends there (no tool calls), the user gets the error as an answer, and an outer fallback never runs.** `retry_on` defaults to every `Exception` (except `ModelError` with `is_retryable=False`), so it also retries 400s and context-length errors | `on_failure="error"`; `retry_on` limited to timeouts, 429, 5xx |
| `ModelFallbackMiddleware(first, *more)` | `wrap_model_call` | catches **every** exception, including invalid requests that will fail on the fallback too | put it outside the retry; prefer one gateway (`23`) over per-agent fallback when you have more than one agent |
| `ToolRetryMiddleware(max_retries=2, tools=None, ...)` | `wrap_tool_call` | `tools=None` means *all* tools, including non-idempotent writes | list only read-only or idempotent tools |
| `ToolErrorMiddleware(on_error)` | `wrap_tool_call` | opt-in: returning `None` re-raises | return a short message naming the error type, not `str(exc)` (stack details, SQL, internal hosts) |
| `HumanInTheLoopMiddleware(interrupt_on={tool: True \| InterruptOnConfig})` | `after_model` (+ `wrap_tool_call`) | uses `interrupt()`, so **without a checkpointer it cannot resume**. Decisions: `approve`, `edit`, `reject`, `respond` | durable checkpointer (Postgres), approvals keyed by `tool_call_id`, `allowed_decisions` as narrow as possible |
| `SummarizationMiddleware(model, trigger=None, keep=("messages", 20), trim_tokens_to_summarize=4000)` | `before_model` | **`trigger=None` never summarizes** (it is a no-op). When it runs, it **removes all messages** from state and writes summary + kept tail (permanent). The rewritten prefix also invalidates the provider prompt cache | set `trigger=("fraction", 0.7)` or `("tokens", N)`; use a cheap model; keep raw history elsewhere if you need audit |
| `ContextEditingMiddleware(edits=[ClearToolUsesEdit(...)])` | `wrap_model_call` | transient: state keeps everything, the prompt does not | preferred first step against context bloat (`30` §8) |
| `PIIMiddleware(pii_type, strategy="redact", apply_to_input=True, apply_to_output=False, apply_to_tool_results=False)` | `before_model`, `after_model` | **only the user input is scanned by default.** PII in tool results (CRM lookups, emails) goes to the model provider and the checkpoint unredacted | `apply_to_tool_results=True` for data-returning tools; regex detectors are a floor, not a DLP (`17` §8) |
| `LLMToolSelectorMiddleware(model, max_tools, always_include)` | `wrap_model_call` | one extra LLM call per model call; `on_parsing_failure="error"` | only above roughly 30 tools; consider provider tool search (`ProviderToolSearchMiddleware`, `30` §3) |
| `TodoListMiddleware()` | `wrap_model_call`, `after_model` | adds a `write_todos` tool and prompt text on every call | long multi-step tasks only |
| `ShellToolMiddleware(execution_policy=...)` | `before_agent`, `after_agent` | `execution_policy=None` means `HostExecutionPolicy`: commands run on your host | `DockerExecutionPolicy` or a real sandbox (`28` §7) |
| `LLMToolEmulator` | `wrap_tool_call` | replaces real tools with LLM-made results | tests and evals only; fail the build if it is configured in prod |

---

## 5. Structured output: three paths and the state machine behind them

> **In plain words.** There are three ways to get a typed object. Ask the provider to enforce a JSON
> schema (**provider strategy**). Give the model a fake tool whose arguments are the schema and force it
> to call a tool (**tool strategy**). Or, for one call with no loop, use `with_structured_output`. They
> fail differently, retry differently, and leave different messages in history.
>
> **Real-world example.** A ticket-triage agent returns `Triage(priority: int 1–5)`. With the tool
> strategy, the model sends `priority=7`. Pydantic rejects it, the agent sends the error back as a
> `ToolMessage`, and the model fixes it on the next call. With the provider strategy, the provider's
> schema has no `maximum` (some providers drop numeric bounds), the JSON parses, Pydantic rejects it,
> and the run **raises**. There is no retry on that path.

### 5.1 Which path you get

| You pass | What happens *(source: `create_agent`, `_get_bound_model`)* |
|---|---|
| `response_format=MySchema` (raw type) | wrapped in `AutoStrategy`. **At each model call**, if the model's profile says `structured_output` (or the model name matches a fallback regex list), it uses `ProviderStrategy`; otherwise `ToolStrategy`. Exception: Gemini models before the 3 series with tools fall back to `ToolStrategy` |
| `ToolStrategy(MySchema, handle_errors=True, tool_message_content=None)` | always the tool path |
| `ProviderStrategy(MySchema, strict=None)` | always the native path |
| `model.with_structured_output(MySchema, method=..., include_raw=False)` | one call, no loop. Base implementation: `bind_tools([schema], tool_choice="any")` + a tools parser. Partner packages override it and add `method="json_schema"` (native) next to `"function_calling"` |

Because auto-selection runs per call, **a fallback middleware can silently switch strategies** in the
middle of a run: primary model with native output, fallback model with the tool path. The message
history then looks different, and evals tuned on one shape can break on the other. Pin the strategy
explicitly when you use fallbacks.

### 5.2 `ToolStrategy`, step by step *(source + probe)*

1. At build time each schema (each member of a `Union`) becomes a `StructuredTool` named after the
   class, with the class docstring as its description.
2. At each model call those tools are added to your real tools, and **`tool_choice="any"`** is set. The
   model *must* call some tool on every turn. It can never answer in plain text. It ends the run by
   calling the schema tool.
3. If the model calls the schema tool and Pydantic validation passes, the result is
   `structured_response`, plus a `ToolMessage("Returning structured response: ...")`. The loop ends.
4. If validation fails and `handle_errors` allows it, a `ToolMessage` with
   `"Error: {error}\n Please fix your mistakes."` is added and the loop **goes back to the model**.
   If the model calls two schema tools at once, that is a `MultipleStructuredOutputsError`, handled
   the same way.
5. If the model calls real tools, they run, and the loop continues.

The transcript of a successful retry *(probe)*:

```
HumanMessage   'rate'
AIMessage      tool_calls=[Answer(score=42)]
ToolMessage    "Error: Failed to parse structured output for tool 'Answer': ... less than or equal to 10 ... Please fix your mistakes."
AIMessage      tool_calls=[Answer(score=7)]
ToolMessage    'Returning structured response: score=7'
→ structured_response = Answer(score=7)
```

What bites:

- **The retries have no limit.** `handle_errors=True` loops until the model gets it right, and
  `recursion_limit` is 9,999. A schema the model cannot satisfy (a contradictory validator, an enum the
  model never picks) burns tokens until something else stops it. *(probe: with
  `ModelCallLimitMiddleware(run_limit=3)` the run ends after 3 calls with `structured_response=None`)*
- **Validation errors are fed back to the model and saved in the checkpoint.** Pydantic messages
  include the bad `input_value`. If a field can hold user PII, pass a callable
  `handle_errors=lambda e: "priority must be 1-5"` that names the rule, not the value.
- **Raw JSON-schema `dict`s are not validated.** The tool arguments are returned as they are, and
  `handle_errors` does nothing. Use Pydantic, a dataclass or a `TypedDict`.
- **`Union[A, B]` is how you model "answer or refuse".** Each member becomes a separate tool, which is
  clearer to the model than an optional-fields-everywhere schema (`07` §5.4).
- **Forced tool choice changes the model's behaviour.** Some reasoning models do worse with forced
  tool use, and some providers do not allow extended thinking together with forced tool choice. Check
  your provider before you combine them.

### 5.3 `ProviderStrategy`, step by step *(source)*

1. The schema is sent as the provider's native `response_format` (OpenAI `json_schema` shape;
   partner packages translate it for other providers). For OpenAI-compatible models, `strict=True`
   is also set on the tools.
2. If the model returns tool calls, they run as normal. The schema applies only to the **final**
   text message, the one without tool calls.
3. The text is `json.loads`-ed and validated. **Any failure raises `StructuredOutputValidationError`.**
   There is no `handle_errors` and no retry in the loop.

Why it is still the better default when available: constrained decoding makes structural failures
rare (`07` §4), the model ends with a normal message instead of a forced tool call, and there is no
extra round trip. What it does not guarantee is **semantic** validity. Provider schema subsets drop
things like numeric ranges, regex patterns and custom validators, so Pydantic can still reject a
response that the provider accepted. Wrap the agent call in your own bounded retry, or use
`ToolStrategy` when the schema has strong validators.

### 5.4 Choosing

| Situation | Choose |
|---|---|
| One extraction call, no tools, no loop | `with_structured_output(Schema, method="json_schema")` |
| Agent with tools, provider has native structured output, schema is mostly structural | `ProviderStrategy(Schema)`, with your own outer retry (max 1–2) |
| Strong validators (ranges, cross-field rules, lookups) or "answer \| refuse" unions | `ToolStrategy(Schema, handle_errors=<callable>)` **plus** `ModelCallLimitMiddleware` |
| Fallbacks across providers | pin one strategy explicitly (§5.1) |
| You need the raw message for audit or debugging | `include_raw=True` on `with_structured_output`; on agents, the `AIMessage` is in `messages` |
| Streaming partial JSON to a UI | stream the `messages` mode and parse partial tool-call args (`07` §10). `structured_response` arrives only at the end |

`with_structured_output(..., include_raw=True)` never raises on parse failure. It returns
`{"raw": ..., "parsed": None, "parsing_error": exc}` (*source*: `.with_fallbacks(..., exception_key="parsing_error")`).
**Check `parsing_error`.** Code that reads only `["parsed"]` treats a failure as `None` and moves on.

---

## 6. Tools in 1.x: `ToolRuntime`, context, state and store

> **In plain words.** A tool can ask for a `runtime: ToolRuntime` parameter. The model never sees it.
> Through it the tool gets the per-request context (who is calling), the agent state, the long-term
> store, its own `tool_call_id`, and a stream writer.
>
> **Real-world example.** `search_kb(query)` must only search the caller's tenant. If `tenant_id` is
> a tool argument, a prompt injection can change it. If it comes from `runtime.context`, the model
> cannot touch it.

### 6.1 Three places for data

| Where | Lifetime | Saved in checkpoint | Who can change it | Put here |
|---|---|---|---|---|
| `runtime.context` (`context_schema`) | one `invoke` | **no** | only your server code | tenant, user, scopes, request id, feature flags |
| `state` (`AgentState` + middleware fields) | one thread | yes | nodes, tools via `Command(update=...)` | messages, counters, plan, intermediate results |
| `store` (`BaseStore`) | across threads | its own storage | tools, middleware | user preferences, long-term memory (`25`) |

Never put secrets or auth tokens in `state`. It is checkpointed, often traced, and sent back to clients
in stream output. Put identity in `context` and resolve credentials inside the tool (`29`).

### 6.2 Error behaviour *(source: `langgraph.prebuilt.tool_node`)*

The default `handle_tool_errors` returns **only argument-validation errors**
(`ToolInvocationError`) to the model. **Any other exception raised by a tool ends the run.** That is
a safe default, since internal errors are not shown to the model, but it means one flaky downstream
API kills the conversation. Choose per tool:

- expected business failures ("order not found") → `return` a clear string, or raise `ToolException`
  and handle it;
- transient infrastructure failures → `ToolRetryMiddleware(tools=[...])` for idempotent tools;
- everything else → `ToolErrorMiddleware(on_error=...)` returning a short message that names the error
  type, with the full error going to logs.

### 6.3 Idempotency for side-effecting tools

`runtime.tool_call_id` is stable for a given model output, and that output is checkpointed. After a
crash or an approval pause, the resumed run executes the **same** tool call id. Use it (with the thread
id) as the idempotency key for writes. That is what turns "at least once" into "effectively once"
(`24` §8, `21` §5).

---

## 7. Best practices: a production skeleton

> **In plain words.** The code below is a small, complete agent with each production concern handled
> once, in the layer where it belongs. Change the names, keep the structure.

```python
from dataclasses import dataclass

from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.agents.middleware import (
    ContextEditingMiddleware, ClearToolUsesEdit, HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware,
    PIIMiddleware, ToolCallLimitMiddleware, ToolErrorMiddleware,
)
from langchain.tools import ToolRuntime, tool
from langgraph.checkpoint.postgres import PostgresSaver


@dataclass(frozen=True)
class Ctx:                                   # per-request identity: never in state, never a tool arg
    tenant_id: str
    user_id: str
    scopes: frozenset[str]


class Resolution(BaseModel):
    """Final answer to the customer."""
    answer: str = Field(description="Reply to the customer, max 120 words.")
    cited_doc_ids: list[str] = Field(description="IDs of KB documents used; empty if none.")


class Escalation(BaseModel):
    """Use when policy does not allow an answer or a human is needed."""
    reason: str


@tool
def search_kb(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """Search the help-center articles for this customer's product. Use for policy questions."""
    hits = kb.search(query, tenant_id=runtime.context.tenant_id, k=8)   # tenant from context
    return render_hits(hits)                                           # short, ID-tagged text


@tool
def issue_refund(order_id: str, amount_cents: int, runtime: ToolRuntime[Ctx]) -> str:
    """Refund part or all of an order. Requires human approval."""
    if "refunds:write" not in runtime.context.scopes:
        return "Not permitted for this user."                          # authz in code, not in prompt
    key = f"{runtime.config['configurable']['thread_id']}:{runtime.tool_call_id}"
    payments.refund(order_id, amount_cents, idempotency_key=key)       # safe on resume and parallel calls
    return f"Refund of {amount_cents} cents queued for {order_id}."


def tool_error_message(exc: Exception, request) -> str | None:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        log.warning("tool failed", tool=request.tool_call["name"], exc_info=exc)
        return f"{request.tool_call['name']} is temporarily unavailable ({type(exc).__name__})."
    return None                                                        # unknown errors still stop the run


checkpointer = PostgresSaver(pool)                     # pool built from a DSN read from the vault;
checkpointer.setup()                                   # run once at deploy, not per request

agent = create_agent(
    model="anthropic:claude-sonnet-5-5",
    tools=[search_kb, issue_refund],
    system_prompt=SUPPORT_PROMPT,                                      # static → cache-friendly prefix
    context_schema=Ctx,
    response_format=ToolStrategy(
        Resolution | Escalation,
        handle_errors=lambda e: "The response did not match the schema. Fix the fields and try again.",
    ),
    middleware=[
        HumanInTheLoopMiddleware(                                      # first → its after_model runs last
            interrupt_on={"issue_refund": {"allowed_decisions": ["approve", "reject"]}},
        ),
        ModelCallLimitMiddleware(run_limit=12, exit_behavior="end"),  # the loop's real stop condition
        ToolCallLimitMiddleware(tool_name="issue_refund", run_limit=1, exit_behavior="end"),
        ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=60_000)]),  # transient trim
        PIIMiddleware("email", strategy="redact", apply_to_tool_results=True),
        ModelFallbackMiddleware("openai:gpt-5.5"),                     # outer: only sees exhausted retries
        ModelRetryMiddleware(max_retries=2, on_failure="error",
                             retry_on=(TimeoutError, RateLimitError, InternalServerError)),
        ToolErrorMiddleware(on_error=tool_error_message),
    ],
    checkpointer=checkpointer,
    name="support_agent",
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": text}]},
    config={"configurable": {"thread_id": f"{tenant_id}:{conversation_id}"}},
    context=Ctx(tenant_id=tenant_id, user_id=user_id, scopes=scopes),
)
answer = result.get("structured_response")       # None when a limit stopped the run → handle it
```

Why each line is there:

| Practice | Why (mechanism) |
|---|---|
| **Always set `ModelCallLimitMiddleware(run_limit=...)`** | `recursion_limit` is 9,999 (§2.4) and `ToolStrategy` retries without limit (§5.2) |
| **Read `structured_response` with `.get()` and handle `None`** | limits end the run with a plain `AIMessage` (§4) |
| **Retry inside, fallback outside, `on_failure="error"`** | otherwise the error text becomes the user's answer and the fallback never runs (§4, *probe*) |
| **Identity in `context`, not in tool arguments or state** | the model cannot forge it, and it is never checkpointed (§6.1) |
| **Authorization inside the tool, approval as middleware** | the prompt is not a security boundary (`17`, `28`); HITL adds a human, not authz |
| **Idempotency key = thread id + `tool_call_id`** | parallel `Send`s and resumes repeat calls (§2.3, §6.3) |
| **Durable checkpointer whenever HITL or multi-turn** | `interrupt()` needs it; `InMemorySaver` loses approvals on restart |
| **`thread_id` includes the tenant** | thread ids are the only key into checkpoints; a guessable id means a cross-tenant read |
| **Transient context edits before summarization** | keeps audit history and the prompt cache prefix (§3.2, §4) |
| **Static system prompt, dynamic parts at the end** | the provider prompt cache matches on prefix (`30` §10). `@dynamic_prompt` that changes per call invalidates it |
| **Narrow `retry_on`** | retrying a 400 or a context-length error only multiplies cost |
| **Pin strategy when using fallbacks** | auto-selection is per model (§5.1) |
| **Name the agent** | `name` tags the model output and the LangSmith metadata (`lc_agent_name`), which you need to tell sub-agents apart in traces |
| **Test with `GenericFakeChatModel` + `LLMToolEmulator`, eval with real models** | unit tests check graph wiring and middleware order cheaply; behaviour needs evals (`14`) |

---

## 8. Worst practices: what fails in production, and why

> **In plain words.** Each row is a pattern that works in a demo and fails under load, under attack or
> during an outage. The "mechanism" column is the reason in the code, so you can argue it in a review.

| Worst practice | Mechanism | Do instead |
|---|---|---|
| No call limit "because the model will stop" | `recursion_limit=9_999`; a confused model or an unsatisfiable schema loops | `ModelCallLimitMiddleware` on every agent; alert when it fires |
| `ModelRetryMiddleware()` with defaults | `on_failure="continue"` returns the error as the final `AIMessage`; it retries all exceptions | `on_failure="error"`, narrow `retry_on` |
| Retry and fallback in every agent, and in the gateway too | retries multiply: 3 × 3 × 3 = 27 calls per user request in an outage | one retry layer (the gateway, `23`); agents fail fast |
| `ToolRetryMiddleware()` over all tools | `tools=None` retries writes; the payment may already have gone through | list idempotent tools only; idempotency keys for the rest |
| `response_format=dict_schema` | dicts are not validated; `handle_errors` does nothing | Pydantic / dataclass / TypedDict |
| Pydantic validators that need I/O or are very strict | each failure is a full model round trip; with no limit it is unbounded | cheap structural validation in the schema; business checks after the agent, with your own bounded retry |
| `tenant_id` / `user_id` as tool parameters | the model fills them, so a prompt injection can change them | `ToolRuntime.context` |
| Secrets or tokens in state | state is checkpointed, traced and streamed | resolve credentials inside the tool from the vault |
| `SummarizationMiddleware` in a regulated workflow | deletes original messages from state (`REMOVE_ALL_MESSAGES`) | `ContextEditingMiddleware` (transient) plus an append-only audit log |
| `SummarizationMiddleware(model)` with no `trigger` | `trigger=None` never runs; context grows until the provider rejects it | set `trigger` |
| `PIIMiddleware` and assuming the data is clean | default scans user input only | `apply_to_tool_results=True`; real DLP for regulated data |
| `InMemorySaver` in production with HITL | approvals and paused runs are lost on deploy or restart | `PostgresSaver` / `RedisSaver`, with a retention policy (`21` §5.7) |
| One giant agent with 80 tools | tool definitions fill the context; selection accuracy drops | sub-agents as tools, tool search, or `LLMToolSelectorMiddleware` (`30` §3, §6) |
| Business rules in the system prompt ("never refund over $500") | the prompt is a suggestion; injection overrides it | enforce in the tool or in a `wrap_tool_call` policy |
| Changing middleware order "for readability" | order is semantics (§3.3) | treat order as config under test: assert the compiled graph in a unit test |
| A `@dynamic_prompt` that adds the current time or user data at the top | every call misses the provider prompt cache | keep the prefix static, add dynamic parts as the last message |
| Reading `result["structured_response"]` | `KeyError`/`None` when a limit or interrupt stopped the run | `.get()`, and check `__interrupt__` in the result |
| `include_raw=True` and reading only `parsed` | parse failures become `None` silently | check `parsing_error` |
| Unpinned `langchain` / `langgraph` | defaults and behaviour change in minor versions (deprecations in §9) | pin exact versions; re-run §10 probes in CI on upgrade |
| Using `create_agent` for a fixed pipeline | a loop where a sequence would do adds cost, latency and variance | plain code or LCEL for workflows (`22` §1) |

---

## 9. When not to use `create_agent`, and version hygiene

### 9.1 Choosing the level

| Situation | Use |
|---|---|
| Fixed steps (retrieve → rerank → generate) | plain functions or LCEL. Not an agent |
| One model call that returns a typed object | `with_structured_output` |
| Tool loop, standard shape, needs limits/approval/retry | `create_agent` + middleware |
| Custom control flow: per-tool branches, parallel branches with different failure handling, map-reduce, a planner and an executor with different models | your own `StateGraph` (`21`), possibly with `create_agent` sub-graphs as nodes |
| Long tasks with planning, a file system and sub-agents | `deepagents` (built on `create_agent`) or your own graph |
| Work that must survive days, with strict exactly-once side effects across systems | a durable workflow engine (Temporal) calling agents as activities (`21` §14) |
| You only use one provider and a 40-line loop is enough | the provider SDK directly. The framework is optional (`20` §13) |

Rule: **reach for middleware first, for a custom graph second.** If you need three custom middleware
that jump between nodes to express your flow, the flow is a graph. Write it as one.

### 9.2 Version hygiene

- **Pin exact versions** of `langchain`, `langchain-core`, `langgraph`, `langgraph-checkpoint-*` and
  every partner package. They are released separately and their combinations matter.
- **Deprecations to grep for on upgrade:** `create_react_agent`, `AgentExecutor`, direct assignment
  on `ModelRequest`, `SummarizationMiddleware(max_tokens_before_summary=...)` (now `trigger=`),
  `request.system_prompt` (now `system_message`).
- **Checkpoint compatibility is a data migration.** Checkpoints are serialized state. Adding a
  required field to the state schema or renaming middleware (node names change) can break resuming
  threads that were paused before the deploy. Test "resume a thread paused on the old version" in CI.
- **Read the source when a default matters.** The code is short:

```bash
python -c "import langchain, pathlib; print(pathlib.Path(langchain.__file__).parent)"
# then read: agents/factory.py (create_agent, edges), agents/structured_output.py,
#            agents/middleware/types.py (hooks), agents/middleware/<name>.py (defaults)
```

---

## 10. Probe it yourself

> **In plain words.** These scripts use fake chat models, so they cost nothing and need no API key.
> Run them after every upgrade. If an output changes, a default changed.

```bash
uv venv && uv pip install "langchain==1.4.5" "langgraph==1.2.15"
```

**Probe A: the compiled graph and its config.**

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain.agents import create_agent
from langchain.agents.middleware import (HumanInTheLoopMiddleware, ModelCallLimitMiddleware,
    ModelFallbackMiddleware, ModelRetryMiddleware, SummarizationMiddleware, ToolCallLimitMiddleware)
from langchain.tools import tool
from pydantic import BaseModel

class Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self

@tool
def search(q: str) -> str:
    """Search docs."""
    return "result"

class Answer(BaseModel):
    text: str

m = Fake(messages=iter([AIMessage("hi")]))
agent = create_agent(m, [search], response_format=Answer, middleware=[
    ModelCallLimitMiddleware(run_limit=8), SummarizationMiddleware(m, trigger=("tokens", 4000)),
    HumanInTheLoopMiddleware({"search": True}), ModelRetryMiddleware(), ModelFallbackMiddleware(m),
    ToolCallLimitMiddleware(run_limit=5),
])
for e in agent.get_graph().edges:
    print(e.source, "->", e.target, "(cond)" if e.conditional else "")
print(agent.config)   # {'recursion_limit': 9999, ...}
```

Expected: no node for `ModelRetry` or `ModelFallback`; `model -> ToolCallLimitMiddleware.after_model`
(reverse order); `recursion_limit: 9999`.

**Probe B: `ToolStrategy` retry and a limit stopping it.**

```python
from langchain.agents.structured_output import ToolStrategy
from pydantic import Field

class Score(BaseModel):
    """Final answer."""
    score: int = Field(ge=0, le=10)

bad  = AIMessage("", tool_calls=[{"name": "Score", "args": {"score": 42}, "id": "c1"}])
good = AIMessage("", tool_calls=[{"name": "Score", "args": {"score": 7},  "id": "c2"}])

a = create_agent(Fake(messages=iter([bad, good])), [], response_format=ToolStrategy(Score))
print(a.invoke({"messages": [{"role": "user", "content": "rate"}]})["structured_response"])  # score=7

b = create_agent(Fake(messages=iter([bad] * 50)), [], response_format=ToolStrategy(Score),
                 middleware=[ModelCallLimitMiddleware(run_limit=3)])
out = b.invoke({"messages": [{"role": "user", "content": "rate"}]})
print(out["messages"][-1].content, out.get("structured_response"))
# 'Model call limits exceeded: run limit (3/3)' None
```

**Probe C: retry default hides the fallback.**

```python
class Down(GenericFakeChatModel):
    def _generate(self, *a, **k):
        raise TimeoutError("primary down")

backup = GenericFakeChatModel(messages=iter([AIMessage("from backup")] * 5))
for on_failure in ("continue", "error"):
    agent = create_agent(Down(messages=iter([])), [], middleware=[
        ModelFallbackMiddleware(backup),
        ModelRetryMiddleware(max_retries=1, initial_delay=0, jitter=False, on_failure=on_failure),
    ])
    print(on_failure, agent.invoke({"messages": [{"role": "user", "content": "hi"}]})["messages"][-1].content)
# continue → 'Model call failed after 2 attempts with TimeoutError: primary down'
# error    → 'from backup'
```

**Turn the probes into tests.** Put probe A's edge list in a snapshot test. Middleware order then
becomes a reviewed change, not an accident.

---

## 11. Interview questions

**Q1. What does `create_agent` return, and how is it different from `AgentExecutor`?**
A compiled LangGraph `StateGraph`: a `model` node, a `tools` node, a node for each `before_*`/`after_*`
middleware hook, and conditional edges. Wrapper hooks run inside the `model` and `tools` nodes.
Unlike `AgentExecutor`, it is checkpointable, can be interrupted, streams, and can be extended
through middleware, and it can be used as a sub-graph.

**Q2. Your agent ran 400 model calls on one request. Why didn't LangGraph stop it?**
`create_agent` sets `recursion_limit=9_999`. The loop only ends when the model stops calling tools, a
structured response parses, or middleware jumps to `end`. Fix: `ModelCallLimitMiddleware(run_limit=...)`,
an alert when it fires, and check for an unsatisfiable `ToolStrategy` schema.

**Q3. During a provider outage users received "Model call failed after 3 attempts…" as an answer, and
the fallback model was never used. Explain.**
`ModelRetryMiddleware` defaults to `on_failure="continue"`. It turns the final exception into an
`AIMessage`, which has no tool calls, so the loop ends and the fallback (outer wrapper) sees a success.
Use `on_failure="error"` with fallback outside retry.

**Q4. `ToolStrategy` vs `ProviderStrategy`: how does each fail?**
`ToolStrategy` forces `tool_choice="any"`, validates the schema-tool arguments, and on failure sends
the error back as a `ToolMessage` and retries without limit. `ProviderStrategy` uses native
constrained decoding on the final message and raises `StructuredOutputValidationError` on any parse or
validation failure, with no in-loop retry. A raw schema chooses between them per model call.

**Q5. Where should you trim conversation history: `before_model` or `wrap_model_call`?**
`before_model` changes are saved to state (permanent, checkpointed). `wrap_model_call` changes affect
only that call's prompt. Trim transiently in a wrapper when you need the full history for audit or
for later turns. Rewrite state in a node only when you want the history itself to shrink.

**Q6. Three middleware `[A, B, C]` all implement `before_model`, `wrap_model_call` and `after_model`.
In what order do they run?**
`before_model`: A, B, C. `wrap_model_call`: A wraps B wraps C wraps the call. `after_model`: C, B, A.

**Q7. How do you stop a prompt injection from making `search_kb` read another tenant's data?**
Do not let the model supply the tenant. Pass it in `context_schema` at `invoke`, read
`runtime.context.tenant_id` in the tool, and enforce the filter in the retrieval layer (`04` §12).

**Q8. A model emits the same `transfer_funds` call twice in one turn. What happens, and what protects
you?**
Each pending tool call becomes a separate `Send`, so both run concurrently in one super-step. Protect
with an idempotency key (thread id + `tool_call_id` or a business key), a
`ToolCallLimitMiddleware(tool_name="transfer_funds", run_limit=1)`, and HITL approval.

**Q9. Why can adding `ModelFallbackMiddleware` change the shape of your message history?**
With a raw schema, the strategy is chosen per model. A primary model with native structured output and
a fallback without it produce different transcripts (final text vs a forced tool call plus a
`ToolMessage`). Pin the strategy.

**Q10. What breaks when you deploy a middleware rename while threads are paused on approval?**
Node names come from middleware names. The checkpoint records the next node to run. A paused thread
may point to a node that no longer exists, or state may lack a newly required field. Treat state schema
and middleware names as a versioned contract, and test resuming old checkpoints.

---

## 12. Sources

- `langchain` 1.4.5 source: `langchain/agents/factory.py` (`create_agent`, `_get_bound_model`,
  `_handle_model_output`, `_chain_model_call_handlers`, edge builders, `recursion_limit`),
  `langchain/agents/structured_output.py`, `langchain/agents/middleware/types.py`, and each file under
  `langchain/agents/middleware/`. Repository: <https://github.com/langchain-ai/langchain> (`libs/langchain_v1`).
- `langgraph` 1.2.15 / `langgraph-prebuilt` 1.1.0 source: `langgraph/prebuilt/tool_node.py`
  (`ToolNode`, `ToolRuntime`, `_default_handle_tool_errors`). Repository:
  <https://github.com/langchain-ai/langgraph>. The `recursion_limit` change references
  langgraph issue 7313.
- `langchain-core` 1.6.10: `BaseChatModel.with_structured_output` in
  `langchain_core/language_models/chat_models.py`.
- `deepagents` 0.7.25: `deepagents/graph.py` (`create_deep_agent` returns `create_agent(...)`).
- LangChain docs: Agents, Middleware, Structured output, Tools, Short-term memory —
  <https://docs.langchain.com/oss/python/langchain/>.
- LangChain 1.0 release notes and migration guide (October 2025):
  <https://docs.langchain.com/oss/python/migrate/langchain-v1>.
