# 14 — Agent identity: user × agent, sub-agents, A2A, registries and gateways

> **Prerequisites:** [`07-oauth-hardening-and-advanced-specs.md`](07-oauth-hardening-and-advanced-specs.md) (§7 token
> exchange, §6 RAR), [`11-authorization-models-and-policy-engines.md`](11-authorization-models-and-policy-engines.md)
> (§10 the intersection), [`12-workload-and-service-identity.md`](12-workload-and-service-identity.md) (agent runtimes are
> workloads), [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md).
> Companion deep dive: [`ai-rag/29-agent-identity-and-delegated-authorization.md`](../ai-rag/29-agent-identity-and-delegated-authorization.md)
> (delegation RFC details, capability tokens, IETF agent drafts). Gateway designs:
> [`solutions/llm-gateway-design.md`](../solutions/llm-gateway-design.md), [`solutions/tool-platform-design.md`](../solutions/tool-platform-design.md).
>
> **Feeds into:** [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md).
>
> **THESIS:** an agent action has at least three identities behind it: **the human who authorized it, the agent that
> decided it, and the runtime that executed it**, plus often a chain of sub-agents in between. If your tokens and logs
> carry only one of them (usually a shared service key, sometimes the user), you can't give *the same user* different
> rights in *different agents*, can't stop a sub-agent from exceeding its parent, and can't answer "who did this?". The
> fix is a data model and a token shape, not a new protocol: **register every agent as a principal with a permission
> ceiling; mint per-action tokens whose subject is the user and whose actor is the specific agent; compute access as the
> intersection of user rights, agent ceiling, user's grant to that agent, and the task's needs; attenuate at every
> delegation hop; and put registries and gateways where those rules are enforced, outside the model's reach.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The principals behind one agent action](#1-the-principals-behind-one-agent-action)
2. [User × agent: same user, different agents, different access](#2-user--agent-same-user-different-agents-different-access)
3. [Getting delegated tokens for an agent](#3-getting-delegated-tokens-for-an-agent)
4. [Sub-agents: internal delegation and attenuation](#4-sub-agents-internal-delegation-and-attenuation)
5. [A2A: agents calling other agents across trust boundaries](#5-a2a-agents-calling-other-agents-across-trust-boundaries)
6. [Registries: agents, tools and MCP servers as governed inventory](#6-registries-agents-tools-and-mcp-servers-as-governed-inventory)
7. [Gateways: AI gateway, MCP gateway, agent gateway](#7-gateways-ai-gateway-mcp-gateway-agent-gateway)
8. [Reference architecture, end to end](#8-reference-architecture-end-to-end)
9. [Audit, kill switches and revocation](#9-audit-kill-switches-and-revocation)
10. [Code: exchange policy, user × agent PDP, attenuation check](#10-code-exchange-policy-user--agent-pdp-attenuation-check)
11. [Anti-patterns](#11-anti-patterns)
12. [Interview questions](#12-interview-questions)
13. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Alice uses three AI agents at work: a **calendar assistant**, a **finance agent** and a **coding agent**. All three act
for Alice. But Alice wouldn't want the calendar assistant to approve invoices, even though Alice herself can. So access
can't just be "whatever Alice can do". It has to be "what Alice can do **and** what this particular agent is allowed to
do **and** what Alice agreed to let this agent do".

To make that work:

1. Each agent is **registered** like an employee badge: an ID, an owner team, a list of things it may ever do.
2. When an agent acts, it gets a short-lived token that says **both** names: "for Alice (`sub`), by the finance agent
   (`act`)". APIs check both.
3. When the finance agent hands part of the job to a helper (**sub-agent**), the helper gets a token that can only do
   **less**, never more.
4. When an agent talks to another company's agent (**A2A**), it proves who it is and carries a narrowed token for Alice.
5. **Gateways** sit in front of models and tools: they check tokens, look up the registries, apply policy, hold the real
   secrets, and log everything. The model itself never holds a credential.

---

## 1. The principals behind one agent action

| Principal | Example | Identified by | Why it matters |
|---|---|---|---|
| **Human (resource owner)** | Alice | `sub` from the IdP | Her own permissions bound everything; she consents |
| **Host application / client** | "Acme Assistant" web app, an IDE, a desktop app | OAuth `client_id` (or CIMD URL) | Which product is asking; consent is per client |
| **Agent definition** | `agent:finance-reconciler@v7` | Agent registry ID + version | Permission ceiling, owner, risk tier |
| **Agent instance / run** | Run `r-8842` started 09:14 for task "reconcile October" | Run ID, ephemeral key | Attribution, per-run limits, kill one run |
| **Sub-agent** | `agent:ledger-reader` spawned by the run | Its own registry ID + parent run | Must be ≤ parent |
| **Runtime workload** | Pod `agents/finance-runner` | SPIFFE ID / cloud identity (ch 12) | Proves the code runs where you think, authenticates to the AS |
| **Tool / MCP server** | `mcp:erp` | Registry ID, server URL, audience | Resource server that enforces |
| **Model** | The LLM | — | **Not a principal.** It produces text; it holds no credentials and is never trusted for authorization |

The model is deliberately absent from the identity chain. Prompt injection means the model's output is partly authored by
whoever wrote the documents it read; any design where the model "decides" authorization or "holds" a token is a design
where an attacker can.

---

## 2. User × agent: same user, different agents, different access

### 2.1 The permission formula

```
effective(user, agent, action, resource) =
      user_entitlement(user, action, resource)          # Alice's own rights (RBAC/ReBAC/ABAC, ch 11)
  AND agent_ceiling(agent_definition, action, resource) # what this agent type may EVER do (registry)
  AND user_grant(user, agent, action)                   # what Alice consented to for THIS agent (+ expiry)
  AND task_scope(run, action, resource)                 # what this run/task needs (down-scoped token, RAR)
  AND context_ok(approval, step_up, budget, time)       # HITL approvals, fresh auth, rate/budget (ch 15)
```

Example, same user, three agents:

| Action | Alice herself | Calendar assistant (ceiling) | Finance agent (ceiling) | Coding agent (ceiling) |
|---|---|---|---|---|
| `calendar.events.write` | ✔ | ✔ | ✘ | ✘ |
| `invoice.read` | ✔ | ✘ | ✔ | ✘ |
| `invoice.approve ≤ €500` | ✔ (limit €10k) | ✘ | ✔ **with approval** | ✘ |
| `repo.push` to `acme/api` | ✔ | ✘ | ✘ | ✔ (branches only, not `main`) |
| `email.send` external | ✔ | ✔ (only to meeting attendees) | ✘ | ✘ |

Note the finance agent's limit (€500) is lower than Alice's (€10k): the **ceiling** is per agent, independent of the
user. A junior employee using the same finance agent gets `min(their limit, €500)`.

### 2.2 Where each term lives

| Term | Stored in | Enforced by |
|---|---|---|
| User entitlement | Your authorization system (roles, relationships) | Resource server / PDP |
| Agent ceiling | **Agent registry** (allowed tools, scopes, RAR types, limits, data classes) | AS at token exchange, gateway, PDP |
| User grant to agent | **Consent store** at the AS: `(user, client, agent_id, scopes/authorization_details, expires_at)` | AS at token exchange |
| Task scope | The exchanged token (`scope`, `authorization_details`, `aud`) | Resource server |
| Context | Approval records, step-up claims, budgets | Gateway / PDP |

### 2.3 Token shape

An access token minted for one agent action (JWT, `typ: at+jwt`):

```json
{
  "iss": "https://auth.acme.example",
  "sub": "user:alice",
  "aud": "https://erp.acme.example/mcp",
  "client_id": "acme-assistant",
  "act": { "sub": "agent:finance-reconciler", "agent_version": "7", "run_id": "r-8842",
           "workload": "spiffe://prod.acme/ns/agents/sa/finance-runner" },
  "scope": "invoice.read invoice.approve",
  "authorization_details": [{ "type": "invoice_approval", "max_amount": {"currency": "EUR", "value": "500.00"} }],
  "cnf": { "jkt": "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I" },
  "iat": 1761730000, "exp": 1761730300, "jti": "…"
}
```

- `sub` = the human; `act.sub` = the specific agent (RFC 8693 delegation). Extra members inside `act` (version, run,
  workload) are your profile; the RFC only standardizes `sub` and nesting.
- `aud` = one tool server; `exp` = minutes; `cnf` = bound to the runtime's key (DPoP) so a leaked token is useless.
- Resource servers **must read `act`**. A server that ignores it can't apply agent-specific limits and logs Alice as
  the actor of something she never clicked.

---

## 3. Getting delegated tokens for an agent

### 3.1 Interactive: the user is present

```
1. Alice signs in to the host app (OIDC). Host holds her session (BFF, ch 02).
2. First use of the finance agent: consent screen at the AS, naming the AGENT, not just the app:
     "Finance Reconciler (by Acme Finance Platform team) wants to: read invoices; approve invoices up to €500
      (each approval asks you first). Valid 30 days."
   → AS stores user_grant(alice, acme-assistant, agent:finance-reconciler, …, exp)
3. Each run: the runtime (authenticating as its workload, private_key_jwt or mTLS) performs token exchange:
     subject_token = Alice's access token (or a refresh-token-derived user token held by the host)
     actor_token   = the agent runtime's own token (JWT-SVID / client assertion naming agent:finance-reconciler)
     resource      = https://erp.acme.example/mcp
     scope / authorization_details = what this step needs
4. AS checks: subject valid; actor is a registered agent whose workload identity matches the registry; agent status active;
   user_grant exists and covers request; request ⊆ agent ceiling ⊆ user entitlement (coarse); then issues the token above.
```

### 3.2 Background: the user isn't present

Options, best first:

1. **Agent acts as itself** (client credentials, its own ceiling), on resources the agent itself owns or is explicitly
   granted. Use when the work isn't "Alice's" (nightly reconciliation of the shared ledger).
2. **Standing delegated grant**: Alice granted the agent offline access for a bounded scope and period; the AS keeps the
   grant; the runtime exchanges at run time (`subject` = a stored grant reference or offline token held by the AS or
   vault, never by the model). Every token still says `sub=alice, act=agent`.
3. **Ask when needed**: CIBA or a push approval to Alice's phone when the run reaches a step outside the standing grant
   (ch 07 §9, ch 15 §5).

Never: the agent uses **Alice's personal refresh token** copied from her session or a developer's personal access
token. That is impersonation, has no ceiling, and outlives Alice's employment.

### 3.3 Enterprise variant: ID-JAG

If the host app and the tool server both use the corporate IdP, the IdP can mint the cross-app grant under admin policy
(ch 10 §7, ch 13 §7). To make it agent-aware, include the agent as a requested actor/claim in your profile, and make IdP
policy reference agent registry attributes ("only agents with risk tier ≤ 2 may access the ERP MCP server").

---

## 4. Sub-agents: internal delegation and attenuation

> **In plain words.** When an agent hands work to a helper agent, the helper must get a smaller key, never a copy of
> the parent's key and never a bigger one.

### 4.1 Two kinds of sub-agents

| | In-process sub-agent (same runtime, e.g. a LangGraph subgraph, a "task" tool spawning a worker) | Remote sub-agent (separate service, A2A, other team) |
|---|---|---|
| Separate credentials? | Often none; shares the parent's credential broker | Yes, its own workload identity |
| How to attenuate | Broker enforces a narrower **tool allowlist and scope set** for the sub-agent's context; tokens minted per tool call carry the sub-agent in `act` | Token exchange producing a token for the sub-agent's audience with narrower scope and nested `act` |
| Typical failure | Sub-agent inherits every tool the parent has ("it's the same process") | Parent forwards its own token |

### 4.2 Attenuation rules (enforced at the AS or broker, not by the agents)

1. **Scope**: child scopes ⊆ parent token scopes ∩ child agent ceiling.
2. **Audience**: child gets tokens only for the resources its task needs.
3. **Lifetime**: child `exp` ≤ parent `exp`.
4. **Limits**: numeric limits (amount, count, rate) ≤ parent's remaining budget.
5. **Depth**: cap delegation depth (e.g. 3). Deep chains are where audit and reasoning break down.
6. **Chain recorded**: `act: {sub: agent:ledger-reader, act: {sub: agent:finance-reconciler}}`. Per RFC 8693 only the
   outermost actor is used for access decisions; nested actors are for audit, and your policy can additionally require
   that each hop was permitted.
7. **No re-delegation unless allowed**: a flag in the registry (`may_delegate_to: [agent:ledger-reader]`), the agent
   analogue of `may_act`.

### 4.3 Capability tokens as an alternative

Macaroons and Biscuit tokens let a holder **add caveats offline** ("only invoice 4711", "expires in 5 minutes") and hand
the weaker token to a sub-agent without calling the AS. Useful when the AS round trip is a bottleneck or across
organizational boundaries that share a root key. Trade-offs (revocation, verification complexity, ecosystem support) are
covered in [ai-rag ch 29 §6](../ai-rag/29-agent-identity-and-delegated-authorization.md).

---

## 5. A2A: agents calling other agents across trust boundaries

When Alice's planner agent (org X) asks a travel agent (org Y) to book a trip:

```
Planner (X) ──mTLS: spiffe://x.example/agents/planner──▶ Travel agent (Y)
  Authorization: Bearer <token: iss=Y's AS, sub=alice@x (pairwise or federated), act={sub: x:agent:planner},
                           aud=https://travel.y.example/a2a, authorization_details=[{type: trip_booking, max: €400}]>
```

How the token is obtained, by trust relationship:

| Relationship | Mechanism |
|---|---|
| Same organization | Internal AS token exchange (§3) |
| Both trust Alice's enterprise IdP | ID-JAG (ch 10 §7) |
| Federated orgs | X's AS issues a JWT assertion that Y's AS accepts (RFC 7523 / identity chaining across trust domains, an OAuth WG draft) |
| No prior relationship | Alice authorizes the planner at Y's AS via normal OAuth (A2A card declares `oauth2` scheme); task enters `auth-required` until she does |

**Different access for different calling agents, same user, in A2A.** The receiving agent (Y) authorizes on
`(caller agent identity, user, skill, parameters)`:

- Y's agent card can declare **per-skill security requirements** (e.g. `book_trip` needs `trips.book` scope).
- Y's AS keeps grants per `(user, calling agent)`: Alice may have let the planner book trips under €400 but let a
  different "expense agent" only *read* bookings.
- Y logs both identities and can block a calling agent without blocking the user.

The receiving agent must treat the calling agent's natural-language task as **untrusted input**, exactly like a tool
result: authorization comes from the token and Y's policy, never from what the message says ("the user approved this").

---

## 6. Registries: agents, tools and MCP servers as governed inventory

> **In plain words.** You can't control what you haven't listed. A registry is the list of every agent and every tool,
> with who owns it, what it may do, and whether it's switched on. Token services and gateways read it on every decision.

### 6.1 Agent registry record

```yaml
id: agent:finance-reconciler
version: 7
owner_team: finance-platform
description: Reconciles invoices against bank statements
runtime_identities: ["spiffe://prod.acme/ns/agents/sa/finance-runner"]   # who may act as this agent
oauth_client: acme-assistant
risk_tier: 2                                   # drives approval and monitoring requirements
allowed_tools: [mcp:erp/invoice.read, mcp:erp/invoice.approve, mcp:bank/statements.read]
max_scopes: [invoice.read, invoice.approve, statements.read]
authorization_details_limits: {invoice_approval: {max_amount_eur: 500}}
data_classes: [financial-confidential]         # what data it may see
egress_allowlist: [erp.acme.example, bank-api.acme.example]
may_delegate_to: [agent:ledger-reader]
approval_policy: {invoice.approve: always}     # HITL (ch 15)
models_allowed: [model-a-large, model-b-small]
status: active                                 # active | suspended | retired  (kill switch)
evaluation: {last_eval: 2026-10-20, suite: finance-agent-v7, pass: true}
```

### 6.2 Tool registry record

```yaml
id: mcp:erp/invoice.approve
server: https://erp.acme.example/mcp          # audience for tokens
definition_hash: sha256:9f1c…                 # pinned tool name+description+schema
scopes_required: [invoice.approve]
side_effects: write-irreversible              # read | write-reversible | write-irreversible | external-communication
risk: high
approval_required: true
data_classes_out: [financial-confidential]
rate_limit: {per_user_per_hour: 20}
owner_team: erp
status: active
```

Why `definition_hash`: an MCP server can change a tool's description or schema after approval (a **rug pull**; the
description is model-visible text and can carry injected instructions). The gateway compares the live definition hash
to the registered one and blocks or re-requests approval on change.

### 6.3 Public and private MCP registries

The official **MCP Registry** (preview since September 2025) is a metadata catalogue of public MCP servers with
namespace verification (reverse-DNS names such as `io.github.username/server`, proven at publish time via GitHub OAuth/OIDC,
DNS or HTTP), which downstream
"sub-registries" (marketplaces, enterprise catalogues) can consume. For organizations, the pattern is a **private
registry** curated from public entries plus internal servers, and clients/gateways configured to allow only registry
entries. Registry entries tell you *what a server is*; they don't vouch for its behaviour: keep your own review,
pinning and tool hashes.

### 6.4 How registries connect to identity

| Consumer | Reads | Decision |
|---|---|---|
| Authorization server (token exchange) | Agent record | Is actor a registered, active agent; does workload identity match; request ⊆ ceiling |
| MCP gateway | Agent + tool records | Is this tool allowed for this agent; approval needed; definition hash matches |
| AI gateway | Agent record | Which models; token budgets; data classes allowed in prompts |
| PDP | Both | Agent ceiling term of the formula |
| Audit / SIEM | Both | Enrich events with owner, risk tier |
| Incident response | Agent record | `status: suspended` = kill switch everywhere at once |

---

## 7. Gateways: AI gateway, MCP gateway, agent gateway

Three gateway types appear in agent platforms. They are the **policy enforcement points** of ch 11 §1 for agent traffic.

```
                     ┌──────────────── control plane ───────────────┐
                     │ Agent registry · Tool registry · Policy (PDP) │
                     │ Consent store · AS / token exchange · Vault   │
                     └───────▲───────────────▲───────────────▲───────┘
                             │               │               │
User ─▶ Host app ─▶ Agent runtime ──▶ AI gateway ──▶ model providers
                     │  (orchestrator,        │ per-agent/per-user virtual keys, budgets,
                     │   no credentials)      │ redaction, prompt logging, model allowlist
                     │
                     ├──▶ MCP gateway ──▶ MCP servers (internal / SaaS)
                     │     terminates client auth, checks agent×tool policy, approvals,
                     │     definition hashes, injects upstream credentials or exchanges tokens, audits
                     │
                     └──▶ Agent gateway ──▶ remote agents (A2A)
                           outbound: mints narrowed tokens, verifies agent cards; inbound: authenticates callers
```

### 7.1 AI gateway (LLM gateway)

Identity role: callers (agents, apps) authenticate to the gateway with **their own identity** (workload identity or
per-agent virtual key); the gateway holds the **provider API keys** in a vault; it attributes every request to
`(tenant, user, agent, run)` for cost and audit, enforces model allowlists and budgets per agent, and can redact PII or
secrets from prompts before they leave. Design detail: [`solutions/llm-gateway-design.md`](../solutions/llm-gateway-design.md),
[ai-rag appendix L](../ai-rag/appendix-l-control-plane-guardrails-budgets-and-gateways.md).

### 7.2 MCP gateway

Aggregates many MCP servers behind one endpoint. Identity rules that keep it from becoming a giant confused deputy:

1. **It is the resource server for its clients**: clients get tokens with `aud` = the gateway, per the MCP spec (ch 13).
   It validates them fully, including `act`.
2. **It is a client to each backend**: it obtains a **separate** backend-audience token by token exchange (`sub` = user,
   `act` = agent, plus itself in the chain), or injects the user's stored credential for that backend from the vault. It
   **never** forwards its inbound token.
3. **It enforces the tool registry**: allowed tools per agent, definition hashes, approvals, rate limits, argument
   validation, output scrubbing (secrets/PII), egress allowlists.
4. **It's tier-0**: it holds or can mint credentials for every backend. Run it with its own workload identity, minimal
   operator access, full audit, and no ability for the model to address it except through tool calls.

### 7.3 Agent gateway (A2A)

Outbound: resolves and verifies remote agent cards (signatures, registry membership), obtains narrowed tokens per remote
agent (§5), strips internal context that shouldn't cross the boundary. Inbound: authenticates calling agents (mTLS, OAuth),
maps them to registry entries, applies per-caller-agent policy, rate limits.

### 7.4 Trade-offs of gateways

| Benefit | Cost |
|---|---|
| One place for policy, credentials, audit, kill switches | Single point of failure (HA, multi-region) |
| Model and agents never hold secrets | Concentrated value: compromise = access to everything behind it |
| Consistent attribution and budgets | Added latency per call (keep policy local, cache decisions) |
| Faster onboarding of new tools | Can hide end-to-end identity if implemented with passthrough or shared service keys; insist on per-call `sub`+`act` tokens downstream |

---

## 8. Reference architecture, end to end

**Interactive flow: "Reconcile October and approve anything under €500."**

```
1. Alice → Host app (BFF session). Host starts run r-8842 of agent:finance-reconciler@v7.
2. Runtime (spiffe://…/finance-runner) → AS: token exchange (subject = Alice's token via BFF, actor = runtime)
   AS: registry says runtime may act as finance-reconciler v7, status active; consent exists; issues
       T1: sub=alice, act=finance-reconciler, aud=mcp-gateway, scope=invoice.read invoice.approve statements.read, 15 min, DPoP-bound
3. Model plans; calls tool invoice.read → runtime → MCP gateway with T1
   Gateway: validate T1; tool registry ok; exchange for T2: aud=erp/mcp, scope=invoice.read, act chain +gateway
   ERP MCP server: validates T2 (aud, act), checks Alice's ReBAC rights on each invoice → results
   Gateway scrubs output → runtime → model context
4. Model wants a sub-agent for ledger lookups → runtime spawns agent:ledger-reader (allowed by may_delegate_to)
   Broker gives it tool allowlist {statements.read} only; its tool calls carry act={ledger-reader, act={finance-reconciler}}
5. Model calls invoice.approve(4711, €80) → gateway: tool requires approval (registry) → approval request to Alice
   (in-app or CIBA push, binding message "Approve invoice 4711 €80 to Vendor A?") → Alice approves
   Gateway exchanges for T3 with authorization_details {invoice: 4711, amount: 80}, single use → ERP approves
6. Every step: audit (user, agent, version, run, sub-agent, tool, args hash, decision, approval id, token jti)
7. Alice is offboarded mid-run → SCIM → AS revokes grants; next exchange fails; run stops with "needs re-authorization" (ch 15 §6)
```

**Background flow**: same, except step 2 uses the agent's own identity plus a standing, narrowly scoped user grant held
at the AS, and step 5 always routes approvals asynchronously (CIBA/push) with the run paused.

---

## 9. Audit, kill switches and revocation

**Audit tuple per action:**

```
time, tenant, user (sub), host client_id, agent_id, agent_version, run_id, delegation chain (act…),
workload identity, tool id, tool definition hash, resource, action, arguments hash (and redacted args),
decision (+ policy version, reasons), approval id (if any), token jti, outcome, model id, cost
```

**Kill switches, from narrowest to widest:**

| Scope | Mechanism | Effect time |
|---|---|---|
| One run | Cancel run; revoke its exchanged tokens (`jti` denylist at gateway) | Seconds |
| One user's grant to one agent | Delete consent at AS | Next token exchange (minutes at most) |
| One agent version | Registry `status: suspended` for version | Gateway/AS read registry: seconds |
| Whole agent | Registry suspend; AS refuses exchanges; gateway blocks | Seconds |
| One tool / MCP server | Tool registry suspend | Seconds |
| A user | IdP disable → SCIM cascade (ch 10 §5) | Minutes |

Design so the kill switch **doesn't need the agent's cooperation**: it lives in the AS, gateway and registry. Short token
lifetimes (≤15 min) bound the worst case for tokens already issued.

---

## 10. Code: exchange policy, user × agent PDP, attenuation check

### 10.1 Token-exchange policy at the AS

```python
from dataclasses import dataclass

@dataclass
class AgentRecord:
    id: str
    version: str
    runtime_identities: set[str]
    max_scopes: set[str]
    may_delegate_to: set[str]
    status: str
    max_depth: int = 3

class ExchangeDenied(Exception):
    pass

def act_chain(act: dict | None) -> list[str]:
    chain = []
    while act:
        chain.append(act["sub"])
        act = act.get("act")
    return chain                                  # outermost (current) actor first

def authorize_exchange(subject_claims: dict, actor_identity: str, requested_agent: str,
                       requested_scopes: set[str], audience: str,
                       registry: dict[str, AgentRecord], consents, entitlements) -> dict:
    agent = registry.get(requested_agent)
    if agent is None or agent.status != "active":
        raise ExchangeDenied("unknown or suspended agent")
    if actor_identity not in agent.runtime_identities:
        raise ExchangeDenied("workload may not act as this agent")

    user = subject_claims["sub"]
    parent_chain = act_chain(subject_claims.get("act"))
    if parent_chain:                                              # delegation from another agent
        parent = registry.get(parent_chain[0])
        if parent is None or requested_agent not in parent.may_delegate_to:
            raise ExchangeDenied("parent agent may not delegate to this agent")
        if len(parent_chain) + 1 > agent.max_depth:
            raise ExchangeDenied("delegation too deep")
        ceiling = set(subject_claims.get("scope", "").split())    # child <= parent token
    else:
        ceiling = consents.granted_scopes(user, requested_agent)  # user's grant to THIS agent

    allowed = requested_scopes & agent.max_scopes & ceiling & entitlements.coarse_scopes(user)
    if allowed != requested_scopes:
        raise ExchangeDenied(f"not permitted: {sorted(requested_scopes - allowed)}")

    new_act = {"sub": requested_agent, "agent_version": agent.version}
    if subject_claims.get("act"):
        new_act["act"] = subject_claims["act"]
    return {"sub": user, "aud": audience, "scope": " ".join(sorted(allowed)), "act": new_act,
            "exp_cap": subject_claims.get("exp")}                  # child exp <= parent exp
```

### 10.2 Resource-side check using both identities

```python
def decide(token: dict, action: str, resource, user_pdp, agent_registry) -> tuple[bool, str]:
    user = token["sub"]
    agent_id = (token.get("act") or {}).get("sub")
    if action not in token.get("scope", "").split():
        return False, "token lacks scope"
    if not user_pdp.allowed(user, action, resource):
        return False, "user not entitled"                         # Alice's own rights
    if agent_id:
        agent = agent_registry.get(agent_id)
        if agent is None or agent.status != "active" or action not in agent.max_scopes:
            return False, "agent not permitted"                   # agent ceiling, re-checked at the edge
    return True, "ok"
```

---

## 11. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| One service account key shared by all agents | No attribution; every agent has every agent's power | Registered agents, per-action delegated tokens |
| Agent uses the user's own token/PAT | Impersonation; no agent ceiling; outlives the user | Token exchange with `act` |
| Same consent for all agents of one app | Calendar agent can approve invoices | Consent per `(user, agent)` |
| Resource servers ignore `act` | Can't apply agent limits; audit shows the user did it | Policy and logs use `sub` + `act` |
| Sub-agents inherit parent's full toolset | Privilege creep in multi-agent graphs | Attenuation at broker/AS; `may_delegate_to` |
| Parent passes its token to a remote sub-agent | Token leaves its audience; no narrowing | Exchange for child audience |
| Registry as documentation only | Kill switch that nobody enforces | AS, gateways, PDP read the registry on every decision |
| MCP gateway that forwards inbound tokens or uses one service key per backend | Giant confused deputy | Per-backend exchange or vault injection, per call |
| Trusting a calling agent's claim "the user approved" | Injection-driven escalation | Approvals as signed records/tokens checked by policy |
| No tool definition pinning | Rug pulls | Definition hash in tool registry |

---

## 12. Interview questions

**Q1. How do you give the same user different permissions in different agents?**
Register each agent with a ceiling; record user consent per agent; mint per-action tokens via token exchange with
`sub`=user and `act`=agent; compute effective access as user entitlement ∩ agent ceiling ∩ user's grant to that agent ∩
task scope; resource servers and gateways check both identities.

**Q2. A planner agent spawns three sub-agents. How do you ensure none exceeds the planner?**
Sub-agent tokens come from exchange (or broker allowlists in-process) with scope ⊆ parent token ∩ child ceiling, audience
limited to the child's tools, lifetime ≤ parent, budgets ≤ parent's remainder, depth cap, `may_delegate_to` in the registry,
nested `act` for audit. Agents never pass their own tokens.

**Q3. What is an MCP gateway's identity role and its main risk?**
It's the resource server for clients and a client to each backend MCP server; it validates inbound tokens, enforces agent×tool
policy and approvals, and obtains backend-specific tokens or injects vaulted credentials. Main risk: it concentrates authority,
so passthrough or shared backend keys turn it into a confused deputy; mitigate with per-call exchanged tokens, tier-0
hardening and full audit.

**Q4. Why does an agent registry matter for security rather than just documentation?**
Because token services, gateways and PDPs consult it at runtime: it defines each agent's ceiling, allowed runtimes, delegation
rights and status. Suspending an agent there revokes its ability to obtain tokens or call tools everywhere at once.

---

## Sources

- RFC 8693, OAuth 2.0 Token Exchange (`act`, `may_act`, delegation vs impersonation): https://datatracker.ietf.org/doc/html/rfc8693
- RFC 9396, Rich Authorization Requests: https://datatracker.ietf.org/doc/html/rfc9396
- OAuth Identity and Authorization Chaining Across Domains (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-chaining/
- MCP 2026-07-28 Authorization and Security Best Practices: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- MCP Registry (about, preview status, namespace authentication): https://modelcontextprotocol.io/registry/about
- A2A Protocol specification: https://a2a-protocol.org/latest/specification/
- OWASP Top 10 for Agentic Applications / Agentic AI threats and mitigations (OWASP GenAI Security Project): https://genai.owasp.org/
- SPIFFE: https://spiffe.io/
- Companion chapter: [ai-rag/29-agent-identity-and-delegated-authorization.md](../ai-rag/29-agent-identity-and-delegated-authorization.md)
