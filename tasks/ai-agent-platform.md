## System Design Task: Internal AI Agent Platform

### Problem Statement

Design an **internal AI Agent Platform** — the system that lets product teams
across the company build, deploy, and operate **LLM-powered agents** at
enterprise scale, without each team standing up its own inference plumbing,
tracing stack, eval harness, or credential vault.

Today, every team that wants to ship an agent reinvents the same nine things
badly: a hand-rolled loop around a chat completions call, ad-hoc tool
functions with no schema validation, prompts hardcoded as Python f-strings and
edited via `git blame`-hostile diffs, no tracing beyond `print()`, no way to
tell if a prompt change made things worse before a customer does, and
API keys for three different model providers sitting in `.env` files that
leak into Slack. Cost is invisible until Finance asks why the OpenAI bill 10x'd
in a month, and nobody can say which of the 40 internal agents caused it.

The platform's job is to make the **right way the easy way**: an agent
defined in a few dozen lines of Python or a YAML file should get, for free,
multi-provider model access, tool orchestration, retrieval, versioned prompts,
memory, distributed tracing, cost attribution, offline and online evaluation,
guardrails, and role-based access control — all running on shared,
horizontally scaled infrastructure that the platform team, not each product
team, operates.

This is an **internal developer platform** problem wearing an AI costume. The
hard parts are the same as any multi-tenant PaaS — isolation, quotas,
backward compatibility, blast-radius control — compounded by LLM-specific
failure modes that don't exist in ordinary backend systems: non-determinism,
unbounded generation length, prompt injection, runaway tool-calling loops,
and a cost model where a single bad request can cost $50 instead of $0.0001.

Assume the platform will be adopted by **150+ internal teams** within two
years, running everything from a customer-support triage bot to a
multi-step research agent that plans, searches, writes code, and executes it.
Some agents are user-facing and latency-sensitive (chat); others are
async batch jobs that may run for hours (deep research, document processing).
The platform must serve both without forcing either into the other's shape.

---

### Functional Requirements

Your system must support:

1. **Agent SDK and Definition**

   * A **Python SDK** that lets a developer define an agent as code:
     tools as decorated functions, a system prompt, a model choice, and an
     execution pattern — and run it locally against the same runtime
     semantics used in production.
   * A **declarative definition format** (YAML/JSON) for agents that don't
     need custom code: prompt, tools (by registry reference), model, RAG
     sources, guardrails — enabling less technical teams and a future
     no-code builder UI to produce a valid, deployable agent.
   * Support for multiple **execution patterns** out of the box: simple
     single-turn completion, **ReAct** (reason-act-observe loop),
     **plan-and-execute** (upfront plan, then execute steps, replan on
     failure), and **fully custom graphs** for teams with bespoke control
     flow.
   * **LangGraph-compatible graph definitions**: nodes, edges, conditional
     edges, and cycles, so teams already using LangGraph can bring their
     graph and run it on the platform's execution engine with minimal
     rewrite. Define precisely what "compatible" means — full graph object
     import, or a subset with an adapter layer.
   * Agents must be **versioned** as a unit (code/config + prompt version +
     tool set + model config), so a given `AgentVersion` is fully
     reproducible.

2. **Agent Runtime**

   * An **execution engine** that runs agent graphs: a state machine with at
     minimum `idle → thinking → acting → observing → deciding → done/error`
     transitions, driving an LLM call, tool dispatch, and looping until a
     terminal condition (final answer, max steps, timeout, or explicit stop).
   * **Parallel tool calls**: when a model returns multiple tool calls in one
     turn, execute independent ones concurrently and merge results back into
     a single observation before the next model turn.
   * **Streaming**: token-level streaming of the model's output, and
     event-level streaming of the agent's full trajectory (tool call started,
     tool call finished, step boundary) to a client over SSE/WebSocket.
   * **Cancellation**: a caller can cancel a running agent mid-execution;
     in-flight tool calls must be cancelled or allowed to complete cleanly,
     never left as orphaned side effects with no record.
   * **Checkpointing**: long-running agents (minutes to hours) must persist
     state at defined points so they can resume after a worker crash or
     redeploy without restarting from step 0, and so a human can inspect or
     edit state mid-run (human-in-the-loop).
   * **Max-step / budget enforcement**: every run has a hard ceiling on
     steps, wall-clock time, and dollar cost; the engine must terminate a
     run that exceeds any of these, distinctly from a "successful" stop.

