# Appendix J — The production AI stack: layers, reference stacks, and what is inside each tool

> **Why this appendix exists.** Most production LLM systems use the same few stacks. An enterprise
> RAG service, a LangGraph agent backend and a coding agent like Claude Code, Codex or Cursor are
> built from the same layers: a model client, a loop, tools, state, retrieval, a sandbox, evals and
> traces. Many "senior AI engineer" roadmaps list 80+ tool names. A list of names does not tell you
> which layer each tool belongs to, what it does inside, or how it fails.
>
> This page does three things:
>
> 1. It maps the usual roadmap checklist to the chapters in this folder that already cover each
>    item (§1). Most of it is already written, so read it there.
> 2. It fills the gaps that no chapter covers: the LangGraph package ecosystem and its internals
>    (§2), the coding-agent harness (§4), the agent-backend runtime (§5), the eval and observability
>    tools (§6), and the frameworks other than LangChain (§7).
> 3. It gives three **reference stacks** (§3, §4, §5): what a staff engineer would pick for each
>    layer by default, when to deviate, and what breaks.
>
> **Read from source.** Package facts were checked against PyPI and the installed source code in
> October 2026: `langgraph` 1.2.15, `langgraph-checkpoint-postgres` 3.1.3, `langchain-mcp-adapters`
> 0.3.2, `langgraph-supervisor` 0.0.31, `deepagents` 0.7.25, `openai-agents` 0.23.1, `pydantic-ai`
> 2.56.0, `claude-agent-sdk` 0.2.166, `google-adk` 2.12.0, `agent-framework` 1.21.0. Marked
> *(source)* or *(probe)* when checked by reading code or running it. Claims about closed products
> (Cursor, Claude Code internals) come from their public engineering posts and docs and are marked
> *(public)*.
>
> **Companion:** [appendix I](appendix-i-langchain-v1-in-practice.md) goes deep on `create_agent`,
> middleware and structured output. This page is the wider map.

## Contents

