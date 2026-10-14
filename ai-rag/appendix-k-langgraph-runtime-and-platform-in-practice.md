# Appendix K — LangGraph runtime and platform in practice: Functional API, durability, caching, streaming, LangSmith, Deep Agents, store and MCP

> **Why this appendix exists.** [Appendix I](appendix-i-langchain-v1-in-practice.md) covers
> `create_agent`, middleware and structured output. [Appendix J](appendix-j-production-ai-stack.md)
> maps the whole stack. This page covers the rest of the LangChain toolset a production team uses
> every day and that no chapter covered in practice:
>
> - the **Functional API** (`@entrypoint`, `@task`), the second way to write LangGraph;
> - the **runtime controls**: durability modes, retry, timeouts, caching, rate limiting, streaming;
> - **LangSmith** as an engineering tool, not a dashboard;
> - **Deep Agents** in depth;
> - the long-term **store** and **MCP adapters** in practice.
>
> Same method as appendix I: each section says how to use the tool, what it does inside, and the
> best and worst practices. Claims marked *(source)* were read in the installed code; claims marked
> *(probe)* were run. Versions: `langgraph` 1.2.15, `langchain-core` 1.6.10, `langsmith` 0.14.8,
> `deepagents` 0.7.25, `langchain-mcp-adapters` 0.3.2 (October 2026). The probe scripts are in §7.
>
> **Where the neighbouring detail lives:** `StateGraph`, reducers, checkpointing, interrupts and the
> original stream-mode tour in [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md); durable
> execution vs Temporal in `21` §14; evaluation method in
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) and
> [`14-agent-evaluation.md`](14-agent-evaluation.md); memory design in
> [`25-memory-and-state-management.md`](25-memory-and-state-management.md); MCP itself in
> [`26-mcp-and-agent-protocols.md`](26-mcp-and-agent-protocols.md).

## Contents