3. **Tool Platform Integration**

   * A **tool registry**: tools are registered once (name, description,
     JSON Schema input/output, auth requirements, owner team) and become
     discoverable and attachable to any agent with the right permissions —
     not re-implemented per agent.
   * **Schema validation** of tool call arguments before dispatch, and of
     tool results before they're fed back to the model.
   * **Credential injection**: tools that call external systems (Salesforce,
     internal billing API, a customer's own S3 bucket) receive short-lived
     scoped credentials injected at call time; the agent code and the model
     never see raw secrets.
   * **Timeout and retry** per tool, configurable per tool and overridable
     per agent, with idempotency handling for retried side-effecting calls.
   * **Sandboxed execution** for tools that run arbitrary code (a "code
     interpreter" tool) — isolated, resource-limited, network-restricted.

4. **Model Gateway**

   * A single internal API in front of **multiple model providers**
     (Anthropic, OpenAI, internal fine-tuned/self-hosted models) with a
     unified request/response shape, so switching providers is a config
     change, not a code change.
   * **Routing**: choose a model/provider per request based on rules (task
     type, cost ceiling, latency SLA, data-residency requirement) or
     explicit pinning.
   * **Fallback**: on provider error, timeout, or rate limit, fall back to
     an alternate provider/model within the same request, transparently to
     the caller where policy allows.
   * **Rate limiting** per tenant/team/agent, independent of and in addition
     to each provider's own limits, so one team cannot exhaust the shared
     provider quota.
   * **Cost tracking**: every call attributed to a team, agent, and run, with
     token counts and dollar cost recorded before the response is returned
     to the caller.

5. **RAG Integration**

   * **Knowledge base CRUD**: teams create knowledge bases, attach data
     sources (docs, wikis, tickets, code, structured tables), and manage
     their lifecycle independently of any one agent.
   * **Ingestion pipelines**: documents → parsing → chunking → embedding →
     indexing, running continuously to keep sources fresh, with
     per-document status (ingested, failed, stale) visible to owners.
   * **Retrieval API**: given a query (and caller identity), return ranked
     relevant chunks; support **hybrid search** (dense + sparse) and
     **reranking**.
   * **Permission-aware retrieval**: a chunk is only returned to a query if
     the calling user/agent is authorized to see the source document — no
     retrieval-layer privilege escalation.

6. **Prompt Management**

   * **Versioned prompt registry**: every prompt has an immutable version
     history; an agent version pins an exact prompt version.
   * **Templating** (Jinja2/Mustache-style) with typed variables, so prompts
     are parameterized rather than string-concatenated.
   * **A/B testing**: traffic-split two prompt versions for the same agent,
     measure defined metrics, and support a controlled rollout/rollback.
   * Prompt changes go through the same review/versioning discipline as
     code — diffable, revertible, attributable to an author.

7. **Memory and State**

   * **Conversation memory**: short-term buffer of the current
     conversation/run, with a defined eviction/summarization strategy when
     it exceeds the model's context budget.
   * **Long-term memory**: durable, queryable memory that persists across
     sessions for a given user or agent (facts learned, past outcomes),
     retrievable by the agent on future runs.
   * **Session state**: arbitrary structured state an agent graph reads and
     writes as it executes (the working state of a LangGraph-style graph),
     checkpointed and resumable.
   * **Cross-agent memory sharing**: a defined, permissioned way for one
     agent (or a supervisor in a multi-agent system) to read memory written
     by another.

8. **Observability**

   * **Distributed tracing** of a full agent run: the top-level run span,
     and nested spans for each LLM call, tool call, and retrieval call, with
     causal ordering preserved even across parallel branches.
   * **Token and cost tracking** per run, per step, aggregated per
     team/agent/day.
   * **Latency histograms** per span type and per agent, not just an
     end-to-end number.
   * **Error classification**: distinguish model provider errors, tool
     errors, timeout/budget-exceeded terminations, and application-level
     agent failures (e.g., the agent "gave up" or looped) — each routed to
     the right owner.

9. **Evaluation**

   * **Offline evaluation**: golden datasets of (input, expected
     output/criteria) pairs, run against a candidate agent version, scored
     automatically (exact match, rubric, **LLM-as-judge**) before it ships.
   * **Online evaluation**: sampled production traffic scored continuously
     by the same or complementary judges, to catch drift that offline data
     didn't anticipate.
   * **Human feedback collection**: thumbs up/down and structured feedback
     from real users or internal reviewers, attributable back to the
     specific run and agent version.
   * **Regression detection**: statistically sound comparison of eval
     metrics across agent/prompt versions, gating deploys or at minimum
     flagging regressions loudly before they reach 100% of traffic.

10. **Security**

    * **PII detection and redaction** on inputs, outputs, and anything
      written to logs/traces/memory by default.
    * **Prompt injection defense**: detection of adversarial content in
      tool results, retrieved documents, or user input intended to hijack
      agent behavior, plus containment so a successful injection has bounded
      blast radius (e.g., cannot escalate tool permissions).
    * **Output content filtering** against defined policy categories before
      a response reaches a user or triggers a side-effecting tool call.
    * **RBAC**: who can create/deploy/modify an agent, who can attach which
      tools and knowledge bases, who can view another team's traces or
      costs — enforced platform-wide, not per agent.
    * **Audit logging**: every deploy, permission change, and
      credential-scoped tool call recorded immutably, attributable to a
      principal.

11. **Multi-Agent Orchestration**

    * **Agent-to-agent delegation**: one agent can invoke another as if it
      were a tool, with its own model/tool/memory context, and receive a
      structured result.
    * **Supervisor pattern**: a coordinating agent that decomposes a task,
      routes subtasks to specialist agents, and synthesizes their outputs.
    * **Fan-out / fan-in**: dispatch the same or related subtasks to N
      agents or N tool calls in parallel and aggregate results, with partial
      failure handled explicitly (not "the whole run fails if one of ten
      branches errors").
    * Define how tracing, cost attribution, and budget enforcement compose
      across agent boundaries — a sub-agent's cost must roll up to the
      parent run.

---

### Non-Functional Requirements

1. **Scale**

   * 150+ internal teams, ~2,000 distinct agent definitions within 2 years
   * Sustained: 3,000 agent runs/sec platform-wide at steady state; peak
     10,000 runs/sec
   * Average run: 4–8 LLM calls, 2–5 tool calls; long-tail runs: hundreds of
     steps over hours (deep research / autonomous agents)
   * 500M+ LLM calls/day platform-wide at target scale
   * RAG corpora: up to 50M documents per knowledge base, 5,000+ knowledge
     bases

2. **Latency**

   * Interactive (chat) agents: time-to-first-token P99 ≤ **800 ms** from
     request receipt (excluding model provider TTFT itself)
   * Tool dispatch overhead (platform-added, not the tool's own latency):
     P99 ≤ **50 ms**
   * Model Gateway routing/fallback decision: P99 ≤ **20 ms**
   * RAG retrieval (query → ranked chunks): P99 ≤ **300 ms** including
     rerank
   * Async/batch agents: no hard latency SLA, but throughput and queueing
     fairness across tenants must be guaranteed

3. **Availability**

   * Control plane (agent CRUD, deploy, registry reads): **99.95%**
   * Data plane (ability to start and execute agent runs): **99.9%**,
     ≈ 8.7 hours/year, budgeted mostly against provider-side outages
   * Model Gateway must maintain partial availability (fallback provider)
     even during a full outage of any single model provider
   * No single team's misbehaving agent (traffic spike, infinite loop) may
     degrade another team's SLA — hard multi-tenant isolation required

4. **Cost and Budgeting**

   * Every run's cost attributable to team + agent + version within 60
     seconds of completion
   * Per-team monthly budget with configurable soft (alert) and hard
     (throttle/block) thresholds
   * Platform overhead (gateway, tracing, orchestration) must add **< 5%**
     to raw model provider cost at target scale

5. **Multi-Tenancy and Isolation**

   * Hard isolation of tenant data: knowledge bases, memory, traces, prompts
     never cross-visible without explicit sharing
   * Noisy-neighbor protection: per-tenant rate limits and quotas enforced
     independently of total platform load
   * A tenant's agent code (arbitrary Python in the SDK path) must not be
     able to affect another tenant's runtime — process/container isolation
     is a requirement, not an optimization

6. **Consistency and Durability**

   * Agent definitions and prompt versions: strongly consistent reads after
     write (a deploy must be immediately visible to the runtime that serves
     it)
   * Traces and cost records: eventually consistent is acceptable (seconds
     of lag), but must never silently drop data — durability over latency
     here
   * Checkpointed agent state: durable enough that a worker crash loses at
     most one in-flight step, never the whole run

7. **Extensibility**

   * Adding a new model provider must not require changes to agent code —
     only Model Gateway configuration
   * Adding a new tool must not require a platform redeploy
   * The execution engine must support adding new execution patterns
     (beyond ReAct/plan-and-execute) without breaking existing agents

8. **Compliance**

   * Data residency: some tenants require EU-only processing, including
     which model provider regions may be used
   * PII handling auditable end-to-end: ingestion, storage, retrieval,
     generation, logging

---

### Constraints and Assumptions to State Explicitly

* Whether the platform **hosts** open-weight models itself (GPU fleet) or is
  purely a router to external APIs, and how that choice affects the Model
  Gateway design and cost model.
* Whether agent code (the Python SDK path) executes **on the platform's
  infrastructure** (agent-as-a-service) or **in the team's own service**
  with the platform providing only the supporting services (gateway, RAG,
  observability) via SDK calls — this materially changes the isolation and
  deployment story. Pick one as the default and justify it; you may offer
  the other as a supported mode.
* The trust boundary for tool execution: are tools first-party (written and
  owned by platform users, trusted code) or can any team register
  arbitrary/third-party tools that other teams' agents might call?
* How much of LangGraph (or a similar OSS framework) is adopted wholesale
  vs. reimplemented for multi-tenant, checkpointed, traced execution.

---

### What You Should Deliver

1. **Requirement clarification and key assumptions**, including the
   hosting-model and trust-boundary decisions above.
2. **High-level architecture** — every major component (SDK, Runtime,
   Tool Gateway, Model Gateway, RAG Service, Prompt Registry, Memory
   Service, Observability Pipeline, Evaluation Service, Security Layer) and
   how they talk to each other; control plane vs. data plane.
3. **Agent SDK design** — the developer-facing API surface, with concrete
   code showing how a team defines a ReAct agent, a graph-based agent, and
   a declarative YAML agent.
4. **Agent Runtime internals** — the execution engine's state machine, how
   work is scheduled onto workers, how parallel tool calls, streaming,
   cancellation, and checkpointing are implemented.
5. **Tool Platform, Model Gateway, and RAG Pipeline designs**, each with
   data flow, failure handling, and capacity numbers.
6. **Prompt Management and Memory Architecture**, including how versioning,
   A/B testing, and multi-tier memory are implemented and stored.
7. **Observability and Evaluation designs**, including what gets traced,
   what gets measured, and how regressions are caught before full rollout.
8. **Security design** — RBAC model, injection/PII defenses, credential
   handling, audit trail.
9. **Multi-agent orchestration design** — delegation, supervisor pattern,
   fan-out/fan-in, and how cost/tracing compose across agent boundaries.
10. **Data models** for the core entities (Agent, AgentRun, ToolCall,
    PromptVersion, EvalDataset, EvalResult, MemoryEntry, Trace) and the
    key APIs.
11. **Deployment architecture**, capacity planning against the numbers
    above, and failure-mode analysis (provider outage, tool timeout
    cascade, runaway agent, cost explosion, prompt injection).
12. **Explicit trade-offs** for at least: SDK-first vs. no-code-first,
    managed vs. bring-your-own-model, centralized vs. federated tool
    registry, synchronous vs. asynchronous execution as the default.
13. **An evolution path** from a v1 (single agent + tools, one team) to a
    v4 platform (autonomous multi-agent systems with human oversight,
    150+ tenants).
