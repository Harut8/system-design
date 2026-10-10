# 27 — A2A and agent interoperability

> **Prerequisites:** [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md)
> (§5 multi-agent patterns and §6 agent communication are the *in-process* versions of what this
> chapter does across a network; §10 is the failure taxonomy this chapter extends),
> [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md) (§13 multi-agent architectures, §8
> subgraphs; a remote agent can sit behind a graph node exactly like a subgraph),
> [`24-tool-calling-and-enterprise-integration.md`](24-tool-calling-and-enterprise-integration.md)
> (§8 idempotency and §9 error handling are the single-call versions of §8 here), and
> [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md)
> (a remote agent's output is untrusted input; §9 tool-call authorization).
> Useful: [`26-mcp-and-agent-protocols.md`](26-mcp-and-agent-protocols.md) (agent→tool, the layer
> below this one), [`29-agent-identity-and-delegated-authorization.md`](29-agent-identity-and-delegated-authorization.md)
> (who the caller *is* when agent A calls agent B on behalf of a user),
> [`28-agentic-security-owasp-and-mcp-threats.md`](28-agentic-security-owasp-and-mcp-threats.md),
> and [`../sre-observability/02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md)
> (§4 context propagation: the `traceparent` header this chapter carries across the boundary).
>
> **Feeds into:** `14-agent-evaluation.md` (evaluating a delegated trajectory you cannot see
> inside), [`28-agentic-security-owasp-and-mcp-threats.md`](28-agentic-security-owasp-and-mcp-threats.md),
> [`29-agent-identity-and-delegated-authorization.md`](29-agent-identity-and-delegated-authorization.md).
>
> **THESIS:** an interoperability protocol does not make a remote agent safe, reliable or cheap; it
> only makes it *addressable*. A2A standardises the envelope — discovery (Agent Card), a task state
> machine, messages and artifacts, streaming and webhooks — so that agents built by different teams
> or vendors can talk without bespoke glue. Everything that decides whether the system works is
> still yours: whether the boundary should exist at all, what you send across it, who you believe
> on the other side, what happens when the reply is lost after the work was done, and how you
> prove afterwards what happened. **A remote agent is a distributed system with a language model
> inside it; engineer it with the discipline of a payment API, not a function call.** The default
> answer to "should this be a remote agent?" is no, until separate ownership, trust, or deploy
> cadence makes it yes.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The interop layers: MCP, A2A, AGENTS.md, AG-UI — and when a remote boundary is justified](#1-the-interop-layers-mcp-a2a-agentsmd-ag-ui--and-when-a-remote-boundary-is-justified)
2. [A2A: versions, history, governance](#2-a2a-versions-history-governance)
3. [The Agent Card: discovery, skills, capabilities, security](#3-the-agent-card-discovery-skills-capabilities-security)
4. [The data model: Task, Message, Part, Artifact](#4-the-data-model-task-message-part-artifact)
5. [Operations and the three protocol bindings](#5-operations-and-the-three-protocol-bindings)
6. [Streaming, push notifications, and multi-turn (input-required)](#6-streaming-push-notifications-and-multi-turn-input-required)
7. [Authentication and authorization in A2A](#7-authentication-and-authorization-in-a2a)
8. [Other interop efforts: AGENTS.md, ACP, AG-UI](#8-other-interop-efforts-agentsmd-acp-ag-ui)
9. [Engineering the remote-agent boundary](#9-engineering-the-remote-agent-boundary)
10. [Trusting a remote Agent Card; data leakage and context minimisation](#10-trusting-a-remote-agent-card-data-leakage-and-context-minimisation)
11. [Trace propagation across the boundary](#11-trace-propagation-across-the-boundary)
12. [Failure modes that only appear across process and vendor boundaries](#12-failure-modes-that-only-appear-across-process-and-vendor-boundaries)
13. [Wiring a remote agent into the tool registry and an orchestrator](#13-wiring-a-remote-agent-into-the-tool-registry-and-an-orchestrator)
14. [Anti-patterns](#14-anti-patterns)
15. [Interview questions](#15-interview-questions)
16. [Lab exercises](#16-lab-exercises)
17. [Real-world cases](#17-real-world-cases)
18. [Sources](#sources)

---

## Start here — the whole chapter in plain words

**The problem.** Your company's travel agent needs a visa check. The visa team already runs its own
AI agent with its own tools, data, on-call rota and release cycle. You can (a) copy their logic
into your agent, (b) call their agent over the network as if it were one more tool, or (c) merge
the teams. Option (b) is what agent-to-agent protocols are for. The catch: their agent is not a
function. It takes seconds to minutes, may need to ask you a clarifying question halfway, may
fail after doing half the work, and is run by people you cannot page at 3 a.m.

**What A2A is.** The Agent2Agent protocol is an open standard for exactly that call. A server
publishes a JSON "business card" (the **Agent Card**) at a well-known URL, saying what it can do and
how to authenticate. A client sends a **Message**; the server answers with either a Message or a
**Task** — a long-running unit of work with a state machine (`working`, `input-required`,
`completed`, ...) whose results arrive as **Artifacts**. Progress can be streamed (SSE) or pushed
to a webhook. Auth is plain HTTP auth (OAuth 2, API keys, mTLS) declared in the card.

**What A2A is not.** Not how an agent calls a database or API (MCP, chapter 26), not how a repo instructs a coding agent (AGENTS.md), and not a guarantee the remote is trustworthy.

**A worked example (all numbers illustrative).** The travel agent delegates a visa check. The
first `SendMessage` times out at 5 s; the visa agent had in fact already started the task and its
reply was lost. A naive client retries and a *second* visa case is opened (and, if it has side
effects, a second application is filed). A correct client reuses the same `messageId`, so the
server can recognise the replay, and polls the task by ID with a deadline; if the deadline
passes it calls `CancelTask`. Section 9 is about this; the lab in section 16 reproduces it.

### Symbols and parameters used in this chapter

| Symbol | Meaning |
|---|---|
| **A2A v1.0** | The spec version described here: v1.0.0 released 2026-03-12, v1.0.1 on 2026-05-26 (GitHub releases) |
| `AgentCard` | JSON document describing a remote agent: identity, interfaces, skills, capabilities, security |
| `supportedInterfaces` | Card field listing `{url, protocolBinding, protocolVersion, tenant?}`; first entry is preferred |
| `Task` | Stateful unit of work: `id`, `contextId`, `status`, `artifacts`, `history` |
| `contextId` | Server-generated grouping of related tasks/messages (a "conversation") |
| `messageId` | Client-generated unique ID of a Message; the natural **idempotency key** (section 9.1) |
| `TASK_STATE_*` | Task states: SUBMITTED, WORKING, COMPLETED, FAILED, CANCELED, REJECTED (terminal-ish); INPUT_REQUIRED, AUTH_REQUIRED (interrupted) |
| `Part` | Smallest content unit: text, raw bytes, URL, or structured data, with `mediaType` |
| `Artifact` | A task output made of Parts |
| `A2A-Version` | HTTP header (or query parameter) carrying `Major.Minor`; mandatory on requests in v1.0 |
| `traceparent` | W3C Trace Context header, carries trace ID across the hop (section 11) |
| `T_conn`, `T_task`, `T_poll` | Connect timeout, whole-task deadline, polling interval used in section 9 |

---

## 1. The interop layers: MCP, A2A, AGENTS.md, AG-UI — and when a remote boundary is justified

> **In plain words.** There is no single "agent protocol". There are several narrow ones, each
> covering one relationship: agent to tool, agent to agent, repo to agent, agent to UI. Mixing them
> up is the most common design error.
>
> **Real-world example.** A coding assistant reads `AGENTS.md` for build commands, calls a
> GitHub MCP server to open a PR, delegates a security review to another team's agent over A2A,
> and streams its progress to the IDE panel over AG-UI. Four relationships, four protocols.

### 1.1 The layering

| Relationship | Protocol | Question it answers | Chapter |
|---|---|---|---|
| agent → tool/data | **MCP** (Model Context Protocol) | "How does a model-driven client discover and call a capability or read a resource?" | [26](26-mcp-and-agent-protocols.md) |
| agent → agent | **A2A** (Agent2Agent) | "How does one autonomous agent hand a task to another, opaque, agent and follow it to completion?" | this chapter |
| repo → agent | **AGENTS.md** | "How does a project tell coding agents how to build, test and behave in it?" | §8 |
| agent ↔ frontend | **AG-UI** | "How does an agent stream state, messages and tool activity to a user-facing app?" | §8 |

The key semantic difference between MCP and A2A: an MCP tool is a **described function** — a
name, a JSON-Schema for inputs, a result — that the client's own model decides to call. An A2A
agent is **opaque and stateful**: you hand it a goal in natural language (plus structured parts),
it decides its own plan and tools, may ask questions back, and returns artifacts later. A2A's
design goal is collaboration *without* sharing internals (memory, prompts, tools). That opacity is
the feature (team autonomy) and the cost (you cannot inspect its reasoning; section 12).

### 1.2 When is a remote-agent boundary justified?

> **In plain words.** A network hop between agents is expensive in latency, failure modes,
> debuggability and security surface. Pay it only when a *social* boundary forces it. Technical
> elegance is not a reason.
>
> **Real-world example.** A bank's retail-assistant team is tempted to "make every skill its own
> agent". Their p50 request goes from 3 s to 11 s, and a single bug now spans four on-call rotas.
> Only the fraud-screening agent, owned by a different org with a different data-residency
> regime, genuinely needed a boundary.

Justified when at least one holds:

1. **Separate ownership and release cadence.** The other team ships weekly, you ship daily, and
   neither wants lock-step deploys. A versioned contract (§9.5) replaces a shared repo.
2. **Separate trust domain.** Different tenant, company, regulatory regime, or credential set.
   The remote agent must hold secrets or data you must not.
3. **Vendor / third-party agent.** You cannot run their code; the network is the only interface.
4. **Different runtime or framework** you do not want to couple to (LangGraph here, another SDK
   there) and the work is coarse-grained enough to amortise the hop.
5. **Independent scaling or isolation of a heavy or risky workload** (long-running research,
   code execution) where a failure should not take down the caller.
6. **Long-running, human-in-the-loop work** whose lifecycle outlives the caller's request — the
   Task state machine and push notifications exist for this.

Not justified (just call a function, or a subgraph, §21-8):

- Same team, same repo, same deploy, same trust domain. Use a subgraph or a plain function.
- The "agent" is really a deterministic procedure with one LLM call; make it a tool.
- You want modularity of *code*; use modules. Modularity of *deployment* is a different (costly)
  decision.
- Fine-grained steps (hundreds of ms). The overhead of discovery, auth, task creation and polling
  dominates.

---

## 2. A2A: versions, history, governance

> **In plain words.** A2A started at Google, moved to the Linux Foundation, and reached a
> stable 1.0 in March 2026. A lot of tutorials on the web describe the 0.x protocol, whose names
> differ. Know which version you are reading.
>
> **Real-world example.** You copy a 2025 blog snippet that posts `message/send` to a card at
> `/.well-known/agent.json`; against a v1.0 server you get method-not-found. The names changed.

### 2.1 Timeline (verified unless marked)

| Date | Event | Source / status |
|---|---|---|
| Apr 2025 | Google announces A2A | widely reported; Linux Foundation release describes Google as contributor |
| 2025-06-23 | Linux Foundation launches the "Agent2Agent Protocol Project" | LF press release (URL in Sources; page title confirmed via search, not fetched) |
| 2025-07-30 | v0.3.0: "mTLS and OAuth metadata additions, Agent Card path change, extended card method" | GitHub releases |
| 2025-08 (reported 08-29) | IBM's Agent Communication Protocol (ACP, BeeAI) merges into A2A; `i-am-bee/acp` archived | reported by secondary sources; see §8.2 |
| 2026-03-12 | **A2A v1.0.0** | GitHub releases |
| 2026-05-26 | v1.0.1 (HTTP binding, transcoding errors, TaskStatus values bug fixes) | GitHub releases |
| 2026-08-17 (reported) | A2A becomes a hosted project of the Agentic AI Foundation (AAIF), a Linux Foundation body that also hosts MCP | Reported by Axios and the AAIF blog (one aggregator says Aug 20). As of 2026-10 the A2A repo README still says only "under the Linux Foundation", so treat the AAIF move and date as reported, not confirmed |

The repo describes itself as "an open source project under the Linux Foundation, contributed by
Google", Apache-2.0, with SDKs for Python (`a2a-sdk`), Go, JavaScript (`@a2a-js/sdk`), Java, .NET
and Rust (per the repository README at the time of writing).

### 2.2 What changed from 0.x to 1.0 (from the v1.0.0 release notes)

The release notes call these "breaking changes since v0.3.0"; the ones that bite implementers:

- **Naming and enums.** "canceled" is now American spelling; enum formatting aligned with the
  ProtoJSON convention — hence values like `TASK_STATE_COMPLETED` and `ROLE_USER` in JSON.
  (My recollection of 0.3 is lowercase kebab strings such as `input-required`; treat as recall.)
- **Method names.** The spec's operation table gives PascalCase names that are the same across
  JSON-RPC and gRPC (`SendMessage`, `GetTask`, `CancelTask`, ...). In 0.x the JSON-RPC names were
  slash-style, e.g. `message/send` and `tasks/get` (recall; verify against the version you
  target).
- **New:** `ListTasks` (`tasks/list` in the release note's wording) with filtering and pagination;
  multi-tenancy (a `tenant` on interfaces and requests); mechanisms for SDKs to stay backward
  compatible; the mandatory `A2A-Version` header.
- **OAuth 2.0 flows revised:** implicit and password flows dropped (deprecated in the proto),
  device-code and PKCE added.
- **Agent Card:** the extended-card flag moved into `capabilities` (`extendedAgentCard`);
  interfaces are now a first-class `supportedInterfaces` list with per-interface
  `protocolVersion`; push-notification config types merged.
- **HTTP binding:** version segments removed from URLs (versioning moved to the header).
> **Pitfall.** `A2A-Version: ` empty is interpreted as **0.3** by a v1.0 server (spec text). A
> client that forgets the header is silently speaking an older dialect, or gets
> `VersionNotSupportedError` (-32009). Always send it.

---

## 3. The Agent Card: discovery, skills, capabilities, security

> **In plain words.** The Agent Card is the remote agent's self-description: who it is, where to
> reach it, what skills it advertises, whether it streams, how to authenticate. A client fetches it
> before the first call.
>
> **Real-world example.** `GET https://visa.example.com/.well-known/agent-card.json` returns a card
> that says "I speak JSON-RPC at https://visa.example.com/a2a, protocol 1.0; I support streaming;
> authenticate with OAuth 2 client-credentials; my skill is `visa-check`".

### 3.1 Discovery

The v1.0 spec places the card at `https://{server_domain}/.well-known/agent-card.json` (RFC 8615
well-known URI convention). 0.3 changed the card path; I recall the earlier name being
`agent.json` (recall, not re-verified). Other discovery modes exist in the A2A docs (curated
registries, direct configuration); the protocol itself does not define a global registry. In
enterprise practice **direct configuration or an internal registry is the right default**: you
decide which hosts are agents, instead of discovering strangers (§10).

### 3.2 Fields (from the v1.0 proto)

Required marked (R). Names below are proto field names; JSON uses lowerCamelCase.

| Field | Notes |
|---|---|
| `name` (R), `description` (R), `version` (R) | Human identity; `version` is the *agent's* version, not the protocol's |
| `supportedInterfaces` (R) | Repeated `AgentInterface`: `url` (R), `protocolBinding` (R), `protocolVersion` (R), `tenant`. Clients treat the **first entry as preferred** |
| `provider` | Organisation info |
| `documentationUrl`, `iconUrl` | Optional |
| `capabilities` (R) | `streaming`, `pushNotifications`, `extensions[]`, `extendedAgentCard` (all optional booleans except the list) |
| `securitySchemes` | Map name → scheme (oneof `apiKey`, `httpAuth`, `oauth2`, `openIdConnect`, `mtls`) |
| `securityRequirements` | Which scheme combinations are required (AND/OR semantics via `SecurityRequirement.schemes`) |
| `defaultInputModes` (R), `defaultOutputModes` (R) | Media types accepted/produced by default |
| `skills` (R) | Repeated `AgentSkill`: `id`, `name`, `description`, `tags` (all R); `examples`, `inputModes`, `outputModes`, `securityRequirements` |
| `signatures` | Repeated `AgentCardSignature`: `protected` (R), `signature` (R), `header` — a JWS |

Two things to read carefully:

- **Skills are advertising, not a typed API.** An `AgentSkill` has a description, tags and
  example prompts — no JSON-Schema for inputs. This differs from MCP tool schemas (ch. 26, ch. 24
  §3). The calling agent's model has to infer what to send from prose. This is why contract tests
  (§9.5) matter more, not less, for A2A.
- **The card is untrusted input.** It is data from a remote party that your model may read. A
  malicious `description` or skill `examples` is a prompt-injection vector (same class as MCP tool
  description poisoning; ch. 17 §5, ch. 28). Never splice raw card text into a system prompt.

### 3.3 Signed cards

v1.0 supports optional JWS signatures on the card (`signatures`; RFC 7515). The spec says agent
cards "MAY be signed" and clients "SHOULD verify signatures when present"; the `signatures` field
itself is excluded from the signed content. Before signing, the card MUST be canonicalised with the JSON Canonicalization Scheme (JCS, RFC 8785), respecting protobuf field-presence (explicitly-set vs omitted fields), then signed as a JWS (spec §8.4). What a signature gives you: **integrity and origin of the card** *if* you
verify against a key you already trust (pinned key, or JWKS fetched from an allow-listed origin).
What it does not give you: that the agent behind it behaves well, or that the endpoint inside the
card is the one you intended (check the interface URL against your allow-list, §10).

---

## 4. The data model: Task, Message, Part, Artifact

> **In plain words.** A **Message** is one turn of communication. A **Task** is a unit of work the
> server tracks over time. **Parts** are the content bricks; **Artifacts** are the results.
>
> **Real-world example.** You send Message "Check visa for a UK citizen to Japan, 5 days" →
> server returns Task `t-91`, state `WORKING` → later `INPUT_REQUIRED` with a Message "Which
> passport number?" → you reply (same `taskId`) → `COMPLETED` with an Artifact `visa-decision`
> containing a data Part.

### 4.1 Message and Part

`Message` (proto): `messageId` (R, client-generated), `role` (R: `ROLE_USER` | `ROLE_AGENT`),
`parts` (R), optional `contextId`, `taskId`, `metadata`, `extensions[]`, `referenceTaskIds[]`.

`Part` is a oneof of `text` (string), `raw` (bytes, base64 in JSON), `url`, or `data` (any JSON
value), plus `mediaType`, `filename`, `metadata`. That is the whole content model: text, files
(inline or by reference), and structured JSON. (0.x had `kind`-discriminated TextPart/FilePart/
DataPart; v1.0 collapsed this to one `Part` with a oneof — verify against your SDK's version.)

Design guidance:

- Put **structured inputs in a `data` part** with a `mediaType` such as `application/json`, and
  keep the prose in a `text` part. Your orchestrator's model writes the prose; your code validates
  and builds the data part. That keeps untrusted-model output out of the structured channel.
### 4.2 Task and its states

`Task`: `id` (R, server-generated), `contextId`, `status` (R: `state`, optional `message`,
`timestamp`), `artifacts[]`, `history[]` (Messages), `metadata`.

States (v1.0 enum): `TASK_STATE_SUBMITTED`, `WORKING`, `COMPLETED`, `FAILED`, `CANCELED`,
`REJECTED`, `INPUT_REQUIRED`, `AUTH_REQUIRED` (plus `UNSPECIFIED`=0). The spec lists
COMPLETED, FAILED, CANCELED and REJECTED as **terminal**.

```
                 +-------------------------------+
   SendMessage   |                               v
  ---------> SUBMITTED --> WORKING --------> COMPLETED
                 |            |  ^  \------> FAILED
                 |            v  |   \-----> CANCELED  (CancelTask)
                 |      INPUT_REQUIRED       
                 |      AUTH_REQUIRED  (interrupted: client must act, same task continues)
                 +--> REJECTED  (server refuses the task up front)
```

Semantics worth stating precisely:

- `REJECTED` — the agent decided not to do it (out of scope, policy); distinct from `FAILED`
  (tried and errored). Retrying a `REJECTED` task with the same content is pointless; retrying a
  `FAILED` one may or may not be (see §9.3).
- `INPUT_REQUIRED` — the task is paused waiting for the *client*; the client sends a new Message
  carrying the same `taskId` (and `contextId`) to continue. This is A2A's multi-turn mechanism.
- `AUTH_REQUIRED` — the task needs additional credentials (e.g. the remote agent must act on the
  user's behalf at a third party and needs consent). The client must obtain them out of band and
  continue. See chapter 29 for delegated authorization.
- A terminal task cannot accept further messages: the spec says such a send yields `UnsupportedOperationError`. A follow-up is a *new* task in the same `contextId`, referencing earlier ones via `referenceTaskIds`. Clients may send `taskId` (and optionally `contextId`) to continue a non-terminal task; a mismatching pair MUST be rejected. Client-provided `taskId` for new tasks is not supported.

`contextId` groups messages and tasks into a conversation; the server generates it and clients treat it as opaque. The client echoes it to continue (or to start a new task in the same context). Treat it as a **session key**: scope your authorization to
it, and never let a client use a `contextId` it was not issued.

### 4.3 Artifacts

An `Artifact` has `artifactId` (R), `name`, `description`, `parts` (R), `metadata`, `extensions`.
Artifacts are the *deliverable*; `status.message` is the *commentary*. Streaming sends artifacts in
chunks (artifact update events; the chunk-append/last-chunk flags exist in the streaming events —
check the event definitions in your SDK). **Validate artifacts** like any untrusted output:
size, media type, schema for `data` parts, and a content scan before they enter your model's
context (ch. 17 §7).

---

## 5. Operations and the three protocol bindings

> **In plain words.** A2A defines one abstract set of operations and maps it onto three wire
> formats. Pick the one your stack already speaks; a server may offer several.
>
> **Real-world example.** A Go-based platform team exposes gRPC internally for efficiency, and a
> JSON-RPC interface for external partners; both are listed in its card's `supportedInterfaces`.

### 5.1 The operations (v1.0 spec §5.3)

| Operation | JSON-RPC / gRPC method | HTTP+JSON route |
|---|---|---|
| Send message | `SendMessage` | `POST /message:send` |
| Send message, streaming | `SendStreamingMessage` | `POST /message:stream` |
| Get task | `GetTask` | `GET /tasks/{id}` |
| List tasks | `ListTasks` | `GET /tasks` |
| Cancel task | `CancelTask` | `POST /tasks/{id}:cancel` |
| Subscribe to task | `SubscribeToTask` | `POST /tasks/{id}:subscribe` (SSE response; errors for terminal tasks) |
| Create/Get/List/Delete push config | `CreateTaskPushNotificationConfig`, `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig` | `/tasks/{task_id}/pushNotificationConfigs[/{id}]` |
| Get extended Agent Card | `GetExtendedAgentCard` | `GET /extendedAgentCard` |

Each HTTP route also has a tenant-prefixed form `/{tenant}/...` for multi-tenant servers.

`SendMessageConfiguration` carries: `acceptedOutputModes`, `taskPushNotificationConfig`,
`historyLength`, and **`returnImmediately`** (default false). With `false`, the call waits until the
task reaches a terminal *or interrupted* state; with `true` it returns as soon as the task is
created. (Earlier drafts called this `blocking`; the v1.0 proto has no such field.) For anything
that can exceed your HTTP/gateway timeout, set `returnImmediately: true` and then poll, stream or
use a webhook. Never hold a connection open for a minutes-long LLM task through a load balancer with
a 60 s idle timeout.

### 5.2 Bindings

1. **JSON-RPC 2.0 over HTTP(S).** PascalCase method names; requests/responses
   `application/json`; streaming via SSE. A2A-specific errors use codes in -32001…-32099 (table
   below). Request example shape (v1.0; `A2A-Version` is a header):

   ```json
   POST /rpc   A2A-Version: 1.0   Content-Type: application/json
   {"jsonrpc":"2.0","id":"1","method":"SendMessage",
    "params":{"message":{"messageId":"m-1","role":"ROLE_USER","parts":[{"text":"hello"}]}}}
   ```

2. **gRPC.** Service `A2AService` from `a2a.proto`; the proto is the normative data model. Errors
   map to gRPC status codes. Best for internal, high-throughput, strongly-typed callers.
3. **HTTP+JSON (REST).** The routes above; the spec recommends `application/a2a+json`. Errors map
   to HTTP status codes.

A server MAY support more than one; the card lists each as an `AgentInterface`. The client picks
the first interface whose binding **and** `protocolVersion` it supports (my lab does exactly
this).

### 5.3 Error model (v1.0)

| Error | JSON-RPC | gRPC | HTTP |
|---|---|---|---|
| `TaskNotFoundError` | -32001 | NOT_FOUND | 404 |
| `TaskNotCancelableError` | -32002 | FAILED_PRECONDITION | 400 |
| `PushNotificationNotSupportedError` | -32003 | FAILED_PRECONDITION | 400 |
| `UnsupportedOperationError` | -32004 | FAILED_PRECONDITION | 400 |
| `ContentTypeNotSupportedError` | -32005 | INVALID_ARGUMENT | 400 |
| `InvalidAgentResponseError` | -32006 | INTERNAL | 500 |
| `ExtendedAgentCardNotConfiguredError` | -32007 | FAILED_PRECONDITION | 400 |
| `ExtensionSupportRequiredError` | -32008 | FAILED_PRECONDITION | 400 |
| `VersionNotSupportedError` | -32009 | FAILED_PRECONDITION | 400 |

Standard JSON-RPC errors (-32700…-32603) also apply. Note what is **absent**: no standard
"rate limited", "overloaded" or "deadline exceeded" A2A error; those arrive as HTTP 429/503/504 at
the transport. Your client's retry policy (§9.3) must handle both layers. Also note how
`TaskNotCancelableError` is the answer to the race "cancel arrived after completion" — a normal,
expected outcome, not an incident.

---

## 6. Streaming, push notifications, and multi-turn (input-required)

> **In plain words.** Three ways to learn a long task's outcome: poll it, hold a stream open, or
> give the server a webhook. They differ in cost and in what breaks.
>
> **Real-world example.** A research agent takes 4 minutes. The UI streams interim status over SSE;
> the backend job runner that triggered it instead registers a webhook and goes to sleep.

| Mode | Needs card capability | Mechanism | Fails when |
|---|---|---|---|
| Poll | none | `GetTask` every `T_poll` | polling storms; latency = `T_poll` |
| Stream | `capabilities.streaming` | `SendStreamingMessage` or `SubscribeToTask`, SSE (`text/event-stream`) | connection drops (proxy idle timeouts); you must resubscribe and reconcile |
| Webhook | `capabilities.pushNotifications` | `TaskPushNotificationConfig` {url, token, authentication} | your endpoint is down, SSRF, replay, duplicates |

### 6.1 Streaming

Over JSON-RPC, each SSE `data:` line is a JSON-RPC response whose result is a `StreamResponse`.
Ordering: the stream starts with a `Task` (or a `Message`, which closes the stream immediately),
then status-update and artifact-update events, and **closes when the task reaches a terminal or
interrupted state**. Calling `SubscribeToTask` on a task already terminal returns
`UnsupportedOperationError`. If the card says `streaming` is false, a streaming call must return
`UnsupportedOperationError`.

### 6.2 Push notifications

The client supplies a webhook (`url`, optional opaque `token`, optional `authentication`
{`scheme`, `credentials`}). The agent POSTs a `StreamResponse` payload
(`Content-Type: application/a2a+json`); the receiver must return 2xx. Per the spec delivery is
**at least once**, so the receiver must be idempotent (dedupe on task ID + status timestamp or
event identity).

Security the spec calls out: the **agent must guard against SSRF** — reject private ranges,
localhost, link-local, optionally allowlist webhook hosts — because the client controls the URL.
Conversely, the **receiver must authenticate the sender**: verify the `token` you gave, and the
declared auth scheme, before trusting the body; and treat the body as untrusted data (it may carry
agent text). Prefer to use the webhook as a *nudge*: on receipt, call `GetTask` over your
authenticated channel and trust that response, not the pushed payload.

### 6.3 Multi-turn: `INPUT_REQUIRED`

When the task needs more from the client, it enters `TASK_STATE_INPUT_REQUIRED` with a status
`message` (the question). The client replies with a Message carrying that `taskId` (and
`contextId`). Design implications:

- **Who answers?** If your orchestrator is an agent, a model may answer the remote's question
  by itself — possibly leaking data the remote is fishing for (§10.2) or hallucinating an answer.
  Route `INPUT_REQUIRED` through a policy: auto-answer only from an allow-listed set of fields;
  otherwise escalate to the human (ch. 22 §9).
- **Persist the pending state.** The caller may need to survive a restart while the task waits
  for a human; store `(taskId, contextId, remote, state, deadline)` durably (checkpointer in
  LangGraph, ch. 21 §14).

---

## 7. Authentication and authorization in A2A

> **In plain words.** A2A does not invent a login system. The card says which standard HTTP auth
> the server wants; you do that; the server checks it on every call.
>
> **Real-world example.** The card declares an OAuth 2.0 client-credentials scheme with a token URL
> and scope `visa:check`. Your service fetches a token from your IdP, sends it as a bearer
> token with every request, and the visa agent verifies audience and scope.

- **Declaration.** `securitySchemes` is a map of named schemes in OpenAPI-like shape: API key
  (`location`, `name`), HTTP auth (`scheme` such as `bearer`, `bearerFormat`), OAuth 2.0 (`flows`:
  authorizationCode, clientCredentials, deviceCode; implicit and password are deprecated/removed
  in v1.0; plus an `oauth2MetadataUrl`), OpenID Connect (`openIdConnectUrl`), and mutual TLS.
  `securityRequirements` states which combinations satisfy access; skills may add their own.
- **Per-call authorization.** The spec: authorization checks "MUST occur before any database
  queries or operations that could leak information" about resources outside the caller's scope.
  Concretely: `GetTask`/`CancelTask`/`SubscribeToTask` must check that the *caller* owns or may
  see that task. Task IDs and context IDs are identifiers, **not capabilities** — an unguessable
  ID is defence in depth, not authorization. Test it: call `GetTask` with another tenant's ID and
  expect `TaskNotFoundError`, not data.
- **Delegation.** When agent A acts for user U against agent B, B needs to know both. A2A leaves
  this to the auth layer: OAuth token exchange (RFC 8693) with an `act` claim, or
  on-behalf-of flows. Do not forward the user's raw token to a third-party agent; mint a narrowed,
  audience-bound token. Details in [`29-agent-identity-and-delegated-authorization.md`](29-agent-identity-and-delegated-authorization.md).
- **Authentication is not trust.** Authenticating *as* the visa agent proves you reached the
  visa agent's endpoint. It says nothing about whether its output is true or safe (§10).

---

## 8. Other interop efforts: AGENTS.md, ACP, AG-UI

> **In plain words.** Three neighbours you will hear about. Only one is a competitor-turned-part
> of A2A; the others cover different relationships.
>
> **Real-world example.** A repo ships `AGENTS.md` with `npm test` instructions; any coding agent
> that honours the convention uses them. Unrelated to A2A, but part of the same "agents should
> interoperate" movement.

### 8.1 AGENTS.md (repo → agent)

"A simple, open format for guiding coding agents" — described by its site as a README for agents:
dev-environment tips, test commands, PR instructions, in plain Markdown, stewarded in the open at
`github.com/agentsmd/agents.md` (MIT-licensed; the repo shows a technical charter file). It is
text for a *model to read*, not a wire protocol. Treat it as **untrusted-but-in-scope input**:
it can be edited by anyone with commit rights, including via a malicious PR, so a coding agent
that obeys an AGENTS.md from a freshly cloned third-party repo is a prompt-injection surface
(ch. 17). Nested-file precedence rules are not covered here; check the convention doc at
agents.md before relying on them.

### 8.2 ACP (IBM BeeAI) — merged into A2A

IBM's Agent Communication Protocol (REST-first, from the BeeAI project, contributed to the Linux
Foundation's AI & Data umbrella) was reported by multiple secondary sources to have merged into
A2A in August 2025: the `i-am-bee/acp` repo is archived, docs point to an A2A migration guide, and
IBM's Kate Blair joined the A2A Technical Steering Committee. Date reported as 2025-08-29. I
could not open IBM's or the LF's primary announcement, so word this as "reported". **Naming
trap:** Zed's *Agent Client Protocol* (editor ↔ coding agent) also abbreviates to ACP, is an
unrelated project, and is not covered here.

### 8.3 AG-UI (agent ↔ frontend)

An open, lightweight, **event-based protocol for agent-human interaction** (repo:
`github.com/ag-ui-protocol/ag-ui`). Backends emit events from about 16 standard types over any
event transport (SSE, WebSockets and webhooks are examples), supporting streaming chat,
bidirectional state sync, generative UI, frontend tool integration and human-in-the-loop. Its docs
position it as complementary: MCP gives agents tools, A2A lets agents talk to agents, AG-UI brings
agents into user-facing apps. Choose it when your problem is *UI*, not *delegation*.

I am deliberately not listing further protocols: for anything else ("ANP", "AP2", ...), verify
against its own spec before putting it in a design.

---

## 9. Engineering the remote-agent boundary

> **In plain words.** The moment the call crosses a network, the question "did it happen?" has
> three answers: yes, no, and *I don't know*. Most of this section is about the third.
>
> **Real-world example.** A procurement agent delegates "place the PO" to a supplier agent. The
> HTTP response is lost at 4.9 s of a 5 s timeout. If the client retries blindly, two purchase
> orders exist. If it gives up, none may exist, or one may.

Reference timing symbols: `T_conn` (connect, 1–3 s), `T_call` (one RPC, e.g. 5–10 s for
create/get), `T_task` (the whole delegated task deadline, task-specific), `T_poll` (polling
interval with jitter). The numbers are placeholders; derive yours from the remote's SLO.

### 9.1 Idempotency across the boundary

A2A gives you two handles: the client-generated **`messageId`** (required on every Message) and the server-generated **`taskId`** (client-provided task IDs are not supported). Spec §3.3.1: Get operations are naturally idempotent; **`SendMessage` "MAY be idempotent" — agents "may utilize the messageId to detect duplicate messages"**; `CancelTask` is idempotent (a duplicate may return `TaskNotFoundError` if the task was purged). So dedupe is optional per server: **do not assume it**. Design in layers:

1. **Client: one logical delegation = one `messageId`, reused on every retry of the *create*
   call.** Persist `(delegation_id, messageId)` *before* sending. After a timeout, you cannot know
   whether a task exists; you retry the create with the same `messageId`.
2. **Server (when you own it): dedupe on `(authenticated caller, messageId)`** and return the
   existing task. Keep the mapping at least as long as clients' maximum retry horizon plus the
   task TTL. The lab server does this.
3. **When the remote does not dedupe** (vendor): you cannot make creation exactly-once. Options:
   (a) only retry creation if the remote operation is safe to duplicate; (b) look for the task
   first with `ListTasks` filtered by `contextId` (clients MAY supply a `contextId`; an agent that
   cannot accept one MUST reject rather than invent one, so you learn whether this works); (c) accept and reconcile duplicates downstream; (d) move the side effect
   out of the agent into a tool with a proper idempotency key (ch. 24 §8) that the agent calls.
4. **Side effects inside the remote agent** need their own idempotency keys derived from the task
   (`taskId` + step), because the remote's *own* retries and restarts replay tool calls. That is
   the remote team's problem — and a contract question you must ask them (§9.5).

> **Pitfall.** Treating the *polling* calls as the dangerous ones. `GetTask` is a safe read;
> it is `SendMessage` (create) and any continuation message answering `INPUT_REQUIRED` that
> are the unsafe-to-duplicate writes. A duplicated answer to "Which passport number?" might be
> harmless; a duplicated "yes, approve" is not. Give continuation messages their own stable
> `messageId` and have the server treat a replay as a no-op.

### 9.2 Timeouts and cancellation

Three independent clocks, none of which stops the others automatically:

| Clock | Owned by | If it expires |
|---|---|---|
| HTTP call timeout (`T_call`) | client transport | You know nothing about the server's state. Retry create with same `messageId` / re-`GetTask`. |
| Task deadline (`T_task`) | client policy | Call `CancelTask`, then record the outcome as *unknown/cancelled*, not failed. |
| Server-side work | remote agent | Continues until it finishes, is cancelled, or its own TTL expires. Costs money and may have side effects. |

Rules:

- **Propagate the deadline, don't just enforce it.** The v1.0 spec defines no
  standard deadline field; carry it in `metadata` or an extension (e.g. an absolute deadline
  timestamp) and tell the remote it may abandon work after that. Absolute timestamps beat relative
  durations because every hop's retries would otherwise reset the budget. A hop must never wait
  longer than the caller's remaining budget minus its own reserve.
- **`CancelTask` is a request, not a guarantee.** It may return `TaskNotCancelableError` (already
  terminal, or the agent cannot abort). The correct client handles: cancelled, already-completed
  (take the result if you still want it), already-failed, and not-cancelable. In every case,
  **re-`GetTask` to learn the final state** before deciding what to compensate.
- **Closing a stream or dropping a poll loop cancels nothing.** Orphaned tasks are the
  boundary equivalent of leaked goroutines. Keep a table of open delegations with deadlines and
  run a reaper that cancels expired ones (lab 16.4).
- **Cancellation of a task with side effects needs compensation**, not just cancellation (saga,
  ch. 22 §10 / ch. 24 §11). `CANCELED` tells you the agent stopped; it does not tell you that the
  payment it already authorised was voided.

### 9.3 Retries

| Failure signal | Retry? | How |
|---|---|---|
| connect error / DNS / TLS | yes | exponential backoff + jitter; request definitely not processed |
| read timeout / connection reset after send | **maybe** | request may have been processed; same `messageId`; see §9.1 |
| HTTP 429 / 503 with `Retry-After` | yes | honour `Retry-After`; respect a per-remote circuit breaker |
| `TaskNotFoundError` on `GetTask` | no (investigate) | the task was never created, expired, or you are on the wrong tenant/endpoint |
| task `FAILED` | **not automatically** | read the status message; transient infra failure vs wrong input; a retry = a *new* task and new side-effects |
| task `REJECTED` | no | policy/scope; change the request or route elsewhere |
| `VersionNotSupportedError` | no | negotiation bug; fix the version/interface selection |
| `INPUT_REQUIRED` | n/a | not an error; follow the policy in §6.3 |

Add a **retry budget** (e.g. retries ≤ 10% of requests per remote per minute) and a **circuit
breaker** per remote. Retrying an LLM-backed agent has real cost: each attempt may burn tokens
and each failed-task retry may repeat the side effects. If a remote has a slow-failure mode
(takes the whole `T_task` to fail), a breaker keyed on *latency* as well as errors is needed.

### 9.4 Task ownership and lifecycle state

Who owns what:

- **The remote owns the task's execution state.** You cannot read its plan, memory or tools.
- **The caller owns the delegation record**: why it was delegated, on whose behalf (principal),
  the deadline, the idempotency key, the remote it was sent to, the expected result schema, the
  compensation plan. Persist this independently of the remote. If the remote loses the task (TTL,
  data loss, redeploy), the caller must still know what it asked for.
- **Cross-run ownership**: `ListTasks` (filters `contextId`, `status`; `pageSize`/`pageToken`)
  is how you reconcile after a crash — "which tasks of mine are still working?". Run it on
  startup, diff against your delegation table, adopt or cancel orphans.

### 9.5 Contract versioning and testing

Two version axes: the **A2A protocol** (`A2A-Version`, `protocolVersion` per interface; compatible
within `Major.Minor`) and the **agent's contract** (`card.version`, plus the semantics of its
skills — which are prose). The protocol is versioned; the *behaviour* behind a skill is not,
unless you make it so.

- **Pin and negotiate protocol versions.** Choose the interface by `(binding, protocolVersion)`
  from `supportedInterfaces`; send `A2A-Version`; handle `VersionNotSupportedError` by falling
  back to another listed interface or failing loudly.
- **Put the machine contract in the `data` part and the schema in a repo** both teams test
  against (JSON-Schema or protobuf). Skills in a card are prose; the schema is the actual API.
- **Consumer-driven contract tests**: your CI replays a recorded set of requests against the
  remote's staging agent and asserts on *structure* of artifacts (required fields, media type,
  state transitions reached), never on exact text.
- **Adapter layer.** Wrap the remote in your own interface (`delegate_visa_check(req) -> Result`).
  Orchestrator code never touches A2A types; swapping SDKs, versions, or even vendors touches one
  module.

- **Watch the card and agree semantics** (idempotency, TTLs, latency, `FAILED` vs `REJECTED`, PII, on-call): alert on any change of `version`, skills, interfaces or security schemes.

---

## 10. Trusting a remote Agent Card; data leakage and context minimisation

> **In plain words.** The card is a claim made by whoever answered your HTTP request. Decide whom
> you will believe *before* you fetch it, and send the remote only what the task needs.
>
> **Real-world example.** An orchestrator that auto-discovers agents from a shared registry sends
> a customer's full chat history to a "translator" agent that was added last week by an unknown
> party. Nothing in A2A stopped it; the design did.

### 10.1 Trusting the card

Layered, strongest to weakest:

1. **Allow-list of agent identities.** An internal registry or config maps `agent_id → {issuer
   host, expected card signer, allowed skills, data classes allowed, owner, SLA}`. The
   orchestrator contacts nothing else. The default for enterprise use.
2. **Endpoint pinning.** The interface `url` in the card must match the registered host. A card
   served from an allow-listed host that points elsewhere is how an attacker redirects traffic
   (the lab client checks this). Treat *every change* in `supportedInterfaces` as a deployment
   event.
3. **Signature verification** (JWS in `signatures`, §3.3): verify with a key you obtained out of
   band (pinned, or JWKS from the registered issuer); reject on failure; log the key ID. Signed
   cards defend against tampering in transit/at a CDN and against registry poisoning; they do
   nothing about a legitimately-signed but malicious agent.
4. **Transport authenticity**: HTTPS with normal certificate validation; mTLS where declared.
   Domain ownership = identity of the card's origin; it is not a statement about behaviour.
5. **Content hygiene on card text**: skills' `description`/`examples` and `provider` strings are
   untrusted. If you show them to a routing model, put them in a delimited data block, strip
   control characters and instruction-like content, and cap length. Prefer routing on
   *registry metadata you wrote* (skill IDs, tags) rather than on remote-authored prose.
6. **Rug-pull defence**: hash the normalised card at approval time; alert on diff; the same
   threat as MCP tool-description changes (see [`28`](28-agentic-security-owasp-and-mcp-threats.md)).

### 10.2 Data leakage and context minimisation

> **In plain words.** Everything you put in a Message leaves your trust domain. Send the minimum.

Leak paths specific to the boundary:

- **Over-sharing context.** Orchestrators naturally forward "the conversation so far". Instead
  construct a **task brief**: goal, the specific inputs, constraints, output schema. Chapter
  [30 context engineering](30-context-engineering-for-agents.md) frames this as a context
  budget; here it is also a confidentiality budget.
- **Over-answering `INPUT_REQUIRED`.** A remote agent (honest or not) can ask for more data than
  needed ("please send the full customer record"). Answer from a field allow-list per remote.
- **Return channel**: artifacts and status messages may contain instructions aimed at *your*
  model (indirect prompt injection, ch. 17 §5). Process remote output as untrusted data:
  quarantine, schema-validate, and don't let it choose your next tool without a policy check
  (Rule of Two, ch. 17; CaMeL-style separation, ch. 17 §4.7).
- **Transitive exposure.** The remote may itself delegate to sub-agents or log to a third-party
  observability vendor. Contractually and technically ask: where does my data go, how long does it
  live, is it used for training, which sub-processors?
A practical control: a **boundary gateway** (or an interceptor in the adapter) with per-remote
policy: allowed data classes, DLP scan on outbound parts, size limits, egress allow-list,
and an audit record `(who, on whose behalf, remote, skill, data classes, bytes, taskId)`. This is
the same architecture as the model gateway in chapter 23, applied to agent calls.

---

## 11. Trace propagation across the boundary

> **In plain words.** One user request now crosses two companies' logs. If the trace ID does not
> travel with the call, the first thing you lose in an incident is the ability to say "that
> slow request was this slow remote task".
>
> **Real-world example.** A p99 latency alert fires on the checkout assistant. Without
> propagation the visa agent's spans are a separate, unrelated trace; with `traceparent` carried
> in the HTTP headers the agent's span tree attaches under the caller's "delegate" span, and the
> 38 s is visibly the remote's second `INPUT_REQUIRED` wait.

The W3C Trace Context `traceparent` header (`00-<32-hex trace-id>-<16-hex parent-span-id>-<flags>`)
is an HTTP header, and A2A is HTTP (or gRPC metadata); so propagation is the standard OTel one:
inject on the client, extract on the server. See
[`../sre-observability/02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md)
§4 for the byte layout and the pipeline, and its baggage pitfall: **never trust baggage from across
a security boundary** — strip inbound baggage at your edge, and do not send internal baggage
(tenant, user IDs) to an external agent.

Details specific to long-running, asynchronous tasks:

1. **Span per phase, not per connection.** `delegate` span (client) = from creation to terminal
   state; child spans for `SendMessage`, each poll (or a counter, to avoid span floods), and the
   stream/webhook receipt. Attributes: `a2a.remote`, `a2a.task_id`, `a2a.task_state` (final),
   `a2a.context_id`, attempt count. (These attribute names are *my convention*, not an OTel
   semantic-convention standard; check the current GenAI/agent semantic conventions before
   standardising.)
2. **Webhook and resumed work break the synchronous parent-child chain.** The server should
   persist the incoming `traceparent` with the task, and when it later sends a push notification
   or resumes after `INPUT_REQUIRED` it uses a **span link** to the original span (messaging-style
   semantics) rather than pretending the HTTP request is still open.
3. **Continuation messages** (answering `INPUT_REQUIRED`) are new HTTP requests: carry the same
   trace ID, new parent span, so the whole multi-turn exchange shows as one trace.
4. **Metrics to publish per remote**: delegation count by terminal state, create→terminal latency
   histogram, `INPUT_REQUIRED` turns per task, cancel rate, orphan count, duplicate-create rate,
   cost per task. Alert on terminal-state mix shifts and on orphans, not just latency.

The lab's server prints the received `traceparent` so you can see the shared trace ID and the
changing parent span ID per hop.

---

## 12. Failure modes that only appear across process and vendor boundaries

> **In plain words.** In-process multi-agent systems fail by looping, forgetting and confusing
> roles. Those still happen. Crossing a boundary adds failures that have nothing to do with the
> model: partial failure, version skew, hidden state and conflicting incentives.
>
> **Real-world example.** Inside one LangGraph process, a sub-agent exception unwinds the stack
> and the checkpointer resumes. Across A2A, the sub-agent finished, its response vanished, and the
> parent re-delegated — the work happened twice and nothing threw.

Compare with the in-process material: chapter 22 §5 (supervisor / swarm / hierarchical patterns),
§6 (blackboard / message passing) and §10 (failure taxonomy), and chapter 21 §13 (multi-agent
LangGraph architectures: shared state, handoffs via `Command`, subgraphs). Those share one
address space, one transaction-ish checkpoint, one tracing context, one authorization principal and
one deploy. Each of these is lost at the boundary:

| In-process (22 §5–6, 21 §13) | Across A2A / vendor boundary | Consequence and mitigation |
|---|---|---|
| Call either returns or raises; exception unwinds the stack | **Partial failure**: request processed, response lost; or processing in doubt | Idempotency keys, task-ID reconciliation, `ListTasks` on recovery (§9.1, 9.4) |
| One checkpoint covers the whole graph state; time-travel/replay works | Remote state is **opaque and not in your checkpoint**; replaying your graph re-delegates | Persist delegation records; make delegation nodes idempotent on replay (ch. 21 §14) |
| Shared state / blackboard visible to all agents | Only Messages/Artifacts cross; **no shared memory**, context must be re-sent | Task brief design (§10.2); expect duplicated tokens and cost |
| Same prompt/model versions in one deploy | **Version skew**: remote ships a new prompt/model; same card, different behaviour | Contract tests, canary calls, card/version monitoring (§9.5); pin vendor versions when possible |
| Loop detection via step counters and recursion limits (21 §12) | **Distributed loops**: A delegates to B delegates to A; each side's counters reset | Propagate a hop count / delegation chain in `metadata`; refuse cycles; global budget (below) |
| Cost visible in one trace/token meter | Remote tokens/tool costs invisible and billed elsewhere | Per-delegation budget, return usage in metadata where agreed, cost per task SLO |
| Single authorization principal; tool ACL in one registry | **Confused deputy** across trust domains: remote acts with *its* privileges on *your* instructions | Delegated, narrowed credentials (ch. 29); remote re-authorizes; never rely on caller's claims alone |
| Injection stays inside one context window | **Cross-agent prompt injection** propagates through artifacts and `INPUT_REQUIRED` questions, laundering trust ("another agent said so") | Treat remote output as untrusted data; policy engine on next action (ch. 17, 28) |
| One release train | **Independent deploys**: remote outage or breaking change at any time | Circuit breakers, fallbacks, graceful degradation, SLAs, status page subscription |
| Termination by orchestrator | **Runaway remote**: task keeps running after you gave up | Cancel + reaper + remote-side TTL (§9.2) |

---

## 13. Wiring a remote agent into the tool registry and an orchestrator

The existing lab [`labs/tool-registry/`](labs/tool-registry/) (see its README) registers tools with
schemas, validation and an authorization layer. The clean way to expose a remote agent to an
orchestrator is **as one more registered tool whose implementation is the A2A adapter** — it
reuses everything chapter 24 built (schema validation, authz, audit, idempotency keys, timeouts)
and keeps A2A types out of the agent loop:

The adapter is the only code that knows A2A types: it checks policy for the principal, builds a minimal brief (never chat history), derives a stable `messageId`, calls the lab client, and schema-validates the artifact. Properties you get for free by registering it as a tool: the model sees a typed, narrow function
(`nationality`, `destination`, `days`) instead of an open-ended "talk to the visa agent" channel,
so the *model* cannot be talked into sending arbitrary content; the registry's authorization and
audit hooks apply; the orchestrator's existing retry/timeout policy (ch. 24 §9) wraps it; and
tests (ch. 24 §14) can mock the adapter. The trade: you lose the open-ended, multi-turn
collaboration that makes A2A interesting. For genuinely conversational delegation, expose the
adapter as a *sub-graph node* with an explicit `INPUT_REQUIRED` handling policy and a human
interrupt (ch. 21 §8, ch. 22 §9) rather than a one-shot tool.

---

## 14. Anti-patterns

1. **Agent-per-function.** Remote agents for what a function or subgraph could do (§1.2).
2. **Retrying `SendMessage` with a fresh `messageId`.** Guarantees duplicates (§9.1).
3. **Holding one blocking HTTP call for a long task.** Use `returnImmediately`, then poll/stream/webhook (§5.1).
4. **Closing the stream and assuming the task stopped.** Orphans; no reaper (§9.2).
5. **Trusting whatever the card says**, including endpoint URLs and prose, or auto-discovering agents on the open internet (§10.1).
6. **Forwarding the whole conversation** or the user's raw token to the remote (§10.2).
7. **Letting the orchestrator's model answer `INPUT_REQUIRED` freely**, unbounded in turns (§6.3, §12).
8. **Treating `taskId` as a secret/authorization**, or skipping per-call ownership checks (§7).
9. **Webhook receiver trusting the pushed body**, no sender auth, no dedupe (at-least-once) (§6.2).
10. **No trace propagation, no correlation IDs**, no per-remote metrics (§11).
11. **No contract tests**; discovering behaviour changes from user complaints (§9.5).
12. **Using A2A where MCP fits** (a stateless typed capability) or vice versa (§1.1).

---

## 15. Interview questions

**Q1. MCP vs A2A: one sentence each, and can an agent use both?**
MCP is how a model-driven client calls described tools and reads resources; A2A is how an
autonomous agent delegates a goal to another opaque agent and tracks it as a task. Yes: the
remote agent behind an A2A endpoint typically uses MCP for its own tools.

**Q2. When would you refuse to split an agent into a remote agent?**
Same team, same deploy, same trust domain, or fine-grained steps: a subgraph or function avoids
the latency, failure modes and security surface. Split for ownership/cadence, trust, vendor, or
heavy isolated long-running work.

**Q3. Walk through a task lifecycle including a clarifying question.**
Client `SendMessage` (with `messageId`) -> server returns Task `SUBMITTED`/`WORKING` -> task enters
`INPUT_REQUIRED` with a question -> client sends a Message with the same `taskId`/`contextId` ->
`WORKING` -> `COMPLETED` with Artifacts. Terminal states: completed, failed, canceled, rejected.
`AUTH_REQUIRED` is the other interrupted state.

**Q4. The `SendMessage` call times out. What now?**
Unknown state. Retry the create with the *same* `messageId` (server should dedupe), or find the
task by `ListTasks` on `contextId`/correlation; then poll with a deadline; on deadline `CancelTask`
and `GetTask` for the final state. Never create with a new `messageId`.

**Q5. How does A2A authenticate?**
It delegates to standard HTTP schemes declared in the card (`securitySchemes`: API key, HTTP
bearer, OAuth2, OIDC, mTLS) and requires server-side authorization on every operation. IDs are not
capabilities. Delegated user identity needs token exchange (ch. 29).

**Q6. Name failure modes that exist only across the boundary.**
Lost reply after processing, version skew, hidden state not in your checkpoint, distributed loops,
hidden cost, confused deputy, cross-agent injection laundering, orphaned remote work, split
observability (§12).

**Q7. What changed between A2A 0.3 and 1.0 that breaks clients?**
Enum/spelling changes (`TASK_STATE_CANCELED`), PascalCase operation names, `supportedInterfaces`
with per-interface versions, mandatory `A2A-Version` header, OAuth flow changes, extended-card
flag moved into capabilities, merged push-config types, HTTP URLs without version segments, new
`ListTasks` and tenancy.

---

## 16. Lab exercises

Runnable with Python 3 stdlib only. The server and client below are **A2A-style, not a conformant
implementation**: they follow v1.0 shapes (Agent Card at `/.well-known/agent-card.json`,
`supportedInterfaces`, `SendMessage`/`GetTask`/`CancelTask` over JSON-RPC, `A2A-Version: 1.0`,
`TASK_STATE_*`, error codes -32001/-32002/-32009) but omit streaming, push, auth, signing, parts
validation and tenancy. Both were run before inclusion. Save as `a2a_server.py` and `a2a_client.py`.

### 16.1 Server

```python
import json, threading, time, uuid, random, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
FAULT = {"drop_response_p": 0.0, "extra_latency_s": 0.0, "work_s": 1.0}  # mutable, set via env or /_fault
TASKS, BY_MSG, LOCK = {}, {}, threading.Lock()   # BY_MSG: messageId -> taskId (idempotency)

CARD = {
    "name": "Summarizer", "description": "Summarises text (toy).", "version": "0.1.0",
    "supportedInterfaces": [{"url": f"http://127.0.0.1:{PORT}/rpc",
                             "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
    "skills": [{"id": "summarize", "name": "Summarize", "description": "One-line summary",
                "tags": ["text"]}],
}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}

def work(tid, text):
    time.sleep(FAULT["work_s"])
    with LOCK:
        t = TASKS[tid]
        if t["status"]["state"] in TERMINAL:      # cancelled meanwhile: do not overwrite
            return
        t["artifacts"] = [{"artifactId": str(uuid.uuid4()), "name": "summary",
                           "parts": [{"text": text[:40] + ("..." if len(text) > 40 else "")}]}]
        t["status"] = {"state": "TASK_STATE_COMPLETED"}

def rpc(method, p):
    if method == "SendMessage":
        m = p["message"]; mid = m["messageId"]
        with LOCK:
            if mid in BY_MSG:                       # idempotent replay: same task, no 2nd execution
                return TASKS[BY_MSG[mid]]
            tid = str(uuid.uuid4())
            TASKS[tid] = {"id": tid, "contextId": m.get("contextId") or str(uuid.uuid4()),
                          "status": {"state": "TASK_STATE_WORKING"}}
            BY_MSG[mid] = tid
        text = "".join(x.get("text", "") for x in m["parts"])
        threading.Thread(target=work, args=(tid, text), daemon=True).start()
        return dict(TASKS[tid])
    if method == "GetTask":
        with LOCK:
            if p["id"] not in TASKS: raise KeyError("task")
            return dict(TASKS[p["id"]])
    if method == "CancelTask":
        with LOCK:
            t = TASKS.get(p["id"])
            if not t: raise KeyError("task")
            if t["status"]["state"] in TERMINAL: raise ValueError("notcancelable")
            t["status"] = {"state": "TASK_STATE_CANCELED"}
            return dict(t)
    raise LookupError(method)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/.well-known/agent-card.json": return self._send(200, CARD)
        self._send(404, {})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        rid, tp = req.get("id"), self.headers.get("traceparent")
        err = lambda c, m: self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": c, "message": m}})
        if self.headers.get("A2A-Version", "0.3") != "1.0": return err(-32009, "VersionNotSupportedError")
        time.sleep(FAULT["extra_latency_s"])
        try:
            res = rpc(req["method"], req.get("params", {}))
        except KeyError: return err(-32001, "TaskNotFoundError")
        except ValueError: return err(-32002, "TaskNotCancelableError")
        except LookupError: return err(-32601, "Method not found")
        print(f"[server] {req['method']} traceparent={tp}", flush=True)
        if random.random() < FAULT["drop_response_p"]:   # work done, reply lost
            self.connection.close(); return
        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": res})

if __name__ == "__main__":
    FAULT["drop_response_p"] = float(os.environ.get("DROP_P", "0"))
    FAULT["work_s"] = float(os.environ.get("WORK_S", "1"))
    FAULT["extra_latency_s"] = float(os.environ.get("LATENCY_S", "0"))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
```

### 16.2 Client

```python
import json, time, uuid, secrets, urllib.request, urllib.error, sys

ALLOWED_HOSTS = {"127.0.0.1:8765"}          # allow-list of agent hosts you have decided to trust

def post(url, method, params, traceparent, timeout):
    body = json.dumps({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                             "A2A-Version": "1.0", "traceparent": traceparent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    if "error" in out: raise RuntimeError(out["error"])
    return out["result"]

def fetch_card(base):
    host = base.split("//")[1].split("/")[0]
    if host not in ALLOWED_HOSTS: raise PermissionError(f"{host} not allow-listed")
    with urllib.request.urlopen(base + "/.well-known/agent-card.json", timeout=3) as r:
        card = json.load(r)
    iface = next(i for i in card["supportedInterfaces"]
                 if i["protocolBinding"] == "JSONRPC" and i["protocolVersion"].startswith("1."))
    if iface["url"].split("//")[1].split("/")[0] not in ALLOWED_HOSTS:   # card must not redirect us elsewhere
        raise PermissionError("card points at a non-allow-listed endpoint")
    return card, iface["url"]

def delegate(base, text, deadline_s=10.0, per_call_s=2.0, trace_id=None):
    card, url = fetch_card(base)
    trace_id = trace_id or secrets.token_hex(16)
    tp = lambda: f"00-{trace_id}-{secrets.token_hex(8)}-01"       # new span id per hop, same trace id
    msg = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]}
    t0, task, attempt = time.monotonic(), None, 0
    while task is None:                                            # retry SAME messageId => server dedups
        attempt += 1
        try: task = post(url, "SendMessage", {"message": msg}, tp(), per_call_s)
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            print(f"[client] send attempt {attempt} failed: {type(e).__name__}")
            if time.monotonic() - t0 > deadline_s: raise TimeoutError("deadline before task id known")
            time.sleep(min(0.2 * 2 ** attempt, 1.0))
    while task["status"]["state"] not in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                                          "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}:
        if time.monotonic() - t0 > deadline_s:
            try: post(url, "CancelTask", {"id": task["id"]}, tp(), per_call_s)   # best-effort cancel
            except Exception as e: print("[client] cancel failed:", e)
            raise TimeoutError(f"deadline exceeded; cancel requested for {task['id']}")
        time.sleep(0.3)
        task = post(url, "GetTask", {"id": task["id"]}, tp(), per_call_s)
    return task

if __name__ == "__main__":
    print(json.dumps(delegate("http://127.0.0.1:8765", sys.argv[1] if len(sys.argv) > 1 else
          "A2A lets independent agents delegate work to each other over HTTP."), indent=1))
```

### 16.3 Exercise: happy path and trace propagation

```bash
python3 a2a_server.py &            # terminal 1 (kill it afterwards)
python3 a2a_client.py "A2A lets agents delegate work."
```
Expected: a task in `TASK_STATE_COMPLETED` with a `summary` artifact; the server log shows the same
trace ID with a different parent-span ID per call (`SendMessage`, `GetTask`...).

### 16.4 Exercise: fault injection

Run each scenario with environment variables, then answer the questions.

1. **Lost reply.** `DROP_P=0.5 python3 a2a_server.py`. The client sees `RemoteDisconnected` and
   retries. Check the server log: how many tasks exist? (Should be one: the lab server dedupes on
   `messageId`.) Now delete the dedupe lines in `rpc()` and re-run: count the duplicate tasks. This
   is the double-PO bug.
2. **Slow remote.** `WORK_S=30 python3 a2a_server.py` and `delegate(..., deadline_s=2)`. The client
   cancels. Confirm the server's `work()` does not overwrite `CANCELED` (the guard in `work`);
   remove the guard and see `CANCELED` flip to `COMPLETED` — the cancel/complete race.
3. **Version skew.** Change the client's header to `A2A-Version: 0.3` and observe -32009; add
   fallback logic that selects another interface.
4. **Hostile card.** Change `CARD["supportedInterfaces"][0]["url"]` to another host and confirm
   the client refuses (allow-list on the endpoint, not just the card origin).
5. **Orphans.** Kill the client mid-task; persist `(messageId, taskId, deadline)` and cancel expired tasks on startup (add a `ListTasks` to the server, §9.4).

---

## 17. Real-world cases

These are **illustrative scenarios (composite, not a specific company)**; all numbers are
arithmetic examples.

**Case 1 — Duplicate orders after a lost reply.** A procurement agent delegates "place PO" to a
supplier agent; 1 in 200 calls loses its response at the gateway's 30 s idle timeout. The client
retries with a new `messageId`. Of 10,000 delegations/week, about 50 hit the loss and, with ~90% of
them having been processed, about 45 duplicate POs. Fix: stable `messageId`, server-side dedupe on
(caller, `messageId`), supplier-side idempotency key on the PO. Detection: duplicate-create-rate
metric.

**Case 2 — Orphaned research tasks.** An orchestrator abandons tasks after a 60 s UI timeout but
never calls `CancelTask`. Each remote task runs 8 minutes at about 40k tokens; 2,000 abandoned/day
= 80M tokens/day of unread work (2,000 x 40k). Fix: deadline propagation, reaper, remote-side TTL,
cost per delegation metric.

No verified public A2A production postmortem existed at the time of writing, so the cases below are illustrative.

---

## Sources

Fetched in this session (primary):
- A2A GitHub repository: https://github.com/a2aproject/A2A
- A2A releases (v1.0.0, v1.0.1, v0.3.0 notes): https://github.com/a2aproject/A2A/releases
- A2A specification text (v1.0, raw): https://raw.githubusercontent.com/a2aproject/A2A/main/docs/specification.md
- A2A proto (normative data model): https://raw.githubusercontent.com/a2aproject/A2A/main/specification/a2a.proto
- Published spec site (not reachable from my environment; same content): https://a2a-protocol.org/latest/specification/
- AG-UI repository: https://github.com/ag-ui-protocol/ag-ui
- AGENTS.md repository: https://github.com/agentsmd/agents.md

Seen only as search results (secondary; dates reported, not confirmed from primary pages):
- Linux Foundation launch press release (2025-06-23): https://linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents
- A2A joins AAIF, announcement: https://aaif.io/blog/a2a-joins-aaif
- Axios, A2A and AAIF (reported 2026-08-17): https://axios.com/2026/08/17/a2a-agentic-ai-foundation-open-ai-standards
- IBM Research, Agent Communication Protocol: https://research.ibm.com/blog/agent-communication-protocol-ai

Standards referenced: RFC 7515 (JWS), RFC 8615 (well-known URIs), RFC 8693 (token exchange),
W3C Trace Context (https://www.w3.org/TR/trace-context/), JSON-RPC 2.0 (https://www.jsonrpc.org/specification).
Local: [`../sre-observability/02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md).