1. [The Functional API: `@entrypoint` and `@task`](#1-the-functional-api-entrypoint-and-task)
2. [Runtime controls: durability, retry, timeouts, caching, rate limits, streaming](#2-runtime-controls-durability-retry-timeouts-caching-rate-limits-streaming)
3. [LangSmith in practice](#3-langsmith-in-practice)
4. [Deep Agents in practice](#4-deep-agents-in-practice)
5. [The long-term store in practice](#5-the-long-term-store-in-practice)
6. [MCP adapters in practice](#6-mcp-adapters-in-practice)
7. [Probe it yourself](#7-probe-it-yourself)
8. [Interview questions](#8-interview-questions)
9. [Sources](#9-sources)

---

## 1. The Functional API: `@entrypoint` and `@task`

> **In plain words.** You can write a LangGraph workflow as ordinary Python: an `@entrypoint`
> function that calls `@task` functions. You get checkpoints, interrupts and streaming without
> drawing a graph. The catch: on resume, the entrypoint function runs again **from the top**, and
> only the task results are replayed from the checkpoint.
>
> **Real-world example.** An approval workflow sends a Slack message, then calls `interrupt()` to
> wait for a manager. The Slack call is written directly in the entrypoint, not in a task. When the
> manager approves the next morning, the workflow resumes, runs the entrypoint from the top, and the
> manager gets the same Slack message a second time. *(probe: code outside tasks ran twice, the task
> ran once)*

### 1.1 What it compiles into

```python
from langgraph.func import entrypoint, task
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import interrupt

@task
def draft_reply(ticket: dict) -> str:          # result is saved in the checkpoint
    return llm.invoke(prompt(ticket)).text

@task
def send_reply(ticket_id: str, text: str) -> None:
    helpdesk.reply(ticket_id, text, idempotency_key=f"reply:{ticket_id}")

@entrypoint(checkpointer=checkpointer)          # compiles to a Pregel graph
def handle_ticket(ticket: dict) -> dict:
    text = draft_reply(ticket).result()         # a task call returns a future
    decision = interrupt({"draft": text})       # pause; resume with Command(resume=...)
    if decision["approved"]:
        send_reply(ticket["id"], decision.get("text", text)).result()
    return {"sent": decision["approved"]}
```

*(source: `langgraph/func/__init__.py`)*

- `@entrypoint(...)` returns a **`Pregel`** object, the same runtime class a compiled `StateGraph`
  produces. It has `invoke`, `stream`, `get_state`, and it can be used as a sub-graph.
- Each `@task` call schedules a task in the running graph and returns a **future**. Calling several
  tasks before calling `.result()` runs them **in parallel**.
- Task results are written to the checkpoint (as pending writes, appendix J §2.2). On resume, a task
  that already finished returns its saved result and does not run again.
- The entrypoint body itself is **not** a task. It re-runs on every resume.
- Tasks can only be called inside an entrypoint or a `StateGraph` node. Their inputs and outputs must
  be serializable when a checkpointer is used.

### 1.2 Replay rules

| Code | On resume after an interrupt or a crash |
|---|---|
| Inside a finished `@task` | not run again; saved result returned *(probe)* |
| Inside a `@task` that was running when the process died | run again (make it idempotent) |
| In the entrypoint body | **run again** *(probe)* |
| `interrupt()` that was already answered | returns the saved resume value |

So the entrypoint body must be **deterministic** and **free of side effects**. Everything that
calls a model, reads the clock, generates a random ID or touches an external system goes in a task.

**Changing a deployed workflow while threads are paused is a migration.** Saved task results are
matched to task calls on resume. If you reorder, add or remove task calls in the entrypoint, a
resumed run can receive results that belong to a different call. Version the workflow, and either
drain paused threads or test resuming old checkpoints before you deploy.

### 1.3 State across runs: `previous` and `entrypoint.final`

The functional API has no state schema. The value an entrypoint returns is saved per thread, and
the next run on the same thread receives it as the `previous` argument. `entrypoint.final(value=...,
save=...)` returns one value to the caller and saves another *(source, probe)*:

```python
@entrypoint(checkpointer=checkpointer)
def running_total(n: int, *, previous: int | None = None) -> entrypoint.final[int, int]:
    total = (previous or 0) + n
    return entrypoint.final(value=total, save=total)

# same thread_id: invoke(1) → 1, invoke(2) → 3, invoke(3) → 6
```

There are no reducers. If the saved value is a growing list of messages, it has the same quadratic
storage growth as `MessagesState` (appendix J §2.2).

### 1.4 Functional API or `StateGraph`

| You need | Use |
|---|---|
| A linear or branching workflow that reads like a script, with approval steps | Functional API |
| Existing Python code to make durable with few changes | Functional API (wrap the I/O in tasks) |
| Several nodes writing the same state keys, merged by reducers | `StateGraph` |
| A graph you can draw, inspect and review as a diagram | `StateGraph` |
| Routing decided by state, with many conditional edges, or `Command(goto=...)` handoffs | `StateGraph` |
| Static `interrupt_before` / `interrupt_after` on named nodes | `StateGraph` |
| A tool-calling agent | `create_agent` (a `StateGraph` underneath) |

Both share the same checkpointer, store, streaming and `interrupt()`. A `StateGraph` node can call
tasks, and an entrypoint can call a compiled graph.

### 1.5 Best and worst practices

| Do | Why |
|---|---|
| Put every model call, tool call, clock read and ID generation in a task | the entrypoint re-runs on resume (§1.2) |
| Give side-effecting tasks idempotency keys | a task interrupted mid-run runs again |
| Start independent tasks first, then call `.result()` | futures run in parallel only if you do not wait on each one immediately |
| Keep task inputs and outputs small and serializable | they are stored in the checkpoint |

| Don't | What happens |
|---|---|
| Send a notification or write to a DB in the entrypoint body | it repeats on every resume |
| `uuid4()` or `datetime.now()` in the entrypoint body | different values on resume; the run diverges from its checkpoint |
| Pass a database connection or a client object into a task | serialization fails when a checkpointer is used |
| Reorder task calls in a workflow with paused threads | resumed runs can get the wrong saved results |

---

## 2. Runtime controls: durability, retry, timeouts, caching, rate limits, streaming

### 2.1 Durability modes

> **In plain words.** `durability` decides **when** checkpoints are written: before the next step
> (`"sync"`), in the background while the next step runs (`"async"`), or only when the run ends
> (`"exit"`). Faster modes lose more work in a crash.
>
> **Real-world example.** A team sets `durability="exit"` to cut database writes. A worker is
> OOM-killed during step 3 of 3. When the run is retried, steps 1 and 2 run again, including the
> step that charged the customer. *(probe below)*

Set per call: `graph.invoke(input, config, durability="sync")`. Type:
`Durability = Literal["sync", "async", "exit"]` *(source)*.

*(probe: three nodes a → b → c, `SqliteSaver`, failure in node c, then resume in a new process)*

| Mode | Python exception in c | Process hard-killed in c (`os._exit`, like OOM-kill) |
|---|---|---|
| `"sync"` | resume re-runs only **c** | resume re-runs only **c** |
| `"async"` | resume re-runs only **c** | **depends on timing**: with 0.2 s steps, 5 of 5 runs resumed at c; with instant steps, 5 of 5 lost earlier steps (2 back to the start, 3 back to a) |
| `"exit"` | resume re-runs only **c** (state is saved when the run exits with the error) | **nothing was saved**: a fresh run re-runs **a, b and c** |

Choose:

| Workload | Mode |
|---|---|
| Any step with side effects, or approvals | `"sync"` |
| Chat agents where a lost step only costs tokens | `"async"` (the checkpoint write races the next step; a kill can lose the last steps) |
| Short, pure, cheap runs where checkpoints are only for the final state | `"exit"` |

### 2.2 `RetryPolicy`: what it retries by default

`RetryPolicy(initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, max_attempts=3,
jitter=True, retry_on=default_retry_on)` attaches to a node (`add_node(..., retry_policy=...)`) or a
task (`@task(retry_policy=...)`) *(source)*.

`default_retry_on` *(source, probe)*:

| Exception | Retried by default? |
|---|---|
| `ConnectionError` | yes |
| `httpx.HTTPStatusError` / `requests.HTTPError` with 5xx | yes |
| `ValueError`, `TypeError`, `KeyError`, `RuntimeError` and other programming errors | no |
| **`TimeoutError`** | **no**, because it is a subclass of `OSError`, which is in the "do not retry" list |
| Any other exception (for example a provider SDK's `RateLimitError`) | yes |

The `TimeoutError` row surprises teams. A node whose HTTP client raises the built-in
`TimeoutError` is **not** retried. Pass `retry_on` explicitly.

Compare with appendix I §4: LangChain's `ModelRetryMiddleware` retries **every** exception by
default. Two layers, two opposite defaults. Pick **one** retry layer per call path:

| Layer | Retry here when |
|---|---|
| Gateway (`23`) | provider errors for all agents |
| `ModelRetryMiddleware` / `ToolRetryMiddleware` | a single agent without a gateway |
| `RetryPolicy` on a node or task | non-LLM I/O inside a workflow (databases, APIs) |

### 2.3 Timeouts

Nodes and tasks take `timeout=` as seconds, a `timedelta`, or
`TimeoutPolicy(run_timeout=..., idle_timeout=..., refresh_on="auto" | "heartbeat")` *(source)*.

- `run_timeout` is a hard wall-clock limit. `idle_timeout` resets on progress; call
  `runtime.heartbeat()` from long work that does not stream.
- When it fires, `NodeTimeoutError` is raised, and the retry policy decides what happens next.
- **Timeouts work only for async nodes and tasks.** Sync code cannot be cancelled safely in-process
  *(source docstring)*. A sync node that hangs on a socket hangs the run. Use async I/O, or set
  timeouts on the client itself (`httpx.Client(timeout=...)`), which is good practice anyway.

### 2.4 `CachePolicy`: node and task caching

`CachePolicy(key_func=default_cache_key, ttl=None)` on a node or task. The default key is a pickle
hash of the input *(source)*.

**The policy does nothing unless the graph has a cache backend.** `@entrypoint(cache=InMemoryCache())`
or `graph.compile(cache=...)`. *(probe: with `cache=`, two calls ran the task once; without it, every
call ran it)*. No warning is shown.

| Do | Don't |
|---|---|
| Cache pure, expensive steps: embeddings, parsing, deterministic classification | cache side-effecting tasks (the side effect is skipped on a hit) |
| Include the tenant and the model/prompt version in `key_func` | rely on the input alone when two tenants can send the same input |
| Set `ttl` | cache forever, then ship a prompt change that the cache hides |
| Use a shared cache backend across replicas | assume an in-memory cache helps after a deploy or across pods |

### 2.5 `InMemoryRateLimiter`

`init_chat_model(..., rate_limiter=InMemoryRateLimiter(requests_per_second=..., max_bucket_size=...))`
is a token bucket in **one process**, counting **requests**, not tokens *(source)*.

- With 8 replicas and `requests_per_second=5`, the provider sees up to 40 requests per second.
- Provider limits are usually on tokens per minute too, which this limiter does not count.
- Use it for scripts, batch jobs and local evals. For services, enforce limits in the gateway (`23` §6).

### 2.6 Streaming in 1.x

> **In plain words.** One run can stream several kinds of events at once: state after each step,
> only the changes, LLM tokens, your own progress events, and debug events. Version 2 of the stream
> format gives every event the same shape, so a frontend does not need to guess.

Stream modes *(source: `StreamMode`)*:

| Mode | Emits | Use for |
|---|---|---|
| `values` | full state after each step (functional API: once at the end) | debugging; small states only (`O(n²)` traffic, `21` §7.7) |
| `updates` | each node's or task's returned update | progress UI, logs |
| `messages` | LLM tokens + metadata (node name, tags) from any model call inside nodes or tasks | chat UIs |
| `custom` | whatever you send with `get_stream_writer()` | "searching 3 sources…" progress messages |
| `checkpoints` | an event per checkpoint | audit, external replication |
| `tasks` | task start and finish, with results and errors | live run monitors |
| `debug` | `checkpoints` + `tasks` | development only |

Output shapes:

| Call | You receive |
|---|---|
| `stream(..., stream_mode="updates")` (v1, default) | the chunk |
| `stream(..., stream_mode=["updates", "messages"])` (v1) | `(mode, chunk)` tuples |
| `stream(..., subgraphs=True)` (v1) | `(namespace, mode, chunk)` tuples |
| `stream(..., version="v2")` | typed dicts: `{"type": ..., "ns": (...), "data": ..., ...}` *(source: `StreamPart`)* |

Use `version="v2"` for new code. One shape for every combination of modes and sub-graphs is easier
to forward over SSE and to type-check.

```python
from langgraph.config import get_stream_writer

@task
def search_sources(q: str) -> list[str]:
    writer = get_stream_writer()
    writer({"stage": "search", "status": "started"})       # appears as a "custom" part
    hits = search(q)
    writer({"stage": "search", "status": "done", "count": len(hits)})
    return hits

async for part in app.astream(inp, cfg, stream_mode=["messages", "custom", "updates"], version="v2"):
    if part["type"] == "messages":
        token, meta = part["data"]
        if meta.get("langgraph_node") == "model":           # skip tokens from internal LLM calls
            await sse.send("token", token.text)
    elif part["type"] == "custom":
        await sse.send("progress", part["data"])
```

| Do | Don't |
|---|---|
| Filter `messages` by node or tag | forward every token: summarizers, routers and judges inside the run also stream tokens |
| Stream `updates` + `custom` to the UI, persist from the checkpointer | rebuild state on the client from `values` |
| Let the run continue if the client disconnects; let the client reconnect and read state | tie the run's life to the HTTP connection (appendix J §5.1) |
| Redact before streaming | stream raw tool outputs with PII to the browser |

---

## 3. LangSmith in practice

> **In plain words.** LangSmith records each run as a tree of spans, stores datasets of examples,
> and runs experiments that score a version of your app against a dataset. Used well, it answers
> "did this change help?". Used badly, it is an expensive copy of your users' private data.
>
> **Real-world example.** A team sets `LANGSMITH_TRACING_SAMPLING_RATE=0.05` to cut cost. A week
> later a customer reports a wrong answer, and the trace is not there. Sampling is decided per trace
> **before** the outcome is known, so 95% of failures are dropped too.

### 3.1 How tracing works

*(source: `langsmith/client.py`, `run_helpers.py`)*

1. LangChain and LangGraph runs emit callback events. `@traceable` and `wrap_openai` /
   `wrap_anthropic` do the same for plain code.
2. Each run becomes a node in a run tree (trace ID + dotted order).
3. Runs go into an in-process queue and are sent in **batches by a background thread**
   (`auto_batch_tracing=True`).
4. On process exit, queued runs are flushed. In serverless functions and short jobs, call
   `client.flush()` (or `wait_for_all_tracers()` for LangChain runs) before returning, or the last
   traces are lost.

Tracing adds little latency to the request path because sending is asynchronous. It does add memory
and CPU for serialization, which you notice with large states.

### 3.2 Sampling

`LANGSMITH_TRACING_SAMPLING_RATE` (or `Client(tracing_sampling_rate=...)`) keeps a fraction of
traces. The decision is a deterministic function of the **trace ID** *(source:
`_filter_for_sampling` → `is_sampled_by_id`)*, so a trace is kept or dropped as a whole. That is
good: no half-traces. But it is **head sampling**: it cannot keep errors preferentially.

| Need | Do |
|---|---|
| Every failed run for debugging | log errors with the trace ID in your own logs; tracing at 100% for error-prone flows; or OTel export with tail sampling in your collector |
| Cost control | sample low-value high-volume traffic (health checks, autocompletes), keep 100% on critical flows |
| Evals | evals run on datasets, so they do not depend on trace sampling |

### 3.3 Redaction and data handling

*(source: `Client(hide_inputs=..., hide_outputs=..., anonymizer=...)`, `langsmith.anonymizer.create_anonymizer`)*

- `hide_inputs` / `hide_outputs`: `True` drops them; a function transforms them.
- `anonymizer`: applied to the whole payload; `create_anonymizer([...regex rules...])` builds one.
- **`TracePolicy` on LangGraph nodes and middleware is not redaction.** Its own docstring says it only
  shapes the node's own run and "is not intended to redact secrets"; use the client's
  `hide_inputs`/`hide_outputs`/`anonymizer` *(source: `langgraph/types.py`)*.

Before the first production trace, decide and write down: the region and retention of trace data,
what is redacted, who in the company can read traces, and how a user deletion request reaches trace
data (`25` §10).

### 3.4 Datasets and experiments

```python
from langsmith import Client, evaluate

client = Client()
ds = client.create_dataset("support-golden-v3")
client.create_examples(dataset_id=ds.id, examples=[
    {"inputs": {"question": q}, "outputs": {"answer": a, "doc_ids": ids}} for q, a, ids in golden
])

def target(inputs: dict) -> dict:                     # the system under test
    out = agent.invoke({"messages": [{"role": "user", "content": inputs["question"]}]},
                       context=TEST_CTX)
    r = out.get("structured_response")
    return {"answer": r.answer if r else None, "doc_ids": r.cited_doc_ids if r else []}

def cited_correct_doc(outputs: dict, reference_outputs: dict) -> dict:   # deterministic first
    hit = bool(set(outputs["doc_ids"]) & set(reference_outputs["doc_ids"]))
    return {"key": "cited_correct_doc", "score": int(hit)}

results = evaluate(
    target, data="support-golden-v3",
    evaluators=[cited_correct_doc, faithfulness_judge],   # judge calibrated per 08 §11
    experiment_prefix="prompt-v14",
    max_concurrency=8,                                     # default 0 = one example at a time
    num_repetitions=3,                                     # agents are stochastic: measure pass^k (14)
)
```

Practice notes:

- **`max_concurrency` defaults to 0, which runs examples one at a time** *(source)*. A 300-example
  agent eval then takes hours. Set it, within your provider rate limit.
- **Repeat runs** (`num_repetitions`) for agents. One run per example measures luck (`14` §6).
- **Version the dataset** and keep its split fixed. Adding new examples to the same dataset name
  makes old and new experiments incomparable.
- **Deterministic evaluators first**, judges second (`08` §11). A judge without measured agreement
  with human labels is not a gate.
- The **pytest plugin** (`@pytest.mark.langsmith`, registered by `langsmith/pytest_plugin.py`) is a good fit for a small
  CI gate. Full experiments belong in a scheduled job.

### 3.5 Online evaluation and feedback

- Online evaluators score a sample of production traces automatically. Treat their output as a
  monitoring signal with false positives, not as ground truth.
- User feedback (thumbs, corrections) attaches to a run ID. Return the run ID to the frontend so
  feedback can be linked.
- Promote failed production traces into the dataset after a human labels them. That is the label
  flywheel (`08` §15).

### 3.6 Best and worst practices

| Do | Don't |
|---|---|
| One project per environment and service (`LANGSMITH_PROJECT`) | mix dev, staging and prod traces in one project |
| Tag traces with tenant, release and prompt version (metadata) | try to find a regression by scrolling through traces |
| `flush()` at the end of serverless handlers and batch jobs | lose the last traces of every job |
| Redaction configured in code, reviewed like a security change | turn on tracing in prod with defaults "for a week" |
| OTel for infrastructure spans, LangSmith (or another LLM tool) for LLM-level views, joined by trace ID | run two unrelated tracing systems with no shared ID |
| Use prompts from the prompt hub only with pinned versions or commit hashes | pull "latest" in production; a prompt edit becomes an unreviewed deploy |

---

## 4. Deep Agents in practice

> **In plain words.** `deepagents` gives you a Claude-Code-like agent in one call: file tools, a
> `task` tool for sub-agents, skills, memory files, summarization and prompt caching, built as
> middleware on `create_agent`. The main design choice is the **backend**: where the agent's files
> actually live.
>
> **Real-world example.** A team uses `create_deep_agent` with defaults for a research agent. Each
> run writes 20 notes files of 5–20 KB. The files live in **agent state**, so every write is stored
> again in the checkpoint. A month later the checkpoint database is larger than the document corpus.

### 4.1 What `create_deep_agent` builds

*(source: `deepagents/graph.py`; probe with a fake model)*

- Tools offered to the model by default: `ls`, `read_file`, `write_file`, `edit_file`, `glob`,
  `grep`, `delete`, `task` *(probe)*. `execute` (shell) appears only when the backend is a sandbox
  backend.
- Middleware: filesystem, sub-agents, summarization, skills, memory, tool-call patching, prompt
  caching (Anthropic), and HITL when `interrupt_on` is set. It returns `create_agent(...)`, so all of
  appendix I applies.
- `model=None` falls back to a default Anthropic model, which is **deprecated** *(source)*. Always
  pass a model.
- Default backend: `StateBackend()`. Files are stored in the `files` key of agent state *(probe)*.

### 4.2 Backends: where files live

| Backend | Files live in | Persists across threads | Watch out *(source)* |
|---|---|---|---|
| `StateBackend` (default) | agent state → checkpoints | no | every write is stored again per checkpoint (appendix J §2.2 growth) |
| `StoreBackend(namespace=...)` | the LangGraph store | yes | `namespace` is a **required** factory; derive it from the authenticated user, never from model input |
| `FilesystemBackend(root_dir, virtual_mode=True)` | real disk under `root_dir` | yes | `virtual_mode` blocks `..`, `~` and absolute paths, but "does not provide sandboxing or process isolation" |
| `LocalShellBackend` | real disk **plus unrestricted host shell** | yes | its own docstring: no sandboxing, no isolation; local dev only |
| Sandbox backends (`BaseSandbox` implementations) | a remote or containerized sandbox | depends | the only safe place for `execute` with untrusted input (`28` §7) |
| `CompositeBackend(default, routes={"/memories/": store_backend})` | routed by path prefix, longest first | per route | a clean way to keep scratch files ephemeral and memories durable |

A good default for a multi-user service:

```python
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

def user_ns(rt):                                   # identity from server-side context
    return ("tenant", rt.context.tenant_id, "user", rt.context.user_id, "memories")

agent = create_deep_agent(
    model=model,
    tools=[search_kb],
    backend=CompositeBackend(
        default=StateBackend(),                    # scratch files: per thread
        routes={"/memories/": StoreBackend(namespace=user_ns)},   # durable, per user
    ),
    subagents=[{
        "name": "researcher",
        "description": "Searches the knowledge base and returns 5 cited findings. Use for any lookup.",
        "system_prompt": "Search, read, and return at most 5 findings with doc IDs. No other text.",
        "tools": [search_kb],
    }],
    interrupt_on={"write_file": False, "delete": True},
    context_schema=Ctx,
    checkpointer=checkpointer,
    store=store,
)
```

### 4.3 Sub-agents

- The main agent calls `task(subagent_name, instructions)`. The sub-agent runs with its **own**
  message history and returns only its final answer. That is the context-isolation benefit (`30` §6).
- A spec has `name`, `description`, and optionally `system_prompt`, `tools`, `model`, `middleware`,
  `interrupt_on`, `skills`, `permissions` *(source)*. A `general-purpose` sub-agent with the main
  agent's tools is available by default.
- The `description` is the routing prompt. Write it as "use for X; returns Y".
- Sub-agents cost tokens: each one re-reads its own context. Use them for wide exploration, not for
  one tool call.

### 4.4 When Deep Agents fit

| Fits | Does not fit |
|---|---|
| Long, open-ended tasks: research, report writing, codebase work | a fixed pipeline (use a workflow) |
| Tasks that need a scratchpad larger than the context window | a single tool call or extraction |
| Delegating exploration to sub-agents | strict latency budgets (sub-agents and file reads add turns) |

| Do | Don't |
|---|---|
| Pass `model=` explicitly | rely on the deprecated default model |
| `CompositeBackend`: scratch in state, memories in the store | keep everything in `StateBackend` for long runs (checkpoint growth) |
| A sandbox backend for `execute` | `LocalShellBackend` in a service |
| `interrupt_on` for destructive file operations | give `delete` and shell access without approval |
| Keep skills and memory files under code review | let the agent edit its own memory files without review (memory poisoning, `28`) |

---

## 5. The long-term store in practice

> **In plain words.** The store is a key–value database with namespaces, optional vector search
> and expiry. It is where cross-conversation memory lives. Namespaces organise data; they do not
> protect it.
>
> **Real-world example.** Memories are stored under `("acme", user_id, "prefs")`. A "related
> memories" feature calls `store.search(("acme",), query=...)`. `search` matches a namespace
> **prefix**, so it returns every user's memories in the tenant.

API *(source: `langgraph/store/base`, `langgraph/store/postgres`)*:

```python
from langgraph.store.postgres import PostgresStore

store = PostgresStore(
    conn,
    index={"dims": 1024, "embed": embeddings, "fields": ["text"]},   # what to embed
    ttl={"default_ttl": 60 * 24 * 90, "refresh_on_read": True},       # minutes
)
store.setup()                                          # at deploy, like the checkpointer
ns = ("tenant", ctx.tenant_id, "user", ctx.user_id, "memories")
store.put(ns, key=memory_id, value={"text": "...", "source_thread": tid})
hits = store.search(ns, query="dietary restrictions", filter={"kind": "preference"}, limit=5)
store.delete(ns, memory_id)
```

| Practice | Why |
|---|---|
| Build the **full** namespace from server-side identity (`runtime.context`), in one helper | `search` takes a prefix; a short prefix crosses users |
| Put `index=False` on items that should not be embedded, or list `fields` | embedding everything costs money and puts secrets into vectors |
| Set TTL, and run `store.start_ttl_sweeper()` (or a scheduled `sweep_ttl()`) | expired items are hidden by `omit_expired` but only deleted when swept |
| Implement user deletion as "delete every key under the user's namespace" + checkpoint `delete_thread` | GDPR erasure must reach both stores (`25` §10) |
| Store the source (thread, message) with each memory | you can explain, correct and delete it later |
| Do not let the model choose namespaces or keys freely | prompt injection becomes cross-user writes |

---

## 6. MCP adapters in practice

> **In plain words.** `langchain-mcp-adapters` turns MCP server tools into LangChain tools. Three
> settings decide whether it works in production: how sessions are opened (appendix J §2.3), how
> tool names from different servers are kept apart, and how each call is authorized.
>
> **Real-world example.** A team connects a Jira server and a Confluence server. Both expose a tool
> called `search`. The agent can only ever call one of them. *(probe: two tools named `search` in one
> `ToolNode` → one entry, the second one wins)* Nothing errors.

`MultiServerMCPClient(connections, *, callbacks=None, tool_interceptors=None,
tool_name_prefix=False, handle_tool_errors=True)` *(source)*.

| Setting | Default | Production setting |
|---|---|---|
| `tool_name_prefix` | `False` | `True` when you connect more than one server; names become `<server>_<tool>` *(source)* |
| Sessions | new session per tool call | one session per run for `stdio` or chatty servers (`client.session(name)` + `load_mcp_tools`) |
| `handle_tool_errors` | `True`: MCP tool errors return to the model as error messages | keep it, and log the server name |
| `tool_interceptors` | none | per-call auth, audit and limits (below) |

**Interceptors** wrap each MCP tool call like `wrap_tool_call` middleware, in onion order, and can
change the tool name, arguments and **HTTP headers** *(source: `MCPToolCallRequest`)*. That is the
place for per-user authorization on remote servers:

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

async def user_token(request, handler):
    ctx = request.runtime.context                        # server-side identity (appendix I §6)
    token = await token_exchange(ctx.user_id, audience=request.server_name)   # 29 §4
    return await handler(request.override(headers={"Authorization": f"Bearer {token}"}))

async def audit(request, handler):
    result = await handler(request)
    audit_log.write(server=request.server_name, tool=request.name, user=request.runtime.context.user_id)
    return result

client = MultiServerMCPClient(
    {"jira": {"transport": "streamable_http", "url": JIRA_MCP_URL},
     "wiki": {"transport": "streamable_http", "url": WIKI_MCP_URL}},
    tool_name_prefix=True,
    tool_interceptors=[audit, user_token],               # audit is outermost
)
tools = await client.get_tools()
```

Headers only apply to HTTP transports. For `stdio` servers, the process runs with whatever
credentials you started it with, which is one more reason to keep `stdio` servers for local
development (`26` §5).

| Do | Don't |
|---|---|
| Prefix tool names with more than one server | let names collide silently |
| Exchange the user's token per call in an interceptor | give the MCP server a shared service account with access to everything (`29` §2) |
| Pin and review the tool list you load (`26` §9) | load every tool a remote server advertises; descriptions are prompts and can change (`28` tool poisoning) |
| Keep one session per run for stateful servers | rely on server state across the default per-call sessions |

---

## 7. Probe it yourself

> **In plain words.** These scripts need no API key. Run them after upgrades; if an output changes,
> a default changed.

```bash
uv pip install "langgraph==1.2.15" "langgraph-checkpoint-sqlite" "deepagents==0.7.25"
```

**Probe A: functional API replay, `previous`, retry defaults, cache.**

```python
from langgraph.func import entrypoint, task
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command, CachePolicy
from langgraph.cache.memory import InMemoryCache
from langgraph._internal._retry import default_retry_on   # internal path; may move

calls = {"task": 0, "outside": 0}

@task
def expensive(x: int) -> int:
    calls["task"] += 1
    return x * 2

@entrypoint(checkpointer=InMemorySaver())
def wf(x: int) -> dict:
    calls["outside"] += 1                     # side effect outside a task
    a = expensive(x).result()
    return {"a": a, "approved": interrupt({"approve": a})}

cfg = {"configurable": {"thread_id": "1"}}
wf.invoke(3, cfg)
print(wf.invoke(Command(resume=True), cfg), calls)    # {'task': 1, 'outside': 2}

for e in (TimeoutError(), ConnectionError(), ValueError(), Exception()):
    print(type(e).__name__, default_retry_on(e))      # False, True, False, True

n = {"c": 0}
@task(cache_policy=CachePolicy(ttl=60))
def cached(x):
    n["c"] += 1
    return x

@entrypoint(cache=InMemoryCache())
def with_cache(x): return cached(x).result()
@entrypoint()
def without_cache(x): return cached(x).result()

with_cache.invoke(1); with_cache.invoke(1); print(n["c"])        # 1
without_cache.invoke(1); without_cache.invoke(1); print(n["c"])  # 3
```

**Probe B: durability under a hard kill.** Save as `dur.py`, run
`python dur.py exit crash; python dur.py exit resume`, then the same with `sync`.

```python
import operator, os, sqlite3, sys
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

class S(TypedDict):
    log: Annotated[list, operator.add]

mode, phase = sys.argv[1], sys.argv[2]
def node(name):
    def f(s):
        print("run", name)
        if name == "c" and phase == "crash":
            os._exit(9)                                 # like an OOM-kill
        return {"log": [name]}
    return f

g = StateGraph(S)
for n in "abc":
    g.add_node(n, node(n))
g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("b", "c"); g.add_edge("c", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(f"cp-{mode}.db", check_same_thread=False)))
cfg = {"configurable": {"thread_id": "t"}}
if phase == "crash":
    app.invoke({"log": []}, cfg, durability=mode)
else:
    nxt = app.get_state(cfg).next
    print("next:", nxt)                                 # sync: ('c',)   exit: ()
    app.invoke(None if nxt else {"log": []}, cfg, durability=mode)
```

**Probe C: Deep Agents default tools and where files go.** Use the `Fake` model from appendix I §10
with one `write_file` tool call, then inspect `out["files"]` and the tool names passed to
`bind_tools`.

---

## 8. Interview questions

**Q1. A functional-API workflow sends an email twice after a human approves it. Why?**
On resume the entrypoint body runs again from the top; only finished `@task` results are replayed.
The email was sent in the entrypoint body. Move it into a task with an idempotency key.

**Q2. When do you use the Functional API instead of `StateGraph`?**
For script-shaped workflows and for making existing Python durable with small changes. Use
`StateGraph` when several nodes merge into shared state through reducers, when routing depends on
state, or when you want the graph as a reviewable diagram.

**Q3. What does `durability="exit"` trade away?**
Checkpoints are written only when the run ends. A Python exception still saves state at exit, but a
hard kill (OOM, SIGKILL, node loss) loses the whole run, so earlier side-effecting steps run again.

**Q4. A node calls an HTTP API that times out with `TimeoutError`. You set `RetryPolicy()`. Why is it
not retried?**
`default_retry_on` does not retry `OSError` subclasses, and `TimeoutError` is one. Pass `retry_on`
explicitly.

**Q5. You added `CachePolicy` to an expensive task and nothing got faster. Why?**
The graph or entrypoint has no `cache=` backend, so the policy is ignored without a warning.

**Q6. You set LangSmith sampling to 5%, and failed runs are missing. Why, and what do you do?**
Sampling is decided per trace ID before the outcome is known (head sampling). Log trace IDs with
errors, trace critical flows at 100%, or use OTel export with tail sampling in the collector.

**Q7. Why is `evaluate()` slow on a 300-example dataset?**
`max_concurrency` defaults to 0, which runs examples one at a time. Set it within your rate limits.

**Q8. Where should Deep Agents files live in a multi-user service?**
Scratch files in `StateBackend` (per thread), durable memories in `StoreBackend` with a namespace
built from server-side identity, combined with `CompositeBackend`. Shell only through a sandbox
backend.

**Q9. How do you authorize per user when the agent calls a remote MCP server?**
A tool interceptor that exchanges the user's token for one scoped to that server and sets the
`Authorization` header on each call, plus `tool_name_prefix=True` and a reviewed tool list.

**Q10. Why can `store.search(("acme",), ...)` leak data?**
`search` matches a namespace prefix. A tenant-level prefix returns every user's items. Build the full
namespace from server-side identity in one helper.

---

## 9. Sources

- `langgraph` 1.2.15 source: `langgraph/func/__init__.py` (`task`, `entrypoint`, `entrypoint.final`),
  `langgraph/types.py` (`Durability`, `RetryPolicy`, `TimeoutPolicy`, `CachePolicy`, `TracePolicy`,
  `StreamMode`, `StreamPart`), `langgraph/_internal/_retry.py` (`default_retry_on`),
  `langgraph/pregel/main.py` (`stream`, `version`), `langgraph/config.py` (`get_stream_writer`),
  `langgraph/store/base`, `langgraph/store/postgres`.
- `langchain-core` 1.6.10: `langchain_core/rate_limiters.py` (`InMemoryRateLimiter`).
- `langsmith` 0.14.8: `client.py` (sampling, batching, `hide_inputs`, `anonymizer`, `flush`),
  `anonymizer.py`, `evaluation/_runner.py` (`evaluate`, `max_concurrency`, `num_repetitions`),
  `wrappers/`, `testing/`.
- `deepagents` 0.7.25: `graph.py`, `backends/` (`state.py`, `store.py`, `filesystem.py`,
  `local_shell.py`, `composite.py`, `sandbox.py`), `middleware/subagents.py`.
- `langchain-mcp-adapters` 0.3.2: `client.py`, `tools.py`, `interceptors.py`.
- LangChain docs: Functional API, durable execution, persistence, streaming, LangSmith evaluation and
  observability, Deep Agents — <https://docs.langchain.com/>, <https://docs.smith.langchain.com/>.
