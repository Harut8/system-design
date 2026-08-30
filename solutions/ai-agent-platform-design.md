# Internal AI Agent Platform: Design Document

> Solution to [`tasks/ai-agent-platform.md`](../tasks/ai-agent-platform.md).

### Prerequisites and Learning Resources

Before or alongside this document, study these deep-dive chapters from the curriculum:

| Topic | Resource | Why |
|-------|----------|-----|
| Agent orchestration patterns | [`ai-rag/22-agent-orchestration-patterns.md`](../ai-rag/22-agent-orchestration-patterns.md) | ReAct, plan-and-execute, supervisor, multi-agent — the patterns this platform hosts |
| LangGraph internals | [`ai-rag/21-langgraph-deep-dive.md`](../ai-rag/21-langgraph-deep-dive.md) | Graph execution model, state channels, checkpointing — §7 of this doc builds on it |
| LangChain architecture | [`ai-rag/20-langchain-architecture-and-internals.md`](../ai-rag/20-langchain-architecture-and-internals.md) | LCEL, runnables, callback system — the SDK layer our platform abstracts over |
| RAG mental models | [`ai-rag/00-mental-models.md`](../ai-rag/00-mental-models.md) | Representation, retrieval, generation pipeline — context for §14 RAG Pipeline |
| Embeddings | [`ai-rag/01-embeddings-and-representation.md`](../ai-rag/01-embeddings-and-representation.md) | Embedding models, similarity, and vector spaces — used in RAG and memory |
| Chunking strategies | [`ai-rag/02-chunking-and-document-processing.md`](../ai-rag/02-chunking-and-document-processing.md) | Chunking trade-offs referenced in §14 |
| Retrieval and reranking | [`ai-rag/04-retrieval-hybrid-and-reranking.md`](../ai-rag/04-retrieval-hybrid-and-reranking.md) | Hybrid search, cross-encoder reranking — the retrieval path agents use |
| Evaluation methodology | [`ai-rag/08-evaluation-methodology.md`](../ai-rag/08-evaluation-methodology.md) | Metrics, LLM-as-judge, golden datasets — foundation for §19 Evaluation |
| Deployment and compute | [`ai-rag/appendix-e-deployment-and-compute.md`](../ai-rag/appendix-e-deployment-and-compute.md) | GPU provisioning, inference optimization — relevant to §23 Deployment |

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Key Decisions, Made Explicit](#2-key-decisions-made-explicit)
3. [Capacity Estimates](#3-capacity-estimates)
4. [High-Level Architecture](#4-high-level-architecture)
5. [Agent SDK Design](#5-agent-sdk-design)
6. [Declarative Agent Definitions](#6-declarative-agent-definitions)
7. [LangGraph Integration and the Graph Model](#7-langgraph-integration-and-the-graph-model)
8. [Agent Runtime: Execution Engine Internals](#8-agent-runtime-execution-engine-internals)
9. [Scheduling and the Worker Pool](#9-scheduling-and-the-worker-pool)
10. [Checkpointing, Resumption, and Human-in-the-Loop](#10-checkpointing-resumption-and-human-in-the-loop)
11. [Streaming and Cancellation](#11-streaming-and-cancellation)
12. [Tool Platform](#12-tool-platform)
13. [Model Gateway](#13-model-gateway)
14. [RAG Pipeline](#14-rag-pipeline)
15. [Prompt Management](#15-prompt-management)
16. [Memory Architecture](#16-memory-architecture)
17. [Multi-Agent Orchestration](#17-multi-agent-orchestration)
18. [Observability](#18-observability)
19. [Evaluation Framework](#19-evaluation-framework)
20. [Security](#20-security)
21. [Data Models](#21-data-models)
22. [API Design](#22-api-design)
23. [Deployment Architecture](#23-deployment-architecture)
24. [Failure Modes](#24-failure-modes)
25. [Cost Model](#25-cost-model)
26. [Trade-offs and Design Decisions](#26-trade-offs-and-design-decisions)
27. [Evolution Path](#27-evolution-path)
28. [Exercises](#28-exercises)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| **Hosting** | Does the platform run agent code, or just support it? | Platform **hosts and executes** agent runs as a managed service ("Agent-as-a-Service"). Teams ship SDK-defined or declarative agents to the platform; the platform's workers run them. A BYO-service mode exists for teams that must run agent code inside their own service mesh, using the platform only for Gateway/RAG/Observability (§2.1). |
| **Trust boundary** | Are tools trusted first-party code? | No. Any team can register a tool. Tools are treated as **untrusted by default** and sandboxed; only tools explicitly marked and reviewed as "trusted, in-process" skip the sandbox (§2.2, §12). |
| **LangGraph** | Full LangGraph runtime, or something else? | We adopt **LangGraph's graph abstraction** (`StateGraph`, nodes, edges, conditional edges, reducers) as the authoring API because it's the de facto standard our teams already use. We **do not** run LangGraph's own in-memory executor in production — we compile the graph to our own distributed, checkpointed, multi-tenant execution engine (§7). |
| **Scale** | Runs/sec at target? | 3,000/sec sustained, 10,000/sec peak, 500M+ LLM calls/day (§3). |
| **Tenancy model** | Isolation granularity? | Tenant = internal team. Hard data isolation (knowledge bases, memory, traces, prompts, budgets) per tenant; agents within a tenant share nothing across tenant boundaries without an explicit, audited grant. |
| **Consistency** | What must be strongly consistent? | Agent/prompt definitions: read-after-write strong (a deploy is a control-plane write gated by consensus). Traces, costs, eval results: eventually consistent, append-only, never silently dropped. |
| **Long-running agents** | How long can a run last? | Minutes for interactive agents (bounded by max-steps/budget); up to **24 hours** for async/batch "deep research" style agents, checkpointed so a worker restart costs at most one in-flight step. |
| **Model hosting** | Does the platform run GPUs for open-weight models? | Yes, as one provider behind the Model Gateway (§13) — self-hosted vLLM fleet for a small set of fine-tuned/open models, alongside proxied external providers (Anthropic, OpenAI). Both look identical to callers. |
| **Failure model** | Byzantine tools/models? | No malicious platform components, but models and tools are treated as **unreliable and occasionally adversarial-input-bearing** — a retrieved document or tool result may contain a prompt injection attempt, and the runtime defends against that specifically (§20.2), distinct from classic Byzantine fault tolerance. |
| **Team size** | Who operates this? | A platform team of ~25–30 engineers (SDK, Runtime, Gateway, RAG, Eval, Security sub-teams) serving 150+ consuming teams. This ratio is why "self-serve with strong defaults" beats "flexible but requires platform-team hand-holding" throughout. |

### Key Assumptions

1. **Most agents are simple; a few are extremely complex.** 80% of agents are single-pattern ReAct loops with 2–5 tools. The platform must make that path trivial (a decorator and a few lines) while not blocking the 5% doing bespoke multi-agent graphs with custom control flow.
2. **Latency requirements are bimodal.** Interactive chat agents care about time-to-first-token; batch/research agents care about throughput and cost, not latency. One scheduling policy for both would be wrong (§9).
3. **Cost is a first-class correctness property, not an afterthought metric.** A platform that can't stop a runaway agent from spending $10,000 in an hour has failed a functional requirement, not just an NFR.
4. **Prompts change far more often than code.** Prompt iteration velocity (many times a day, by non-engineers in some teams) is the dominant workflow the Prompt Registry must optimize for — it is closer to a CMS than to a code repository in usage pattern, even though it is versioned like one.
5. **Tool results and retrieved documents are the primary injection attack surface**, not the user's direct chat input (which is easier to filter). Design attention is weighted accordingly.
6. **Determinism is not achievable and not the goal.** Evaluation and observability are built around *distributions of behavior*, not bit-for-bit reproducibility of a given run.

### What We Are Explicitly *Not* Promising

- **Not** exactly-once tool execution for non-idempotent tools — at-least-once with idempotency keys is the contract; tools that can't be made idempotent must say so and accept manual reconciliation on retry.
- **Not** a no-code builder UI in v1 — the declarative YAML format is designed so one can be layered on top later (§6, §27), but building it is out of scope here.
- **Not** protection against a model provider returning subtly wrong-but-plausible output — that's an evaluation and guardrail problem (§19, §20), not something the runtime can detect in general.
- **Not** cross-region strong consistency for control-plane writes — each region's control plane is authoritative for tenants pinned to it (data residency, §20.6); there is no global transaction across regions.

---

## 2. Key Decisions, Made Explicit

### 2.1 Hosting model: Agent-as-a-Service, with a BYO-service escape hatch

The central architectural fork is: **does agent code run on the platform's infrastructure, or does the platform just provide services (gateway, RAG, tracing) that agent code calls from wherever the owning team hosts it?**

| | Agent-as-a-Service (chosen default) | BYO-service (supported escape hatch) |
|---|---|---|
| Where agent code runs | Platform-operated worker fleet | Team's own Kubernetes namespace / service |
| Isolation unit | Per-run sandboxed container/process, multi-tenant fleet | Whole service is the isolation unit — team already owns it |
| Checkpointing, retries, scheduling | Platform-provided, uniform | Team re-implements or opts out |
| Deploy path | `platform deploy agent.yaml` → running in minutes | Team's own CI/CD; only SDK calls hit the platform |
| Onboarding cost | Very low — no infra to stand up | Higher — team owns a service |
| Best for | The 90% case: teams that want an agent, not an infra project | Teams with existing services that need agent capability bolted on, or extreme latency/colocation needs |

We choose **Agent-as-a-Service as the default** because the stated goal is "without managing infra" — that's a direct requirement, and it's also what makes the platform's other guarantees (checkpointing, budget enforcement, uniform tracing, cancellation) cheap to provide uniformly. BYO-service remains supported because forcing every consumer onto the hosted runtime would exclude latency-critical or already-service-shaped use cases, and because it's a natural fallback if a team's workload doesn't fit the hosted execution model (e.g., needs GPU-colocated tool execution).

Consequence: the Agent Runtime (§8–§11) is designed as a **multi-tenant execution engine**, not a library. Tenant isolation between arbitrary teams' Python code is now a hard requirement, not a nice-to-have (§12.4).

### 2.2 Trust boundary: tools are untrusted by default

Because any of 150+ teams can register a tool, and any agent (potentially from another team, via multi-agent delegation, §17) might invoke it, **we do not assume tools are safe code**. Three tiers:

| Tier | Definition | Execution | Example |
|---|---|---|---|
| **Platform-native** | Written and reviewed by the platform team | In-process, in the runtime worker, no sandbox overhead | `web_search`, `retrieve_from_kb`, `send_slack_message` |
| **First-party, reviewed** | Written by a product team, code-reviewed, marked trusted by an owning team's tech lead | Isolated process (cgroup/namespace limited), same host | A team's internal billing-lookup tool |
| **Third-party / unreviewed / code-execution** | Anything else, including any tool that itself executes model- or user-supplied code (a "code interpreter" tool) | **Sandboxed** — gVisor or Firecracker microVM, no network by default, explicit egress allowlist, CPU/memory/time limits | A "run this Python snippet" tool, a community-contributed connector |

Default tier for a newly registered tool is **third-party/sandboxed**; a tool is promoted to a lower-overhead tier only through an explicit review step recorded in the audit log. This is a deliberately conservative default — most tools never need promotion, since sandbox overhead (§12.5) is small relative to the LLM call latency it's nested inside.

### 2.3 LangGraph: adopt the authoring model, replace the executor

LangGraph is the de facto graph-based agent authoring pattern our internal teams already know. Two options:

1. **Embed LangGraph's own runtime** (its `CompiledGraph.invoke/stream`, in-memory or `Checkpointer`-backed) directly inside our worker processes.
2. **Adopt LangGraph's graph-building API surface** (`StateGraph`, `add_node`, `add_edge`, `add_conditional_edges`, reducers on state channels) as the **authoring interface**, but compile the resulting graph into our own execution engine's internal representation, with our own scheduler, checkpoint store, and tracing.

We choose **(2)**. LangGraph's own executor is single-process and its checkpointer abstraction, while pluggable, isn't built for the multi-tenant admission control, cross-run budget enforcement, and worker-pool scheduling this platform needs at 10,000 runs/sec peak across 150 tenants. Reimplementing the executor lets us:

- Enforce **per-tenant budgets and rate limits** at the node-dispatch level, not just around the whole graph.
- Checkpoint to a **shared, durable, tenant-partitioned store** (§10) instead of a per-process/per-thread checkpointer.
- Emit **first-class trace spans** for every node/edge transition without instrumenting each agent's code.
- Run graphs from **teams whose code we don't trust** inside worker sandboxes without giving them access to a raw LangGraph process that could, in principle, escape resource limits.

The cost: we maintain a compatibility shim that accepts `StateGraph` definitions and translates them into our internal `AgentGraph` IR, and we must track LangGraph API surface changes upstream. We scope "compatible" precisely in §7.1.

---

## 3. Capacity Estimates

| Quantity | Value | Basis |
|---|---|---|
| Registered agent definitions | ~2,000 | 150 teams × ~13 agents/team average |
| Sustained agent runs | 3,000/sec | Stated NFR |
| Peak agent runs | 10,000/sec | Stated NFR (3.3× sustained, typical for launch-day/incident spikes) |
| Avg LLM calls per run | 6 | ReAct loop, ~3 reasoning turns × ~2 (main + occasional retry) |
| LLM calls/sec sustained | ~18,000/sec | 3,000 runs/sec × 6 |
| LLM calls/day | ~1.5B at peak-sustained blend | ≈ within the "500M+/day" NFR floor with headroom |
| Avg tool calls per run | 3.5 | Stated NFR range 2–5 |
| Tool calls/sec sustained | ~10,500/sec | 3,000 × 3.5 |
| Trace spans/sec | ~250,000/sec | ~25 spans/run (LLM calls, tool calls, retrieval, node transitions) × 10,000 peak runs/sec |
| RAG queries/sec | ~6,000/sec | Assume 60% of runs perform ≥1 retrieval |
| Avg run duration (interactive) | 4–12 s | 6 LLM calls × ~1–2s each, some parallelized |
| Long-tail run duration | up to 24h | Async/deep-research agents, checkpointed |
| Concurrent long-running runs | ~50,000 | Checkpointed, mostly idle-waiting-on-tool/model, cheap to hold |
| Worker fleet (interactive pool) | ~4,000 pods | Sized for peak 10,000 runs/sec at ~2.5 concurrent-runs/pod effective (I/O-bound, async) |
| Worker fleet (batch pool) | ~1,500 pods | Sized for long-tail concurrency, not per-request latency |
| Model Gateway throughput | ~20,000 req/sec sustained, 60,000 peak | LLM calls/sec + retries/fallbacks (~1.15×) headroom |
| Trace storage ingest | ~250,000 spans/sec × ~1.5 KB/span ≈ 375 MB/sec | Sized for a columnar trace store (§18) with local SSD buffering ahead of durable storage |
| Trace storage retention | Hot (queryable) 30 days, cold (archived) 1 year | Debugging window vs. compliance/audit window |
| Vector store size | 50M docs/KB × up to 5,000 KBs — realistically ~200M total chunks across all KBs at steady state | Most KBs are far smaller than the 50M ceiling; ceiling sizes the largest tenants |
| Embedding calls/sec (ingestion) | ~2,000/sec sustained | Continuous re-ingestion + new sources, batched |
| Model Gateway cost overhead budget | < 5% of raw provider spend | Stated NFR — bounds how much gateway/tracing/orchestration compute we can spend per dollar of model spend |

**Rule of thumb used throughout:** at this scale, the platform's own overhead (gateway routing, tracing, checkpoint writes) must be **sub-linear-cost relative to the LLM call it wraps** — a $0.01 model call cannot ride on $0.02 of platform bookkeeping. This is the constraint that rules out synchronous, strongly-consistent writes on the hot path everywhere they aren't strictly required (§8, §18).

---

## 4. High-Level Architecture

```
                              ┌─────────────────────────────────────────────┐
                              │              Control Plane                  │
                              │  ┌───────────┐ ┌────────────┐ ┌──────────┐  │
                              │  │  Agent     │ │  Prompt    │ │  Tool    │  │
                              │  │  Registry  │ │  Registry  │ │  Registry│  │
                              │  └───────────┘ └────────────┘ └──────────┘  │
                              │  ┌───────────┐ ┌────────────┐ ┌──────────┐  │
                              │  │  RBAC /    │ │  Budget /  │ │  KB      │  │
                              │  │  IAM       │ │  Quota Svc │ │  Admin   │  │
                              │  └───────────┘ └────────────┘ └──────────┘  │
                              └───────────────────────┬───────────────────────┘
                                                       │ deploy / read config (strongly consistent)
     ┌─────────────────────────────────────────────────┼─────────────────────────────────────────────────┐
     │                                          Data Plane                                                 │
     │                                                                                                       │
     │   Client / Caller                                                                                     │
     │   (product service, chat UI, cron)                                                                    │
     │        │  gRPC/REST: CreateRun / StreamRun / CancelRun                                                 │
     │        ▼                                                                                                │
     │   ┌──────────────┐        ┌──────────────────────────────────────────────────────────────────────┐    │
     │   │  API Gateway  │──────▶│                     Agent Runtime Service                             │    │
     │   │  (authn,      │       │  ┌───────────┐  ┌───────────────┐  ┌───────────────┐  ┌────────────┐  │    │
     │   │  RBAC, rate   │       │  │  Run       │  │  Execution     │  │  Checkpoint    │  │  Scheduler │  │    │
     │   │  limit)       │       │  │  Manager   │─▶│  Engine        │─▶│  Store         │  │  / Worker   │  │    │
     │   └──────────────┘       │  │  (state    │  │  (state        │  │  (Postgres +   │  │  Pool       │  │    │
     │                           │  │  machine)  │  │  machine per   │  │  object store) │  │             │  │    │
     │                           │  └───────────┘  │  run)          │  └───────────────┘  └────────────┘  │    │
     │                           └──────┬──────────┴───────┬────────┴──────────┬──────────────────┬───────┘    │
     │                                  │                   │                   │                  │            │
     │                                  ▼                   ▼                   ▼                  ▼            │
     │                        ┌──────────────┐   ┌──────────────────┐  ┌───────────────┐  ┌────────────────┐  │
     │                        │  Model         │   │  Tool Gateway     │  │  RAG Service   │  │  Memory Service │  │
     │                        │  Gateway       │   │  (registry,       │  │  (ingest +     │  │  (short/long/   │  │
     │                        │  (multi-       │   │  sandboxing,      │  │  retrieve)     │  │  working)       │  │
     │                        │  provider,     │   │  credential       │  └───────┬───────┘  └────────┬───────┘  │
     │                        │  routing,      │   │  injection)       │          │                    │          │
     │                        │  fallback)     │   └─────────┬─────────┘          ▼                    ▼          │
     │                        └───────┬───────┘             │          ┌────────────────┐  ┌────────────────────┐│
     │                                │                       │          │ Vector Store   │  │ Postgres (session, ││
     │                                ▼                       ▼          │ (pgvector/     │  │ entity, episodic)  ││
     │                     ┌────────────────────┐  ┌────────────────────┐│ Qdrant)        │  └────────────────────┘│
     │                     │ External Providers  │  │ Sandboxed Executors ││                │                       │
     │                     │ (Anthropic, OpenAI)  │  │ (gVisor/Firecracker)│└────────────────┘                       │
     │                     │ + Self-hosted vLLM   │  │ + Credential Vault  │                                        │
     │                     │ fleet                │  └────────────────────┘                                        │
     │                     └────────────────────┘                                                                  │
     │                                                                                                              │
     │   Cross-cutting (every component emits into these):                                                         │
     │   ┌──────────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────────────────────┐   │
     │   │  Observability Pipeline   │   │  Evaluation Service       │   │  Security Layer (PII, injection,      │   │
     │   │  (OTel collector → trace  │   │  (offline/online eval,    │   │  content filter — inline middleware   │   │
     │   │  store, metrics, logs)    │   │  regression detection)    │   │  + async audit)                       │   │
     │   └──────────────────────────┘   └──────────────────────────┘   └──────────────────────────────────────┘   │
     └──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Control plane vs. data plane

This split mirrors the KV-store and other platform designs in this repo deliberately: it's the right shape for any multi-tenant system that must stay available even when its own management layer is under stress.

| | Control Plane | Data Plane |
|---|---|---|
| Contains | Agent Registry, Prompt Registry, Tool Registry, RBAC/IAM, Budget/Quota service, KB admin | Agent Runtime Service, Model Gateway, Tool Gateway, RAG Service, Memory Service |
| Consistency | Strongly consistent (Postgres primary + read replicas, or a small Raft-backed config store) | Mostly eventually consistent / best-effort durable |
| Write rate | Low — deploys, permission changes, prompt edits: ~10s/sec platform-wide | High — run creation, LLM calls, tool calls: tens of thousands/sec |
| Availability target | 99.95% | 99.9%, and must **degrade gracefully**, not fail closed, if control plane is briefly unreachable (workers cache the last-known-good agent config) |
| Failure isolation | A control-plane outage blocks *new deploys and permission changes*; it must **not** block already-deployed agents from running | — |

The critical design rule enforced everywhere below: **the data plane must be able to run already-deployed agents using cached config even if the control plane is completely down.** Every worker holds a local, TTL'd cache of the `AgentVersion`, `PromptVersion`, RBAC decisions, and budget snapshots it needs, refreshed asynchronously. This is the same "cache the control plane, don't call it synchronously on the hot path" pattern used by service meshes (Envoy + xDS) and is why the platform can hit 99.9% data-plane availability while the control plane sits at a lower 99.95% (fewer nines because it's simpler to run at higher consistency, not because it's less important).

### 4.2 Request path, end to end (interactive agent)

1. Caller sends `CreateRun(agent_id, input, stream=true)` to the API Gateway. Gateway authenticates the caller, checks RBAC (can this principal invoke this agent?), checks the tenant's rate limit/budget headroom, and forwards to the Run Manager.
2. Run Manager resolves the `AgentVersion` (from local cache, refreshed from Agent Registry), assigns a `run_id`, writes an initial `AgentRun` record (async, fire-and-forget durable log — not on the critical path to starting execution), and hands off to the Scheduler.
3. Scheduler picks a worker from the interactive pool (bin-packed by current concurrent-run count and tenant fairness weight, §9) and dispatches the run.
4. The Execution Engine on that worker instantiates the compiled `AgentGraph`, enters the state machine (§8), and begins executing nodes — each LLM call routes through the Model Gateway, each tool call through the Tool Gateway, each retrieval through the RAG Service.
5. Every state transition and every sub-call emits an OTel span, asynchronously shipped to the Observability Pipeline; token/cost counters are accumulated in-run and flushed to the Cost Ledger on each LLM call.
6. Output tokens and trajectory events stream back to the caller over the original connection (SSE) as they're produced — the caller does not wait for the full run to finish to see first output.
7. On terminal state (`done`, `error`, `cancelled`, `budget_exceeded`), the Execution Engine writes a final checkpoint, flushes the `AgentRun` record, and releases the worker slot.
8. Asynchronously: the Evaluation Service may sample this run for online eval; the Security Layer's async audit path double-checks anything the inline filters flagged as borderline.

Everything in step 5 onward that isn't on the "get tokens back to the user" path is **decoupled via a durable queue** (not a synchronous call), so a slow trace store or a backlogged eval sampler never adds latency to a user-facing agent response.

---

## 5. Agent SDK Design

The SDK is the primary developer surface. Design goal: **a working ReAct agent in under 20 lines, and the same object runs identically on a developer's laptop (against a sandboxed local runtime) and in production** — no "it worked locally but the deployed behavior differs" class of bugs.

### 5.1 Tool definition

Tools are plain Python functions with type hints; the SDK derives a JSON Schema from the signature and docstring rather than requiring a hand-written schema.

```python
from platform_sdk import tool, ToolContext

@tool(
    name="get_account_balance",
    description="Look up the current balance for a customer account.",
    timeout_s=5,
    retries=2,
    requires_credential="billing_api",   # injected at call time, never touches agent code
)
def get_account_balance(account_id: str, ctx: ToolContext) -> dict:
    """
    Args:
        account_id: Internal account identifier, e.g. 'acct_9F2B'.
    Returns:
        {"balance_cents": int, "currency": str, "as_of": str}
    """
    client = ctx.credential("billing_api")   # short-lived scoped token, injected
    resp = client.get(f"/accounts/{account_id}/balance")
    return resp.json()
```

The `@tool` decorator:

- Introspects the type-hinted signature to build the **input JSON Schema** (`account_id: str` → `{"type": "string"}`), and the return type / docstring `Returns:` block to build the **output schema** used to validate what comes back before it's shown to the model (§12.2).
- Registers `requires_credential="billing_api"` as a declared dependency — deploying an agent that uses this tool without a grant for that credential fails validation at deploy time, not at run time in front of a user.
- Wraps the call with the declared `timeout_s` and `retries`, and — because no `sandbox=` override is given — defaults to the **third-party/sandboxed** tier (§2.2) unless the tool is registered by the platform team or explicitly promoted.
- `ToolContext` is the one piece of "ambient" state a tool function receives: it carries the current `run_id`, `tenant_id`, credential accessor, and a handle to write structured sub-events into the trace (`ctx.log_event(...)`) — deliberately not a global/thread-local, so tools remain testable as pure functions.

### 5.2 Agent definition — ReAct

```python
from platform_sdk import Agent, ReActPattern, ModelConfig

support_agent = Agent(
    name="support-triage",
    pattern=ReActPattern(max_steps=8),
    model=ModelConfig(
        primary="claude-sonnet-4-5",
        fallback=["gpt-4.1"],
        max_cost_usd_per_run=0.50,
    ),
    system_prompt="prompts/support-triage/v3",   # reference into Prompt Registry, not inline
    tools=[get_account_balance, search_kb, escalate_to_human],
    knowledge_bases=["support-docs"],
    memory=MemoryConfig(
        conversation="sliding_window(turns=20)",
        long_term="entity_memory(scope='per_user')",
    ),
    guardrails=["pii_redaction", "injection_detection"],
)
```

`Agent(...)` does not itself execute anything — it's a declaration. Running it is a separate, explicit step, which matters for testability:

```python
run = support_agent.run(
    input={"message": "Why was I charged twice?"},
    context={"user_id": "u_123"},
)
for event in run.stream():          # local: same event schema as production streaming API
    if event.type == "token":
        print(event.text, end="")
    elif event.type == "tool_call_started":
        print(f"\n[calling {event.tool_name}]")

final = run.result()                 # blocks until terminal state
```

Locally, `support_agent.run(...)` executes against an embedded copy of the Execution Engine (§8) running in-process with a local SQLite checkpoint store and a sandboxed subprocess for tool calls — **the same state machine and node semantics as production**, so a developer's local test of "does the agent call `escalate_to_human` when it should" is a valid predictor of production behavior. What differs locally is only the substrate (in-process vs. distributed scheduler) and that Model Gateway calls hit a dev-tier quota.

### 5.3 Agent definition — Plan-and-Execute

```python
from platform_sdk import PlanAndExecutePattern

research_agent = Agent(
    name="deep-research",
    pattern=PlanAndExecutePattern(
        planner_model=ModelConfig(primary="claude-opus-4-1"),
        executor_model=ModelConfig(primary="claude-sonnet-4-5"),
        max_replans=3,
        checkpoint_every_step=True,     # required for runs that may last hours
    ),
    tools=[web_search, read_url, write_file, run_python],
    execution_pool="batch",              # scheduled on the async/batch worker pool, not interactive
)
```

`PlanAndExecutePattern` compiles to a fixed sub-graph: `plan → [execute_step]* → (replan | finish)`, where the `execute_step` node is itself a nested ReAct loop over the declared tools. `checkpoint_every_step=True` is the knob that trades a small amount of latency per step (a durable checkpoint write, §10) for the ability to resume a 4-hour research run from step 47 instead of step 0 after a worker eviction — the default is `False` for short interactive agents where restart-from-scratch is cheaper than checkpoint overhead.

### 5.4 Lifecycle hooks

```python
@support_agent.on_step_start
def log_step(state: AgentState):
    ...

@support_agent.on_tool_error
def handle_billing_timeout(error: ToolError, state: AgentState) -> ToolErrorAction:
    if error.tool_name == "get_account_balance":
        return ToolErrorAction.RETRY_WITH_FALLBACK(tool=search_kb)
    return ToolErrorAction.PROPAGATE

@support_agent.before_output
def enforce_disclaimer(output: AgentOutput) -> AgentOutput:
    output.text += "\n\n_This is an automated response._"
    return output
```

Hooks are the escape hatch for team-specific policy that doesn't belong in the generic runtime: custom error recovery, output post-processing, step-level logging beyond what tracing captures automatically. They execute **inside the tenant's sandbox**, not the runtime's trusted process, for the same trust-boundary reason as tools (§2.2) — a hook is arbitrary team-authored code.

### 5.5 Streaming interface (client side)

```python
async for event in client.stream_run(agent_id="support-triage", input=payload):
    match event:
        case TokenEvent(text=t):          ...
        case ToolCallStarted(name=n, args=a): ...
        case ToolCallFinished(name=n, result=r, latency_ms=l): ...
        case StepBoundary(step=i, state_snapshot=s): ...
        case RunFinished(output=o, cost_usd=c, trace_id=tid): ...
```

The event schema is identical whether the client is the local SDK loop (§5.2) or a remote SSE/WebSocket connection to the hosted Runtime — one contract, two transports.

---

## 6. Declarative Agent Definitions

Not every team wants (or should need) a Python service. The declarative format targets: ops/support teams tuning an existing pattern, and — per the evolution path (§27) — a future no-code UI that emits this same YAML rather than a bespoke format.

```yaml
apiVersion: platform/v1
kind: Agent
metadata:
  name: support-triage
  team: cx-platform
spec:
  pattern: react
  maxSteps: 8
  model:
    primary: claude-sonnet-4-5
    fallback: [gpt-4.1]
    maxCostUsdPerRun: 0.50
  prompt:
    ref: prompts/support-triage
    version: v3
  tools:
    - ref: registry/get_account_balance
    - ref: registry/search_kb
    - ref: registry/escalate_to_human
  knowledgeBases:
    - support-docs
  memory:
    conversation: { strategy: sliding_window, turns: 20 }
    longTerm: { strategy: entity_memory, scope: per_user }
  guardrails: [pii_redaction, injection_detection]
  execution:
    pool: interactive
  budgets:
    dailyUsdCap: 200
```

Key design constraints on this format:

1. **It compiles to the exact same internal `AgentGraph` IR that the SDK's `Agent(...)` object compiles to.** There is one compiler, two front-ends. This is what makes "LangGraph-compatible" (§7) and "YAML-declarative" and "Python SDK" three views of the same underlying execution model rather than three code paths to maintain and keep behaviorally consistent.
2. **Tools and prompts are referenced, never inlined.** A YAML agent cannot define a new tool inline (that requires code, hence the SDK) — it can only compose tools that already exist in the registry. This keeps the declarative surface genuinely safe for less-technical authors: there's no arbitrary-code path through YAML.
3. **Every field has a schema-validated, versioned meaning.** `apiVersion: platform/v1` is not decorative — the Agent Registry rejects a YAML agent whose `apiVersion` it doesn't recognize rather than guessing, the same discipline Kubernetes manifests use and for the same reason (silent reinterpretation of old configs under new semantics is how platforms accumulate undebuggable drift).
4. **`kind: Agent` today; `kind: Graph` for custom control flow** (§7.2) lets the same YAML family express both simple pattern-based agents and raw graphs, sharing registry/validation/deploy tooling.

Validation on `platform deploy agent.yaml` runs synchronously and rejects on: unknown tool refs, missing credential grants for any referenced tool, prompt version that doesn't exist, model alias not permitted for the tenant's data-residency tier, and budget fields below platform-enforced floors (nobody accidentally sets `dailyUsdCap: 0` and then wonders why the agent never runs).

---

## 7. LangGraph Integration and the Graph Model

### 7.1 What "LangGraph-compatible" means, precisely

We support the **graph-construction API** — `StateGraph`, typed state schemas, `add_node`, `add_edge`, `add_conditional_edges`, `START`/`END`, reducer-annotated state channels (`Annotated[list, add_messages]`-style merge semantics) — as an **import path**, not a runtime. Concretely:

- A team can write a graph using the standard LangGraph Python API against our SDK's `StateGraph` (a thin, API-compatible re-export) and call `.compile()`.
- `.compile()` does **not** return a LangGraph `CompiledGraph` that executes locally. It returns our `AgentGraph` IR — nodes, edges, conditional routing functions, and the state schema, statically analyzed and validated (cycle detection, reachability of `END`, channel type-checking).
- That IR is what the Execution Engine (§8) runs. LangGraph-authored node functions (`def my_node(state: State) -> dict: ...`) run unmodified — a node function is just a node function, regardless of which engine calls it.
- **Not supported**: LangGraph's own built-in checkpointers, LangGraph Platform-specific deployment features, and any node that reaches into LangGraph's runtime internals directly rather than through the public graph-building API. A compatibility linter flags these at compile time with an actionable error rather than failing silently at run time.

This gets a team ~95% of "just import LangGraph and it works" for graphs built the idiomatic way, while giving us a single execution engine to secure, scale, checkpoint, and trace.

### 7.2 Graph model

```python
from platform_sdk.graph import StateGraph, START, END
from typing import Annotated, TypedDict
from operator import add

class ResearchState(TypedDict):
    query: str
    findings: Annotated[list[str], add]     # reducer: new findings are appended, not replaced
    plan: list[str]
    step_idx: int

graph = StateGraph(ResearchState)
graph.add_node("plan", make_plan)
graph.add_node("search", run_search)
graph.add_node("synthesize", synthesize)

graph.add_edge(START, "plan")
graph.add_conditional_edges(
    "plan",
    lambda s: "search" if s["step_idx"] < len(s["plan"]) else "synthesize",
    {"search": "search", "synthesize": "synthesize"},
)
graph.add_edge("search", "plan")            # cycle: replan/advance after each search
graph.add_edge("synthesize", END)

compiled = graph.compile(checkpointer="platform")   # our distributed checkpoint store
```

| Concept | Support | Notes |
|---|---|---|
| Nodes | Full | Any callable `(state) -> partial_state`; may itself be a sub-agent (§17) |
| Edges (static) | Full | Direct node-to-node transitions |
| Conditional edges | Full | Routing function evaluated by the engine, traced as a `graph.route` span |
| Cycles | Full | Required for ReAct-style loops; engine enforces `max_steps` as a cycle-breaker independent of graph logic |
| State channels + reducers | Full | Reducers (`add`, `add_messages`, custom) run inside the engine's state-merge step after each node, same semantics as upstream |
| Subgraphs | Full | A compiled graph can be embedded as a node in another graph — this *is* our multi-agent composition primitive (§17) |
| Human-in-the-loop breakpoints | Full, engine-native | `interrupt_before=["synthesize"]` pauses the run at a checkpoint and surfaces it via the Runs API for human approval/edit before resuming (§10.3) |
| Streaming per-node | Full | Each node's output streams as a `StepBoundary` event (§5.5) |
| LangGraph's `Send` (dynamic fan-out) | Full | Maps to our fan-out primitive (§17.3): one node's output spawns N parallel node instances, results reduced back via the channel's reducer |

### 7.3 Why not just run upstream LangGraph as-is

The tempting alternative — embed the real `langgraph` package, run `CompiledGraph.ainvoke()` inside a worker — was rejected for three concrete reasons:

1. **Checkpointing granularity we don't control.** Upstream checkpointers persist whole-state snapshots at graph superstep boundaries to whatever backend you plug in; we need checkpoint writes to also carry tenant/cost metadata, participate in our budget-enforcement transaction, and be queryable by our observability pipeline without a translation layer.
2. **No native multi-tenant admission control.** Node execution in upstream LangGraph is just Python function calls — there's no hook to say "this node's LLM call must be rate-limited against tenant X's quota before it fires." We need that hook to be structural, not bolted on via monkey-patching.
3. **Version coupling.** Running teams' arbitrary graphs against whatever LangGraph version they pinned in their own `requirements.txt` inside our multi-tenant workers is a supply-chain and stability risk at 150-tenant scale. Compiling to our own IR means a team's graph is insulated from upstream breaking changes, and we upgrade our compatibility shim on our own schedule.

---

## 8. Agent Runtime: Execution Engine Internals

### 8.1 The per-run state machine

Every run — whether authored as a simple ReAct agent or a hand-built graph — executes as an instance of the same top-level state machine. Pattern-specific behavior (ReAct vs. plan-and-execute vs. custom graph) is expressed as **which node the state machine is currently inside**, not as a different state machine.

```
        ┌────────┐
        │  idle   │  (queued, waiting for a worker slot)
        └───┬────┘
            │ scheduled
            ▼
        ┌────────┐   node has an LLM call    ┌───────────┐
        │thinking │ ─────────────────────────▶│  (model    │
        └───┬────┘                            │  gateway   │
            │ model returns                    │  call)     │
            │ tool_calls[]                     └─────┬─────┘
            ▼                                          │ response
        ┌────────┐   dispatch N tool calls            │
        │ acting  │◀────────────────────────────────────┘
        └───┬────┘   (parallel, §8.3)
            │ all tool results in (or partial timeout)
            ▼
        ┌───────────┐
        │ observing  │  (merge tool results into state, apply reducers)
        └───┬───────┘
            │
            ▼
        ┌───────────┐   conditional edge / pattern logic
        │ deciding   │───────────────┬─────────────────┬───────────────┐
        └───┬───────┘                │                 │               │
            │ loop back              │ terminal          │ step/budget    │ unrecoverable
            │ to thinking            │ (final answer)    │ exceeded       │ error
            ▼                         ▼                   ▼               ▼
        (thinking)                ┌──────┐          ┌────────────┐  ┌───────┐
                                   │ done  │          │budget_exceeded│ error │
                                   └──────┘          └────────────┘  └───────┘
```

State transitions are driven by the Execution Engine's **event loop**, one instance per run, not per worker — many run-loops multiplex onto a shared worker process using cooperative async I/O (§9), since the loop spends nearly all wall-clock time awaiting a model or tool response, not computing.

| State | Entered when | Exit condition | What's recorded |
|---|---|---|---|
| `idle` | Run created, awaiting scheduling | Scheduler assigns a worker | `queued_at` |
| `thinking` | Engine is about to invoke the model for the current node | Model Gateway responds (or errors/times out) | `llm.call` span (§18.2), token/cost delta |
| `acting` | Model response contains ≥1 tool call | All dispatched tool calls resolve, error, or hit their timeout | one `tool.execute` span per call |
| `observing` | All tool results (or partial-timeout results) are in | State reducers applied, next-node routing function evaluated | state diff written to checkpoint if `checkpoint_every_step` |
| `deciding` | Routing function/pattern logic runs | Route back to `thinking` (loop), or to a terminal state | `graph.route` span |
| `done` | Terminal node reached, or pattern signals completion | — | final output, total cost, full trajectory persisted |
| `budget_exceeded` | Step count, wall-clock, or dollar cost ceiling crossed | — | distinct error code from `error`, so callers/evals can separate "agent gave a bad answer" from "we cut it off" |
| `error` | Unrecoverable exception (unhandled tool error with no hook override, model call exhausted all fallbacks) | — | error classification (§18.4) |
| `cancelled` | Explicit `CancelRun` while in any non-terminal state | — | last-completed checkpoint retained for inspection |

### 8.2 Node dispatch and the LLM call

`thinking` is where the platform's per-call bookkeeping is heaviest, because it's the state that fans out to the Model Gateway:

1. Resolve the node's bound prompt (Prompt Registry, cached locally, §15) and render it against current state via the templating engine.
2. Check the run's **budget ledger** (in-memory, backed by a per-run Redis counter) — if the projected cost of this call (estimated from prompt token count × model's rate) would exceed `max_cost_usd_per_run`, transition directly to `budget_exceeded` without making the call.
3. Issue the call through the Model Gateway client (§13), with the run's `trace_id`/`tenant_id`/`run_id` propagated as gateway-level metadata for cost attribution and gateway-side rate limiting — this is a second, independent budget check, since the Gateway enforces tenant-wide limits the single run doesn't know about.
4. Stream tokens back to the Run Manager's SSE fan-out as they arrive; simultaneously accumulate the full response for state-machine purposes (the engine needs the complete `tool_calls[]` before it can transition to `acting`).
5. On response: update the budget ledger with actual cost, emit the `llm.call` span with token counts, latency, and model/provider actually used (which may differ from `primary` if a fallback fired), and transition to `acting` or `deciding` depending on whether tool calls were returned.

### 8.3 Parallel tool call dispatch

When the model returns multiple tool calls in one turn, the engine partitions them into independent vs. dependent sets — v1 treats **all tool calls within a single model turn as independent** (this matches how models actually emit them: a turn's parallel tool calls are, by construction, calls the model believed could be issued concurrently) and dispatches all of them at once via `asyncio.gather`-equivalent fan-out to the Tool Gateway.

```
model turn: [get_account_balance(acct_1), search_kb("refund policy"), get_shipping_status(order_9)]
                    │                              │                              │
                    ▼                              ▼                              ▼
              Tool Gateway                   Tool Gateway                   Tool Gateway
              (sandboxed proc)               (in-process, native tool)      (sandboxed proc)
                    │  2.1s                        │ 0.3s                         │ 4.8s (times out at 5s config)
                    ▼                              ▼                              ▼
              result: {...}                  result: {...}                 error: ToolTimeout
```

Merge semantics into `observing`:

- Each tool call's result (or error) is attached to its own `tool_call_id` — the model sees a structured mapping back to what it asked for, never an ambiguous merged blob.
- A single tool timeout or error does **not** fail the whole turn by default — the engine collects whatever resolved within a bounded wait (max of all configured timeouts, capped platform-wide at 60s for the interactive pool) and passes partial results plus explicit error entries back to the model, letting the model's own reasoning decide whether to retry, use a fallback tool, or proceed without that data. A per-tool `on_tool_error` hook (§5.4) can override this per tool.
- If **all** tool calls in a turn error, the engine treats this as an `acting`-state failure: apply the same hook-or-propagate logic, and if unhandled, transition to `error` rather than looping the model against an entirely empty observation (this is the single most common cause of "runaway agent" loops in unguarded systems — a model that gets nothing back tends to just retry the same failing call).

### 8.4 Retry semantics

Retries exist at three independent layers, and the design goal is that they **compose without multiplying** (a naive stack-up of 3 retries × 3 retries × 3 retries turns one bad request into 27 downstream calls):

| Layer | What it retries | Policy | Interaction |
|---|---|---|---|
| Model Gateway (§13.5) | A single provider call | Exponential backoff, ≤3 attempts, only on retryable errors (429, 5xx, timeout) | Invisible to the Execution Engine unless all attempts + fallback providers are exhausted |
| Tool Gateway (§12.3) | A single tool invocation | Per-tool configured retries (default 1), only if the tool is marked idempotent or the call is read-only | Invisible to the Engine unless exhausted |
| Execution Engine | A whole `thinking`→`acting` step, on hook-directed `RETRY_WITH_FALLBACK` | Only on explicit hook instruction (§5.4) — the engine itself does not silently retry a full step, to avoid compounding | Counts against `max_steps`, so a retry loop still terminates |

No layer retries indefinitely; every retry budget is finite and every layer's exhaustion surfaces as a distinct, classified error (§18.4) rather than a generic failure, so the eventual `error` or `budget_exceeded` state carries enough information to know *which* layer gave up.

### 8.5 Cancellation

`CancelRun(run_id)` sets a `cancel_requested` flag on the run's control record (Redis, checked by the event loop between every state transition — never mid-tool-call, to avoid tearing a side-effecting call). Behavior depends on current state:

- In `thinking` (awaiting a model response): the Model Gateway call is cancelled at the transport level (best-effort — a request already fully sent to a provider may still complete server-side and be billed; we cannot undo that, only stop waiting on it and not use the result).
- In `acting` (tool calls in flight): each dispatched tool call receives a cancellation signal; sandboxed tool executors are killed at the process level with a grace period, then hard-killed; non-idempotent tools that already committed a side effect **cannot be un-committed** — this is surfaced explicitly in the cancellation response (`partial_side_effects: [...]`) rather than pretending the cancel was clean.
- The run transitions to `cancelled`, not `error` — a distinct terminal state so eval/observability pipelines don't count a user-initiated cancel as an agent failure.

---

## 9. Scheduling and the Worker Pool

### 9.1 Two pools, not one

Interactive (chat, latency-sensitive) and batch (research, document processing, hours-long) workloads have opposite scheduling objectives — one wants low queueing delay per run, the other wants high aggregate throughput and doesn't care if an individual run waits 30 seconds to start. Sharing one pool means tuning for neither. We run **two independently scaled worker pools** behind the same Scheduler API, selected by the agent's declared `execution.pool` (§6):

| | Interactive pool | Batch pool |
|---|---|---|
| Scheduling objective | Minimize queueing delay | Maximize throughput, fairness over time |
| Autoscaling signal | In-flight run count vs. target concurrency per pod | Queue depth |
| Pod count (steady) | ~4,000 | ~1,500 |
| Typical run lifetime | Seconds | Minutes to 24h |
| Checkpoint frequency | Only on request (`checkpoint_every_step`) | Always on |
| Preemption | Never — an interactive run that started keeps its slot | Long batch runs **can** be preempted (checkpoint + requeue) to make room, since resuming is cheap by construction |

### 9.2 Worker internals

Each worker pod runs an async event-loop process hosting many concurrent run-loops (§8.1), bounded by a **target concurrency** derived from measured I/O-wait ratio — since a run-loop spends >90% of wall-clock awaiting a model or tool response, one pod can host on the order of 100–300 concurrent interactive runs before CPU (JSON parsing, templating, tracing overhead) becomes the bottleneck rather than I/O. This ratio is re-measured continuously and used to drive the autoscaler's target rather than hardcoded.

Tenant code (SDK hooks, custom tool functions not in the trusted tier) does **not** run in the same OS process as the Engine's control logic — it runs in a per-run sandboxed subprocess/microVM (§2.2, §12.5) that the Engine communicates with over a local IPC channel. This means one tenant's misbehaving hook (infinite loop, memory bomb) can crash only its own sandbox, not the worker process hosting hundreds of other tenants' concurrent runs.

### 9.3 Admission control and fairness

At 10,000 runs/sec peak, the scheduler must prevent one tenant from starving the other 149. We use **weighted fair queueing** at the scheduler, not simple FIFO:

- Each tenant has a configured weight (default equal, adjustable for platform-team-negotiated SLAs).
- The scheduler maintains a per-tenant virtual-time counter (deficit round robin); a tenant that has been under-served recently is prioritized for the next available slot.
- Hard per-tenant concurrency ceilings (independent of fairness weight) prevent a single tenant from ever claiming more than a configured fraction (default 15%) of total pool capacity even if every other tenant is idle — bounding blast radius from a traffic spike or bug, not just ensuring average fairness.
- When the interactive pool is saturated, new runs queue with a bounded wait (default 2s) before returning `503 pool_saturated` to the caller rather than queueing indefinitely — callers are expected to handle backpressure, not the platform silently building an unbounded queue that turns into a latency cliff.

### 9.4 Scheduling numbers

| Metric | Target |
|---|---|
| Scheduling decision latency (queue → assigned worker) | P99 ≤ 30 ms, interactive pool |
| Interactive pool queueing delay under normal load | P99 ≤ 100 ms |
| Interactive pool queueing delay under 2× traffic spike | P99 ≤ 1.5 s, degrading gracefully via autoscale + admission control rather than cliff-failing |
| Batch pool queue depth alarm threshold | > 50,000 queued runs sustained for 5 min triggers autoscale + platform-team page |
| Preemption checkpoint overhead (batch) | ≤ 200 ms per preempted run |

---

## 10. Checkpointing, Resumption, and Human-in-the-Loop

### 10.1 What a checkpoint contains

A checkpoint is a versioned snapshot of everything needed to resume a run on a *different* worker with no loss of correctness:

```sql
CREATE TABLE checkpoints (
    run_id          UUID NOT NULL,
    checkpoint_seq  BIGINT NOT NULL,       -- monotonic per run
    tenant_id       UUID NOT NULL,
    graph_state     JSONB NOT NULL,        -- the AgentGraph's typed state at this point
    current_node    TEXT NOT NULL,
    pending_tool_calls JSONB,              -- in-flight calls at snapshot time, for exactly-resumable semantics
    budget_ledger   JSONB NOT NULL,        -- steps used, cost spent so far
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, checkpoint_seq)
) PARTITION BY HASH (tenant_id);
```

Stored in tenant-partitioned Postgres for the structured fields (fast point lookups by `run_id`, cheap tenant isolation via partition), with large `graph_state` blobs (e.g., accumulated research findings, long conversation buffers) offloaded to object storage and referenced by pointer once they exceed a size threshold (~256 KB) — keeping the hot Postgres rows small and the checkpoint-write path fast even when the state a research agent has accumulated is large.

### 10.2 Write policy

- **Interactive agents** (default `checkpoint_every_step=False`): checkpoint only at `done`/`error`/`cancelled`, or when explicitly configured. Rationale: a 4-second ReAct run that crashes mid-flight is cheap to just restart; paying a durable write per step for something this short is pure overhead relative to the < 5% platform-cost-overhead budget (§3).
- **Batch/long-running agents** (`checkpoint_every_step=True`, or engine-forced for any run projected to exceed 5 minutes): checkpoint after every `observing` state, before evaluating the next routing decision. Resuming after a crash costs at most one in-flight step, matching the stated durability NFR.
- Checkpoint writes are **async relative to the model/tool call they follow** — the engine doesn't block the next node's start on checkpoint durability for interactive runs, only for batch runs where preemption/resumption correctness depends on it.

### 10.3 Human-in-the-loop breakpoints

`interrupt_before=["synthesize"]` (§7.2) is implemented as: the engine treats the named node as requiring an **approval gate**. On reaching it, the run transitions to a new state, `awaiting_approval` (a sub-state of `deciding`), writes a checkpoint, and surfaces the pending state via the Runs API:

```
GET /v1/runs/{run_id}
{
  "status": "awaiting_approval",
  "pending_node": "synthesize",
  "state_preview": { "findings": [...], "plan": [...] },
  "resume_token": "chk_9f2b..."
}

POST /v1/runs/{run_id}/resume
{ "resume_token": "chk_9f2b...", "state_patch": { "findings": [...edited...] } }
```

A human reviewer (or another system) can inspect the exact graph state, optionally **edit it** via `state_patch` (validated against the graph's typed state schema before it's accepted — a human editing `findings` to something that violates the schema is rejected, same as a node's own output would be), and resume. This is the mechanism underlying both compliance-required approval workflows (e.g., "don't send this email without sign-off") and debugging ("pause here, let me see what the agent is thinking").

A run can sit in `awaiting_approval` indefinitely — it consumes checkpoint storage but no compute or worker slot, which is why this is cheap at the scale of "50,000 concurrent long-running runs" (§3): most of those are not actively burning worker time, they're checkpointed and idle.

### 10.4 Resumption correctness

The one subtlety: what happens to `pending_tool_calls` that were in flight when a checkpoint was taken (worker crash mid-`acting`), given the at-least-once tool contract (§8.4, §12.3)? On resume, the engine re-issues any tool call whose result was not recorded in the checkpoint, using the **same idempotency key** it originally generated for that call (a UUID derived from `run_id + checkpoint_seq + tool_call_id`, passed to the Tool Gateway). Idempotent tools (marked as such in the registry) safely no-op or return the cached prior result; non-idempotent tools without a dedupe-capable backend are documented as **at-risk-of-duplicate-effect on crash-resume**, and teams registering such tools must acknowledge this at registration time — the platform will not silently pretend it can make an arbitrary side-effecting call exactly-once.

---

## 11. Streaming and Cancellation

### 11.1 Two streams, multiplexed

A running agent produces two distinct streams that clients often want independently:

1. **Token stream** — raw model output tokens, for rendering a typing-effect UI.
2. **Trajectory stream** — structural events: step boundaries, tool calls starting/finishing, state transitions, sub-agent delegation.

Both are multiplexed over one SSE/WebSocket connection as typed events (§5.5's `TokenEvent` / `ToolCallStarted` / etc.), rather than two separate connections — this halves connection overhead and guarantees ordering between "a token was emitted" and "a tool call started," which matters for UIs that need to show, e.g., "thinking… [tool call] … here's the answer" in the correct interleaving.

### 11.2 Streaming architecture

```
Execution Engine (worker)  ──publish──▶  Run Event Bus (Redis Streams, per-run channel)
                                                  │
                                    ┌─────────────┼─────────────┐
                                    ▼             ▼              ▼
                              SSE Gateway    SSE Gateway    (Observability
                              (client A)     (client B,     Pipeline consumer)
                                             reconnect)
```

- The Engine publishes every event to a per-run Redis Streams channel, not directly to the client socket — this decouples "the engine produced an event" from "a client is currently connected to receive it," which is what makes **reconnect-and-resume** possible: a client that drops and reconnects passes the last-seen event ID and replays from the stream rather than losing everything between disconnect and reconnect.
- The channel has a bounded retention (default: life of the run + 5 minutes) — long enough for reconnect, not a permanent log (that's what the trace store is for, §18).
- Any number of SSE Gateway instances can subscribe to the same run's channel — this is what lets a supervisor UI *and* an end-user client both watch the same run concurrently without the Engine needing to know about either.

### 11.3 Cancellation over the wire

`POST /v1/runs/{run_id}/cancel` writes the `cancel_requested` flag (§8.5) directly to the run's control record — not through the event bus, since cancellation must not be delayable by event-bus backpressure. The engine's event loop checks this flag at every state-transition boundary (bounded latency: at most one `thinking`/`acting` step's duration, capped by the platform-wide per-step timeout, so cancellation is acknowledged within seconds even for a slow-responding tool). A `RunCancelled` event is published to the same run channel so any connected client sees the terminal state consistently with the token/trajectory stream it was already watching.

---

## 12. Tool Platform

### 12.1 Tool interface

Every tool, regardless of author or trust tier, is described by a single normalized schema — internally derived from the SDK decorator (§5.1) or hand-written for tools registered outside the SDK (e.g., a wrapped OpenAPI spec):

```json
{
  "name": "get_account_balance",
  "version": "3",
  "owner_team": "billing-platform",
  "description": "Look up the current balance for a customer account.",
  "input_schema": {
    "type": "object",
    "properties": { "account_id": { "type": "string", "pattern": "^acct_[A-Za-z0-9]+$" } },
    "required": ["account_id"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "balance_cents": { "type": "integer" },
      "currency": { "type": "string" },
      "as_of": { "type": "string", "format": "date-time" }
    }
  },
  "requires_credential": "billing_api",
  "trust_tier": "third_party_sandboxed",
  "idempotent": true,
  "timeout_s": 5,
  "retries": 2,
  "rate_limit": { "per_tenant_qps": 50 }
}
```

This is deliberately **OpenAPI/JSON-Schema-native** rather than a bespoke format — it's what lets us auto-generate a tool wrapper from an existing internal REST API's OpenAPI spec (a common onboarding path: "here's our billing service's OpenAPI doc, register the three endpoints agents are allowed to call" takes minutes, not a rewrite), and it's what the model providers themselves expect for function-calling, so there's no format translation on the hot path into the Model Gateway request.

### 12.2 Registry and discovery

The Tool Registry is control-plane state (Postgres, strongly consistent on write, cached at workers as described in §4.1). Discovery has two modes:

1. **Explicit attachment** — an agent's definition lists exact tool refs (the common case, §5–6). No ambiguity about what's available at run time.
2. **Capability search** — for agents that need to select from a large tool surface dynamically (e.g., a general-purpose assistant with access to 200 internal tools), the registry exposes a `search_tools(query, tenant_id)` semantic-search endpoint (embeddings over tool name+description) that returns the top-K relevant tools, RBAC-filtered to what the *calling agent's identity* is permitted to use. This list is then injected into that turn's model call as the available function set — the model never gets tools statically bound that the calling identity isn't authorized for, closing an easy privilege-escalation path.

### 12.3 Dispatch: validation, credentials, timeout, retry

```
Model returns tool_call(name="get_account_balance", args={"account_id": "acct_9F2B"})
        │
        ▼
1. Schema-validate args against input_schema  ──fail──▶ synthetic ToolError returned to model
        │ pass                                            (model can retry with corrected args —
        ▼                                                  cheaper than failing the whole run)
2. RBAC check: can this agent/tenant invoke this tool?  ──fail──▶ ToolError(permission_denied), audit-logged
        │ pass
        ▼
3. Resolve trust tier → route to in-process / isolated-process / sandbox executor (§12.5)
        │
        ▼
4. Inject credential: Credential Vault issues a short-lived (≤15 min), scoped token
   for `requires_credential`, bound to (run_id, tenant_id) — never the raw long-lived secret
        │
        ▼
5. Execute with configured timeout; on timeout/error, apply retry policy IF idempotent
        │
        ▼
6. Schema-validate result against output_schema  ──fail──▶ ToolError(invalid_output), tool owner paged if recurring
        │ pass
        ▼
7. Return structured result to Execution Engine (§8.3), tagged with tool_call_id
```

Step 4 is the credential-injection guarantee stated in the requirements: **the agent's code, the model, and the trace all see a scoped, short-lived token or nothing at all — never the underlying long-lived secret.** The Credential Vault (HashiCorp Vault or equivalent, §20.5) issues tokens scoped to exactly the permissions declared in `requires_credential`, with an audit record tying the issuance to `(run_id, tool_call_id, tenant_id)`.

### 12.4 Isolation model, restated for tools

Building on §2.2's trust tiers:

| Tier | Isolation mechanism | Overhead (P50 added latency) | Network |
|---|---|---|---|
| Platform-native | None — trusted code in the runtime worker process | ~0 ms | Full, whatever the platform code needs |
| First-party reviewed | Linux namespace + cgroup limits, same host, separate process | ~5–15 ms (process boundary + IPC) | Allowlisted egress only |
| Third-party / code-execution | gVisor (syscall-filtered) or Firecracker microVM, fresh instance per invocation from a warm pool | ~50–150 ms (warm pool) / ~400–800 ms (cold start) | Denied by default; explicit per-tool egress allowlist |

A **warm pool** of pre-initialized sandboxes (per language runtime: Python, Node) amortizes the cold-start cost — sandboxes are recycled between invocations from *different* tenants only after a full teardown/rebuild (never state-reused across tenant boundaries, to eliminate any risk of memory/filesystem residue leaking between tenants), while same-tenant repeat invocations can reuse a still-warm instance within a short TTL.

### 12.5 Result caching

Idempotent, read-only tools (marked `idempotent: true` and no side effects) are eligible for **result caching** keyed on `(tool_name, tool_version, args_hash, tenant_id)`, TTL configurable per tool (default 60s — long enough to absorb a burst of near-duplicate calls within one agent's exploratory loop, short enough that stale data isn't a correctness concern for most lookups). Cache hit rate is tracked per tool; tools with high hit rates and expensive backends (e.g., a slow internal search API) are flagged to owners as good caching candidates if not already opted in — this directly reduces both tail latency and downstream-system load at the stated 10,500 tool-calls/sec sustained rate.

### 12.6 Numbers

| Metric | Target |
|---|---|
| Schema validation (steps 1, 6) | P99 ≤ 2 ms |
| Credential issuance (step 4) | P99 ≤ 15 ms |
| Platform-added dispatch overhead (excludes tool's own execution time) | P99 ≤ 50 ms — matches stated NFR |
| Sandbox cold start | P99 ≤ 800 ms, mitigated by warm pools to keep effective P99 ≤ 150 ms |
| Cache hit rate (cacheable tools, steady state) | 30–50% typical, tool-dependent |

---

## 13. Model Gateway

### 13.1 Provider abstraction

The Gateway presents one request/response shape to every caller; provider-specific adapters translate to/from Anthropic's Messages API, OpenAI's Chat Completions/Responses API, and our internal vLLM fleet's OpenAI-compatible endpoint.

```
POST /v1/gateway/completions
{
  "model": "auto",                 // or a pinned model id, or a named alias e.g. "fast-cheap"
  "messages": [...],
  "tools": [...],                  // normalized JSON-Schema tool defs (§12.1)
  "routing": {
    "task_type": "reasoning",
    "max_cost_usd": 0.05,
    "max_latency_ms": 3000,
    "data_residency": "eu"
  },
  "tenant_id": "team_cx-platform",
  "run_id": "run_9f2b...",
  "stream": true
}
```

Callers (the Execution Engine, §8.2) never construct a provider-specific request. This is what makes "adding a new model provider must not require agent code changes" (stated NFR) true by construction — a new provider is a new adapter behind this same interface, plus an entry in the capability matrix below.

### 13.2 Capability matrix

The Gateway maintains a live-updated matrix used for both routing and validation (e.g., reject a request that needs vision input against a text-only model before it wastes a round trip):

| Model | Provider | Context window | Vision | Tool calling | Streaming | $/1M input tok | $/1M output tok | Typical TTFT | Region availability |
|---|---|---|---|---|---|---|---|---|---|
| claude-opus-4-1 | Anthropic | 200K | Yes | Yes | Yes | $15.00 | $75.00 | ~600 ms | US, EU |
| claude-sonnet-4-5 | Anthropic | 200K | Yes | Yes | Yes | $3.00 | $15.00 | ~350 ms | US, EU |
| claude-haiku-4-5 | Anthropic | 200K | Yes | Yes | Yes | $1.00 | $5.00 | ~200 ms | US, EU |
| gpt-4.1 | OpenAI | 128K | Yes | Yes | Yes | $2.00 | $8.00 | ~400 ms | US |
| internal-llama-ft-70b | Self-hosted vLLM | 32K | No | Yes (via prompt-templated JSON) | Yes | GPU-amortized (~$0.40 equiv) | ~$0.40 equiv | ~250 ms | US, EU (region-pinned GPU pools) |

*(Illustrative figures — the matrix is refreshed from provider pricing/capability pages and internal benchmarking, not hardcoded; it drives both routing and cost estimation for the pre-flight budget check in §8.2.)*

### 13.3 Routing

Three routing modes, selected by the `model` field:

1. **Pinned** (`model: "claude-sonnet-4-5"`) — no routing logic, exact model used unless it's fully unavailable (then §13.6 fallback applies).
2. **Aliased** (`model: "fast-cheap"` / `"best-quality"`) — platform-curated aliases mapped to a model at Gateway config level, so a team can express intent ("I want the cheap fast tier") without hardcoding a model id that becomes stale as models are deprecated.
3. **Auto** (`model: "auto"`, the `routing` block drives selection) — the Gateway scores eligible models (those meeting `data_residency`, capability requirements) against `max_cost_usd`, `max_latency_ms`, and `task_type` (a coarse hint: `reasoning`, `extraction`, `chat`, `code`) using a weighted scoring function, and picks the highest-scoring model with current healthy status.

Most production agents use **pinned or aliased** routing — `auto` is primarily a convenience for prototyping and for cost-optimization experiments (§25.3), because pinned routing is what makes evaluation results (§19) attributable to a specific, reproducible model choice. An agent version's eval results for "auto-routed" requests would be comparing apples to a moving target.

### 13.4 Streaming proxy

The Gateway proxies provider streaming responses (SSE) through to the Execution Engine with normalized chunk framing — token deltas, tool-call-argument deltas (some providers stream partial JSON for tool arguments; the Gateway buffers and validates these into complete, schema-checked tool calls before handing them to the Engine, since a partially-streamed invalid JSON tool call must never reach the Engine's `acting` state). Token counting happens **incrementally as chunks arrive** (via the provider's tokenizer or a fast approximate counter, reconciled against the provider's final usage report) so the run's budget ledger (§8.2) can be updated without waiting for the full response — critical for stopping a runaway generation mid-stream if it's about to blow a cost ceiling.

### 13.5 Rate limiting

Two independent layers:

| Layer | Scope | Purpose |
|---|---|---|
| Tenant quota | Per team, configured QPS/TPM (tokens/minute) ceiling | Prevents one team from exhausting shared provider capacity — the stated "independent of provider limits" requirement |
| Provider quota shadow | Per provider account, tracked by the Gateway against the provider's actual contracted limits | Prevents the platform in aggregate from tripping a provider-side 429 storm; the Gateway throttles proactively at ~90% of known provider limits rather than reactively after errors |

Both are enforced with a token-bucket algorithm at the Gateway's edge, distributed via a shared Redis-backed limiter (so limits are consistent across the Gateway's horizontally scaled instances, not per-instance-approximate). A tenant hitting its own quota gets a fast, clear `429 tenant_rate_limited` — distinguishable in the Engine's error classification (§18.4) from a provider-side `429`, since the remediation is different (tenant should back off / request a quota increase, vs. platform should investigate provider capacity).

### 13.6 Fallback and circuit breakers

```
Primary: claude-sonnet-4-5 (Anthropic)
   │
   │  request fails: timeout, 5xx, or circuit open
   ▼
Fallback 1: gpt-4.1 (OpenAI)   ── only if agent declared this as an acceptable fallback (§5.2 ModelConfig.fallback) ──
   │
   │  also fails
   ▼
Fallback 2: internal-llama-ft-70b (self-hosted)  ── last resort, if declared ──
   │
   │  also fails
   ▼
Return classified error to Execution Engine → agent's on_tool_error-equivalent model-error hook, or `error` state
```

- Fallback only occurs across models an agent **explicitly opted into** (`ModelConfig.fallback=[...]`) — silently substituting a different model changes output characteristics (style, tool-calling reliability, cost) in ways that must be a deliberate choice, not a hidden Gateway behavior, since it directly affects eval validity (§19) and user-facing quality.
- **Per-provider circuit breakers**: the Gateway tracks rolling error rate per provider; above a threshold (default 25% over a 30s window with ≥50 samples) the breaker opens and the Gateway stops sending *new* traffic to that provider for a cooldown period (default 30s, exponential backoff on repeated trips), routing everything eligible straight to fallback rather than paying the timeout cost of a provider that's currently down for every single request.
- Breaker state is shared across Gateway instances (same Redis-backed coordination as rate limiting) so the whole fleet reacts together, not instance-by-instance with staggered detection.

### 13.7 Cost tracking

Every completed (or failed-but-billed) call writes a row to the Cost Ledger **before** returning to the caller — this is the one place in the whole pipeline where we accept a small synchronous write on the hot path, because cost attribution is a stated correctness property (§1), not just an observability nicety, and "attributable within 60 seconds" (NFR) needs a durable write, not a best-effort async one that could be lost.

```sql
CREATE TABLE cost_ledger (
    id              BIGSERIAL,
    tenant_id       UUID NOT NULL,
    agent_id        UUID NOT NULL,
    agent_version   TEXT NOT NULL,
    run_id          UUID NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        NUMERIC(12,6) NOT NULL,
    fallback_used   BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);   -- daily partitions, rolled up hourly into per-tenant aggregates
```

This synchronous write is kept cheap (single-row insert, no joins, append-only, daily-partitioned) so it doesn't threaten the Gateway's own P99 ≤ 20ms routing-decision NFR — the write happens in parallel with returning the response to the caller, not serialized before it, and is itself budgeted at ≤5ms P99 against a local Postgres instance with synchronous replication to one standby (durability without waiting on cross-region replication for every token).

---

## 14. RAG Pipeline

### 14.1 Knowledge base lifecycle

```sql
CREATE TABLE knowledge_bases (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    name            TEXT NOT NULL,
    embedding_model TEXT NOT NULL,          -- pinned per KB; changing it requires full re-embed (§14.6)
    chunking_config JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE kb_sources (
    id              UUID PRIMARY KEY,
    kb_id           UUID REFERENCES knowledge_bases(id),
    source_type     TEXT NOT NULL,          -- 'confluence' | 's3' | 'gdrive' | 'api' | 'upload'
    connector_config JSONB NOT NULL,
    sync_schedule   TEXT,                   -- cron expr, null = one-shot
    last_synced_at  TIMESTAMPTZ,
    status          TEXT NOT NULL           -- 'active' | 'syncing' | 'error' | 'paused'
);

CREATE TABLE kb_documents (
    id              UUID PRIMARY KEY,
    kb_id           UUID REFERENCES knowledge_bases(id),
    source_id       UUID REFERENCES kb_sources(id),
    external_id     TEXT NOT NULL,          -- source system's native ID, for dedupe/update detection
    content_hash    TEXT NOT NULL,          -- change detection without re-fetching content
    acl             JSONB NOT NULL,         -- permission-aware retrieval, §14.5
    ingestion_status TEXT NOT NULL,         -- 'pending' | 'chunked' | 'embedded' | 'indexed' | 'failed' | 'stale'
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (kb_id, source_id, external_id)
);
```

Teams own KB lifecycle independently of any agent — a KB can exist, be populated, and be queried directly (e.g., for a search UI) with zero agents attached, and multiple agents (even across teams, with an explicit sharing grant) can attach to the same KB.

### 14.2 Ingestion pipeline

```
Source (Confluence, S3, GDrive, upload API)
      │  connector poll/webhook
      ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Fetch      │────▶│   Parse      │────▶│   Chunk      │────▶│   Embed      │────▶│   Index      │
│ (dedupe via  │     │ (PDF/HTML/   │     │ (strategy    │     │ (batched     │     │ (upsert to   │
│ content_hash)│     │ MD/DOCX →    │     │ per §14.3)   │     │ calls to     │     │ vector store │
│              │     │ clean text)  │     │              │     │ embed model) │     │ + sparse idx)│
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                                          │
                                                                                          ▼
                                                                              kb_documents.ingestion_status
                                                                              = 'indexed', visible to owners
```

Implemented as a durable pipeline (queue-backed stages, e.g., a workflow engine or Kafka-connected workers per stage) so a failure at `embed` doesn't require re-`fetch`/re-`parse` — each stage's output is persisted before advancing, and per-document status is queryable (`ingestion_status`) so KB owners can see "47 documents failed to parse, here's why" rather than a black box.

Continuous sync (`sync_schedule`) re-fetches on a cron, compares `content_hash`, and only re-chunks/re-embeds documents that actually changed — at 50M-document KB scale, re-processing everything on every sync would make the ~2,000 embedding-calls/sec ingestion budget (§3) impossible to hold.

### 14.3 Chunking strategies

| Strategy | How | Best for | Trade-off |
|---|---|---|---|
| Fixed-size with overlap | N tokens (default 512) per chunk, ~15% overlap | Uniform prose (wikis, docs) | Simple, fast; can split a sentence/table mid-way |
| Semantic/recursive | Split on structural boundaries (headers, paragraphs) first, fall back to fixed-size within an oversized section | Structured docs (specs, runbooks) | Better retrieval precision; slower to compute, needs a parser per format |
| Table-aware | Tables extracted and chunked as self-contained units with header context repeated per chunk | Data-heavy docs (pricing sheets, spec tables) | Prevents a table row losing its column headers when isolated |
| Sliding-window with parent-doc reference | Small chunks for retrieval precision, but each chunk stores a pointer to a larger parent section returned on demand | Long technical docs where a small chunk lacks context | Adds a second store/lookup; best precision-vs-context trade-off |

Default is **semantic/recursive** with a 512-token target and 15% overlap, table-aware extraction layered on top when tables are detected — chosen because pure fixed-size chunking measurably hurt retrieval precision in eval (§19) on our largest internal doc sets (runbooks, API references) where losing a heading's context inside a chunk directly caused wrong or incomplete answers.

### 14.4 Embedding model selection

One embedding model per KB (`embedding_model`, pinned) — mixing embedding models within one vector index is a correctness bug (distances aren't comparable across models), so this is enforced, not just recommended. Model choice is a trade-off table itself:

| Model class | Dimension | Cost/1M tokens | Retrieval quality (internal eval) | Notes |
|---|---|---|---|---|
| General-purpose large (e.g., `text-embed-3-large`-class) | 3072 | ~$0.13 | Highest | Default for new KBs unless cost/latency pushes otherwise |
| General-purpose small | 1536 | ~$0.02 | Good, ~3–5% recall drop vs. large in our eval | Default for high-volume/low-margin KBs |
| Domain fine-tuned (internal, code/legal-specific) | 1024 | Self-hosted, GPU-amortized | Best-in-class *for its domain*, worse generally | Used only for KBs eval confirms it beats general-purpose on |

### 14.5 Retrieval: hybrid search

Pure dense (embedding cosine similarity) retrieval misses exact-match cases models are good at otherwise (an error code, a product SKU, a person's name) because embedding similarity blurs exact tokens. Pure sparse (BM25) misses paraphrase/semantic matches. We run both and fuse:

```
Query
  │
  ├──▶ Dense: embed query → ANN search (HNSW index) → top-50 by cosine similarity
  │
  └──▶ Sparse: BM25 (tokenized, stemmed) → top-50 by term-weighted score
                    │
                    ▼
         Reciprocal Rank Fusion (RRF): score(doc) = Σ 1/(k + rank_i(doc))  across both lists, k=60
                    │
                    ▼
              Fused top-30 candidates
                    │
                    ▼
       Permission filter (§14.6) — drop anything caller isn't authorized to see
                    │
                    ▼
         Cross-encoder reranker (§14.7) → final top-K (default 5–8) returned
```

RRF is chosen over a learned fusion model for v1 because it requires no training data and is robust to the two retrievers' scores being on incomparable scales — a pragmatic default that a learned fusion model (§27, evolution path) can later replace once enough query/relevance-judgment data exists from production usage and eval.

### 14.6 Permission-aware retrieval

The `acl` field on `kb_documents` (§14.1) is enforced as a **hard filter applied to retrieval candidates before reranking**, not as a post-hoc filter on final results — this matters for two reasons: (1) it prevents a caller from inferring the *existence* of a document they can't read via subtle ranking effects (a document that's filtered after appearing in top results can still leak information through timing/count side channels — filtering pre-rerank against the full candidate pool, then re-topping-up from the next candidates if too many were dropped, avoids this), and (2) it keeps the reranker's expensive cross-encoder pass from being wasted on documents that will be discarded anyway.

Permission evaluation itself calls out to the platform RBAC service (§20.4) with the caller's identity (propagated from the agent run's `tenant_id` + optionally an end-user identity the agent is acting on behalf of) and the document's `acl` — cached aggressively (per caller-identity + KB, short TTL) since this check sits directly in the retrieval hot path budgeted at P99 ≤ 300ms total.

### 14.7 Reranking

A cross-encoder reranker (jointly encodes query+candidate rather than comparing precomputed embeddings) re-scores the ~20–30 permission-filtered candidates for final relevance — cross-encoders are far more accurate than bi-encoder cosine similarity but too expensive to run over a whole corpus, hence the two-stage retrieve-then-rerank design. Reranking is the single biggest lever on retrieval quality in our internal eval (§19.1): moving from dense-only-top-8 to hybrid-plus-rerank-top-8 improved answer-groundedness scores by double digits in the eval harness on our hardest internal doc sets, at a cost of roughly 60–120ms added latency — well within the 300ms retrieval budget.

### 14.8 Freshness scoring

For KBs where recency matters (release notes, incident postmortems, pricing) a freshness signal is blended into the final ranking: `final_score = rerank_score * decay(now - doc.updated_at, half_life=source_configured)`, half-life configurable per source (e.g., 30 days for release notes, effectively infinite/disabled for reference documentation that doesn't go stale). This is opt-in per source, not global, because for most KBs (API reference docs, policy documents) age is irrelevant or even inversely correlated with reliability (an old, stable doc is often more trustworthy than a draft).

### 14.9 Vector store choice

We run **pgvector on the same tenant-partitioned Postgres fleet already used for control-plane and checkpoint data** for KBs under ~5M chunks (the large majority — this avoids operating a second stateful system for most tenants), and a dedicated **Qdrant cluster** for KBs that exceed that threshold or need higher QPS than a shared Postgres instance can sustain (the handful of KBs approaching the 50M-document ceiling). The threshold is a capacity decision, not a religious one: pgvector's HNSW implementation is good enough and operationally simpler up to the point where a single large tenant's query load would otherwise compete with everyone else's control-plane traffic on the same database — past that point, dedicating an isolated, purpose-built vector store is worth the extra operational surface.

---

## 15. Prompt Management

### 15.1 Why prompts get their own registry, not just git

Prompts change at a completely different cadence and by a completely different population than code — a support-ops lead tuning wording ten times in an afternoon is a normal workflow, not an anomaly. Git-as-the-only-source-of-truth would force every wording tweak through a PR/CI/deploy cycle sized for code, which teams will route around (the same failure mode that makes marketing teams paste copy into a CMS instead of a repo). The Prompt Registry gives prompts **CMS-like edit velocity with code-like versioning discipline** — both properties, not a trade-off between them.

### 15.2 Schema

```sql
CREATE TABLE prompts (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    name            TEXT NOT NULL,          -- e.g. "support-triage/system"
    UNIQUE (tenant_id, name)
);

CREATE TABLE prompt_versions (
    id              UUID PRIMARY KEY,
    prompt_id       UUID REFERENCES prompts(id),
    version         INTEGER NOT NULL,       -- monotonic, immutable once created
    template        TEXT NOT NULL,          -- Jinja2 source
    template_vars   JSONB NOT NULL,         -- typed schema for required variables
    few_shot_examples JSONB,                -- §15.5
    author          TEXT NOT NULL,
    commit_message  TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (prompt_id, version)
);

CREATE TABLE prompt_deployments (
    prompt_id       UUID REFERENCES prompts(id),
    environment     TEXT NOT NULL,          -- 'prod' | 'staging' | agent-specific label
    version         INTEGER NOT NULL,
    traffic_split   JSONB,                  -- §15.4, null = 100% to `version`
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prompt_id, environment)
);
```

`prompt_versions` rows are **immutable once written** — an edit always creates a new version, never mutates an existing one, for the same reason git commits aren't mutated: diffability, revertibility, and — critically for this platform — **reproducibility of past eval results**, since an `EvalResult` (§21) references an exact `prompt_version`, and that reference must never silently point at different content later.

### 15.3 Templating

```jinja2
You are a support triage assistant for {{ company_name }}.

Customer context:
{% for fact in customer_facts %}
- {{ fact.key }}: {{ fact.value }}
{% endfor %}

{% if customer_facts | selectattr("key", "equalto", "tier") | map(attribute="value") | first == "enterprise" %}
This is an enterprise customer. Prioritize escalation over deflection.
{% endif %}

Respond following the tone guidelines in the linked style guide.
```

`template_vars` declares the typed contract (`customer_facts: list[Fact]`, `company_name: str`) — rendering fails loudly at the start of `thinking` (§8.2) if the Execution Engine's state doesn't supply a required variable, rather than silently rendering `{{ customer_facts }}` as the literal string into the prompt sent to the model, which is the single most common and hardest-to-notice prompt bug in ungoverned systems.

### 15.4 A/B testing

`prompt_deployments.traffic_split` drives routing at the start of `thinking`:

```json
{ "version_a": { "version": 12, "weight": 0.9 }, "version_b": { "version": 13, "weight": 0.1 } }
```

- Assignment is **sticky per conversation/session** (hashed on `session_id`, not re-randomized per turn) — a multi-turn conversation must not flip prompt versions mid-conversation, which would confound both the user experience and the eval data.
- Every run's trace and `EvalResult` records which `prompt_version` served it, so the Evaluation Service (§19) can compute per-arm metrics directly from production data without a separate experiment-tracking system.
- Rollout is a config change to `traffic_split` (`10% → 50% → 100%`), not a redeploy — the same mechanism that runs an A/B test also runs a canary rollout; they're the same primitive with different framing.
- Regression detection (§19.4) can **auto-halt** a rollout — if version B's tracked eval metrics drop below a statistically significant threshold relative to version A while both are live, `traffic_split` is automatically reset to 100% A and the prompt owner is paged, closing the loop between evaluation and deployment rather than leaving it as a manual dashboard-watching exercise.

### 15.5 Few-shot example management

Few-shot examples are stored as structured, versioned data (`few_shot_examples` — a list of `{input, output, rationale}` triples), not baked into the template string, for two reasons: they can be **curated independently** of prompt wording changes (an ops team adding a new example doesn't require touching the instruction text), and they can be **selected dynamically** — a `k_shot_selector` config (`static` | `similarity_to_input`, k=3 default) lets a prompt pull the K most relevant stored examples for the current input via embedding similarity rather than always injecting the same fixed set, which measurably helps on prompts covering a wide input distribution (e.g., a triage agent handling wildly different ticket types) without bloating every call with examples irrelevant to that particular input.

### 15.6 Prompt optimization loop

The registry is also the substrate for **systematic prompt improvement**, not just manual editing: given an `EvalDataset` (§19.1) and a scoring function, an optimization job (e.g., an automated few-shot-selection search, or an LLM-assisted prompt-rewriting loop scored against the same dataset) can propose a new `prompt_version` as a candidate, which then goes through the same offline-eval-gate → A/B-rollout path as a human-authored edit — the platform does not distinguish "a human wrote this prompt version" from "an optimization job proposed this prompt version" in terms of the deployment safety rails applied to it (§27, v3 evolution stage).

---

## 16. Memory Architecture

Four distinct memory types, each with different consistency, retention, and access-pattern needs — collapsing them into one abstraction (a common early mistake) makes every one of them worse:

| Type | Scope | Storage | Lifetime | Access pattern |
|---|---|---|---|---|
| **Working memory** (scratchpad) | Single run | In-memory / checkpoint (`graph_state`, §10.1) | Life of the run | Read/write every step, highest QPS, no cross-run visibility |
| **Conversation memory** | Single session (may span multiple runs, e.g. a multi-turn chat) | Redis (hot) + Postgres (durable) | Session TTL (default 30 days idle) | Append per turn, read at start of each `thinking` step |
| **Long-term / episodic memory** | Per user or per agent, across sessions | Postgres (structured facts) + vector store (semantic recall) | Indefinite, subject to retention policy | Written on significant events, retrieved by semantic query |
| **Entity memory** | Per entity (a customer, an account) referenced across many sessions/agents | Postgres, keyed by entity ID | Indefinite | Read/write keyed lookup, not semantic search |

### 16.1 Working memory (scratchpad)

This *is* the graph's typed state (§7.2, `ResearchState` etc.) — not a separate system. It's what gets checkpointed (§10.1) and what reducers merge into. No separate design is needed here beyond what §7–§10 already specify; it's included in this section only to make the four-tier picture complete and to be explicit that working memory does **not** persist beyond the run except via checkpoint (which is a resumption mechanism, not a memory-retrieval mechanism — a *different* run does not read another run's scratchpad directly; it goes through conversation or long-term memory if it needs continuity).

### 16.2 Conversation memory

```python
memory=MemoryConfig(
    conversation="sliding_window(turns=20)",
    # alternatives: "full_history" (bounded only by model context),
    #               "summary_buffer(max_tokens=2000)"
)
```

| Strategy | Mechanism | Trade-off |
|---|---|---|
| `sliding_window(turns=N)` | Keep the last N turns verbatim, drop older | Cheap, predictable token cost; loses early-conversation context entirely once it scrolls off |
| `full_history` | Keep everything, truncate only when the model's context window is actually exceeded | Maximum fidelity; cost grows with conversation length, eventually forces truncation anyway |
| `summary_buffer(max_tokens=N)` | Keep recent turns verbatim; periodically summarize older turns into a running summary via a (cheap) model call, replacing the verbatim text | Bounds token cost while retaining gist of long history; summarization is lossy and itself costs a model call |

Default is `sliding_window(turns=20)` for most agents (simple, cheap, and sufficient for the 80% case of short-to-medium interactive conversations) with `summary_buffer` recommended for agents where sessions routinely exceed 20 turns (e.g., long troubleshooting sessions) — the summarization call runs asynchronously, off the critical path of the current turn, so it doesn't add latency to the in-progress response; the summary is ready by the *next* turn.

Storage: the live buffer sits in Redis (fast read at the top of every `thinking` step, §8.2), with an async write-behind to Postgres for durability past Redis's TTL and for the long-term-memory pipeline (§16.3) to mine from.

### 16.3 Long-term / episodic memory

```sql
CREATE TABLE memory_entries (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    scope_type      TEXT NOT NULL,        -- 'user' | 'agent' | 'entity'
    scope_id        TEXT NOT NULL,        -- user_id, agent_id, or entity_id
    content         TEXT NOT NULL,
    embedding       VECTOR(1536),
    source_run_id   UUID,                 -- provenance: which run wrote this
    importance      REAL NOT NULL DEFAULT 0.5,   -- §16.5 compaction signal
    created_at      TIMESTAMPTZ NOT NULL,
    last_accessed_at TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ           -- null = indefinite, subject to compaction instead
);
```

Written **not** automatically from every turn (that would flood it with noise) but from an explicit `memory.remember(...)` SDK call inside agent logic, or an automatic "memory extraction" post-processing step (a cheap model call at run end that identifies durable facts worth remembering — "user prefers email over phone," "account X had a billing dispute resolved on date Y") gated behind an opt-in `auto_extract_memory=True` config, since automatic extraction has real cost (a model call per run) and real risk (extracting and persisting something that shouldn't be retained, e.g., transient/incorrect information) that not every agent wants to accept by default.

Retrieval is a semantic query, same retrieve-then-optionally-rerank shape as RAG (§14) but against `memory_entries` scoped to the caller's `(tenant_id, scope_type, scope_id)` rather than a document KB — architecturally the same subsystem, reused rather than reimplemented (the Memory Service calls the RAG Service's retrieval primitives internally, scoped differently).

### 16.4 Entity memory

Keyed lookup, not semantic search — "what do we know about account `acct_9F2B`" is a point query, not a similarity search, and modeling it as one (embedding an account ID and doing ANN search) would be both slower and wrong. Entity memory is a straightforward `(tenant_id, entity_type, entity_id) → JSONB facts` store, updated via explicit `memory.update_entity(...)` calls, read at the start of a run when the input identifies a known entity (e.g., `context={"account_id": "acct_9F2B"}` triggers an automatic entity-memory prefetch merged into initial state before `thinking` begins).

### 16.5 Cross-agent memory sharing

By default, memory scoped to `agent` is **not** visible to a different `agent_id`, even within the same tenant — sharing requires an explicit grant (`memory.share_grant(from_agent, to_agent, scope)`), recorded and auditable like any other cross-boundary permission (§20.4). The one built-in exception is the **supervisor pattern** (§17.2): a supervisor agent and its declared sub-agents share a `run`-scoped memory namespace (a blackboard, §17.4) for the duration of one orchestrated task by design, not by a separate grant, since that's the entire point of the pattern — but this shared namespace is still distinct from each sub-agent's own long-term memory, which remains private unless separately granted.

### 16.6 Compaction

`memory_entries` grows unboundedly if nothing evicts it — at scale (150+ tenants, potentially per-user entries) this becomes both a cost and a retrieval-quality problem (more noise to search through, most of it stale). A background compaction job runs per scope periodically:

1. **Age-based expiry**: entries past `expires_at` (set at write time based on a per-tenant retention policy, default 180 days for episodic memory) are deleted.
2. **Importance-weighted consolidation**: low-`importance`, semantically-similar entries (e.g., ten near-duplicate "user asked about billing" entries from ten separate sessions) are periodically merged into a single consolidated entry by a summarization pass, preserving the gist while reducing entry count — the same idea as `summary_buffer` for conversation memory, applied at the long-term-memory tier.
3. **Access-frequency decay**: entries with no `last_accessed_at` update in a configurable window have their `importance` score decayed, making them progressively less likely to surface in retrieval ranking and eventually eligible for consolidation or expiry — memory that's never useful in practice fades rather than permanently cluttering retrieval, mirroring how the freshness scoring in RAG (§14.8) treats staleness as a ranking signal rather than a hard cutoff.

---

## 17. Multi-Agent Orchestration

### 17.1 Agent-as-tool: the base primitive

The core mechanism underlying every multi-agent pattern below is simple and deliberately reuses machinery already built: **an agent can be invoked as a tool by another agent.**

```python
from platform_sdk import agent_as_tool

research_subagent_tool = agent_as_tool(
    agent_ref="agent://cx-platform/deep-research@v7",
    description="Delegate an open-ended research question to a specialist research agent.",
    timeout_s=120,
    max_cost_usd=2.00,      # sub-agent's own budget, separate ledger entry rolling up to parent (§17.5)
)

supervisor = Agent(
    name="research-supervisor",
    pattern=ReActPattern(max_steps=6),
    tools=[research_subagent_tool, summarize_tool, write_report_tool],
)
```

`agent_as_tool(...)` wraps a target `AgentVersion` behind the exact same `Tool` interface (§12.1) the Tool Gateway already dispatches — input/output JSON Schema (the sub-agent's declared input contract and final-output schema), timeout, budget, credential-adjacent semantics (the sub-agent runs with **its own** RBAC identity, not an inherited one, §20.4). This means multi-agent orchestration adds **zero new dispatch machinery** — the Execution Engine's `acting` state (§8.3) doesn't know or care whether a "tool call" is a REST API call or a full nested agent run; it awaits a result and merges it into `observing` identically either way.

### 17.2 Supervisor pattern

A supervisor is just an agent whose tools are entirely (or mostly) other agents:

```
User request
    │
    ▼
┌───────────────┐
│  Supervisor    │  decomposes task, decides which specialist(s) to invoke
└───────┬───────┘
        │ agent_as_tool calls (§17.1) — each is a full nested run with its own trace subtree
   ┌────┼────┬────────┐
   ▼    ▼    ▼        ▼
┌─────┐┌─────┐┌─────┐┌─────┐
│Sub A ││Sub B ││Sub C ││Sub D │   each: own model, own tools, own memory scope,
└─────┘└─────┘└─────┘└─────┘   own RBAC identity, own budget ledger
   │    │    │        │
   └────┴────┴────────┘
        │  results returned as structured tool outputs
        ▼
┌───────────────┐
│  Supervisor    │  synthesizes final answer from sub-agent outputs
└───────────────┘
```

The supervisor's own reasoning loop treats each sub-agent exactly like any other tool call in `acting` — including the parallel-dispatch behavior of §8.3: if the supervisor's model turn requests three sub-agents in one turn, they execute concurrently, not sequentially.

### 17.3 Fan-out / fan-in

For the common "same task, N parallel instances, aggregate" shape (e.g., "research these 5 competitors in parallel, then compare"), the graph model's `Send` primitive (§7.2 table) is the mechanism, not a manual loop of `agent_as_tool` calls:

```python
def fan_out_competitors(state: ResearchState):
    return [Send("research_one", {"competitor": c}) for c in state["competitor_list"]]

graph.add_conditional_edges("plan", fan_out_competitors)
graph.add_node("research_one", research_single_competitor)   # invoked once per Send, in parallel
graph.add_edge("research_one", "compare")                     # reducer on ResearchState.findings merges results
```

Partial-failure handling mirrors §8.3's tool-partial-failure policy at the fan-out level: by default, `compare` proceeds once all `Send`-spawned branches have either completed or hit their individual timeout, working with whatever findings did come back, each failed branch recorded as a structured error entry in the merged state rather than failing the whole fan-out — a graph author who needs strict all-or-nothing semantics instead opts into `fan_in_policy="require_all"` on the receiving node, which routes to an `error` transition if any branch failed.

### 17.4 Shared memory / blackboard

For a supervisor + sub-agents working on one task, a **run-scoped shared memory namespace** (§16.5) — the "blackboard" — lets sub-agents post intermediate findings other sub-agents (or the supervisor) can read without an explicit tool-call round trip through the supervisor for every piece of shared context:

```python
ctx.blackboard.write("competitor_a_pricing", {...})     # from Sub A
...
data = ctx.blackboard.read("competitor_a_pricing")       # from Sub C, without going through the supervisor
```

This is deliberately **not** the default communication path (`agent_as_tool` structured input/output is) — the blackboard exists for cases where forcing every cross-sub-agent fact through the supervisor's context window would be wasteful or where sub-agents run concurrently and need to react to each other's *partial* progress. It's scoped to the top-level `run_id` and torn down when the run completes; it is not long-term memory (§16.3) unless something explicitly promotes an entry into it.

### 17.5 Cost, tracing, and budget composition across agent boundaries

This is the part naive multi-agent implementations get wrong: a sub-agent's cost, trace, and step count must **roll up** to the parent, or none of the platform's core guarantees (budget enforcement, cost attribution, tracing) mean anything once agents start delegating.

- **Tracing**: a sub-agent's run is a full `AgentRun` in its own right (own `run_id`, own complete trace) but its top-level trace span is a **child span** of the parent's `tool.execute` span for the `agent_as_tool` call that invoked it (§18.2) — so a trace viewer can expand a supervisor's trace and drill into a sub-agent's entire trajectory inline, and conversely a sub-agent's trace independently shows its own `parent_run_id` for direct debugging.
- **Cost**: every Cost Ledger row (§13.7) carries both its own `run_id` and a `root_run_id` (the top-level supervisor run, propagated at every delegation hop) — cost dashboards and budget enforcement (§25) key off `root_run_id` so a team's per-agent budget for the supervisor actually reflects the true end-to-end cost of everything it triggered, not just the supervisor's own direct model calls.
- **Budget enforcement composes as a ceiling, not a sum**: the parent's `max_cost_usd_per_run` check (§8.2) evaluates against the running total across the *entire* `root_run_id` tree, not just its own direct spend — so a supervisor with a $5 budget whose sub-agents have already spent $4.80 combined gets a tight remaining allowance for its own next call and any further delegation, exactly as if it had spent that $4.80 itself. This is what prevents the "budget enforcement only looking at its own layer" bug where a $0.50-capped supervisor delegates to five $2-capped sub-agents and the true cost ceiling is silently $10.50 instead of $0.50.
- **Step count** composes the same way against `max_steps` where an agent's pattern is configured to count delegated sub-runs against its own step budget (`count_subagent_steps=True`, default for supervisor patterns) — the alternative default (independent step budgets per agent) is available for teams that want sub-agents to have real autonomy up to their own limits regardless of how many delegation hops occurred, but is not the default because it reopens the same "true ceiling is silently much higher" problem for step count that budget composition closes for cost.

---

## 18. Observability

### 18.1 Why OpenTelemetry, and what's non-standard on top of it

We use **OpenTelemetry** (OTel) as the wire format and SDK, not a bespoke tracing protocol — it's what lets platform-team-operated collectors interoperate with each tenant's own existing observability stack (many teams already have Datadog/Honeycomb/Grafana pipelines; OTel export means an agent's trace can land in both the platform's central store *and* a team's own backend without double-instrumentation). What's platform-specific is a set of **custom span types and attribute conventions** layered on standard OTel spans, because generic HTTP/RPC span semantics don't capture what matters for an agent run (token counts, prompt versions, tool trust tiers).

### 18.2 Span taxonomy

```
Trace: run_id = "run_9f2b..."
└── agent.run  [span: root]  attrs: {agent_id, agent_version, tenant_id, root_run_id}
    ├── graph.node  (node="thinking", step=1)
    │   └── llm.call  attrs: {provider, model, input_tokens, output_tokens, cost_usd,
    │                          latency_ms, fallback_used, prompt_version}
    ├── graph.node  (node="acting", step=1)
    │   ├── tool.execute  attrs: {tool_name, tool_version, trust_tier, cache_hit, args_hash}
    │   ├── tool.execute  attrs: {...}                     ← parallel siblings, §8.3
    │   └── tool.execute  attrs: {tool_name: "deep-research", ...}
    │       └── agent.run  [span: nested, §17.5]  ← sub-agent's full trace, inlined
    │           └── ... (full nested tree)
    ├── retrieval.query  attrs: {kb_id, query_hash, candidates_returned, rerank_ms, top_score}
    ├── graph.route  attrs: {from_node, to_node, condition_result}
    └── memory.access  attrs: {scope_type, scope_id, op: "read"|"write", entry_count}
```

Every span carries `tenant_id`, `run_id`, `root_run_id`, and `agent_version` as baggage propagated automatically by the Execution Engine — no agent author instruments this manually; it's structural to running on the platform, the same way a service mesh injects standard spans without application code participating.

### 18.3 Metrics

| Metric | Type | Dimensions | Use |
|---|---|---|---|
| `agent_run_duration_seconds` | Histogram | agent_id, tenant_id, pattern, status | Latency SLOs (§3) |
| `agent_run_cost_usd` | Histogram | agent_id, tenant_id, model | Cost distribution, budget alerting input |
| `llm_call_latency_ms` | Histogram | provider, model | Model Gateway health, routing quality |
| `llm_tokens_total` | Counter | provider, model, tenant_id, direction (in/out) | Cost/usage dashboards, provider capacity planning |
| `tool_call_duration_ms` | Histogram | tool_name, trust_tier | Tool health, sandbox overhead tracking (§12.6) |
| `tool_call_error_rate` | Counter/ratio | tool_name, error_class | Tool reliability, owner alerting |
| `retrieval_latency_ms` | Histogram | kb_id | RAG SLO (§3, ≤300ms P99) |
| `agent_run_status_total` | Counter | agent_id, status (done/error/budget_exceeded/cancelled) | Error-rate dashboards, regression signal input |
| `checkpoint_write_latency_ms` | Histogram | pool (interactive/batch) | Runtime health |
| `scheduler_queue_depth` | Gauge | pool, tenant_id | Admission control tuning (§9.4) |

Latency metrics are tracked as **full histograms** (not just averages) per the stated NFR — P50/P95/P99/P99.9 are all queryable per agent, per tool, per model, because averages hide exactly the tail behavior (a slow provider, a hanging tool) that causes user-visible incidents.

### 18.4 Error classification

Errors are tagged with a structured `error_class` at the point of origin so downstream dashboards, alerting, and evaluation don't have to reverse-engineer cause from a stack trace:

| `error_class` | Origin | Typical owner | Example |
|---|---|---|---|
| `provider_error` | Model Gateway, all fallbacks exhausted | Platform (Gateway team) / provider status | Anthropic 503 with no healthy fallback configured |
| `tool_error` | Tool Gateway, unhandled | Tool owner team | Billing API returns 500 |
| `tool_timeout` | Tool Gateway | Tool owner team | Tool exceeded configured `timeout_s` |
| `budget_exceeded` | Execution Engine | Agent owner (expected, not a "bug") | Run hit `max_cost_usd_per_run` |
| `step_limit_exceeded` | Execution Engine | Agent owner — often signals a looping agent | Run hit `max_steps` without reaching a terminal node |
| `schema_validation_error` | Tool Gateway or Model Gateway | Ambiguous — could be model behaving unexpectedly or tool schema being wrong | Model returns malformed tool-call arguments after retries |
| `injection_detected` | Security Layer | Security team (investigate), agent owner (notified) | Input classifier flagged adversarial content (§20.2) |
| `permission_denied` | RBAC / Tool Gateway | Agent owner (misconfiguration) or Security (probing attempt) | Agent attempted a tool call outside its grant |
| `application_error` | Agent's own hook/graph code | Agent owner | Unhandled exception in a custom node function |

This taxonomy is what makes the "each routed to the right owner" requirement operational: alerting rules key off `error_class`, not raw exception text, so a spike in `tool_timeout` for one specific tool pages that tool's owning team, not the platform on-call, while a spike in `provider_error` pages the Gateway team.

### 18.5 Logging pipeline

Structured logs (not free-text) flow from every component through a common ingest path (Fluent Bit sidecars → Kafka → columnar store, e.g., ClickHouse, same trace store backing span queries so logs and traces are joinable by `run_id`). Two log categories with different retention and access rules:

- **Operational logs** (worker health, scheduler decisions, Gateway routing) — 30-day retention, platform-team-visible.
- **Run content logs** (rendered prompts, tool arguments, model outputs) — same 30-day hot retention, but **tenant-scoped access** (a team can query its own agents' run content; cross-tenant access requires the same audited grant as everything else, §20.4) and subject to the PII redaction pipeline (§20.1) **before** indexing, not after — redaction is applied at ingest, so unredacted PII is never durably stored in the logging pipeline in the first place.

### 18.6 Dashboards

Three tiers, matching the three audiences who look at this data:

1. **Platform health** (platform team) — Gateway error rates by provider, worker pool saturation, checkpoint store latency, cross-tenant fairness (is any tenant being starved or hogging capacity).
2. **Agent owner** (product team) — per-agent latency/cost/error trends, prompt version comparison (post-A/B), trace explorer scoped to their own agents, budget burn-down against monthly cap.
3. **Executive/cost rollup** (finance, eng leadership) — spend by team/agent over time, month-over-month trend, projected month-end spend vs. budget, top-N most expensive agents — the direct answer to the "why did the bill 10x" question the platform exists partly to prevent.

---

## 19. Evaluation Framework

### 19.1 Offline evaluation

```sql
CREATE TABLE eval_datasets (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    agent_id        UUID NOT NULL,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,       -- datasets version too — a case added/removed changes the meaning of a score
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE eval_cases (
    id              UUID PRIMARY KEY,
    dataset_id      UUID REFERENCES eval_datasets(id),
    input           JSONB NOT NULL,
    expected_output JSONB,                  -- exact-match / structural cases
    rubric          TEXT,                   -- LLM-as-judge cases: grading criteria in natural language
    tags            TEXT[],                 -- e.g. {'refund_policy', 'edge_case', 'regression_2024_11'}
    source          TEXT NOT NULL           -- 'hand_written' | 'production_sample' | 'synthetic'
);
```

Golden datasets are built from three sources, in practice blended: **hand-written** cases (the agent owner encoding known-important behaviors, including edge cases that bit them before — every production incident should leave behind a regression case), **production samples** (real anonymized inputs pulled from traced runs, especially ones flagged by users/reviewers as problematic, §19.3), and **synthetic** cases (LLM-generated variations covering input distribution the hand-written set doesn't reach). Hand-written cases anchor precision (we know the right answer); production samples anchor realism (we know these inputs actually occur); synthetic cases anchor coverage.

Running an eval:

```
platform eval run --agent support-triage --version candidate-v14 --dataset regression-suite-v9
```

executes every case against the candidate `AgentVersion` (same Execution Engine, same tracing — an eval run is a normal `AgentRun` tagged `eval_run=true`, not a separate code path, which is what guarantees eval results reflect actual production behavior rather than a simulator's approximation of it) and scores each case:

| Scoring method | How | Best for |
|---|---|---|
| Exact/structural match | Output equals or structurally matches `expected_output` | Deterministic tasks (classification, extraction with a known schema) |
| Rubric-based LLM-as-judge | A separate judge-model call scores the output against `rubric` on a defined scale, with the rubric decomposed into **named criteria** (not one holistic score) — e.g., `groundedness`, `completeness`, `tone_appropriateness`, each 1–5 | Open-ended generation where there's no single correct string |
| Reference-based similarity | Semantic similarity (embedding cosine, or a judge comparing to a reference answer) | Cases with a known good answer but acceptable paraphrase |
| Human review | Routed to a review queue, §19.3 | Highest-stakes or judge-disagreement cases |

**LLM-as-judge specifics**: the judge is a different, typically stronger, model than the one being evaluated (avoids the agent's own biases grading its own work), given the rubric criteria, the input, and the output, and asked to score each criterion with a short justification — the justification is stored alongside the score specifically so a human can audit *why* the judge scored something low without re-running anything. Judge consistency itself is periodically checked against a held-out set of human-labeled cases (judge-vs-human agreement rate tracked as its own metric, §19.5) — an ungoverned LLM-as-judge is itself a source of silent drift if never validated against ground truth.

### 19.2 Online evaluation

Offline datasets go stale — they can't anticipate every real input distribution shift. Online eval continuously samples **live production traffic** (a configurable percentage, default 2–5%, higher for newly-deployed or high-risk agents) and runs the same judge-scoring pipeline asynchronously, off the critical path, against real runs already captured in the trace store:

```
Production run completes → trace persisted → sampling decision (weighted: recent deploys, flagged runs, random baseline)
                                                        │
                                                        ▼
                                        Async judge scoring (same rubric criteria as offline)
                                                        │
                                                        ▼
                                   EvalResult written, tagged eval_type='online', linked to run_id
```

Sampling weight is **not** uniform-random by default — it's biased toward runs from recently-changed `prompt_version`s or `agent_version`s (to get statistical power on a change quickly) and toward runs flagged by inline guardrails as borderline (§20), so online eval budget is spent where it's most informative rather than spread thin uniformly.

### 19.3 Human feedback collection

```
POST /v1/runs/{run_id}/feedback
{ "rating": "thumbs_down", "reason_tags": ["incorrect_info", "wrong_tone"], "comment": "...", "rater_id": "..." }
```

Two feedback populations, tracked separately since they have different reliability and volume characteristics: **end-user feedback** (thumbs up/down on a chat response — high volume, noisy, useful in aggregate trend but not for high-stakes individual case review) and **internal reviewer feedback** (a dedicated review queue where a domain expert on the owning team scores a sampled or flagged run against the same rubric criteria the LLM judge uses — lower volume, high reliability, and the primary source of the human-labeled ground truth §19.1 uses to validate judge consistency). Every feedback record is attributed to `(run_id, agent_version, prompt_version)`, so feedback trends are sliceable by exactly the dimension that matters for "did the last prompt change help or hurt."

### 19.4 Regression detection

Comparing two agent/prompt versions' eval metrics naively (average score of A vs. average score of B) is a well-known way to ship a regression that "looked fine" — a small eval set or noisy judge scores makes a real regression indistinguishable from noise without a statistical test. The Evaluation Service runs a **paired significance test** (the same eval cases scored under both versions, so the comparison is within-case, not just distributional) — a paired t-test or, for LLM-judge ordinal scores, a Wilcoxon signed-rank test — and reports a regression only when the observed drop clears a significance threshold (default p < 0.05) **and** a minimum effect-size floor (to avoid flagging statistically-significant-but-practically-meaningless drops as blocking).

```
Candidate v14 vs. baseline v13, dataset regression-suite-v9 (n=240 cases):
  groundedness:   4.31 → 4.09   Δ=-0.22   p=0.031   [FLAGGED: regression]
  completeness:   4.02 → 4.15   Δ=+0.13   p=0.19    [no significant change]
  tone_appropriateness: 4.5 → 4.48  Δ=-0.02  p=0.71  [no significant change]
```

Deploy gating (§15.4's rollout mechanism) consumes this directly: a candidate version flagged with a regression on any criterion tagged `blocking` in the agent's eval config is **prevented from auto-advancing** past its initial canary traffic percentage — a human must explicitly override, which is logged. Non-blocking criteria regressions surface as a loud warning in the deploy UI but don't halt rollout, since not every metric should have veto power (a team may accept a small tone regression in exchange for a large accuracy gain).

### 19.5 Evaluation dataset and judge management

- Datasets are versioned (`eval_datasets.version`) for the same reason prompts are: a score is only meaningful relative to the exact case set that produced it, and cases get added (new edge case from an incident) or removed (a case turns out to be ambiguous/wrong) over time.
- Judge prompts are themselves versioned Prompt Registry entries (§15) — a judge is just another agent, evaluated by the same machinery, closing the loop rather than treating "the thing that grades" as unaccountable infrastructure.
- **Judge-human agreement rate** is tracked as an ongoing metric (Cohen's kappa between judge scores and human reviewer scores on overlapping cases); a judge whose agreement drops below a threshold (default κ < 0.6) is flagged for prompt revision before its scores are trusted for gating decisions — this is the mechanism that keeps LLM-as-judge from becoming an unaudited black box that quietly drifts away from what humans actually consider good.

---

## 20. Security

### 20.1 PII detection and redaction

A dedicated classifier (a small, fast fine-tuned model plus regex/pattern rules for structured PII — SSNs, credit cards, emails, phone numbers) runs as **inline middleware** at three points: on user input before it reaches `thinking`, on tool results before they're merged into `observing`, and on the final output before it's returned to the caller or persisted to logs/traces (§18.5). Detected PII is either **redacted** (replaced with a typed placeholder, `[REDACTED:SSN]`, so the model still sees "there was a value of this type here" without seeing the raw value — important because completely stripping it can break the agent's ability to reason about what the user said) or **tokenized** (replaced with a reversible token that can be rehydrated only by a caller with explicit permission, used when the downstream step genuinely needs the real value, e.g., a tool call that must pass a real account number to a backend system).

Policy is configurable per KB/agent (some agents legitimately need to handle PII — a billing agent must see account numbers) via a declared `pii_policy` (`redact_all` | `tokenize_reversible` | `allow_scoped(fields=[...])`), defaulting to `redact_all` — the conservative default that a team must deliberately opt out of, not opt into.

### 20.2 Prompt injection defense

The stated assumption (§1) is that the primary attack surface is **tool results and retrieved documents**, not direct user chat input (which existing input-classification approaches handle reasonably well already). Defense in depth:

1. **Classifier-based detection**: every tool result and retrieved chunk passes through an injection classifier before being merged into agent state — trained to recognize patterns like embedded instructions ("ignore previous instructions and...", "system: you must now..."), before that content ever reaches the model's context.
2. **Provenance tagging**: content from tools/retrieval is wrapped with explicit provenance markers in the rendered prompt (e.g., `<tool_result source="untrusted_external">...</tool_result>`) so the model itself has a structural signal distinguishing "instructions from my system prompt" from "data returned by a tool call" — models trained with this distinction are measurably more resistant to treating embedded text as commands, and this pattern is enforced by the templating layer (§15.3), not left to each prompt author to remember.
3. **Containment over prevention**: because detection is never perfect, the design assumes some injection attempts **will** succeed at influencing model output, and bounds the damage structurally — a successful injection cannot itself grant additional tool permissions (RBAC, §20.4, is evaluated against the *agent's* and *caller's* identity, never anything derived from model output), cannot escalate the trust tier of a tool call, and cannot exceed the run's budget ceiling (§8.2, enforced independently of what the model "wants" to do).
4. **High-risk action confirmation**: tools marked `side_effecting=True` with `risk_tier=high` (e.g., "send a payment," "delete a record") can be configured to require the human-in-the-loop approval gate (§10.3) regardless of how confidently the model wants to call them — an explicit circuit breaker between "an LLM decided to do this" and "it actually happened," for the actions where getting it wrong is expensive.

Every detected or suspected injection attempt is logged with `error_class = injection_detected` (§18.4) and feeds a continuously updated detection ruleset — this is treated as an active security surface with its own on-call rotation, not a one-time filter shipped and forgotten.

### 20.3 Output content filtering

A second, independent classifier pass (distinct from injection detection — this one screens the agent's *own* generated output against policy categories: harassment, self-harm content, disallowed advice categories, tenant-specific custom policies) runs before output reaches a user or triggers any side-effecting tool call. Flagged output is either blocked (with a safe fallback response) or — for borderline cases — allowed through but flagged for the online-eval/human-review pipeline (§19.2, §19.3), depending on a configurable confidence threshold, since a filter tuned to zero false negatives will have unacceptable false-positive rates and vice versa; the threshold is a per-tenant policy knob, not a platform-wide constant.

### 20.4 RBAC

```
Role hierarchy (per tenant):
  tenant_admin        — full control: create/deploy/delete agents, manage grants, view all traces/costs
  agent_developer      — create/deploy/modify agents owned by their team; attach tools/KBs they're granted
  agent_operator        — deploy existing agent versions (promote canary → 100%), view traces/costs, cannot author new versions
  agent_viewer            — read-only: view agent config, traces, costs
  tool_owner                — register/modify tools their team owns; grant/revoke other teams' access to those tools
  cross_tenant_auditor        — platform-team-only: read (never write) across all tenants, for incident investigation
```

Grants are **resource-scoped**, not just role-scoped — `agent_developer` on team A's agents does not imply any permission on team B's agents, tools, or KBs; cross-team access (a supervisor agent calling another team's sub-agent, §17; a KB shared across teams, §14) requires an explicit `ResourceGrant(principal, resource, permission, granted_by, expires_at)` record, itself auditable and optionally time-bounded. This is enforced at every layer that matters — Agent Registry (who can deploy), Tool Registry (who can attach a tool), KB access (§14.6), Model Gateway (which models a tenant's data-residency tier permits) — not just at the API Gateway's front door, since a single front-door check is bypassable by any component that trusts an already-authenticated request too much.

### 20.5 Secrets management

Long-lived credentials (API keys for external tools, database passwords) never enter agent code, tool code, or the trace/log pipeline — they live exclusively in a Credential Vault (§12.3), referenced by name (`requires_credential="billing_api"`) and resolved to a short-lived scoped token at dispatch time. Vault access itself is governed by the same RBAC grants (a tool's `requires_credential` declaration is validated against the tool owner's actual Vault grant at registration time, not just trusted blindly). Model provider API keys (Anthropic, OpenAI) are held centrally by the Model Gateway, never distributed to individual agents or teams — a team's "API key" for the platform is a tenant-scoped platform credential, not a passthrough to the underlying provider account, which is precisely what makes centralized rate limiting, fallback, and cost tracking (§13) possible in the first place.

### 20.6 Audit logging

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    principal       TEXT NOT NULL,           -- user or service identity
    action          TEXT NOT NULL,           -- 'agent.deploy' | 'grant.create' | 'credential.issue' | 'kb.access' | ...
    resource        TEXT NOT NULL,
    result          TEXT NOT NULL,           -- 'allowed' | 'denied'
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (created_at);
```

Append-only, no update/delete path exposed to any application role (only a time-boxed, itself-audited retention-expiry job can remove rows past the compliance retention window — default 1 year, longer for regulated tenants). Every deploy, permission grant/revoke, credential issuance, and denied access attempt is recorded — denied attempts specifically, since a pattern of denied attempts is itself a security signal (probing, misconfiguration, or a compromised principal) worth alerting on independently of any single event.

### 20.7 Data residency

Tenants with an EU-only requirement are pinned to an EU control-plane region and an EU-only subset of the Model Gateway's capability matrix (§13.2, `data_residency` field) — the Gateway's `auto` and pinned routing both hard-filter out any model/provider whose serving region doesn't satisfy the tenant's declared residency requirement, before any cost/latency scoring happens, so residency is enforced as a **hard constraint**, not a soft preference that a routing optimization could override under load. RAG knowledge bases and Memory Service storage for EU-pinned tenants are provisioned in EU-region infrastructure exclusively — there is no cross-region replication of EU-tenant data for these subsystems, trading disaster-recovery scope (an EU-region outage affects only EU tenants, with EU-local DR, not global failover) for residency correctness.

---

## 21. Data Models

The core entities referenced throughout, consolidated here. Several tables were already shown inline near the component that owns them (§10.1 `checkpoints`, §12.1's JSON tool schema, §13.7 `cost_ledger`, §14.1 KB tables, §15.2 prompt tables, §16.3 `memory_entries`, §19.1 eval tables, §20.6 `audit_log`); this section adds the remaining top-level entities and shows how they relate.

```sql
CREATE TABLE agents (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    name            TEXT NOT NULL,
    owner_team      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, name)
);

CREATE TABLE agent_versions (
    id                  UUID PRIMARY KEY,
    agent_id            UUID REFERENCES agents(id),
    version             INTEGER NOT NULL,          -- immutable once created
    definition_type     TEXT NOT NULL,             -- 'sdk_compiled' | 'declarative_yaml' | 'graph'
    graph_ir            JSONB NOT NULL,             -- compiled AgentGraph (§7)
    prompt_versions     JSONB NOT NULL,             -- {node_name: prompt_version_id}, pins exact prompts
    model_config        JSONB NOT NULL,             -- primary/fallback/budget (§13, §8.2)
    tool_refs           UUID[] NOT NULL,
    kb_refs             UUID[],
    guardrail_config     JSONB NOT NULL,
    execution_pool       TEXT NOT NULL,             -- 'interactive' | 'batch'
    created_by            TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL,
    UNIQUE (agent_id, version)
);

CREATE TABLE agent_runs (
    id                  UUID PRIMARY KEY,
    root_run_id         UUID NOT NULL,             -- self-referencing for top-level runs; §17.5
    parent_run_id        UUID,                      -- null for top-level runs
    agent_id             UUID REFERENCES agents(id),
    agent_version         INTEGER NOT NULL,
    tenant_id             UUID NOT NULL,
    status                TEXT NOT NULL,             -- idle|thinking|acting|observing|deciding|done|error|
                                                       -- budget_exceeded|cancelled|awaiting_approval
    input                 JSONB NOT NULL,
    output                 JSONB,
    total_cost_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    total_steps             INTEGER NOT NULL DEFAULT 0,
    error_class              TEXT,                    -- §18.4
    trace_id                  TEXT NOT NULL,
    started_at                TIMESTAMPTZ NOT NULL,
    finished_at                TIMESTAMPTZ,
    eval_run                    BOOLEAN NOT NULL DEFAULT false
) PARTITION BY RANGE (started_at);

CREATE TABLE tool_calls (
    id              UUID PRIMARY KEY,
    run_id          UUID REFERENCES agent_runs(id),
    tool_name       TEXT NOT NULL,
    tool_version    TEXT NOT NULL,
    trust_tier      TEXT NOT NULL,
    args            JSONB NOT NULL,
    result           JSONB,
    error             TEXT,
    latency_ms         INTEGER,
    cache_hit           BOOLEAN NOT NULL DEFAULT false,
    started_at           TIMESTAMPTZ NOT NULL
) PARTITION BY RANGE (started_at);

CREATE TABLE eval_results (
    id              UUID PRIMARY KEY,
    dataset_id      UUID REFERENCES eval_datasets(id),
    case_id          UUID REFERENCES eval_cases(id),
    agent_version_id  UUID REFERENCES agent_versions(id),
    run_id             UUID REFERENCES agent_runs(id),
    eval_type            TEXT NOT NULL,        -- 'offline' | 'online' | 'human'
    scores               JSONB NOT NULL,       -- {criterion: score, ...}
    judge_prompt_version   UUID,
    justification            TEXT,
    created_at                TIMESTAMPTZ NOT NULL
);
```

### 21.1 Entity relationship summary

```
tenant ──1:N── agents ──1:N── agent_versions ──1:N── agent_runs ──1:N── tool_calls
                  │                  │                     │  │
                  │                  │ pins                │  └─1:N── checkpoints (§10.1)
                  │                  ▼                      │
                  │            prompt_versions ──N:1── prompts
                  │                                          │
                  ├──N:M (via tool_refs)── tools             └── root_run_id self-ref (multi-agent, §17.5)
                  │
                  ├──N:M (via kb_refs)── knowledge_bases ──1:N── kb_sources ──1:N── kb_documents
                  │
                  └──1:N── eval_datasets ──1:N── eval_cases ──1:N── eval_results ──N:1── agent_versions
```

`agent_versions` is the join point almost everything hangs off — it's the unit that makes a run **reproducible**: given an `agent_version_id`, you can reconstruct the exact graph, exact pinned prompt versions, exact model config, and exact tool/KB set that produced any historical run or eval result.

---

## 22. API Design

### 22.1 REST — control plane (agent/prompt/tool CRUD)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/agents` | Create agent (metadata only) |
| `POST` | `/v1/agents/{id}/versions` | Deploy a new `AgentVersion` (from SDK-compiled IR or YAML) |
| `GET` | `/v1/agents/{id}/versions/{version}` | Fetch a specific version's full definition |
| `POST` | `/v1/agents/{id}/deployments` | Update traffic routing across versions (canary/A-B, §15.4) |
| `POST` | `/v1/prompts/{id}/versions` | Create a new prompt version |
| `POST` | `/v1/prompts/{id}/deployments` | Set A/B traffic split (§15.4) |
| `POST` | `/v1/tools` | Register a tool |
| `POST` | `/v1/tools/{id}/promote` | Change trust tier (requires review approval, §2.2) |
| `POST` | `/v1/knowledge-bases` | Create KB |
| `POST` | `/v1/knowledge-bases/{id}/sources` | Attach a data source (§14.1) |
| `POST` | `/v1/grants` | Create a `ResourceGrant` (§20.4) |
| `GET` | `/v1/audit-log` | Query audit trail (cross-tenant requires `cross_tenant_auditor`) |

### 22.2 gRPC — data plane (run execution, hot path)

Chosen over REST for the data plane specifically because of streaming (native bidirectional streaming, no SSE-over-HTTP1.1 workarounds) and because the Execution Engine ↔ Model Gateway ↔ Tool Gateway internal calls benefit from gRPC's lower serialization overhead at the stated 18,000+ LLM-calls/sec and 10,500+ tool-calls/sec internal call rates (§3) — REST/JSON overhead compounds meaningfully at that volume, while it's irrelevant at control-plane write rates (~10s/sec), which is why control plane stays REST for its simpler tooling/debuggability.

```protobuf
service AgentRuntime {
  rpc CreateRun(CreateRunRequest) returns (CreateRunResponse);
  rpc StreamRun(StreamRunRequest) returns (stream RunEvent);   // §5.5, §11
  rpc CancelRun(CancelRunRequest) returns (CancelRunResponse);
  rpc GetRun(GetRunRequest) returns (AgentRun);
  rpc ResumeRun(ResumeRunRequest) returns (ResumeRunResponse); // §10.3, human-in-the-loop
}

service ModelGateway {
  rpc Complete(CompletionRequest) returns (CompletionResponse);
  rpc StreamComplete(CompletionRequest) returns (stream CompletionChunk);
}

service ToolGateway {
  rpc InvokeTool(ToolInvocation) returns (ToolResult);
  rpc InvokeToolsBatch(stream ToolInvocation) returns (stream ToolResult); // §8.3 parallel dispatch
}
```

A thin REST/SSE gateway (§4's "SSE Gateway") sits in front of `AgentRuntime.StreamRun` for external/browser clients that can't hold a raw gRPC stream — translating gRPC-stream events to SSE frames 1:1, so the event schema (§5.5) stays identical regardless of transport.

### 22.3 Eval submission API

```
POST /v1/eval/runs
{
  "agent_version_id": "...",
  "dataset_id": "...",
  "compare_against": "agent_version_id_of_baseline"   // triggers paired significance test, §19.4
}
→ 202 Accepted, { "eval_run_id": "..." }

GET /v1/eval/runs/{eval_run_id}
→ { "status": "complete", "results": {...}, "regressions": [...] }
```

---

## 23. Deployment Architecture

### 23.1 Kubernetes topology

```
Region: us-east                                    Region: eu-west (data-residency, §20.7)
┌────────────────────────────────────┐            ┌────────────────────────────────────┐
│  Control Plane Cluster               │            │  Control Plane Cluster (EU-scoped)   │
│  (Agent/Prompt/Tool Registry,        │            │                                       │
│   RBAC, Budget Svc — Postgres HA)    │            │                                       │
├────────────────────────────────────┤            ├────────────────────────────────────┤
│  Interactive Worker Pool             │            │  Interactive Worker Pool (EU)         │
│  (~4,000 pods, HPA on concurrency)   │            │                                       │
├────────────────────────────────────┤            ├────────────────────────────────────┤
│  Batch Worker Pool                    │            │  Batch Worker Pool (EU)               │
│  (~1,500 pods, HPA on queue depth)    │            │                                       │
├────────────────────────────────────┤            ├────────────────────────────────────┤
│  Model Gateway (stateless, HPA)       │◀──cross-region routing for non-residency-  ─▶│  Model Gateway (EU)                    │
├────────────────────────────────────┤   constrained tenants only                    ├────────────────────────────────────┤
│  Tool Gateway + sandbox node pool      │            │  Tool Gateway + sandbox node pool       │
│  (gVisor/Firecracker-enabled nodes)    │            │                                        │
├────────────────────────────────────┤            ├────────────────────────────────────┤
│  Self-hosted vLLM GPU pool              │            │  Self-hosted vLLM GPU pool (EU)         │
│  (A100/H100 node pool, separate         │            │                                        │
│   autoscaling — GPU warm-up is slow,     │            │                                        │
│   scale on a longer time constant)        │            │                                        │
├────────────────────────────────────┤            ├────────────────────────────────────┤
│  Trace/Metrics/Log store (ClickHouse)   │            │  Trace/Metrics/Log store (EU)           │
│  RAG vector store (pgvector/Qdrant)      │            │  RAG vector store (EU)                  │
└────────────────────────────────────┘            └────────────────────────────────────┘
```

### 23.2 Autoscaling policy by component

| Component | Scaling signal | Scale-out speed | Notes |
|---|---|---|---|
| Interactive workers | In-flight-run count vs. target concurrency (§9.2) | Fast (seconds, standard HPA) | Sized generously above baseline — cold-start latency for a new pod is on the order of seconds, too slow to absorb a sudden spike without headroom |
| Batch workers | Queue depth (§9.4) | Moderate (tens of seconds) | Preemption (§9.1) absorbs short spikes before new pods are even needed |
| Model Gateway | Request rate | Fast | Stateless, trivially horizontal; rate-limiter/circuit-breaker state lives in shared Redis, not pod-local |
| Tool Gateway sandbox nodes | Sandbox pool utilization + warm-pool depth (§12.5) | Slow for cold nodes (minutes — new node provisioning), fast for warm-pool replenishment | Warm pool sized with a buffer specifically to absorb the gap while new nodes join |
| Self-hosted vLLM GPU pool | Queue depth + token-throughput headroom | Slowest (minutes, GPU node provisioning is expensive to keep hot) | Kept at a higher baseline utilization target than CPU pools — GPU idle capacity is the most expensive kind to over-provision |

### 23.3 Multi-region and GPU allocation

GPU allocation for the self-hosted model fleet is its own scheduling problem, decoupled from the general Kubernetes autoscaler: GPU node pools are provisioned with **longer-horizon capacity planning** (day-ahead reservation based on forecasted load) rather than pure reactive autoscaling, because GPU node boot + model-weight-load time (minutes) is too slow to be a purely reactive control loop at the request-latency timescales the Model Gateway needs — reactive autoscaling handles the residual variance around a well-forecasted baseline, not the baseline itself. Requests routed to self-hosted models queue briefly against this fleet's fixed capacity rather than triggering instant scale-out, and the Gateway's fallback chain (§13.6) is configured so self-hosted-model agents have an external-provider fallback for load spikes the GPU fleet can't absorb in time.

Cross-region: each region's control plane is authoritative for tenants pinned to it (§20.7); tenants without a residency constraint are assigned a home region (lowest-latency to their primary user base) with cross-region Model Gateway routing available as a fallback path, not the default path — keeping the common case single-region for latency and operational simplicity, while making the multi-region capability real (not aspirational) for the tenants that need it.

---

## 24. Failure Modes

| Failure | Detection | Mitigation | Residual risk |
|---|---|---|---|
| **Model provider outage** (full) | Circuit breaker trips (§13.6) on error-rate threshold within ~30s | Automatic fallback to declared alternate provider; agents without a declared fallback surface `provider_error` cleanly rather than hanging | Agents with no fallback configured are unavailable until the provider recovers — a deliberate opt-in cost, not a platform gap (teams choosing single-provider pinning accept this trade explicitly) |
| **Model provider degraded (slow, not down)** | Latency histogram (§18.3) crosses P99 SLA threshold | Gateway's `max_latency_ms` routing hint (§13.3) shifts new traffic to faster alternatives even before the breaker trips on hard errors | A brief window of degraded-but-not-failed requests before routing adapts |
| **Tool timeout cascade** (one slow tool backs up an entire worker pool) | Per-tool latency metrics (§18.3); worker pool saturation alert | Per-call timeouts are hard-enforced at the Tool Gateway independent of the tool's own behavior (§12.3 step 5); sandboxed tools are killed at the process level on timeout, freeing the slot; circuit-breaker-equivalent per-tool "pause new calls" kicks in above a tool-specific error/timeout rate | A tool with a slow but non-zero success rate can still degrade the pool's effective throughput before the breaker trips — tuned via per-tool breaker thresholds, not one global value |
| **Memory corruption** (bad write to conversation/long-term memory poisons future runs) | Schema validation on write (§16 stores are schema-checked, not free-form blobs); anomaly detection on `memory_entries` growth rate per scope | `source_run_id` provenance (§16.3) allows targeted rollback — delete/quarantine entries from a specific bad run without wiping a user's entire memory; compaction jobs (§16.6) bounded so corrupted-but-valid-schema entries don't compound silently forever | A schema-valid but semantically wrong memory write (e.g., a hallucinated "fact" the auto-extraction step persisted) is not caught by schema validation alone — mitigated by keeping `auto_extract_memory` opt-in (§16.3) and periodic sampling of extracted memories through the eval/review pipeline |
| **Prompt injection attack succeeds** | Injection classifier flags (§20.2), even retroactively via online eval sampling | Containment: no privilege escalation possible regardless of model output (§20.2.3); high-risk actions gated behind human approval (§10.3, §20.2.4) | A successful injection that stays within the agent's already-granted (but broad) permissions and doesn't trigger a `high_risk` gate can still cause real but bounded damage — this is why tool permission scoping (grant the narrowest tool set an agent actually needs) matters as much as injection detection itself |
| **Runaway agent (infinite/near-infinite loop)** | `max_steps` and wall-clock ceiling (§8.1, always enforced, no opt-out) | Hard termination to `budget_exceeded`/`step_limit_exceeded`, never a true infinite loop regardless of agent logic | A tight loop that's expensive per-step but stays under step count (e.g., few very-large-context calls) is caught by cost ceiling instead — the two ceilings (steps, cost) are complementary specifically to close each other's gap |
| **Cost explosion** (single agent or single tenant spending far above normal) | Real-time budget ledger (§8.2, per-run) + rolling per-tenant spend rate anomaly detection (§25) | Per-run hard cost ceiling (cannot be exceeded, checked before every LLM call, §8.2); per-tenant daily/monthly budget with soft-alert and hard-throttle thresholds (§25) | A burst of many *individually cheap* runs from a buggy high-volume caller (not any single run exceeding its ceiling) is caught by the tenant-level rate limiter (§13.5) and budget throttle, with a detection lag on the order of the metrics aggregation window (seconds, not instant) — the residual exposure is bounded by that window's worth of spend at the tenant's rate limit, not unbounded |
| **Worker crash mid-run** | Kubernetes liveness probe / pod eviction | Checkpoint-based resumption (§10) for batch/long-running agents; interactive agents below the checkpoint threshold simply fail fast and the caller (which already expected to handle a `503`/timeout per normal service semantics) retries | Sub-checkpoint-interval work is lost and re-executed on resume — bounded by checkpoint frequency (§10.2), not unbounded |
| **Control plane outage** | Health checks on Registry/RBAC services | Data plane runs on cached config (§4.1) — new deploys and permission changes are blocked, but already-running and newly-triggered runs of already-deployed agents continue | Configuration changes made in the outage window are simply queued/rejected until recovery — no split-brain risk since there's no write path during the outage, by design |

---

## 25. Cost Model

### 25.1 Attribution

Every dollar spent is attributable to `(tenant_id, agent_id, agent_version, run_id)` via the Cost Ledger (§13.7), rolled up to `root_run_id` for multi-agent runs (§17.5). Attribution latency (spend → visible in dashboards) is bounded by the ledger's synchronous-write design plus a rollup aggregation job (default 60s cadence — meeting the stated NFR exactly, not with large margin, since sub-minute rollups on a high-write-rate ledger have their own cost and aren't warranted when nothing downstream needs sub-minute granularity).

### 25.2 Budget enforcement, three layers

| Layer | Enforced by | Granularity | Action on breach |
|---|---|---|---|
| Per-run ceiling | Execution Engine (§8.2) | `max_cost_usd_per_run`, checked before every LLM call | Hard stop → `budget_exceeded` |
| Per-agent daily/monthly cap | Budget Service (control plane), enforced at Model Gateway request time via a fast quota check | `agents.daily_usd_cap` / rolling monthly | New runs rejected at admission (`429 budget_exhausted`) once cap is hit; in-flight runs allowed to finish |
| Per-tenant monthly budget | Budget Service | Soft threshold (default 80%): Slack/email alert to team + platform. Hard threshold (default 100% or explicit override cap): new run creation blocked tenant-wide until next cycle or manual override by `tenant_admin` | Existing in-flight runs unaffected; this is an admission-control action, not a kill-switch on running work |

The layering matters: a per-run ceiling alone doesn't stop 10,000 cheap runs from adding up; a per-tenant cap alone doesn't stop one bad agent version from spending the whole tenant's monthly budget in an hour. All three together bound the blast radius at every granularity simultaneously.

### 25.3 Cost optimization strategies

- **Model tiering**: routing rules (§13.3) that default cheap/fast models for simple sub-tasks (classification, extraction) and reserve expensive frontier models for the reasoning steps that actually need them — the "plan with Opus, execute steps with Sonnet" pattern in §5.3's `PlanAndExecutePattern` example is this principle applied structurally, not just a routing suggestion.
- **Prompt caching**: providers that support prompt/context caching (repeated static prefix — system prompt, few-shot examples, long RAG context reused across a conversation's turns) are used by default where available; the Model Gateway tracks cache-hit savings per call in the Cost Ledger (`cache_discount_usd` field) so the savings are visible, not just assumed.
- **Tool result caching** (§12.5) reduces redundant expensive tool calls, indirectly reducing LLM calls too (a cached tool result returns faster, keeping conversations shorter in wall-clock terms, though not directly in token cost).
- **Retrieval-before-generation sizing**: RAG context (§14) is truncated to the top-K chunks that actually improve groundedness in eval (§19.1), not "as many chunks as fit" — larger context isn't free, and eval-driven tuning of K per KB avoids paying for context that doesn't move quality.
- **Budget-aware routing**: the `auto` routing mode (§13.3) can be configured to actively trade quality for cost within a declared `max_cost_usd` ceiling per call, useful for high-volume, quality-tolerant use cases (bulk classification, tagging) distinct from the low-volume, quality-critical use cases where pinned frontier models are worth the cost.

### 25.4 Illustrative unit economics

| Agent profile | Avg cost/run | Volume | Monthly cost (illustrative) |
|---|---|---|---|
| Simple classification agent (Haiku-tier, 1–2 calls) | ~$0.002 | 5M runs/mo | ~$10,000 |
| Standard ReAct support agent (Sonnet-tier, ~6 calls, 2 tools) | ~$0.08 | 500K runs/mo | ~$40,000 |
| Deep research agent (Opus planner + Sonnet executors, ~40 calls, hours) | ~$3.50 | 10K runs/mo | ~$35,000 |

The platform's own overhead (Gateway, tracing, orchestration compute) is budgeted at **<5% of raw model spend** (stated NFR, §3) — at the scale implied above (order $10M+/year aggregate model spend across 150 tenants), that ceiling is what justifies the platform team's own infrastructure budget and is tracked as its own line item, distinct from the model spend it wraps, specifically so platform overhead creep is visible and accountable rather than hidden inside "the AI bill."

---

## 26. Trade-offs and Design Decisions

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| **SDK-first vs. no-code-first** | SDK-first (Python), with declarative YAML as a constrained subset (§5, §6) | No-code builder UI as the primary authoring surface | Our initial adopters are engineering teams who want code-level control and testability; a no-code UI can be layered on top later *generating* the same YAML (§27) without redesigning the execution model, but building the reverse (retrofitting code-level power onto a no-code-first system) is much harder. SDK-first also gives us local dev/test parity (§5.2) as a natural consequence, which a no-code-first design would have to bolt on separately. |
| **Managed vs. BYO models** | Both — Model Gateway abstracts over managed external providers *and* a self-hosted fleet (§13.2) | Pure proxy to external providers only, or pure self-hosted only | Pure-external ties the platform's ceiling to provider pricing/availability with no lever to pull; pure-self-hosted forgoes frontier-model quality the business needs for hard tasks. The abstraction cost of supporting both (one more adapter, one more capacity-planning problem, §23.3) is worth the flexibility, especially for cost-sensitive high-volume agents that can move to self-hosted once eval confirms quality parity. |
| **Centralized vs. federated tool registry** | Centralized registry, federated ownership (§12.2) — one registry, but each tool's lifecycle is owned and reviewed by its authoring team | Fully federated (each team runs its own tool registry, agents reference across registries) | A single registry is what makes cross-team discovery (§12.2's capability search), uniform trust-tier enforcement (§2.2), and uniform credential injection (§12.3) possible without N-way integration work. Federated ownership *within* the one registry preserves team autonomy over their own tools' review/promotion without fragmenting the platform's security guarantees. |
| **Synchronous vs. asynchronous execution as default** | Two pools (§9.1): synchronous/interactive is the default for chat-shaped agents, async/batch is opt-in via `execution.pool` | One unified async-only model (submit, poll/webhook for result) | Forcing every interactive chat agent through poll-or-webhook semantics adds real latency and complexity to the dominant use case (a user waiting for a chat response) for the benefit of a minority (long-running batch) use case that's better served by its own explicit mode. Two pools with one scheduler API (rather than two entirely separate systems) keeps the operational surface unified while letting each workload's scheduling policy be genuinely different (§9.1's comparison table). |
| **LangGraph: embed vs. reimplement executor** | Reimplement executor, adopt authoring API (§2.3, §7) | Embed upstream LangGraph runtime directly | Covered in depth in §7.3 — multi-tenant admission control, our checkpoint model, and supply-chain/version-coupling risk at 150-tenant scale outweigh the maintenance cost of a compatibility shim. |
| **Agent-as-a-Service vs. BYO-service as default** | Agent-as-a-Service (§2.1) | BYO-service (platform provides only supporting APIs) | Directly serves the stated goal ("without managing infra"); BYO-service remains available for the minority of teams with existing service infrastructure or extreme colocation needs, rather than being the primary path everyone has to justify deviating from. |
| **Tool trust default** | Untrusted/sandboxed by default, promoted only via explicit review (§2.2) | Trusted by default, sandboxed only for flagged/third-party tools | At 150-tenant scale with self-serve tool registration, "trusted by default" means one team's careless or malicious tool is a lateral-movement path into every agent that attaches it. The conservative default costs sandbox latency (§12.6) most tools never notice next to LLM call latency, and saves an entire class of incident. |
| **Prompt versioning: CMS-like vs. git-only** | Dedicated Prompt Registry with CMS-like edit velocity (§15.1) | Prompts live in the team's own git repo, deployed via normal CI/CD | Prompt iteration cadence and authorship population (non-engineers included) don't match code-review cadence; a git-only approach either gets bypassed (teams start hardcoding prompt strings elsewhere to move fast) or slows down exactly the workflow — fast prompt iteration — the platform should be accelerating. |

---

## 27. Evolution Path

### v1 — Single agent + tools (one team, prove the core loop)

- Agent SDK (§5) with ReAct pattern only; no graph/LangGraph compatibility yet.
- Tool Registry + Tool Gateway (§12) with two trust tiers only (platform-native, sandboxed) — no first-party-reviewed middle tier yet.
- Model Gateway (§13) fronting two external providers, no self-hosted fleet, no `auto` routing — pinned/aliased only.
- Basic tracing (OTel spans, §18.2) but no custom dashboards beyond raw trace search.
- No RAG, no memory beyond in-run working memory, no eval framework — manual testing only.
- Single-tenant-grade RBAC (auth, but no fine-grained resource-scoped grants yet).
- **Goal**: prove that "an agent defined in a few dozen lines gets multi-provider access, tool orchestration, and tracing for free" — the core value proposition — for one pilot team.

### v2 — Multi-agent + RAG (several teams, real production traffic)

- LangGraph-compatible graph model (§7), plan-and-execute pattern (§5.3), multi-agent orchestration primitives (§17).
- Full RAG pipeline: KB CRUD, ingestion, hybrid search, reranking, permission-aware retrieval (§14).
- Prompt Registry with versioning and basic A/B testing (§15), but no automated regression gating yet.
- Conversation and long-term memory (§16.2, §16.3), no compaction yet.
- Three-tier tool trust model (§2.2) with sandboxed execution fully built out (§12.4–12.5).
- Cost Ledger and per-tenant budgets (§25.2), basic dashboards (§18.6).
- Fine-grained RBAC with resource-scoped grants (§20.4), PII redaction (§20.1), basic injection classifier (§20.2).
- **Goal**: support the "supervisor delegates to specialists, grounded in company knowledge" shape that most production agents actually need, across ~20–30 teams.

### v3 — Evaluation + optimization (scale to 100+ teams, quality becomes the bottleneck)

- Full offline eval framework: golden datasets, multi-criteria LLM-as-judge, paired significance testing, regression-gated rollout (§19.1, §19.4).
- Online eval with production sampling (§19.2), human feedback pipeline (§19.3), judge-human agreement tracking (§19.5).
- Prompt optimization loop (§15.6) — automated candidate generation scored against the same eval gate as human edits.
- Self-hosted model fleet online (§13.2, §23.3), cost-aware `auto` routing (§13.3, §25.3) mature enough to trust for non-critical workloads.
- Memory compaction (§16.6), cross-agent memory sharing with grants (§16.5).
- Mature security posture: provenance-tagged prompts (§20.2.2), high-risk-action human-in-the-loop gates (§20.2.4), full audit trail (§20.6).
- **Goal**: make "did this change help or hurt" an automated, statistically sound answer instead of a vibes-based one, at a scale where manual QA of every prompt change is no longer feasible.

### v4 — Autonomous agents with human oversight (150+ teams, long-running/high-autonomy agents are normal)

- Checkpointed long-running agents (up to 24h, §10) and human-in-the-loop approval gates (§10.3) as first-class, widely adopted patterns — not edge cases.
- No-code builder UI (§26) generating declarative YAML (§6) on top of the same execution model — extending authorship to non-engineering teams without a parallel system.
- Cross-tenant agent marketplace: teams can publish agents/tools for other teams to consume (governed by the same grant model, §20.4, extended to a discovery/catalog layer).
- Fully automated prompt-optimization-and-deploy loops for well-evaluated, low-risk agents (extending §15.6/§19.4's gate to close the loop without a human in it, for agents that have earned that trust via a sustained eval track record).
- Multi-region as the default posture (not the exception), with data-residency-aware routing (§20.7) mature across most tenants, not just the early EU adopters.
- **Goal**: agents that plan, act, and delegate over long horizons with proportionally scaled human oversight — heavy for high-risk/high-autonomy agents, minimal for well-evaluated low-risk ones — rather than uniform heavy-handed gating that doesn't scale to 150+ teams' worth of agents.

---

## 28. Exercises

1. **Budget composition bug.** §17.5 describes budget ceilings composing across `agent_as_tool` delegation via a shared `root_run_id` ledger. Design the race condition this creates: two sibling sub-agents dispatched in parallel (§8.3) both check remaining budget against the same `root_run_id` total at nearly the same instant. Show how a naive check-then-spend implementation lets combined spend exceed the ceiling, and design the concurrency control that prevents it without serializing all parallel sub-agent calls through a single lock (which would defeat the purpose of parallel dispatch).

2. **Design the warm-pool sizing model for sandboxed tools** (§12.4–12.5). Given a tool's observed call rate, P99 execution duration, and desired P99 dispatch-overhead SLA (≤50ms per §12.6), derive a formula for warm-pool size, and describe what signal should trigger the pool to grow/shrink. What happens to your formula during a traffic spike that outpaces the pool's growth rate — what's the degraded behavior, and is it acceptable?

3. **Extend the regression-detection framework (§19.4)** to handle an agent version that improves on 90% of eval cases but severely regresses on a specific, high-value 10% slice (e.g., a particular customer tier or ticket category). Design a slicing/segmentation layer on top of the paired significance test that would catch this, where a single aggregate p-value would not, and propose a deploy-gating policy that accounts for both aggregate and segment-level regressions.

4. **Design the reconciliation job for non-idempotent tool calls that may have double-executed** due to crash-resume (§10.4). Given that the platform cannot make an arbitrary side-effecting call exactly-once, what data does the platform need to record at dispatch time to make *post-hoc* detection and reconciliation of a duplicate side effect possible, and who (platform or tool owner) should own resolving a detected duplicate?

5. **A tenant's monthly budget hard-stops new run creation mid-month** (§25.2). Design the UX and system behavior for the following conflict: a batch agent has a long-running (18-hour) checkpointed run in flight when the tenant's hard cap is hit. Should that run be allowed to continue, checkpointed-and-paused, or killed? Justify your answer against the platform's stated durability and cost-control guarantees, and describe how your answer changes if the in-flight run is itself the thing about to exceed the cap.

6. **Design a load test plan** that validates the platform meets the stated capacity numbers (§3) — specifically, the 10,000 runs/sec peak and the "no single tenant's misbehaving agent degrades another tenant's SLA" isolation requirement. What synthetic workload mix (agent types, tool latency distributions, one deliberately misbehaving tenant) would you construct, and what specific metrics would prove or disprove the isolation guarantee under that load?

7. **Prompt injection through the blackboard** (§17.4). A malicious or compromised sub-agent writes adversarial content to the shared run-scoped blackboard, intended to influence a sibling sub-agent that reads it later. Walk through which of the defenses in §20.2 do and do not apply to this internal, agent-to-agent channel (as opposed to the external tool-result/retrieved-document channel they were designed for), and propose what additional control is needed to close this gap.

8. **Design the migration path for re-embedding a knowledge base** after changing its `embedding_model` (§14.4, noted as pinned per KB precisely because mixing models is a correctness bug). Given a 40M-chunk KB actively serving production retrieval traffic, design a zero-downtime re-embedding and cutover strategy, including how you'd validate the new embedding model's retrieval quality against the old one (using the eval framework, §19) before fully cutting traffic over.