1. [The roadmap checklist, mapped to this folder](#1-the-roadmap-checklist-mapped-to-this-folder)
2. [The LangChain / LangGraph package map, with internals](#2-the-langchain--langgraph-package-map-with-internals)
3. [Reference stack A: enterprise RAG](#3-reference-stack-a-enterprise-rag)
4. [Reference stack B: coding and code-execution agents](#4-reference-stack-b-coding-and-code-execution-agents)
5. [Reference stack C: the agent backend](#5-reference-stack-c-the-agent-backend)
6. [Evaluation and observability tools](#6-evaluation-and-observability-tools)
7. [Frameworks other than LangChain, by their core idea](#7-frameworks-other-than-langchain-by-their-core-idea)
8. [Providers and serving: the facts that leak through abstractions](#8-providers-and-serving-the-facts-that-leak-through-abstractions)
9. [What to learn first, and what to skip](#9-what-to-learn-first-and-what-to-skip)
10. [Interview questions](#10-interview-questions)
11. [Sources](#11-sources)

---

## 1. The roadmap checklist, mapped to this folder

> **In plain words.** Before learning a new tool, find out whether its layer is already covered.
> The left column is the usual roadmap item. The right column says where to read it.
>
> **Real-world example.** "Learn LangGraph checkpointing" and "learn Temporal" look like two topics.
> They are one question, how to survive a crash in the middle of a run. `21` §5 and §14 answer it
> once.

| Roadmap item | Covered in | Gap filled here |
|---|---|---|
| Models, messages, prompts, tool binding, output parsers | `20` §2–§8 | — |
| `init_chat_model`, `create_agent`, middleware, retries, fallbacks | [appendix I](appendix-i-langchain-v1-in-practice.md) | — |
| Structured output (provider vs tool strategy, Pydantic, Instructor) | `07` §3–§7, §12; appendix I §5 | — |
| `StateGraph`, reducers, `MessagesState`, `Command`, `Send`, `ToolNode` | `21` §2–§4, §9 | — |
| Checkpointing, interrupts, resume, streaming, subgraphs | `21` §5–§8 | storage internals and growth (§2.2) |
| Retry policies, loop limits, durable execution, LangGraph vs Temporal | `21` §11–§14 | — |
| Agent patterns: ReAct, router, supervisor, plan-and-execute, reflection | `22` §3, §5 | supervisor *library* status (§2.1) |
| Packages: checkpoint-postgres/sqlite, store, supervisor, swarm, sdk, cli, mcp-adapters, classic | `20` §12 (0.x split) | **§2** |
| Document parsing (PyMuPDF, Unstructured, Docling, OCR) and chunking | `02`, appendix D | — |
| Embeddings, rerankers, bi- vs cross-encoder | `01`, `04` §4–§8 | — |
| Vector DBs (pgvector, Qdrant, Pinecone, Milvus, OpenSearch), HNSW/IVF/PQ, filtering | `03`, appendix D | — |
| Hybrid search, RRF, query rewriting, multi-hop, GraphRAG | `04`, `05`, appendix H | — |
| Measuring retrieval separately from answers | `08`, appendices F, G | — |
| Providers, LiteLLM, routing, fallback, quotas, cost | `23` | leaking provider differences (§8) |
| vLLM, Ollama, GPUs, Compose vs Kubernetes, pipeline orchestration | appendix E | — |
| MCP, OpenAPI → tools, JSON Schema | `24`, `26` | `langchain-mcp-adapters` sessions (§2.3) |
| A2A | `27` | — |
| OAuth/OIDC, delegated identity, tool-boundary authz | `29`, `24` §6 | — |
| Prompt injection, untrusted tool output, sandboxing | `17`, `28` | coding-agent sandbox layers (§4.5) |
| Memory: short-term, long-term, semantic, episodic, deletion | `25` | — |
| Context engineering, compaction, sub-agents, skills | `30` | — |
| Agent evaluation, trajectory grading, SWE-bench / Terminal-Bench | `14` | — |
| LangSmith, Langfuse, Phoenix, Ragas, DeepEval, promptfoo | planned `19` | **§6** |
| OpenTelemetry GenAI conventions | `../sre-observability/26-llm-and-ai-observability.md` | — |
| LlamaIndex, PydanticAI, OpenAI Agents SDK, ADK, CrewAI, SK/AutoGen, Haystack, DSPy | `22` §11 (partial, and SK/AutoGen are now merged) | **§7** |
| Coding agents (Claude Code, Codex, Cursor, Aider) | `05` §5.6 (search only) | **§4** |
| FastAPI + queue + Postgres agent backend, double-texting, failure modes | `21` §15–§16 (partial) | **§5** |

---

## 2. The LangChain / LangGraph package map, with internals

> **In plain words.** LangChain is many separately released packages. Some are core, some are
> optional, some are not recommended any more, and at least one package with an official-looking
> name is not from LangChain at all.
>
> **Real-world example.** A roadmap tells you to install `langgraph-store-postgres` for long-term
> memory. That PyPI package is published by an independent developer, not by LangChain. The official
> Postgres store ships inside `langgraph-checkpoint-postgres` as `langgraph.store.postgres`.
> Installing a look-alike into a service that holds user memory is a supply-chain decision. Check
> the publisher before you `pip install`.

### 2.1 The map

| Package (Oct 2026) | What it is | Use it? | Staff note |
|---|---|---|---|
| `langchain` 1.x | `create_agent`, middleware, `init_chat_model`, tool and message re-exports | yes | appendix I |
| `langchain-core` | messages, `Runnable`, tools, `BaseChatModel`, callbacks | always (dependency) | the only package partner integrations depend on |
| `langchain-<provider>` (`-openai`, `-anthropic`, `-google-genai`, `-aws`, `-ollama`, …) | one chat/embedding class per provider | yes, pinned | provider features (caching, thinking, built-in tools) arrive here first, and behave differently per package |
| `langchain-text-splitters` | splitters, including code-aware ones | optional | `02` covers what to use instead for hard documents |
| `langchain-classic` | the old chains, `AgentExecutor`, memory classes | only for legacy code | new code should not import it |
| `langchain-tests` | standard test suites for *integration authors* | only if you write an integration | not an app-testing tool |
| `langgraph` | `StateGraph`, Pregel runtime, channels, `Command`, `Send`, `interrupt` | yes | `21` |
| `langgraph-prebuilt` | `ToolNode`, `ToolRuntime`, the deprecated `create_react_agent` | indirectly | used by `create_agent` |
| `langgraph-checkpoint` | `BaseCheckpointSaver`, `InMemorySaver`, serializers, `BaseStore` | dependency | `InMemorySaver` is for tests only |
| `langgraph-checkpoint-postgres` | `PostgresSaver`, `ShallowPostgresSaver`, **and** `PostgresStore` (`langgraph.store.postgres`) | yes, for production | §2.2 |
| `langgraph-checkpoint-sqlite` | `SqliteSaver`, `SqliteStore` | local dev, single-process tools | one writer; not for multi-replica services |
| `langgraph-store-postgres` | **third-party** (independent author, its own SQLAlchemy/psycopg2 schema) | no | name squatting risk; use the official store above |
| `langgraph-supervisor` | prebuilt supervisor multi-agent graph | **not for new code** | its own README now recommends the supervisor pattern "directly via tools", and it still imports the deprecated `create_react_agent` *(source)*. Write the supervisor as an agent whose tools call sub-agents |
| `langgraph-swarm` | prebuilt peer handoff graph | rarely | handoffs are one `Command(goto=...)` tool; write them yourself (`21` §13) |
| `langchain-mcp-adapters` | MCP servers → LangChain tools; `MultiServerMCPClient` | yes, with care | §2.3 |
| `deepagents` | `create_deep_agent`: `create_agent` + planning, filesystem, sub-agent, summarization, skills, memory and prompt-caching middleware *(source)* | for long tasks | the open version of the coding-agent harness (§4) |
| `langgraph-cli`, `langgraph-api`, `langgraph-sdk` | dev server (`langgraph dev`), the Agent Server runtime, and its client | if you deploy on LangChain's platform | §5.3 |
| `langsmith` | tracing, datasets, experiments SDK | if you use LangSmith | §6 |

### 2.2 Checkpointer internals: what is stored, and how fast it grows

`PostgresSaver.setup()` creates four tables *(source)*:

| Table | Key | Holds |
|---|---|---|
| `checkpoints` | `(thread_id, checkpoint_ns, checkpoint_id)` | one row per **super-step**: channel versions, `parent_checkpoint_id`, metadata (JSONB) |
| `checkpoint_blobs` | `(thread_id, checkpoint_ns, channel, version)` | the serialized value of each channel, stored again whenever its version changes |
| `checkpoint_writes` | `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)` | **pending writes** of each task in a super-step |
| `checkpoint_migrations` | `v` | schema version, applied by `setup()` |

Three consequences follow.

**1. Partial completion is recoverable at task level.** When three parallel tool tasks run in one
super-step and one fails, the two successful tasks' writes are already in `checkpoint_writes`. On
resume, only the failed task runs again. The side effects of the two finished tasks are not repeated
*by LangGraph*, but anything a failed task did before it raised **is** repeated. Idempotency keys
are still needed (appendix I §6.3).

**2. Storage grows with the square of the conversation length.** The `messages` channel changes on
every step, and each new version stores the whole list again. *(probe: a one-node graph,
`MessagesState`, `SqliteSaver`, one human message plus one 1,000-character AI message per turn)*

| Turns | Checkpoint rows | Checkpoint bytes | Growth vs 10 turns |
|---|---|---|---|
| 10 | 30 | 220 KB | 1× |
| 20 | 60 | 857 KB | 3.9× |
| 40 | 120 | 3.38 MB | 15.4× |

Doubling the turns multiplies storage by about four. Forty turns holding about 41 KB of actual text
take 3.4 MB. At a million threads that is the difference between a small table and a database
migration project. The levers:

- `ShallowPostgresSaver` keeps only the latest checkpoint. You lose time travel; you keep resume and
  interrupts.
- A retention job that deletes old checkpoints per thread (`21` §5.7), and `delete_thread(thread_id)`
  for user deletion requests (GDPR erasure, `25` §10).
- `DeltaChannel` (beta in `langgraph` 1.2) stores only writes and rebuilds the value by replaying
  them through the reducer *(source)*. It is the real fix, but its on-disk format is marked
  "not yet stable". Do not adopt it for data you must keep for years without a migration test.

**3. Connection settings are not optional.** `from_conn_string` opens the connection with
`autocommit=True`, `prepare_threshold=0` and `row_factory=dict_row` *(source)*. If you build your own
`psycopg` pool, use the same settings. `prepare_threshold=0` is also what makes it work behind
PgBouncer in transaction mode.

`PostgresStore` (long-term memory) is a separate table pair, `store` and `store_vectors`. It has
optional pgvector semantic search via an `index` config, and TTL with `ttl_minutes` and refresh on
read *(source)*. Namespaces are tuples such as `("tenant-42", "user-7", "prefs")`. **Namespaces are not
access control.** Any code with the store handle can read any namespace, so derive the namespace from
`runtime.context`, never from model output (`25` §10).

### 2.3 `langchain-mcp-adapters`: one session per tool call by default

`MultiServerMCPClient(...).get_tools()` returns tools that **open a new MCP session for every tool
call** *(source: the `get_tools` docstring and `tools.py`)*. For a `stdio` server that means
starting a new process per call. For a remote server it means a new initialize handshake per call.
It also means a stateful server (a browser session, an open database transaction) loses its state
between calls.

| Need | Do |
|---|---|
| Stateless remote tools, low call volume | default `get_tools()` is fine |
| `stdio` servers, or many calls per run | `async with client.session("name") as s: tools = await load_mcp_tools(s)` and keep the session for the run |
| Stateful server | one explicit session per agent run, closed in `after_agent` or a `finally` |
| Many servers, many tenants | an MCP gateway in front (`26` §8), not one client per tenant per pod |

---

## 3. Reference stack A: enterprise RAG

> **In plain words.** An enterprise RAG service is a search engine with a model on top. The hard
> parts are permissions, freshness and measurement, not the model call.
>
> **Real-world example.** An HR assistant passes its demo. In month two, an employee who moved teams
> still gets answers from the old team's documents. The vector store kept the old ACLs, because
> permission changes were never synced. Nothing in the model layer could have prevented it.

### 3.1 The default stack

| Layer | Default pick | Deviate when | Inside / what breaks | Read |
|---|---|---|---|---|
| Connectors and sync | your own sync jobs per source (SharePoint, Confluence, Drive, DBs), with change feeds | a managed connector platform is already licensed | **ACL sync is a separate pipeline from content sync.** Permission changes must reach the index faster than content changes | `04` §12, appendix E §8 |
| Parsing | Docling or PyMuPDF for born-digital PDFs; OCR / vision models only for scans | heavy tables or forms | parser upgrades silently change chunk boundaries and IDs; version the parser | `02`, appendix D |
| Chunking | structure-aware (headings, tables kept whole), ~300–800 tokens, contextual prefix | very short FAQ-style documents | chunk IDs must be stable across re-ingests, or citations and eval labels break | `02` |
| Embeddings | one hosted or open model, **version stored on every vector** | multilingual or domain gap measured on your data | changing the model means re-embedding everything; plan dual-write and a cutover | `01` |
| Index | Postgres + pgvector (HNSW) up to roughly tens of millions of vectors; OpenSearch/Elasticsearch when you also need mature BM25 | >100M vectors, or strict latency at high QPS → a dedicated engine (Qdrant, Milvus, Vespa…) | filtered ANN with very selective filters loses recall; ACL filter **inside** the search, not after | `03`, `04` §12 |
| Lexical | Postgres FTS or OpenSearch BM25 | — | analyzer choice (stemming, tokenization of codes like `ERR-4102`) matters more than the engine | `04` §2 |
| Fusion and rerank | RRF over dense + BM25, then a cross-encoder over the top 50 | latency budget below ~150 ms → smaller reranker or none | reranker is usually the largest latency item; measure p95 | `04` |
| Query understanding | rewrite only when measured to help; routing for distinct corpora | — | every extra LLM call adds latency and a new failure mode | `05` |
| Generation | structured output with citations to chunk IDs; abstain path | — | citation validity must be checked in code, not trusted | `07` |
| Guardrails | injection defenses on retrieved text, PII handling, output checks | regulated data → DLP | retrieved documents are untrusted input | `17` |
| Gateway | one model gateway for keys, quotas, routing, fallback, cost | single provider and single team | duplicated retries across layers multiply outage load | `23` |
| Eval | golden set + retrieval metrics + judged faithfulness, gated in CI | — | eval sets rot when documents change; refresh labels on schedule | `08`, F, G |
| Observability | OTel spans per stage + an LLM tracing tool | — | traces contain document text and PII; set retention and redaction | §6, `../sre-observability/26-llm-and-ai-observability.md` |

### 3.2 What breaks in year one

| Failure | Mechanism | Prevention |
|---|---|---|
| Stale permissions | ACL changes not propagated; summaries or caches built from restricted docs | ACL sync SLO; derived data inherits the intersection of source readers (appendix H §6) |
| Silent quality drop after a parser or embedding upgrade | chunk boundaries and vectors change; no one re-ran evals | version every stage; CI eval gate on any ingest-pipeline change |
| Re-embedding project nobody planned | the model was changed or deprecated | model version on every vector, dual index during migration |
| Freshness complaints | batch nightly ingest | change-feed ingestion with a staleness SLO |
| Cost per tenant unknown | no attribution | tenant ID on every span and token count (`23` §7) |
| "Search is fine, answers are wrong" debates | no stage-level metrics | measure each stage separately (appendix F §5) |

---

## 4. Reference stack B: coding and code-execution agents

> **In plain words.** Claude Code, Codex CLI, Cursor's agent, Aider and LangChain's `deepagents`
> are the same machine: a model in a tool loop, with file tools, a shell, a way to search the code,
> a way to keep the context small, and a sandbox with permissions. They differ mostly in how they
> edit files, how they search, and how they isolate commands.
>
> **Real-world example.** A team builds an internal "fix the failing test" agent on a generic agent
> framework, with a `write_file(path, content)` tool. On a 2,000-line file the model rewrites the
> whole file, drops 40 lines it did not mean to touch, and the diff review misses it. Coding agents
> use targeted edit formats for exactly this reason (§4.2).

### 4.1 Anatomy of the harness

| Component | What it does | Common choices *(public)* | Why it matters |
|---|---|---|---|
| Loop | model ↔ tools until no tool calls | a plain tool loop; no planner graph | simple loops with good tools beat complex graphs here (`22` §1) |
| Read tools | read file ranges, list, glob | `Read` with line ranges and a size cap | full-file reads of huge files fill the context |
| Search | find code | `grep`/`ripgrep` + `glob` in a loop (Claude Code, Codex); embeddings index plus grep (Cursor); a tree-sitter "repo map" ranked by references (Aider) | `05` §5.6: exact identifiers favour grep; conceptual questions favour embeddings; layered is the consensus |
| Edit tools | change files | exact string replace (Claude Code `Edit`), patch format (`apply_patch` in Codex), search/replace blocks (Aider), a separate fast "apply" model (Cursor) | §4.2 |
| Shell | run tests, builds, git | `Bash` with timeout, output truncation, background processes | the most powerful and most dangerous tool |
| Context files | project rules loaded at start | `CLAUDE.md`, `AGENTS.md`, `.cursor/rules` | cheap, high-leverage steering; also an injection surface if the repo is untrusted |
| Context management | stay inside the window | automatic compaction near the limit, tool-result clearing, sub-agents with their own context, skills loaded on demand | `30` §6–§8 |
| Planning | keep track of multi-step work | a todo-list tool | gives the model a scratchpad and the user a progress view |
| Permissions | decide which actions need approval | modes (ask / allow edits / plan only / bypass), allow and deny lists per tool and command pattern | §4.5 |
| Sandbox | limit what commands can touch | OS-level (macOS Seatbelt, Linux bubblewrap/Landlock), containers, microVMs for cloud agents | §4.5 |
| Hooks | run your code at loop events | pre/post tool use, prompt submit, stop | deterministic policy (lint after edit, block a path) outside the model |
| Extensibility | more tools | MCP servers, plugins | each server adds tool definitions to every prompt (`30` §2) |
| Checkpoints / undo | roll back file changes | snapshot before edits; git | lets users trust larger autonomous steps |
| Headless / SDK | run the same harness from code | Claude Agent SDK (drives the Claude Code CLI as a subprocess, *source*), `codex exec`, `deepagents` | CI bots and internal agents reuse a tested harness instead of a new one |

### 4.2 Edit formats are the core design choice

| Format | How it works | Fails how | Notes |
|---|---|---|---|
| Whole file | model writes the full new file | drops or changes unrelated lines; slow and costly on big files | fine for new and small files only |
| Exact string replace (`old_string` → `new_string`) | tool finds `old_string`, requires it to be **unique**, replaces it | fails loudly when the text is not found or appears more than once | loud failure is the point: the model re-reads and retries. Requires a "read before edit" rule so the model edits what is really on disk |
| Unified diff / patch (`apply_patch`) | model writes a patch with context lines | wrong line context → the patch does not apply | compact for many small changes; needs a tolerant applier |
| Search/replace blocks | like string replace, several blocks per message | fuzzy matching can apply to the wrong place | Aider published benchmarks showing edit format changes success rates noticeably for the same model |
| Draft + apply model | big model writes a sketch; a small fast model merges it into the file | the apply model can mis-merge | lower latency on big files; one more model to evaluate |

Staff rule: **choose a format that fails loudly and locally.** A silent wrong edit costs more than a
failed tool call, because the model sees the failure and fixes it, while nobody sees the silent one.

### 4.3 Search: agentic grep vs an index

Covered in `05` §5.6. Summary for design reviews:

- **Agentic grep** has no index to build, keep fresh or secure. It is exact on identifiers, and it
  costs more turns on vague questions.
- **An embeddings index** answers conceptual questions ("where do we handle retries?") in one call.
  It needs incremental sync. Cursor describes hashing files into a **Merkle tree** so only changed
  files are re-chunked and re-embedded, and obfuscating file paths before they leave the machine
  *(public)*. It is also a second copy of the source code that you must secure.
- **Default for an internal agent:** grep, glob and read first; add LSP or symbol tools for
  precision; add an index only when your evals show conceptual queries failing.

### 4.4 Building your own coding or code-execution agent

| Best practice | Why |
|---|---|
| Start from an existing harness (Claude Agent SDK, `codex exec`, `deepagents`) | the hard parts (edit tools, compaction, truncation, permissions) are already tuned; your value is in tools and policies |
| Tool outputs truncated with a hint ("output truncated; use a narrower command") | one `cat` of a log file can fill the window |
| Tests as the stop condition | "done" means tests pass, checked by the harness, not claimed by the model |
| A per-repo context file with build and test commands | most failed runs start with the agent guessing how to run tests |
| Separate read-only exploration sub-agent | keeps exploration output out of the main context (`30` §6) |
| Every run in a fresh sandbox, with network egress allow-listed | §4.5 |
| Evaluate on your own repo tasks, not only SWE-bench | public benchmark scores do not transfer to your codebase (`14`) |

| Worst practice | What happens |
|---|---|
| `write_file` as the only edit tool | silent, large, unreviewable diffs |
| Shell with the developer's credentials and no sandbox | one injected instruction in a README or issue can run `curl … \| sh` with production credentials (`28`) |
| Trusting `AGENTS.md` / `CLAUDE.md` from an untrusted repo or PR | the context file is a prompt; a malicious repo controls your agent |
| Unlimited turns and tokens in CI | one confused run burns a day of budget; set turn and cost caps |
| Auto-approve all commands "because it is in Docker" | the container often has the repo's secrets, the Docker socket or open network |

### 4.5 Sandbox and permissions, layered

| Layer | Stops | Does not stop |
|---|---|---|
| Permission prompts / allow lists | unexpected commands, when a human reads them | approval fatigue; humans approve what they do not read |
| Hooks with deterministic policy | known-bad patterns (paths, commands) | anything not in the pattern list |
| OS sandbox (Seatbelt, bubblewrap, Landlock) | writes outside the workspace; with a network proxy, unapproved hosts | kernel exploits; data the process may read legitimately |
| Container | most host access, if configured strictly | shared kernel; mounted secrets; Docker socket |
| gVisor / microVM (Firecracker) | kernel-level escapes, for multi-tenant cloud agents | anything reachable over the network you allowed |
| Egress control | exfiltration to unknown hosts | exfiltration through allowed hosts (GitHub gists, package registries) |

The two that matter most: **filesystem isolation and network isolation together.** Either alone is
weak. Without network limits, a sandboxed agent can still send your code out. Without filesystem
limits, it can read your SSH keys and send them out through any allowed host (`28` §7).

---

## 5. Reference stack C: the agent backend

> **In plain words.** An agent in production is a backend service that runs long, stateful jobs.
> It needs an API, a database for state, a queue for long runs, a stream to the browser, and rules
> for what happens when a user sends a second message while the first run is still going.
>
> **Real-world example.** A user sends "refund my order", then, after 5 seconds of no reply, sends
> "hello?". Both requests start a run on the same thread. Two runs append to the same message list,
> and the agent issues the refund twice. The fix is a per-thread run policy (§5.2), not a better prompt.

### 5.1 Topology

```
browser ──SSE/WebSocket──► API (FastAPI, stateless, N replicas)
                              │  auth → tenant/user/scopes → context
                              ├─ short runs: run in-process, stream events
                              └─ long runs:  enqueue ──► queue (Redis/RabbitMQ/SQS)
                                                            │
                                                     workers (N replicas)
                                                            │ LangGraph run
                         ┌──────────────────────────────────┼──────────────────────────┐
                         ▼                                  ▼                          ▼
              Postgres: checkpoints + store        model gateway (23)          tools / MCP gateway (26)
              (+ app tables, outbox)               keys, quotas, fallback      authz per call (29)
                         │
              OTel traces + LLM tracing (6) ── token & cost metrics per tenant (23 §7)
```

| Decision | Default | Why |
|---|---|---|
| Where runs execute | in the API for runs under ~30 s; workers for longer runs | HTTP timeouts, deploys and autoscaling kill long in-request runs |
| State | Postgres checkpointer, one DB for checkpoints, store and app data | one backup, one consistency model; transactional outbox for side effects |
| Streaming | SSE from the API; workers publish events through Redis pub/sub or the DB | the client reconnects with a cursor; runs must not depend on the socket |
| Durable multi-day work | LangGraph checkpoints for agent state; Temporal when you need timers, cross-system sagas, and strict exactly-once activities | `21` §14 |
| Background jobs (ingest, eval) | the same queue, separate worker pools | an eval backlog must not starve user runs |

### 5.2 Concurrent messages on one thread ("double texting")

LangChain's Agent Server names four strategies for a new run on a thread that already has one running.
They are the right vocabulary even if you build your own:

| Strategy | Behaviour | Use when |
|---|---|---|
| reject | refuse the new run | side-effecting agents; the UI disables input while running |
| enqueue | run the new input after the current run | chat where every message must be answered in order |
| interrupt | stop the current run, keep its progress, start the new one | the user corrects themselves mid-run |
| rollback | stop the current run, **discard** its progress, start the new one | the first message was a mistake |

Implement it with a **per-thread lock or lease** (a row lock or a Redis lock with a fencing token).
Without it, two workers write checkpoints for the same thread concurrently, and the checkpoint chain
forks.

### 5.3 Build vs LangChain's platform

The LangGraph platform (Agent Server, run by `langgraph-api`, with `langgraph-sdk` as client and
`langgraph-cli` for local dev) provides threads, runs, a task queue, cron, double-texting strategies,
streaming and a store as an HTTP API. It is the fastest path if you are already on LangSmith and
its hosting or licensing fits. Build your own when you need your own auth model, data residency, or
one platform for many frameworks. The design in §5.1 is the minimum you would build.

### 5.4 Failure modes and their fixes

| Failure | Mechanism | Fix | Read |
|---|---|---|---|
| Duplicate tool execution | retries, resumes, parallel `Send`s, double texting | idempotency keys from `thread_id` + `tool_call_id`; per-tool call limits | appendix I §6.3, `24` §8 |
| Partial graph completion | crash mid-super-step | task-level pending writes (§2.2); make side effects idempotent | `21` §5 |
| Checkpoint migration | state schema or node names changed while threads are paused | versioned state; test resuming old checkpoints in CI | appendix I §9.2 |
| Concurrent state updates | two writers to one key in one super-step, or two runs on one thread | reducers; per-thread lease | `21` §3.3, §5.2 here |
| Provider throttling | 429s under load | gateway quotas, backoff with jitter, fallback, admission control | `23` §5–§6 |
| Queue backlog | slow runs, retry storms | per-tenant concurrency limits, separate pools, backlog-age alerts | `../python-mastery/29-async-patterns-and-pitfalls.md` |
| Latency budget exceeded | too many steps | step and token budgets per request class; stream early | `22` §12 |
| Storage growth | quadratic checkpoint growth | shallow saver, retention, `delete_thread` | §2.2 |

---

## 6. Evaluation and observability tools

> **In plain words.** Two jobs: **see** what happened in a run (traces) and **decide** whether a
> change made things better (evals). Some tools do both. The real choice is where your trace data is
> allowed to live and whether the tool fits your existing telemetry.
>
> **Real-world example.** A team adds the OpenAI Agents SDK to a service under EU data-residency
> rules. Its default trace exporter sends spans to OpenAI's trace ingest endpoint *(source:
> `agents/tracing/processors.py`)*. The spans include prompts and tool outputs unless
> `trace_include_sensitive_data` is turned off. Nobody approved that data flow.

| Tool | How it hooks in | Strength | Watch out for |
|---|---|---|---|
| **LangSmith** | LangChain callbacks / `@traceable`, env-var switch; OTel ingest too | deepest LangChain/LangGraph view (graph nodes, middleware spans), datasets, experiments, online evaluators | SaaS by default (self-host is an enterprise plan); per-trace pricing at high volume |
| **Langfuse** | SDK built on OpenTelemetry; LangChain callback handler | open source, self-hostable, prompt management, LLM-as-judge | you run ClickHouse + Postgres + Redis + object storage when self-hosting |
| **Arize Phoenix** | OpenTelemetry + OpenInference instrumentors | open source, retrieval-focused views, runs locally | different semantic conventions (OpenInference) from OTel GenAI; map them |
| **Braintrust** | SDK + proxy | experiment-first workflow, fast diffing between runs | SaaS |
| **Ragas** | library: metrics over (question, contexts, answer, reference) | standard RAG metric names | judge-based scores need calibration (`08` §11); names collide with other metrics (appendix G) |
| **DeepEval** | pytest-style LLM test cases | fits CI as unit-test-shaped checks | same judge-calibration caveat |
| **promptfoo** | YAML/CLI test matrix over prompts × models × assertions, plus red-team suites | regression and security testing in CI without code | assertions can drift into "vibes"; keep deterministic checks first |
| **OpenTelemetry GenAI semconv** | standard span and metric names for model and tool calls | vendor-neutral; joins LLM spans with the rest of your traces | still evolving; content capture is opt-in |

Staff defaults:

1. **Instrument with OpenTelemetry first**, and export to whatever tool you choose. Changing vendors
   is then an exporter change, not a re-instrumentation.
2. **Traces are a data store with PII.** Set redaction (LangSmith `hide_inputs`/`hide_outputs`,
   appendix I's `TracePolicy`), retention, and region before the first production trace.
3. **Sample traces, not evals.** Keep 100% of errors and a sample of successes; run evals on fixed
   datasets.
4. **Deterministic checks first, judges second, humans third** (`08` §11). A judge score without a
   measured agreement with human labels is a rumor.

---

## 7. Frameworks other than LangChain, by their core idea

> **In plain words.** Every agent framework has the same loop. They differ in their one big idea:
> typed dependency injection, handoffs, event-driven workflows, roles, or prompt optimization. Learn
> the idea; you can read the API docs when you need them.
>
> **Real-world example.** A Python backend team needs one typed extraction agent with tests. A
> LangGraph graph is more structure than they need, and a typed agent library with a test model
> fits in a day. A month later they need a 3-day approval workflow. That is a graph or a workflow
> engine, whatever the agent library.

| Framework | Core idea | Inside *(source where marked)* | Pick when |
|---|---|---|---|
| **PydanticAI** | typed agents: `Agent[Deps, Output]`, dependency injection via `RunContext` | structured output defaults to a tool named `final_result`; `NativeOutput` and `PromptedOutput` are the other modes *(source)*; `TestModel`/`FunctionModel` for tests; durable execution through Temporal, DBOS or Prefect integrations *(source: `durable_exec/`)* | Python backends that value types and tests; small to medium agents |
| **OpenAI Agents SDK** | agents + **handoffs** + guardrails + sessions | a handoff is a tool named `transfer_to_<agent>` *(source)*; guardrails raise when `tripwire_triggered` *(source)*; tracing on by default to OpenAI | OpenAI-centric stacks; simple multi-agent handoffs |
| **Claude Agent SDK** | the Claude Code harness as a library | runs the Claude Code CLI as a subprocess *(source)*; hooks, permission callbacks, in-process MCP tools, sub-agents, skills | coding and file/shell-heavy agents (§4) |
| **Google ADK** | composable agent types | `LlmAgent` plus workflow agents `SequentialAgent`, `ParallelAgent`, `LoopAgent` *(source)*; session services; A2A support | Gemini / Vertex AI stacks; workflow-shaped multi-agent systems |
| **Microsoft Agent Framework** | successor that merges Semantic Kernel and AutoGen | agents plus graph **workflows** with checkpointing *(source: `_workflows/_checkpoint.py`)*; .NET and Python | Microsoft / Azure / .NET organisations. `22` §11's separate SK and AutoGen rows predate the merge |
| **LlamaIndex** | data framework: loaders, indexes, query engines; event-driven **Workflows** | steps triggered by typed events | RAG-heavy work where its ingestion and index abstractions save time |
| **Haystack** | typed component pipelines | components with declared inputs and outputs, wired into a graph | search-centric pipelines; teams that want explicit pipeline graphs |
| **CrewAI** | roles, goals and tasks | sequential or hierarchical process over role agents | prototyping the *shape* of a multi-agent workflow (`22` §11) |
| **DSPy** | programs, not prompts: signatures + modules, **optimized** against a metric | optimizers such as MIPROv2 and GEPA rewrite instructions and pick few-shot examples *(source: `teleprompt/`)* | you have a metric and labelled data, and a pipeline worth tuning |
| **Vercel AI SDK / Mastra** | TypeScript agents and streaming UI | — | TypeScript full-stack teams |

How to choose in one rule: **pick by the hardest requirement, not the first demo.** Durable
approvals and resumable state → LangGraph, a workflow engine, or Microsoft Agent Framework workflows.
Strict typing and testability → PydanticAI. A coding or shell agent → Claude Agent SDK, `codex`, or
`deepagents`. A metric to optimize against → DSPy. Many frameworks can be combined: a LangGraph node
can call a PydanticAI agent, and everything can export OTel.

---

## 8. Providers and serving: the facts that leak through abstractions

> **In plain words.** `init_chat_model("provider:model")` makes switching providers a one-line
> change. It does not make the providers behave the same.

| Difference | Example of the leak | Mitigation |
|---|---|---|
| Structured output support | native JSON-schema on some models, tool-call fallback on others; supported schema features differ | appendix I §5; pin the strategy; eval per provider |
| Prompt caching | automatic on some providers, explicit cache markers on others; different TTLs and minimum lengths | `30` §10; measure cache hit rate per provider |
| Parallel tool calls | some models emit several per turn, some one; some allow turning it off | idempotent tools; do not depend on either behaviour |
| Reasoning / thinking | separate content blocks, separate token budgets, sometimes incompatible with forced tool choice | use `content_blocks`; test with your strategy |
| Limits | context window, max output tokens, rate limits per org and per model | gateway quotas (`23` §6); model profiles |
| Errors | different status codes and retryable conditions | classify errors in one place (the gateway) |

**Serving your own models** (appendix E §6): vLLM's throughput comes from paged KV-cache memory,
continuous batching and prefix caching. Its structured output uses grammar-constrained decoding
(`07` §4). Ollama is for local development, not for multi-tenant serving. LiteLLM translates many
provider APIs into the OpenAI format as a library or a proxy. Treat that proxy as your gateway (`23`),
with the same reliability requirements.

---

## 9. What to learn first, and what to skip

> **In plain words.** Learn layers, not logos. One tool per layer, learned deeply, beats ten learned
> from their README.

| Order | Layer | Learn deeply | Recognize only |
|---|---|---|---|
| 1 | Loop and state | LangGraph (`21`), `create_agent` + middleware (appendix I) | swarm/supervisor libraries |
| 2 | Retrieval | one vector store (pgvector), BM25, RRF, one reranker, the eval harness (`03`, `04`, `08`) | every other vector DB |
| 3 | Measurement | golden sets, retrieval metrics, judge calibration, traces (`08`, `14`, §6) | the 5 eval SaaS products you are not using |
| 4 | Tools and integration | typed tools, authz at the boundary, MCP, idempotency (`24`, `26`, `29`) | A2A, until you need cross-org agents (`27`) |
| 5 | Production runtime | the agent backend (§5), gateway (`23`), sandboxing (`28`, §4.5) | Kubernetes details beyond Track A |
| 6 | One alternative framework | PydanticAI or the Agents SDK, enough to argue a choice (§7) | the rest |
| 7 | Specialisations | GraphRAG (appendix H), coding agents (§4), DSPy | — |

Skip, unless the job requires it: memorizing integration class names, comparing ten vector DBs on
blog benchmarks, multi-agent frameworks before a single agent works, and fine-tuning (README §7).

**The readiness checklist, as builds in this repo.** Each item is something to build and measure,
not something to read:

| Checklist item | Build |
|---|---|
| Custom graph with reducers, branches, bounded loop, checkpoint + resume, idempotency | `21` §19 labs; capstone C3 in [`LABS.md`](LABS.md) |
| Ingestion, hybrid retrieval, reranking, metadata filter, retrieval eval | capstone C1 |
| Typed tools with authz, MCP server, approval workflow, injection handling | `24` and `26` labs, `labs/tool-registry/`, capstone C2 |
| Compare LangGraph with one alternative | §7: build the same 2-tool agent in both, compare code, traces and failure handling |
| Offline eval dataset, traces of failed runs, latency/token/cost, deploy with recovery | P0–P2 in `README.md` §5; appendix I §10 probes as tests |

---

## 10. Interview questions

**Q1. Your agent service stores conversations in `PostgresSaver`. Six months later the checkpoint
table is the largest in the database. Why, and what do you do?**
Each super-step stores a new version of every changed channel, and the `messages` channel holds the
whole history, so storage grows with the square of thread length (§2.2: 4× per doubling). Use
`ShallowPostgresSaver` if you do not need time travel, add a retention job, delete threads on
request, and consider `DeltaChannel` once its format is stable.

**Q2. Why do coding agents use exact-string-replace or patch edit tools instead of `write_file`?**
Targeted edits fail loudly and locally when the model's view of the file is wrong. Whole-file writes
fail silently by changing lines nobody asked to change, and they cost output tokens proportional to
file size.

**Q3. Claude Code searches with grep; Cursor keeps an embeddings index. Which is right?**
Both, for different queries. Exact identifiers and call chains favour grep; conceptual questions
favour embeddings. An index costs sync, freshness and a second copy of the code to secure. Default
to grep and add an index when evals show conceptual queries failing (`05` §5.6).

**Q4. A user sends a second message while the first run is still calling tools. What should happen?**
Choose a per-thread policy: reject, enqueue, interrupt or rollback (§5.2). Enforce it with a
per-thread lease so two runs never write the same thread's checkpoints concurrently.

**Q5. You use `MultiServerMCPClient.get_tools()` with a `stdio` server, and latency per tool call is
800 ms. Why?**
By default each tool call opens a new MCP session, which for `stdio` starts a new server process.
Open one session per run with `client.session(...)` and `load_mcp_tools(session)`.

**Q6. When would you choose PydanticAI, the OpenAI Agents SDK or the Claude Agent SDK over LangGraph?**
PydanticAI for typed, testable agents in Python services. The Agents SDK for simple handoff-based
multi-agent systems in an OpenAI stack, with its tracing destination checked. The Claude Agent SDK
for file and shell agents, where its harness is the product. LangGraph when you need explicit control
flow, durable state and interrupts. Pick by the hardest requirement.

**Q7. What is the minimum sandbox for an agent that runs shell commands on untrusted repositories?**
A fresh isolated environment per run (container with a hardened runtime, gVisor or a microVM), no
host credentials, the workspace as the only writable path, and egress limited to an allow-list
through a proxy. Filesystem and network isolation together; either alone is weak (§4.5).

**Q8. Which observability decision should be made before the first production trace?**
Where traces are stored, what is redacted, and for how long. Traces contain prompts, retrieved
documents and tool outputs, so they are a PII store with its own residency and retention rules.

---

## 11. Sources

- PyPI metadata and installed source, October 2026: `langgraph` (`channels/delta.py`),
  `langgraph-checkpoint-postgres` (`checkpoint/postgres/base.py`, `shallow.py`,
  `store/postgres/base.py`), `langchain-mcp-adapters` (`client.py`, `tools.py`),
  `langgraph-supervisor` (README note, `supervisor.py`), `langgraph-store-postgres` (publisher and
  dependencies), `deepagents` (`graph.py`), `openai-agents` (`handoffs/__init__.py`, `guardrail.py`,
  `tracing/processors.py`), `pydantic-ai-slim` (`_output.py`, `output.py`, `durable_exec/`),
  `claude-agent-sdk` (`_internal/transport/subprocess_cli.py`), `google-adk` (`agents/`),
  `agent-framework-core` (`_workflows/`), `dspy` (`teleprompt/`).
- LangChain docs: multi-agent guide and supervisor tutorial; Agent Server double-texting —
  <https://docs.langchain.com/>.
- Anthropic, Claude Code documentation (tools, hooks, permissions, sandboxing, sub-agents, skills) and
  engineering posts on agent tools and sandboxing — <https://docs.claude.com/>,
  <https://www.anthropic.com/engineering>.
- OpenAI, Codex CLI repository and docs (`AGENTS.md`, `apply_patch`, sandbox modes) —
  <https://github.com/openai/codex>.
- Cursor engineering blog posts on codebase indexing (Merkle-tree sync, path obfuscation) and semantic
  search for agents — <https://cursor.com/blog>.
- Aider documentation on repository maps and edit formats — <https://aider.chat/docs/>.
- OpenTelemetry GenAI semantic conventions —
  <https://opentelemetry.io/docs/specs/semconv/gen-ai/>.
