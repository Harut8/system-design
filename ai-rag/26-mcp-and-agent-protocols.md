# 26 — MCP: the Model Context Protocol in production

> **Prerequisites:** [`24-tool-calling-and-enterprise-integration.md`](24-tool-calling-and-enterprise-integration.md)
> (this chapter is the wire-protocol sibling of that one: 24 says what a tool call must survive
> between the model's proposal and the side effect, §5 there is the registry that this chapter's
> §8 federates with MCP's discovery, and §6 there is the authorization model that MCP's OAuth layer
> only partly covers), [`23-multi-llm-model-gateway.md`](23-multi-llm-model-gateway.md) (the model
> gateway is the *south-bound* policy point for LLM calls; the MCP gateway of §8.3 is the
> *tool-bound* analogue and reuses its auth, rate-limit, audit and circuit-breaker ideas),
> [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md) (who decides to call a
> tool), and [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md)
> (a tool result is untrusted input; MCP does not change that). Useful: the lab
> [`labs/tool-registry/`](labs/tool-registry/README.md) — §10 of this chapter corrects its
> `mcp.py`, and [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
> for the trace model §9.3 plugs into.
>
> **Feeds into:** [`27-a2a-and-agent-interoperability.md`](27-a2a-and-agent-interoperability.md)
> (agent-to-agent protocols; §11 here is the boundary), [`28-agentic-security-owasp-and-mcp-threats.md`](28-agentic-security-owasp-and-mcp-threats.md)
> (the threat catalogue; §9.5 here is only a pointer),
> [`29-agent-identity-and-delegated-authorization.md`](29-agent-identity-and-delegated-authorization.md)
> (on-behalf-of tokens, workload identity, enterprise-managed authorization in depth — §7 here is
> the MCP-specific minimum), and [`30-context-engineering-for-agents.md`](30-context-engineering-for-agents.md)
> (tool-definition tokens are context cost; tool schemas cost tokens on every turn).
>
> **THESIS:** MCP is a *narrow, boring contract* — JSON-RPC messages that let any model host
> discover and call any tool server — and its value is exactly that boredom. It turns an N×M
> integration matrix into N+M, but it does **not** make a tool safe, correct or cheap: the spec
> itself says tool annotations are untrusted hints and that a human SHOULD be able to deny any
> call. The 2026-07-28 revision pushes the protocol toward what operations people already know how
> to run — stateless requests, per-request headers an L7 load balancer can route on, explicit
> cache TTLs, durable task handles instead of sticky connections — which moves MCP servers from
> "a process a desktop app spawns" to "a horizontally scalable HTTP service behind a gateway".
> **The engineering job is therefore the same as chapter 24's, one layer out: treat every
> MCP server as an untrusted dependency, put a policy enforcement point (the gateway) in front of
> it, and design your own servers as stateless, schema-strict, least-privilege resource servers.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Why MCP exists: N×M, roles, governance](#1-why-mcp-exists-nm-roles-governance)
2. [The wire protocol: JSON-RPC and the primitives](#2-the-wire-protocol-json-rpc-and-the-primitives)
3. [Five revisions in one table](#3-five-revisions-in-one-table)
4. [The 2026-07-28 revision: stateless core, MRTR, Tasks, Apps](#4-the-2026-07-28-revision-stateless-core-mrtr-tasks-apps)
5. [Transports and scaling](#5-transports-and-scaling)
6. [Tool definitions that models and gateways can use](#6-tool-definitions-that-models-and-gateways-can-use)
7. [Authorization: the OAuth resource-server model](#7-authorization-the-oauth-resource-server-model)
8. [Registry, discovery and the MCP gateway](#8-registry-discovery-and-the-mcp-gateway)
9. [Production design](#9-production-design)
10. [Correcting `labs/tool-registry/mcp.py`](#10-correcting-labstool-registrymcppy)
11. [MCP, A2A and AGENTS.md](#11-mcp-a2a-and-agentsmd)
12. [Anti-patterns](#12-anti-patterns)
13. [Interview questions](#13-interview-questions)
14. [Lab exercises](#14-lab-exercises)
15. [Real-world cases](#15-real-world-cases)
16. [Sources](#sources)

---

## Start here — the whole chapter in plain words

**The problem.** Every chat app wants to use every company's data and actions: your calendar, your
tickets, your database. Without a standard, each app writes a custom connector for each service.
With 5 apps and 8 services that is 5 × 8 = 40 connectors, each with its own login, error format and
bugs. MCP (Model Context Protocol) is the standard plug: a service writes **one** MCP server, an
app writes **one** MCP client, and they interoperate: 5 + 8 = 13 pieces instead of 40.

**A real-world example.** A support agent in a chat host needs order data.

1. **Discover.** The client asks the server what it offers: "list your tools". It gets back
   `get_order(id)` with a typed input and output shape (§6).
2. **Call.** The model proposes `get_order("O-5512")`. The client sends one HTTP POST. The server
   answers `{"status": "shipped"}` (§5).
3. **Log in once, correctly.** The first request is rejected with 401 and a pointer to "who issues
   tokens for me". The client does an OAuth login, gets a token that is valid **only for this
   server**, and retries (§7).
4. **A long job.** Later the model starts `export_all_orders`, which takes 20 minutes. The server
   returns a *task handle*; the client polls it, even after a restart (§4.5).
5. **Scale out.** Because 2026-07-28 requests carry everything the server needs, any of 12 server
   replicas can answer any request. No sticky sessions (§5.3).
6. **Control.** A gateway between the hosts and the servers checks who may call what, logs every
   call and can block a server that suddenly changes its tool descriptions (§8.3).

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| MCP host | the app the user talks to (chat app, IDE, agent runtime) | the laptop |
| MCP client | the connector inside the host, one per server | one USB port per device |
| MCP server | a program exposing tools, resources, prompts | the device (printer, camera) |
| Tool | an action the model may call | a printer's "print" button |
| Resource | read-only data addressed by a URI | a file the printer can show you |
| Prompt | a reusable, user-picked message template | a menu preset |
| Elicitation | the server asks the user a question | the printer asking "which tray?" |
| Streamable HTTP | the network transport: every message is one HTTP POST | sending letters one by one |
| stdio | the local transport: the host spawns the server as a child process | a cable directly to the device |
| Resource server | the OAuth word for "the API that checks tokens" | the door with a badge reader |
| Protected Resource Metadata | a JSON file at a well-known URL saying "my tokens come from there" | the sign on the door: "badges issued at reception" |
| Gateway | a proxy in front of many MCP servers enforcing policy | building security desk |
| Task | a durable handle for slow work, polled until done | a dry-cleaning ticket |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `N`, `M` | number of hosts / number of services | 5, 8 | 5×8 = 40 custom connectors vs 5+8 = 13 MCP pieces |
| `_meta` | extension bag on every message; in 2026-07-28 it carries version and capabilities | — | `io.modelcontextprotocol/protocolVersion: "2026-07-28"` |
| `resultType` | required on every result: `"complete"` or `"input_required"` (plus `"task"` under the Tasks extension) | `"complete"` | tells the client whether to retry with answers |
| `ttlMs` | server hint: how long a list/discover result stays fresh | 300000 (5 min) | like `Cache-Control: max-age` |
| `cacheScope` | `"public"` or `"private"` cache hint | `"public"` | public = a shared proxy may cache it |
| `inputSchema` / `outputSchema` | JSON Schema (2020-12 by default) for arguments / structured result | — | validate before and after the call |
| `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` | tool annotations; **untrusted hints** | defaults: false, true, false, true | a lying server can set `readOnlyHint: true` |
| `isError` | flag in a *successful* JSON-RPC result meaning the tool failed | `true`/`false` | "date must be in the future" |
| `-32020` … `-32022` | MCP-reserved error codes in 2026-07-28 | — | `-32022` UnsupportedProtocolVersion |
| `pollIntervalMs`, `ttlMs` (Tasks) | how often to poll a task / how long the server keeps it | server-chosen | poll every 2000 ms |
| `resource` (RFC 8707) | the canonical URI of the MCP server a token is requested for | `https://mcp.example.com` | token audience |

---

## 1. Why MCP exists: N×M, roles, governance

> **In plain words.** Before MCP every agent framework shipped its own "plugin" format, so a
> Jira integration written for one framework was useless in another. MCP fixes the *wire format*
> between "the thing with the model" and "the thing with the capability".
>
> **Real-world example.** A company has 4 internal agent products (support bot, IDE assistant,
> analyst notebook, ops copilot) and 10 internal systems. Hand-built: up to 4 × 10 = 40 adapters,
> each reviewed separately for security. With MCP: 10 servers plus 4 clients = 14 pieces, and the
> security review concentrates on the 10 servers and one gateway. (Arithmetic example, not a
> measurement.)

### 1.2 Roles: host, client, server

The spec's architecture (stable since the first revision; described from memory for 2024-11-05
and confirmed in the 2026-07-28 pages) has three roles:

- **Host** — the user-facing application (a chat app, an IDE, an agent runtime). It owns the
  model connection, the user interface and the **consent/policy decisions**.
- **Client** — a connector the host instantiates, one per server connection. It speaks the
  protocol. In practice "client" and "host" are often one process, which is why people conflate
  them, but the *security* decisions (which tool may run, whether a human confirms) belong to the
  host.
- **Server** — a program that exposes capabilities. It can be a local process (stdio) or a remote
  HTTP service.

The Tools page of 2026-07-28 says tools are *model-controlled* ("the language model can discover
and invoke tools automatically") but that "there SHOULD always be a human in the loop with the
ability to deny tool invocations", and that applications SHOULD show which tools are exposed and
present confirmation prompts. Those are host obligations; a server cannot enforce them.

### 1.3 Governance

MCP was introduced by Anthropic in November 2024. On **9 December 2025** Anthropic announced it was
donating MCP to the **Agentic AI Foundation (AAIF)**, a directed fund under the Linux Foundation
co-founded by Anthropic, Block and OpenAI, with Block's *goose* and OpenAI's *AGENTS.md* as the
other founding projects (Anthropic's announcement and press coverage agree; see Sources). The
reported arrangement is that the AAIF board handles budget and membership while each project, MCP
included, keeps control of its technical direction.

Inside the project, the 2025-11-25 changelog records a formalised governance structure
(SEP-932), Working Groups and Interest Groups (SEP-1302) and an SDK tiering system (SEP-1730);
the 2026-07-28 revision adds a **feature lifecycle and deprecation policy** (SEP-2596) with Active,
Deprecated and Removed states and a **minimum twelve-month deprecation window**. For you as an
integrator that last item matters: deprecated features (§4.7) keep working for at least a year,
so a migration plan is possible.

---

## 2. The wire protocol: JSON-RPC and the primitives

> **In plain words.** Everything is a JSON message with a `method`, `params` and an `id`; the
> reply has the same `id`. Servers offer three kinds of things; clients offer three kinds back.
>
> **Real-world example.** `tools/list` returns the menu; `tools/call` orders one dish;
> `resources/read` fetches a document; `prompts/get` fetches a saved template.

### 2.1 JSON-RPC base

MCP messages are JSON-RPC 2.0: a **request** has `id` and `method`; a **response** has either
`result` or `error`; a **notification** has `method` and no `id` and gets no reply. Error codes are
integers; the 2026-07-28 spec partitions the JSON-RPC server-error range: `-32000…-32019` stays
implementation-defined (legacy), and `-32020…-32099` is reserved for the MCP specification
(`HeaderMismatch` -32020, `MissingRequiredClientCapability` -32021, `UnsupportedProtocolVersion`
-32022). New application-specific codes SHOULD be allocated outside the JSON-RPC reserved range.

JSON-RPC **batching** was added in 2025-03-26 and removed again in 2025-06-18; do not rely on it.

### 2.2 Server primitives (what a server offers)

| Primitive | Controlled by | What it is | Methods (examples) |
|---|---|---|---|
| **Tools** | the model | functions with a JSON-Schema input (and optional output) | `tools/list`, `tools/call` |
| **Resources** | the application | data addressed by URI (files, rows, docs); templates; subscriptions | `resources/list`, `resources/read`, `resources/templates/list` |
| **Prompts** | the user | parameterised message templates a user picks (slash-command style) | `prompts/list`, `prompts/get` |

Content returned by tools and prompts can be text, image, audio, a `resource_link` or an embedded
resource. All list operations are **paginated** with opaque cursors (§6.5). Argument
auto-completion (`completions` capability, added 2025-03-26) is a small fourth utility.

### 2.3 Client primitives (what a client offers back)

| Primitive | Purpose | Status in 2026-07-28 |
|---|---|---|
| **Roots** | the client tells the server which filesystem/URI roots it may work in | **Deprecated** (SEP-2577) |
| **Sampling** | the server asks the *client's* model for a completion (since 2025-11-25 it can include `tools`/`toolChoice`) | **Deprecated** (SEP-2577) |
| **Elicitation** | the server asks the *user* for input: `form` mode (structured) or `url` mode (go to a URL, e.g. to finish OAuth; added 2025-11-25) | Active; now delivered through MRTR (§4.4) |

Elicitation was added in 2025-06-18. Logging (server → client log messages) is the third
deprecated feature of 2026-07-28 (§4.7).

---

## 3. Five revisions in one table

MCP versions are *dates*. This table is taken from the official per-revision changelogs (the
2024-11-05 row is from memory: it is the first revision and has no "changes since" page).

| Revision | What it introduced or changed |
|---|---|
| **2024-11-05** (from memory, not re-verified) | First public revision. JSON-RPC 2.0; stdio and HTTP+SSE transports; tools, resources, prompts; client roots and sampling; the `initialize` handshake with capability negotiation. |
| **2025-03-26** | OAuth 2.1-based **authorization framework**; **Streamable HTTP** replaces HTTP+SSE; JSON-RPC **batching** added; **tool annotations** (read-only vs destructive etc.); `message` on progress notifications; audio content; `completions` capability. |
| **2025-06-18** | Batching **removed**; **structured tool output** (`outputSchema`/`structuredContent`); MCP servers classified as **OAuth resource servers** with Protected Resource Metadata; clients MUST implement **Resource Indicators (RFC 8707)**; new security best-practices page; **elicitation**; **resource links** in tool results; `MCP-Protocol-Version` header required on later HTTP requests; `title` fields separate from `name`; `_meta` on more types. |
| **2025-11-25** | OpenID Connect Discovery for authorization-server discovery; icons; incremental scope consent via `WWW-Authenticate`; tool-name guidance; richer enums in elicitation; **URL-mode elicitation**; **tool calling in sampling**; **OAuth Client ID Metadata Documents** as a recommended registration mechanism; **experimental Tasks** in core; PRM discovery aligned with RFC 9728 (header optional, `.well-known` fallback); JSON Schema 2020-12 as default dialect; input-validation errors to be returned as *tool execution errors*; SSE polling by server-initiated disconnect; HTTP 403 for invalid `Origin`; formal governance, Working Groups, SDK tiers. |
| **2026-07-28** | **Stateless protocol**: no `initialize`, no `Mcp-Session-Id`; version/capabilities per request in `_meta`; `server/discover`; **Multi Round-Trip Requests** replace server-initiated requests; `subscriptions/listen` replaces the GET stream; SSE **resumability removed**; **Tasks moved to an extension** with `tasks/get`/`tasks/update`/`tasks/cancel`; required `resultType`; caching hints `ttlMs`/`cacheScope`; mandatory `Mcp-Method`/`Mcp-Name` headers and `x-mcp-header`; OTel trace context in `_meta`; deprecations of **Roots, Sampling, Logging, HTTP+SSE and DCR**; feature lifecycle policy. |

As of 2026-10-10 the repository's `draft` changelog is empty ("Changes since the most recent
release will accumulate here"), so 2026-07-28 is the latest released revision. Note the SEP text
for Tasks refers to the same release by its earlier working name "2026-06-30"; the published
revision is **2026-07-28**.

---

## 4. The 2026-07-28 revision: stateless core, MRTR, Tasks, Apps

The brief for this chapter said secondary blogs claim a stateless core, a Tasks extension, MCP
Apps and deprecations. All of those are confirmed in the official 2026-07-28 changelog and pages;
the exact mechanics below are from the spec, not from the blogs.

### 4.1 Sessions and the `initialize` handshake are gone

> **In plain words.** Until 2025-11-25, a connection began with a handshake, and over HTTP the
> server handed the client a session ID that every later request had to carry — so a load
> balancer had to send the client back to the same server. 2026-07-28 deletes both.
>
> **Real-world example.** Twelve replicas behind an ordinary round-robin load balancer; request 1
> hits replica 3, request 2 hits replica 9, and both just work.

Changelog items 1–2 (SEP-2567, SEP-2575): protocol-level sessions and the `Mcp-Session-Id` header
are removed from Streamable HTTP; `tools/list`, `resources/list`, `prompts/list` "no longer vary
per-connection"; the `initialize`/`notifications/initialized` handshake is removed. Every request
carries, in `params._meta`:

```json
{
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientInfo": {"name": "ExampleClient", "version": "1.0.0"},
  "io.modelcontextprotocol/clientCapabilities": {}
}
```

`protocolVersion` and `clientCapabilities` are required on every request; `clientInfo` SHOULD be
sent; servers SHOULD put `io.modelcontextprotocol/serverInfo` in each result's `_meta`. A version
the server does not implement gets HTTP 400 with `UnsupportedProtocolVersionError` (code -32022)
whose `data` lists `supported` versions and echoes `requested`; the client picks a mutual version
and retries. There is no negotiation round-trip.

**Where does state live now?** The spec's non-normative "Stateful Tools" section: "MCP has no
protocol-level session, so a server cannot rely on implicit per-connection state". A server that
needs a shopping cart, a browser context or a transaction returns an **explicit handle** from a
creation tool (`create_basket` → `{"basket_id": "bsk_a1b2c3"}`) and takes it as an ordinary
argument afterwards. Design rules from the spec: a handle is a *name*, not a capability — check
authorization against it on every call; keep handles opaque; state the retention policy in the
creation tool's description ("baskets expire after 24 hours of inactivity"); return a tool
execution error on an expired handle so the model can recreate it. The security best-practices
page lists *State Handle Hijacking* among its attacks; chapter 28 covers it.

### 4.2 `server/discover`

Servers MUST implement `server/discover`, which returns `supportedVersions`, `capabilities`,
`instructions` and `_meta.io.modelcontextprotocol/serverInfo`, with `ttlMs` and `cacheScope`. Calling
it is optional: a client may send any request inline and handle the version error. It is useful
for showing server information up front and as the **backward-compatibility probe on stdio**.

```json
{"jsonrpc":"2.0","id":"discover-1","result":{
  "resultType":"complete","supportedVersions":["2026-07-28"],
  "capabilities":{"tools":{},"resources":{}},
  "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"ExampleServer","version":"1.0.0"}},
  "ttlMs":3600000,"cacheScope":"public"}}
```

### 4.4 Multi Round-Trip Requests (MRTR) replace server-initiated requests

> **In plain words.** A server used to be able to interrupt a running request with a question
> ("which GitHub account?"). That needs the server to remember the half-finished request. Now the
> server *finishes* the response with "I need this answer", and the client re-sends the request
> with the answer attached. Any replica can handle the retry.
>
> **Real-world example.** `tools/call deploy` → result `input_required` asking for a confirmation →
> the user taps "yes" → the client re-calls `deploy` with `inputResponses` and the opaque
> `requestState` → the deployment runs.

From the MRTR page and tools page: servers "MUST send server-to-client requests (such as
`roots/list`, `sampling/createMessage`, or `elicitation/create`) using the MRTR pattern. The
previous pattern of server-initiated requests is no longer supported. This is a breaking change."

1. Client sends the request (id 1).
2. Server returns `{"resultType":"input_required","inputRequests":{...},"requestState":"<opaque>"}`.
   `inputRequests` is a map from server-chosen keys to request objects (an `elicitation/create`,
   `sampling/createMessage` or `roots/list`).
3. The client gathers answers and **retries the same request with a new JSON-RPC id**, adding
   `inputResponses` (same keys) and the `requestState` verbatim.
4. The server completes.

`requestState` is "an opaque string meaningful only to the server. Clients MUST NOT inspect, parse,
modify". That is the mechanism that removes shared storage: the server can encode what it needs in
`requestState` and recover it on whichever replica gets the retry. Because the client echoes it
back, **treat it as attacker-controlled**: sign it (HMAC) or encrypt it, bind it to the caller and
request, and give it an expiry — lab 14.1's extension (a) does this. Retries carrying `inputResponses` or
`requestState` MUST NOT be cached (caching page).

The same release removes `notifications/elicitation/complete` and the `elicitationId` field of
URL-mode elicitation: the client learns an out-of-band flow finished by retrying; servers needing
correlation put their own identifier in `requestState`.

### 4.5 Tasks: a durable handle for slow work (official extension)

2025-11-25 added *experimental* Tasks to the core protocol. 2026-07-28 moves them out to an
official extension, identifier `io.modelcontextprotocol/tasks` (SEP-2663, Final; repository
`modelcontextprotocol/ext-tasks`), and redesigns them. From the extension overview:

- **Negotiation.** The client includes `io.modelcontextprotocol/tasks` in its per-request
  `clientCapabilities.extensions`; the server advertises the same in `server/discover`. A server
  must never return a task to a client that did not declare support.
- **Server-directed.** The server decides per request whether to materialise a task; the client
  just handles whichever shape returns. A `tools/call` may answer with a `CreateTaskResult`
  (`resultType: "task"`) carrying `taskId`, status, `ttlMs` and `pollIntervalMs`. The task is
  durably created *before* the response is sent.
- **Methods.** `tasks/get` (poll; terminal states carry `result` or `error`), `tasks/update`
  (client supplies `inputResponses` for a task in `input_required`), `tasks/cancel` (cooperative —
  the server need not stop). The blocking `tasks/result` and `tasks/list` of the experimental
  version are removed.
- **Status values.** `working`, `input_required`, `completed`, `failed`, `cancelled`; the last
  three are terminal.

Design implication: a task ID is a durable, replica-independent handle, so tasks give you
resumability at the *operation* level now that stream resumability is gone (§5.4). The server must
store task state in a shared store (database/queue), not process memory. Persist task IDs in the
client so polling survives a restart.

### 4.6 Subscriptions, caching, headers, observability

- **`subscriptions/listen`** replaces the HTTP GET endpoint and `resources/subscribe`/`unsubscribe`
  (changelog item 4). It is one long-lived POST response stream; the client opts in to
  `toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resourceSubscriptions`; the
  server acknowledges and tags events with `io.modelcontextprotocol/subscriptionId`. Progress and
  message notifications stay on the response stream of their own request. `ping`,
  `logging/setLevel` and `notifications/roots/list_changed` are removed; log level is now a
  per-request `_meta` field `io.modelcontextprotocol/logLevel`.
- **Caching.** `server/discover`, `tools/list`, `prompts/list`, `resources/list`,
  `resources/templates/list` and `resources/read` results MUST carry `ttlMs` (>= 0; freshness hint,
  like `max-age`) and `cacheScope` (`public` or `private`, like `Cache-Control`). Clients should
  not treat TTL as a polling interval; if they poll they MUST apply jitter and backoff. Servers
  SHOULD return tools in a **deterministic order** so clients can cache and LLM prompt caches hit.
- **Headers.** Every Streamable HTTP POST MUST carry `MCP-Protocol-Version` (equal to the `_meta`
  value), `Mcp-Method` (the JSON-RPC method) and, for `tools/call`, `resources/read` and
  `prompts/get`, `Mcp-Name` (tool name or URI). Servers validate body/header agreement and reject
  mismatches with HTTP 400 and `HeaderMismatch` (-32020). Tool authors may mark primitive
  parameters with `x-mcp-header` so clients mirror them to `Mcp-Param-{Name}` headers for routing
  or WAF rules; never mark secrets or PII this way. Non-ASCII values use `=?base64?…?=`.
- **OpenTelemetry.** `traceparent`, `tracestate` and `baggage` in `_meta` carry W3C trace context
  (SEP-414); the spec links the OTel GenAI semantic conventions for MCP. See §9.3.
- Schemas accept any JSON Schema 2020-12 keyword; resource-not-found is now `-32602` (was `-32002`).

### 4.7 Deprecations (still working for at least twelve months)

| Deprecated | Migration advice from the changelog |
|---|---|
| **Roots** | pass directories/files via tool parameters, resource URIs or server configuration |
| **Sampling** (and `includeContext` values `thisServer`/`allServers`) | integrate directly with LLM provider APIs |
| **Logging** | log to `stderr` (stdio) or use OpenTelemetry |
| **HTTP+SSE transport** (deprecated since 2025-03-26, now formally reclassified) | use Streamable HTTP |
| **Dynamic Client Registration (RFC 7591)** | use Client ID Metadata Documents; DCR stays for servers that lack them |

Deprecated means "do not adopt in new work", not "gone". Note the tension: MRTR still lists
sampling and roots as things a server may request, but new servers should not.


### 4.8 MCP Apps (brief)

Official extension `io.modelcontextprotocol/ui` (`ext-apps`): a tool's `_meta.ui.resourceUri` points to a `ui://` HTML resource the host renders in a sandboxed iframe (CSP and permissions in `_meta.ui`), talking to the host over a `postMessage` JSON-RPC dialect. A gateway should treat `ui://` resources as code to review. Message formats not verified in depth.

---

## 5. Transports and scaling

> **In plain words.** Two ways to connect: spawn the server as a local child process and talk over
> its stdin/stdout (**stdio**), or call a remote server with HTTP POSTs (**Streamable HTTP**). The
> older HTTP+SSE transport is legacy.
>
> **Real-world example.** A developer's IDE runs a local filesystem server over stdio. The company's
> CRM server is a remote Streamable HTTP service behind the gateway, with OAuth.

### 5.1 stdio (brief)

The host spawns the server and exchanges JSON-RPC over stdin/stdout (from memory; the stdio page was not read in full). Stdout is the protocol channel, so a stray `print()` corrupts it; logs go to `stderr`. The spec says stdio servers should take credentials from the environment, not follow the HTTP auth flow. A stdio server runs with the user's privileges (chapter 28). `notifications/cancelled` is stdio-only; probe legacy servers with `server/discover`.

### 5.2 Streamable HTTP (2026-07-28 behaviour)

From the transport page:

1. One endpoint path (for example `https://example.com/mcp`) that accepts **POST**.
2. Every JSON-RPC request or notification is its **own POST**, with `Accept: application/json,
   text/event-stream`. The body is a single message; clients never send JSON-RPC responses.
3. For a request the server answers with `application/json` (one object) **or** `text/event-stream`
   (an SSE stream scoped to that request, carrying related `notifications/progress`/`message` and
   then the final response). Clients must support both.
4. The server MUST NOT send independent JSON-RPC *requests* on that stream (that was allowed in
   2025-03-26 to 2025-11-25); server-to-client interactions go through MRTR.
5. Servers SHOULD send `X-Accel-Buffering: no` on SSE responses so nginx-style proxies do not
   buffer events, and SHOULD emit periodic SSE comment lines (`:`) on long-lived streams.
6. **Security:** validate `Origin` (invalid → 403) against DNS rebinding; bind local servers to
   127.0.0.1; authenticate all connections.
7. Notifications get `202 Accepted`. An unknown method is `404` with JSON-RPC `-32601`, which
   distinguishes a modern server from a legacy HTTP+SSE server that simply has no such path.

HTTP+SSE (2024-11-05) used two endpoints (an SSE stream plus a POST endpoint); it was replaced in
2025-03-26 and formally Deprecated in 2026-07-28. Do not build on it.

### 5.3 Horizontal scaling

With sessions gone, an MCP server is an ordinary stateless HTTP service:

What still needs care:

- **`subscriptions/listen` is a long-lived connection.** It pins one TCP connection to one
  replica for its life. Budget connection counts per replica (clients × streams), set idle
  timeouts above the keep-alive interval, and expect clients to reopen on loss. Servers that
  declare no `listChanged` capability avoid this entirely, and for slowly changing catalogs the
  `ttlMs` hint is enough.
- **Shared state moves out of memory.** Task state, handle-backed state (baskets, transactions),
  idempotency records and rate-limit counters need a shared store (Redis/SQL). The server process
  should be disposable.
- **Autoscaling signal.** In-flight SSE requests, not just request rate, drive capacity; a tool
  call that streams progress for 60 s holds a worker for 60 s.
- **Rolling deploys.** Run dual-era (or at least two-version) servers during rollouts;
  `UnsupportedProtocolVersionError` tells clients which versions you accept.
- **Tool lists must not vary per connection** (spec). They may vary by *authorization presented on
  the request* — returning only the tools the caller's scopes allow — because credentials are
  per-request input. Mark such lists `cacheScope: "private"` so a shared cache does not leak the
  tool set of one user to another (an inference from the caching semantics; the spec defines the
  field, you apply it).

### 5.4 Resumability: what you lose and what replaces it

2025-11-25 allowed servers to disconnect at will and clients to resume SSE streams via
`Last-Event-ID` (SEP-1699). **2026-07-28 removes this entirely**: "A broken response stream loses
the in-flight request; clients MUST re-issue it as a new request with a new request ID." So for
anything that can outlive a connection, the answer is *idempotent operations plus Tasks*:

- Short, safe-to-repeat calls (`readOnlyHint` true, or idempotent by construction): just retry.
- Mutating calls: carry an application-level idempotency key as a tool argument and apply
  [chapter 24 §8](24-tool-calling-and-enterprise-integration.md#8-idempotency) on the server — MCP
  gives you no built-in exactly-once.
- Long calls: return a task so that a lost stream does not lose the work (§4.5).

---

## 6. Tool definitions that models and gateways can use

> **In plain words.** A tool definition is what the model reads to decide whether and how to call
> a tool, and what a gateway reads to decide whether it may. Good schemas improve tool selection
> (chapter 24 §3); declared output shapes let clients validate; annotations are hints that must
> never be treated as guarantees.
>
> **Real-world example.** A `delete_ticket` tool declares `destructiveHint: true`. A host that
> trusts the server shows a confirmation. A host that connects to an unknown server ignores the hint
> and applies its own policy — because a malicious server could declare `readOnlyHint: true` on
> `wipe_database`.

### 6.1 The Tool object (2026-07-28)

Fields from the Tools page: `name`, optional `title`, `description`, optional `icons`,
`inputSchema` (JSON Schema, MUST be an object schema, never `null`; default dialect 2020-12),
optional `outputSchema`, optional `annotations`. Name rules (SHOULD): 1–128 characters from
`A-Z a-z 0-9 _ - .`, case-sensitive, unique within a server. Aggregators that merge servers
**SHOULD** disambiguate collisions, for example by prefixing a server identifier — and the server's
self-reported `name` "SHOULD NOT be relied upon" for that.

For tools with no parameters use `{"type":"object","additionalProperties":false}` (recommended).

### 6.2 `outputSchema` and `structuredContent`

Since 2025-06-18 a tool may declare `outputSchema` and return `structuredContent`. The 2026-07-28
rules: if an output schema is provided, servers MUST produce conforming structured results and
clients SHOULD validate them; for backwards compatibility a tool returning structured content
SHOULD also put the serialised JSON in a text block. In 2026-07-28 `structuredContent` can be any
JSON value (arrays, scalars), and schemas may use any 2020-12 keyword (with `$ref` resolution
requirements and bounds on composition keywords). Note the spec's remark that `structuredContent`
"is unrelated to LLM structured outputs" — it is server-produced data, not constrained decoding.

```json
{"name":"get_order","description":"Fetch one order by id.",
 "inputSchema":{"type":"object","properties":{"id":{"type":"string","pattern":"^O-[0-9]+$"}},
                "required":["id"],"additionalProperties":false},
 "outputSchema":{"type":"object","properties":{"status":{"type":"string",
                 "enum":["pending","shipped","delivered"]}},"required":["status"]},
 "annotations":{"readOnlyHint":true,"openWorldHint":false}}
```

### 6.3 Annotations are untrusted hints

`ToolAnnotations` in the 2026-07-28 `schema.ts`:

| Field | Meaning | Default | Note |
|---|---|---|---|
| `title` | human-readable title | — | |
| `readOnlyHint` | tool does not modify its environment | `false` | |
| `destructiveHint` | may perform destructive updates (false = additive only) | `true` | meaningful only when `readOnlyHint == false` |
| `idempotentHint` | repeating with the same args has no additional effect | `false` | meaningful only when `readOnlyHint == false` |
| `openWorldHint` | interacts with an open world of external entities | `true` | web search is open; a memory tool is closed |

The Tools page is explicit: "clients MUST consider tool annotations to be untrusted unless they
come from trusted servers." Notice what the defaults mean for an *unannotated* tool: not read-only,
destructive, not idempotent, open-world — the most pessimistic reading, which is exactly what
chapter 24 §3.3 and the lab's `from_mcp_tool` default to. The consequence for design:

- **Treat hints as inputs to policy, never as policy.** A gateway may *tighten* behaviour based on
  a hint (a `destructiveHint: true` forces approval) but must not *loosen* it (a `readOnlyHint:
  true` from an unreviewed server does not grant auto-approval).
- **Pin trust to the server's identity, not its self-description.** Trusted = reviewed and
  registered (§8). Re-review when the tool list changes.
- **Hints are not authorization.** `idempotentHint: true` does not make retries safe if the server
  lies; do not skip your own idempotency keys on mutating calls.

### 6.4 `isError` versus protocol errors

The spec's Error Handling section defines two mechanisms:

| | Protocol error | Tool execution error |
|---|---|---|
| Shape | JSON-RPC `error` object (`code`, `message`) | normal `result` with `isError: true` and explanatory `content` |
| For | unknown tool, malformed request (fails `CallToolRequest` schema), server faults | API failures, **input validation errors**, business-logic errors |
| Audience | the client/host (models rarely recover) | the model (SHOULD be given to it for self-correction) |

Since 2025-11-25 (SEP-1303), "input validation errors should be returned as Tool Execution Errors
rather than Protocol Errors to enable model self-correction". Write error text for a model: say
what was wrong *and* what a valid call looks like ("Invalid departure date: must be in the future.
Current date is 08/08/2025."). Do not leak stack traces or internal hostnames into it.

### 6.5 Pagination and list-changed

Lists use **opaque cursor** pagination: the result may contain `nextCursor`; the client passes it
back as `params.cursor`; page size is the server's choice and clients MUST NOT assume one.
Cursors must stay valid across replicas (encode position + a signed version in the cursor; do not
store cursors in process memory). For list-changed: servers that declare `tools.listChanged`
SHOULD send `notifications/tools/list_changed` to clients that opted in via `subscriptions/listen`
with `toolsListChanged: true`; the client then re-fetches `tools/list`. A changed tool list is a
*security event* for a gateway (§8.3): the "rug pull" where a previously reviewed tool gains new
behaviour. Hash each tool definition and alert on drift.

---

## 7. Authorization: the OAuth resource-server model

> **In plain words.** A remote MCP server is an ordinary protected web API. It does not run
> logins itself: it says "go to *that* login server", the client signs the user in there and gets
> a token that works only for this server, and the server checks every token.
>
> **Real-world example.** The client calls `https://mcp.example.com/mcp`, gets `401` with a
> pointer to the server's metadata document, discovers the company's identity provider, runs the
> browser login, and retries with `Authorization: Bearer …`.

This section is the MCP-specific minimum. On-behalf-of delegation, token exchange, workload
identity and per-tool authorization depth are in
[chapter 29](29-agent-identity-and-delegated-authorization.md).

### 7.1 The model (2026-07-28 authorization pages)

- Authorization is **optional**. For HTTP transports implementations SHOULD follow the spec; for
  stdio they should not, and take credentials from the environment.
- The MCP server is an **OAuth 2.1 resource server** (spec cites draft-ietf-oauth-v2-1-13); the
  client is an OAuth 2.1 client; the **authorization server** is separate or co-hosted and out of
  scope. Resource-server classification arrived in 2025-06-18.
- Servers MUST implement **OAuth 2.0 Protected Resource Metadata (RFC 9728)** with at least one
  `authorization_servers` entry. Discovery: either `resource_metadata` in the `WWW-Authenticate`
  header of the `401`, or the well-known URI
  `/.well-known/oauth-protected-resource[/path]`; clients MUST support both (header first). The
  header became optional in 2025-11-25.
- Authorization servers MUST provide **RFC 8414** metadata and/or **OpenID Connect Discovery**;
  clients MUST support both and try the documented endpoint orders (path-insertion variants).
- **PKCE** is part of the OAuth 2.1 flow the client uses (the flow diagram shows
  `code_challenge`/`code_verifier`).
- **RFC 8707 resource indicator**: clients MUST send `resource` (the MCP server's canonical URI,
  e.g. `https://mcp.example.com/mcp`) on both authorization and token requests, whether or not the
  authorization server supports it. Servers MUST validate that the token was **issued for them as
  the audience**.
- **No token passthrough.** "MCP servers MUST NOT accept or transit any other tokens"; clients
  MUST NOT send tokens to a server other than ones issued by that server's authorization server.
  If your server calls a downstream API, it uses its **own** credential for that API (or an
  OAuth token exchange), never the client's token — see the spec's *Token Passthrough* and
  *Confused Deputy* sections.
- Tokens go in the `Authorization` header on every request, never in the query string.
- **Scopes:** servers SHOULD put `scope` in the `WWW-Authenticate` challenge; clients follow
  least privilege. Insufficient scope at runtime → `403` with `error="insufficient_scope"`, the
  needed `scope`, and `resource_metadata` (step-up authorization); include all scopes needed for
  the operation in one challenge.
- **Issuer checks (2026-07-28):** authorization servers SHOULD return `iss` (RFC 9207); clients
  MUST validate a present `iss` against the recorded issuer *before* redeeming the code; client
  credentials are bound to the issuer and MUST NOT be reused with another authorization server.

Status codes: `401` authorization required or invalid token, `403` invalid scopes/insufficient
permissions, `400` malformed request.

### 7.2 Client registration: CIMD over DCR

An MCP client and server usually have no prior relationship, so how does the authorization server
know the client? The spec's priority order for clients: (1) pre-registered credentials,
(2) **Client ID Metadata Documents** if the AS advertises `client_id_metadata_document_supported`,
(3) **Dynamic Client Registration** if the AS has a `registration_endpoint`, (4) ask the user.

- **CIMD** (draft-ietf-oauth-client-id-metadata-document-00): the `client_id` is an HTTPS URL with
  a path; fetching it returns JSON with at least `client_id` (must equal the URL), `client_name`
  and `redirect_uris`. The AS fetches and validates it, MUST check redirect URIs against it, and
  SHOULD cache per HTTP headers. Advantage: no registration endpoint to run, no unbounded client
  table. Cost: the AS makes outbound fetches of attacker-influenced URLs (an SSRF surface — the
  spec's security considerations cover it) and must decide which client domains to trust.
- **DCR (RFC 7591)** is **deprecated** in 2026-07-28 but retained for compatibility; clients using
  it must set an appropriate `application_type` to avoid OpenID Connect redirect-URI conflicts.

### 7.3 What a minimal compliant resource server does

```python
# Sketch: the checks an MCP resource server performs on each HTTP request.
import base64, json

CANONICAL = "https://mcp.example.com/mcp"          # == the 'resource' clients send
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"

def challenge(scope: str | None = None, error: str | None = None) -> tuple[int, dict]:
    parts = [f'Bearer resource_metadata="{PRM_URL}"']
    if error: parts.append(f'error="{error}"')
    if scope: parts.append(f'scope="{scope}"')
    return (403 if error == "insufficient_scope" else 401,
            {"WWW-Authenticate": ", ".join(parts)})

def authorize(headers: dict, claims_of, needed_scope: str):
    """claims_of(token)->dict verifies signature/expiry via your JWKS or introspection."""
    h = headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return challenge(scope=needed_scope)
    claims = claims_of(h[7:])                       # raises on bad signature/expiry
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if CANONICAL not in auds:                       # RFC 8707 audience check; no passthrough
        return challenge(scope=needed_scope)
    if needed_scope not in claims.get("scope", "").split():
        return challenge(scope=needed_scope, error="insufficient_scope")
    return None                                     # authorized
```

### 7.4 Authorization is per request, not per session

Because sessions are gone, authorization is re-evaluated on every request. That is a feature:
revocation takes effect at the next call, and `tools/list` may legitimately differ by token. The
authorization of a *tool call* (may this user delete that ticket?) is still your application's job
— OAuth scopes only say what the client may ask of the server. That is chapter 24 §6 verbatim.

---

## 8. Registry, discovery and the MCP gateway

### 8.1 Three kinds of "discovery"

| Question | Mechanism |
|---|---|
| What *can* I install/connect to? | **MCP Registry** (public metadata) or your private catalog |
| What does *this* server support? | `server/discover` (versions, capabilities, identity) |
| What tools does it expose now? | `tools/list` (+ `ttlMs`, `listChanged`) |
| Who issues its tokens? | Protected Resource Metadata (§7) |

### 8.2 The MCP Registry

The official **MCP Registry** (in preview at the time of the doc) is "the official centralized
metadata repository for publicly accessible MCP servers". It stores `server.json` metadata —
a reverse-DNS name such as `io.github.user/server-name`, where the server lives (an npm/PyPI/Docker
package or a remote URL), run instructions and descriptions — and verifies namespaces by GitHub
account or DNS. Important limits from its documentation:

- It hosts **metadata, not code**; package registries host the code and do the scanning.
- It does **not** support private servers (internal hostnames or private package registries).
- It is "intended to be consumed primarily by downstream aggregators" (marketplaces), not
  directly by host applications, and the codebase "is not designed for self-hosting". It defines an
  OpenAPI spec that other (including private) registries can implement so hosts can consume them
  uniformly.

So an enterprise needs its **own** registry, and that is exactly the registry of
[chapter 24 §5](24-tool-calling-and-enterprise-integration.md#5-tool-registries) with an MCP
face: each registry entry gets an owner, a pinned version, reviewed annotations, allowed scopes,
and a hash of the approved tool definitions. Public registry entries are *candidates* feeding an
intake workflow (the lab's `from_mcp_tool` default-restrictive ingestion), never an allowlist.

### 8.3 The MCP gateway as policy enforcement point

The gateway applies the same pattern as the model gateway
([chapter 23 §2](23-multi-llm-model-gateway.md#2-gateway-architecture-and-the-request-lifecycle)):
authenticate, authorize, normalise, rate-limit, route, break circuits, audit. For MCP it adds:

1. **Identity and policy per tool.** Map user/agent identity → allowed servers → allowed tools →
   allowed argument shapes. OAuth scopes are coarse; policy lives here (and in chapter 24's
   registry scoping).
2. **Tool-list filtering and pinning.** Return only approved tools; namespace-prefix them per
   server to avoid the collisions the spec warns of; **hash definitions** and block or alert on
   drift (rug pulls) after any `list_changed` or TTL expiry.
3. **Annotation policy.** Ignore unreviewed `readOnlyHint`; enforce approval for destructive or
   open-world tools by registry-held metadata.
4. **Credential brokering.** The gateway terminates the client's token (audience = gateway),
   then calls upstream servers with tokens minted for *them* (token exchange / own credentials).
   This is the architecture that honours "no token passthrough" across hops.
5. **Cross-server taint control.** Sessions that touched untrusted content should not reach
   exfiltration-capable tools: the Rule-of-Two / CaMeL material of
   [chapter 17](17-safety-guardrails-and-prompt-injection.md) is enforced in the gateway because
   no single MCP server sees the whole agent run.
6. **Audit, OTel, resilience.** One structured record per call, `traceparent` propagation (§9.3), per-server timeouts, retry budgets (read-only or keyed-idempotent calls only) and circuit breakers.

Caveat: a gateway that terminates TLS and sees every payload is a high-value target and a single
point of failure; run it as a horizontally scaled stateless tier (which 2026-07-28 now makes
natural), keep its policy as versioned config, and fail closed.

---

## 9. Production design

### 9.1 Server design checklist

| # | Check | Why / where |
|---|---|---|
| 1 | One purpose per server; < ~20 tools; deterministic `tools/list` order | context cost, cache hits (§6.3) |
| 2 | `inputSchema` strict (`additionalProperties: false`, enums, patterns, bounds) and validated server-side | schemas are not enforcement; chapter 24 §7 |
| 3 | `outputSchema` + `structuredContent` for every data-returning tool, with a text copy | typed clients, validation (§6.2) |
| 4 | Honest annotations, set explicitly (do not rely on defaults) — and assume nobody trusts them | §6.3 |
| 5 | Validation failures → `isError: true` with a fix hint; unknown tool/malformed → protocol error | §6.4 |
| 6 | Stateless handlers; any state behind explicit, authorized, expiring handles | §4.1 |
| 7 | Mutating tools take an idempotency key and dedupe server-side | no resume in 2026-07-28 (§5.4) |
| 8 | Work longer than a few seconds returns a task; task state in a shared store | §4.5 |
| 9 | OAuth resource server: PRM, audience check, no passthrough, least-privilege scopes, `403 insufficient_scope` | §7 |
| 10 | Authorization on the *resource* inside each tool (does this user own this order?), not just scopes | §7.4 |
| 11 | `Origin` validation, localhost binding for local servers, TLS and HTTP rate limits | §5.2 |
| 15 | Conformance tests against the SDK for the revisions you claim; contract tests on schemas | §9.2 |

### 9.2 Versioning and errors

Version axes are independent: protocol revision (per request), your tool contract (semver in the registry; renaming a tool or tightening an input is breaking, so publish `get_order_v2`), and extensions. Report `supportedVersions` truthfully, keep the previous revision through the twelve-month deprecation window, and check your SDK's tier (SEP-1730) before promising a revision.

Errors: downstream 4xx become `isError` results the model can act on; timeouts become retryable messages; use `-32602` for invalid params, unknown tool and missing resources; HTTP 400 with `-32020/-32021/-32022` for header, capability and version problems; `401`/`403` for auth. Stop work promptly when the stream closes.

### 9.3 Observability

2026-07-28 standardises trace propagation: put W3C `traceparent` (and optionally `tracestate`,
`baggage`) in the request `_meta`; the spec points to the OpenTelemetry semantic conventions for
MCP (opentelemetry.io/docs/specs/semconv/gen-ai/mcp/ — not read in detail here). Practical set:

- A **client span** in the host around each `tools/call` and a **server span** continuing the
  parent from `_meta.traceparent`; attributes: method, tool name, server name/version, protocol
  version, outcome (`isError`, error code), result bytes. Never put raw arguments or results in
  span attributes without redaction.
- **RED metrics** per tool (rate, errors split protocol/tool, duration), SSE stream concurrency,
  `subscriptions/listen` connection count, task counts by status and task age.
- **Logging:** the protocol's logging feature is deprecated; use stderr/stdout structured logs and
  OTel instead. The agent-trace model is in
  [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
  and [chapter 24 §13](24-tool-calling-and-enterprise-integration.md#13-observability-for-tool-calls).

### 9.4 Multi-tenancy

Tenant identity comes from validated token claims, never from a tool argument. With no sessions, **handles, caches and task IDs** are the leak surface: namespace storage keys by tenant, check the caller against the handle on every call, make IDs unguessable, mark caller-dependent lists `cacheScope: "private"`, and enforce per-tenant quotas and tool allowlists at the gateway ([chapter 24 §5.4](24-tool-calling-and-enterprise-integration.md#54-scoping-per-agent-user-and-tenant)). Shared servers are fine if every data access is tenant-scoped in code; isolate deployments for regulated data.

### 9.5 Threats: pointer only

MCP-specific attacks (tool poisoning and rug pulls, prompt injection through tool results, confused
deputy in OAuth proxies, SSRF during discovery, local server compromise, mix-up attacks, name
squatting) are catalogued in
[chapter 28](28-agentic-security-owasp-and-mcp-threats.md); the spec's own security best-practices
page (`docs/2026-07-28/tutorials/security/security_best_practices`) lists Confused Deputy, Token
Passthrough, SSRF, State Handle Hijacking, Local MCP Server Compromise, OAuth Authorization URL
Validation, stdio Transport Security in Proxy Scenarios and Mix-Up Attacks. The design rule that
survives all of them: a tool result is untrusted data
([chapter 17](17-safety-guardrails-and-prompt-injection.md)), and no MCP annotation changes that.

---

## 10. Correcting `labs/tool-registry/mcp.py`

[`labs/tool-registry/mcp.py`](labs/tool-registry/mcp.py) was written against an older mental model
of MCP. Its docstring says: "MCP has no first-class output/error schema or annotations field",
that a tool has only "name, description, inputSchema", and that "MCP has no standardized
authz/credop model". Against the specification as of 2026-07-28 (and mostly already by 2025-06-18):

| Lab claim | What the spec has | Since |
|---|---|---|
| "MCP has no annotations field" | `annotations` with `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` | **2025-03-26** (changelog: "comprehensive tool annotations") |
| "no first-class output schema" | optional `outputSchema` + `structuredContent` (client SHOULD validate; server MUST conform) | **2025-06-18**; loosened to any 2020-12 keywords/any JSON value in 2026-07-28 |
| "no error schema" | **Partly true.** There is no per-tool *error* schema: failures are `isError: true` results with free-form `content`, or JSON-RPC errors. You can model error variants inside `outputSchema` yourself. | — |
| "no standardized authz model" | OAuth 2.1 framework; resource-server role, PRM (RFC 9728), RFC 8707 audience binding, CIMD, step-up scopes | framework **2025-03-26**; resource server + RFC 8707 **2025-06-18**; CIMD **2025-11-25** |
| (implicit) description is the only place for hints | `title`, `icons`, tool-name rules, `x-mcp-header`, `_meta` | 2025-06-18 onward |

What *is* still correct about the lab's design, and should be kept:

- The platform definition remains a **superset**: MCP has no field for `requires_approval`,
  `long_running`, execution timeouts/retry, resource limits, credential references, owner/on-call
  or cost centre. (`long_running` maps conceptually to the Tasks *extension*, not an annotation.)
- The platform should still **not delegate authorization or credential injection to the server**.
  The reason is different from "MCP has no authz": MCP's OAuth authenticates a *client* to a
  *server* and bounds coarse scopes; it does not decide whether *this agent run* may call
  *that tool with those arguments for this user*, and it forbids passing the client's token on to
  downstream APIs. Policy and credential brokering belong in the platform/gateway.
- `from_mcp_tool` defaulting to most-restrictive annotations is exactly right, because the spec
  says annotations from untrusted servers MUST be treated as untrusted. It should additionally
  ingest `outputSchema` and *record* the server's claimed hints for the reviewer.

### 10.1 A corrected projection sketch

Verified against the lab's models (`ToolDefinition`, `Annotation`, `ToolSpec`) by running it:

```python
# Replacement sketch for labs/tool-registry/mcp.py (do not edit the lab file from this chapter).
from typing import Any
from models import Annotation, ToolDefinition, ToolMetadata, ToolSpec

def to_mcp_tool(d: ToolDefinition) -> dict[str, Any]:
    """Project to an MCP 2026-07-28 Tool object, emitting real annotations and outputSchema."""
    a, s, m = d.spec.annotations, d.spec, d.metadata
    tool: dict[str, Any] = {
        "name": f"{m.namespace}.{m.name}",            # '.' is permitted in MCP tool names
        "title": m.name.replace("_", " ").title(),
        "description": s.description,
        "inputSchema": s.input_schema or {"type": "object", "additionalProperties": False},
        "annotations": {
            "readOnlyHint": a.read_only,
            # destructive/idempotent hints are only meaningful when readOnlyHint is false
            **({} if a.read_only else {"destructiveHint": a.destructive,
                                       "idempotentHint": a.idempotent}),
            # heuristic: empty egress allowlist == closed world
            "openWorldHint": bool(s.execution.resource_limits.network_egress),
        },
    }
    if s.output_schema:
        tool["outputSchema"] = s.output_schema        # server MUST then return structuredContent
    # requires_approval / long_running / execution / credentials / owner: no MCP field.
    # Keep them platform-side; optionally advertise approval in the description for old clients.
    return tool

def from_mcp_tool(t: dict[str, Any], *, owner_team="unassigned", version="1.0.0") -> ToolDefinition:
    ns, _, name = t["name"].rpartition(".")
    claimed = t.get("annotations") or {}              # record for the reviewer, do not apply
    return ToolDefinition(
        metadata=ToolMetadata(name=name, namespace=ns or "external", owner_team=owner_team,
                              tags=("mcp-imported",)),
        spec=ToolSpec(
            description=t.get("description", ""),
            # untrusted: stay at the most restrictive defaults until a human relaxes them
            annotations=Annotation(read_only=False, idempotent=False,
                                   destructive=True, requires_approval=True),
            input_schema=t.get("inputSchema", {}),
            output_schema=t.get("outputSchema", {}),  # ingest it: clients should validate results
        ),
        version=version)
```


Update `mcp_round_trip_report` so annotations and `output_schema` are *preserved on the way out* and only the server's claims are *discarded on the way in*, by design; store the `claimed` hints beside the reviewed values so a later diff exposes a server that starts lying.

---

## 11. MCP, A2A and AGENTS.md

Three different seams, often confused: **MCP** connects an agent/host to *tools and context* (the
agent is the caller; the server is a capability). **A2A** (Agent2Agent) connects an agent to
*another autonomous agent* that has its own reasoning, long-running tasks and opaque internals;
chapter [27](27-a2a-and-agent-interoperability.md) covers it. **AGENTS.md** is not a wire protocol
at all: a Markdown file of instructions for coding agents in a repository. It joined the AAIF at
launch alongside MCP (§1.3). Rule of thumb: expose a *function* with MCP; delegate a *goal* to
another agent with A2A; tell a coding agent *how your repo works* with AGENTS.md. An agent can be
both: an A2A server whose implementation uses MCP clients to reach its tools. MCP's Tasks extension
(durable handles, `input_required`) and A2A's task model overlap in shape; do not assume they
interoperate (not verified).

---

## 12. Anti-patterns

1. **Trusting annotations.** Auto-approving because a server said `readOnlyHint: true`.
2. **Token passthrough.** Forwarding the client's token to a downstream API (forbidden by the spec).
3. **Wrapping a REST API one-to-one** into 200 tools.
4. **Session-style design on a stateless protocol:** in-memory per-connection state, sticky routing
   — breaks on 2026-07-28 and on any restart.
5. **Opaque state not signed:** an unsigned `requestState` or guessable handle lets a client forge
   server state.
6. **Validation failures as protocol errors**, so the model never sees the fix hint.
7. **No tool-definition pinning:** a server edits a description after review (rug pull).
8. **Installing unknown stdio servers** with the user's full privileges.
9. **Adopting deprecated features** (Sampling, Roots, Logging, DCR, HTTP+SSE) in new code.
10. **Secrets in `x-mcp-header` parameters**, visible to every proxy.
11. **Treating the public Registry as an allowlist.**

---

## 13. Interview questions

**1. What problem does MCP solve and what does it not?** It converts N×M bespoke integrations into
N+M by standardising discovery and invocation of tools/resources/prompts. It does not provide
authorization of individual actions, exactly-once semantics, output trustworthiness or safety
against prompt injection.

**2. Walk through what changed in 2026-07-28 for scaling.** No `initialize` and no `Mcp-Session-Id`;
version and capabilities in each request's `_meta`; GET stream replaced by `subscriptions/listen`;
server-initiated requests replaced by MRTR; SSE resumability removed; required `Mcp-Method`/
`Mcp-Name` headers; `ttlMs`/`cacheScope`. Any replica can serve any request; state moves to
explicit handles, signed `requestState`, task stores.

**3. A client's SSE stream drops mid tool call in 2026-07-28. What now?** The in-flight request is
lost; the client re-issues it with a new id. Safe if read-only/idempotent; otherwise rely on an
application idempotency key, or have the server return a task so the work survives the stream.

**4. How should a gateway treat `destructiveHint`?** As a tightening-only input. Enforce approval
from reviewed registry metadata; a hint can add friction but never remove it, because the spec says
annotations from untrusted servers MUST be treated as untrusted.

**5. Explain the audience check and why passthrough is forbidden.** The client sends `resource` (RFC
8707) so the token's `aud` is the MCP server; the server rejects tokens not issued to it. If it
accepted and forwarded foreign tokens, downstream APIs would trust a token validated by no one for
them (confused deputy) and rate limits/audit tied to audience would be bypassed.

**6. CIMD vs DCR?** CIMD: `client_id` is an HTTPS URL to a metadata document the AS fetches and
validates; no registration endpoint or client table; AS must guard its fetch. DCR (RFC 7591)
creates clients on demand, grows state and is deprecated in 2026-07-28.


---

## 14. Lab exercises

All use Python stdlib only (3.10+). Scratch code lives in your own directory; the lab files under
`labs/tool-registry/` are read-only references.

### 14.1 A stateless server in 80 lines

Run this; it serves `server/discover`, `tools/list`, `tools/call` on a Streamable-HTTP-style POST
endpoint, enforces `_meta` version, header/body agreement (`-32020`) and unsupported version
(`-32022`), returns validation failures as `isError`, and has HMAC helpers for `requestState`.

```python
import hashlib, hmac, json, base64, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION, SECRET = "2026-07-28", b"demo-key-rotate-me"
TOOLS = [{"name": "get_order", "description": "Fetch an order by id.",
          "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                          "required": ["id"], "additionalProperties": False},
          "outputSchema": {"type": "object", "properties": {"status": {"type": "string"}},
                           "required": ["status"]},
          "annotations": {"readOnlyHint": True, "openWorldHint": False}}]

def sign(state):
    b = base64.urlsafe_b64encode(json.dumps(state, sort_keys=True).encode()).decode()
    return b + "." + hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()

def verify(tok):
    b, _, mac = tok.rpartition(".")
    if not hmac.compare_digest(mac, hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()):
        raise ValueError("bad requestState")
    return json.loads(base64.urlsafe_b64decode(b))

def err(i, code, msg, data=None):
    return {"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": msg, **({"data": data} if data else {})}}

def ok(i, **r):
    return {"jsonrpc": "2.0", "id": i, "result": {"resultType": "complete", **r}}

def handle(msg, h_ver, h_method):
    i, method, p = msg.get("id"), msg.get("method"), msg.get("params", {})
    ver = p.get("_meta", {}).get("io.modelcontextprotocol/protocolVersion")
    if ver is None: return 400, err(i, -32602, "missing _meta protocolVersion")
    if ver != h_ver or method != h_method: return 400, err(i, -32020, "HeaderMismatch")
    if ver != VERSION: return 400, err(i, -32022, "Unsupported protocol version",
                                       {"supported": [VERSION], "requested": ver})
    if method == "server/discover":
        return 200, ok(i, supportedVersions=[VERSION], capabilities={"tools": {}}, ttlMs=3600000, cacheScope="public")
    if method == "tools/list":
        return 200, ok(i, tools=TOOLS, ttlMs=300000, cacheScope="public")
    if method == "tools/call":
        if p.get("name") != "get_order": return 200, err(i, -32602, f"Unknown tool: {p.get('name')}")
        if not isinstance(p.get("arguments", {}).get("id"), str):
            return 200, ok(i, isError=True, content=[{"type": "text", "text": "id must be a string like 'O-5512'"}])
        out = {"status": "shipped"}
        return 200, ok(i, content=[{"type": "text", "text": json.dumps(out)}], structuredContent=out)
    return 404, err(i, -32601, "Method not found")

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        code, body = handle(msg, self.headers.get("MCP-Protocol-Version", ""), self.headers.get("Mcp-Method", ""))
        raw = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
def call(method, params=None, version=VERSION, hdr_method=None):
    p = dict(params or {}); p["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                                          "io.modelcontextprotocol/clientCapabilities": {}}
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/mcp",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": p}).encode(),
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": version, "Mcp-Method": hdr_method or method})
    try: return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e: return json.load(e)

print(call("tools/call", {"name": "get_order", "arguments": {"id": "O-1"}})["result"]["structuredContent"])
print(call("tools/call", {"name": "get_order", "arguments": {"id": 7}})["result"]["isError"])   # True
print(call("tools/list", hdr_method="tools/call")["error"]["code"])                            # -32020
print(call("tools/list", version="2025-11-25")["error"]["code"])                               # -32022
print(verify(sign({"step": 2})))
```

Expected output: `{'status': 'shipped'}`, `True`, `-32020`, `-32022`, `{'step': 2}` (verified by running).
**Extensions:** (a) add an `input_required` result for a `delete_order` tool carrying a signed
`requestState` with expiry and the caller's subject, and reject a tampered or replayed retry;
(b) run two instances behind a round-robin client and show nothing breaks; (c) add an
`Origin` check returning 403.

### 14.2 Fix the projection

Copy §10.1 into a scratch module beside `labs/tool-registry/`, build a `ToolDefinition` with
`read_only=True` and an `output_schema`, and assert: the projected dict has `annotations.readOnlyHint
is True`, no `destructiveHint`, and an `outputSchema`; the ingested tool has `destructive=True`,
`requires_approval=True` and the same `output_schema`.

### 14.3 Audience check

Write a validator for §7.3's `authorize` using hand-built claim dicts; cases: right audience,
wrong audience, audience list containing it, missing scope (403), no header (401 + `resource_metadata`).

---

## 15. Real-world cases

These are **Illustrative scenarios (composite, not a specific company)**. All numbers are
arithmetic examples, not measurements.

### Case 1 — The helpful `readOnlyHint` (illustrative)

**Setup.** A host auto-approves tools annotated `readOnlyHint: true`. An employee installs a
third-party "notes" server. **Symptom.** After an update, `append_note` is still annotated
read-only but now POSTs note contents to an external URL. **Diagnosis.** Hints were policy, not
input; there was no definition pinning, and `openWorldHint` was ignored. **Fix.** Registry-held
classifications, definition hashing with block-on-drift, egress allowlist at the gateway.
**Lesson.** Annotations are untrusted by specification.

---

## Sources

All fetched 2026-10-10 unless marked (from memory).

- MCP specification repository (sources of modelcontextprotocol.io): https://github.com/modelcontextprotocol/modelcontextprotocol
- Changelogs: `docs/specification/2025-03-26/changelog.mdx`, `2025-06-18`, `2025-11-25`, `2026-07-28` in that repository (rendered at https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- 2026-07-28 pages: server/tools, basic/transports/streamable-http, basic/patterns/mrtr, basic/authorization (index, authorization-server-discovery, client-registration), basic/versioning, server/discover, server/utilities/caching and pagination, basic/index (same repository)
- `schema/2026-07-28/schema.ts` (ToolAnnotations): https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/schema
- Security best practices: `docs/docs/2026-07-28/tutorials/security/security_best_practices.mdx` (same repository)
- Extensions: overview, Tasks (`extensions/tasks/overview`), MCP Apps (`extensions/apps/overview`), Enterprise-Managed Authorization; SEP-2663: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2663-tasks-extension.md
- MCP Registry docs (`docs/registry/about.mdx`), same repository; https://github.com/modelcontextprotocol/registry
- Anthropic, "Donating the Model Context Protocol and establishing the Agentic AI Foundation": https://www.anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation
- RFC 9728 (Protected Resource Metadata): https://datatracker.ietf.org/doc/html/rfc9728 ; RFC 8707: https://www.rfc-editor.org/rfc/rfc8707.html ; RFC 8414: https://datatracker.ietf.org/doc/html/rfc8414 ; RFC 7591: https://datatracker.ietf.org/doc/html/rfc7591 ; RFC 9207: https://datatracker.ietf.org/doc/html/rfc9207 ; Client ID Metadata Document draft: https://datatracker.ietf.org/doc/html/draft-ietf-oauth-client-id-metadata-document-00
- OpenTelemetry semantic conventions for MCP (linked from the spec, not read): https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/
