# 29 — agent identity and delegated authorization

> **Prerequisites:** [`24-tool-calling-and-enterprise-integration.md`](24-tool-calling-and-enterprise-integration.md)
> (§6 already covers the basics this chapter assumes: never let the model decide authorization, tool-level
> RBAC, OAuth token forwarding, service-to-service auth and the audit log. This chapter does not repeat them;
> it covers what changes when the caller is an *agent* that is neither the user nor an ordinary service),
> [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md) (the Rule of
> Two and CaMeL in §4.7 explain *why* an agent's authority must be small; this chapter is the credential
> machinery that makes "small" enforceable),
> [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md) (sub-agents and handoffs are the
> source of the delegation chains in §4–§5) and
> [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md) (interrupts are the substrate for step-up approval in §7).
> Useful: [`26-mcp-and-agent-protocols.md`](26-mcp-and-agent-protocols.md) (MCP transport and auth in full),
> [`27-a2a-and-agent-interoperability.md`](27-a2a-and-agent-interoperability.md) (agent-to-agent calls cross
> trust domains) and [`28-agentic-security-owasp-and-mcp-threats.md`](28-agentic-security-owasp-and-mcp-threats.md)
> (the threat catalogue this chapter's controls answer).
>
> **Feeds into:** `14-agent-evaluation.md` (authorization-boundary tests are a trajectory-scoring axis),
> `30-context-engineering-for-agents.md` (what credentials and identity facts may enter the context window),
> and any multi-tenant RAG-plus-agents design (§11 here: document-level permissions at retrieval time).
>
> **THESIS:** an agent is a new kind of principal, and the industry's default way of handling it, one shared
> service-account key that can do everything the agent might ever need, destroys the one property that makes
> audit and least privilege possible: knowing *who* did *what* on *whose* behalf. The engineering fix is not
> exotic. Give each agent its own identity; make every action carry a short-lived, narrowly scoped,
> audience-bound token that names the human (or the schedule) that authorized it **and** the agent that is
> acting; check it at an enforcement point the model cannot reach; and log the whole tuple. Everything in this
> chapter is a way of making those four sentences concrete. A second, equally important thesis: **the
> standards for agent-specific delegation are not settled.** The solid base is old and boring (OAuth 2.0,
> RFC 8693, RFC 9396, RFC 9449, SPIFFE). The agent-specific layers on top are Internet-Drafts that may
> expire, merge or change. Build on the base and treat the drafts as reading, not as dependencies.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The problem: who is acting?](#1-the-problem-who-is-acting)
2. [Why the shared service-account key is the anti-pattern](#2-why-the-shared-service-account-key-is-the-anti-pattern)
3. [Agent as a distinct principal](#3-agent-as-a-distinct-principal)
4. [OAuth 2.0 Token Exchange (RFC 8693): subject, actor, `act`, `may_act`](#4-oauth-20-token-exchange-rfc-8693-subject-actor-act-may_act)
5. [Down-scoping and attenuation](#5-down-scoping-and-attenuation)
6. [Capability tokens as an alternative: macaroons and biscuits](#6-capability-tokens-as-an-alternative-macaroons-and-biscuits)
7. [Asynchronous and background agents: CIBA, step-up, consent](#7-asynchronous-and-background-agents-ciba-step-up-consent)
8. [MCP authorization as a concrete case](#8-mcp-authorization-as-a-concrete-case)
9. [Workload identity for agent runtimes: SPIFFE/SPIRE and WIMSE](#9-workload-identity-for-agent-runtimes-spiffespire-and-wimse)
10. [Agent-specific IETF drafts: what exists and how settled it is](#10-agent-specific-ietf-drafts-what-exists-and-how-settled-it-is)
11. [Policy enforcement: PEP, PDP, ReBAC and tenant isolation](#11-policy-enforcement-pep-pdp-rebac-and-tenant-isolation)
12. [Audit and revocation](#12-audit-and-revocation)
13. [Code: exchange request, validator, toy PDP](#13-code-exchange-request-validator-toy-pdp)
14. [Anti-patterns](#14-anti-patterns)
15. [Interview questions](#15-interview-questions)
16. [Lab exercises](#16-lab-exercises)
17. [Real-world cases](#17-real-world-cases)
18. [Sources](#sources)

---

## Start here — the whole chapter in plain words

**The problem.** When a person clicks "delete" in an app, the system knows who it was. When an agent
does it, there are at least three parties: the human who asked, the agent software that decided, and the
tool/API that executed. Most deployments collapse these into one: the agent runs with a service-account API
key, and the logs say "svc-agent deleted 4,000 rows". Nobody can tell whose request it was, whether the
user was even allowed to do it, or which of ten agents sharing the key did it. And the key can do
everything any agent ever needs, so one prompt injection inherits all of it.

**A real-world example.** A company has an "inbox assistant" agent. Alice asks it to "summarise this
week's invoices and forward the unpaid ones to finance". (Names and numbers in this chapter are
illustrative.)

1. **Identity.** The assistant is registered as its own client, `agent:inbox-assistant`, with an owner
   team and a risk tier. It is not Alice and not "the platform".
2. **Delegation.** Alice logs in and consents: "Inbox Assistant may read your mail and send mail to
   finance@ for 1 hour." The authorization server issues the agent a token for that. The token says
   `sub = alice` and `act.sub = inbox-assistant` (§4).
3. **Narrowing.** The agent calls the mail API with a token whose audience is the mail API only and whose
   scope is `mail.read mail.send`; the calendar API rejects it (§5).
4. **Enforcement.** Every call goes through a gateway (a policy enforcement point, §11). It checks the
   token, then asks a policy engine: may this agent, for this user, send to this recipient? An email to
   `attacker@evil.example` planted in an invoice is refused, whatever the model "decided".
5. **Escalation.** Deleting a mailbox folder is above the agent's standing authority. The system pauses
   and sends Alice a push notification naming the agent and the action (§7). No approval, no deletion.
6. **After the fact.** One audit row per call holds user, agent, session, token ID (`jti`) and decision
   (§12). If the agent misbehaves, security revokes `agent:inbox-assistant` and every token it holds
   dies, without touching Alice's own login.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Principal | any identity that can be granted or denied access | a named badge-holder |
| Subject | the user (or system) on whose behalf an action is taken | the homeowner |
| Actor | the party actually performing the action, here the agent | the contractor with a key |
| Delegation | the actor acts for the subject; both identities stay visible | a power of attorney that names both people |
| Impersonation | the actor becomes the subject; the actor's identity is hidden | wearing the homeowner's face |
| Token exchange | trade one token for a different, narrower one | swapping a master pass for a one-room pass |
| `act` claim | JWT claim naming the current actor (and, nested, earlier ones) | "signed by the contractor on behalf of the owner" |
| `may_act` claim | JWT claim saying who is *allowed* to become an actor for this subject | the owner's list of approved contractors |
| Audience (`aud`) | the one service a token is meant for | an envelope addressed to one office |
| Attenuation | making a credential weaker, never stronger, when passing it on | a photocopy that can only be reduced in rights |
| Sender-constrained token | a token usable only by the holder of a private key | a card that needs your PIN, not just possession |
| PEP / PDP | enforcement point (blocks the call) / decision point (answers allow-deny) | the door guard / the rule book |
| CIBA | "ask the user's phone for approval" flow with no browser redirect | the bank texting you to approve a transfer |
| Workload identity | an identity for running software, proved cryptographically, not by a stored secret | a uniform issued to a process, not a password it memorised |
| Kill switch | revoking one agent identity so all its tokens stop working | cancelling one employee's badge |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `sub` | subject of the token: the delegating user | stable user ID | `user:alice` |
| `act` | current actor (nested = earlier actors) | agent client ID | `{"sub":"agent:inbox-assistant"}` |
| `may_act` | who may act for this subject | list of client IDs | `{"sub":"agent:inbox-assistant"}` |
| `aud` | intended recipient of the token | one API URL | `https://mail.internal` |
| `scope` | space-delimited coarse permissions | 1–5 scopes | `mail.read mail.send` |
| `authorization_details` | RFC 9396 fine-grained request, JSON array | 1–3 objects | `type=payment_initiation, amount ≤ 50` |
| `resource` | RFC 8707 target URI(s) in requests | the API base URL | `https://mail.internal` |
| `exp` / TTL | token lifetime | 60–900 s for agent tokens | 300 s |
| `jti` | unique token ID, the audit join key | UUID | `7d1f…` |
| `cnf` | confirmation claim binding the token to a key (`jkt` for DPoP, `x5t#S256` for mTLS) | thumbprint | DPoP key hash |
| `max_depth` | most `act` hops a resource server accepts | 2 | user → planner → worker |
| `sid` | session identifier carried in the token or audit | UUID | `sess-1` |
| `binding_message` | CIBA text shown on the approval device | ≤ ~100 chars | "Approve: delete folder 'Invoices 2024' by Inbox Assistant" |

---

## 1. The problem: who is acting?

> **In plain words.** In a normal app there are two parties: a user and the system. With agents there are
> four: the **user**, the **agent** (an LLM loop with a goal), possibly **sub-agents** it spawns, and the
> **tools** it calls. Authorization must answer "may *this agent*, acting for *this user*, do *this thing*
> to *this resource* right now" and the answer must be recorded.
>
> **Real-world example.** A travel agent app books a flight. The user asked for it, the planner agent chose
> the airline, a payments sub-agent charged the card, and the airline API executed. If the card was charged
> twice, "which of these did it" has four possible answers, and a log that says only `svc-travel` has none.

### 1.1 The four roles

| Role | Examples | What it should be identified by |
|---|---|---|
| Resource owner / user | an employee, a customer | the IdP subject (`sub`), from a real login |
| Agent | "inbox assistant v3", "refund agent" | its own OAuth client or workload identity, versioned |
| Sub-agent | a worker the planner spawns | its own (often ephemeral) identity, with a narrower grant than its parent |
| Tool / resource server | the mail API, an MCP server, a database | an audience URI; it validates tokens and enforces policy |

### 1.2 Two modes of authority: delegated and autonomous

Almost every design question in this chapter reduces to which of two modes an action is in.

- **Delegated (on-behalf-of).** A human initiated or approved the work. The effective permission is
  *the intersection of what the user may do and what the agent is allowed to do*. The agent can never
  exceed the user, and the user's rights are not all handed to the agent. This is the default for chat
  assistants, copilots and anything interactive.
- **Autonomous.** No human is in the loop at the moment of action: a scheduled job, an event-triggered
  triage agent, a long-running research task. The authority is the *agent's own*, granted by an
  administrator or an owning team, and should be small and explicitly approved. Honest labelling matters:
  an autonomous action must not be logged as if a particular user did it.
- **Hybrid.** An interactive request launches a background task that outlives the user's session. Here
  the user's consent has to be turned into an explicit, time-bounded grant (§7), because "the user's session
  token" will expire or be revoked while the work continues.

Chapter 24 §6.3 covers forwarding a user's token. The point here is the delta: pure forwarding gives the
tool the user's identity and *none* of the agent's, so the tool cannot apply agent-specific limits and the
audit cannot tell human clicks from agent actions.

### 1.3 What the model is *not*

The model is not a principal. It has no credentials, makes no commitments and cannot be held to account.
The principal is the *deployment*: a versioned agent with an owner, a registered identity and a policy. The
model only proposes calls; the identity machinery decides what those proposals can do (ch 24 §6.1).

---

## 2. Why the shared service-account key is the anti-pattern

> **In plain words.** One API key for "the agent platform" is easy and wrong: every agent, every user, every
> task shares the same power, and the logs cannot tell them apart.
>
> **Real-world example.** Ten agents share `AGENT_PLATFORM_KEY`, which can read all customer records
> because one of them (billing) needs to. A prompt-injected FAQ bot now has billing's read access to every
> customer, and the audit log shows `AGENT_PLATFORM_KEY` for all of it.

Concretely, a shared key fails in six ways:

1. **Over-privilege by union.** The key must carry the *union* of every agent's needs, forever.
2. **No user binding.** The downstream API sees the service account, so it cannot apply per-user row-level
   rules; the agent becomes a confused deputy (§2.1) that reads data the requesting user may not see.
3. **No attribution.** "Which agent, for which user, in which session" is unrecoverable from the downstream
   side. Incident response degrades to guessing.
4. **No fine-grained revocation.** Rotating the key breaks all agents; not rotating leaves a compromised
   agent live. There is no "stop only agent X".
5. **Long-lived and portable.** A static secret in an env var or a prompt-visible config can be exfiltrated
   once and replayed from anywhere for months. Agent contexts are exactly where secrets leak (tool outputs,
   logs, traces).
6. **Blast radius equals prompt-injection radius.** Per ch 17, assume the model can be steered by anything
   it reads. Whatever the credential can do is what the attacker can do.

### 2.1 The confused deputy, agent edition

The classic confused deputy: a privileged program is tricked into using its authority for someone with
less. An agent with a broad service identity is a confused deputy by construction. A user without access to
HR files asks the agent a question; the agent, with a platform key that *can* read HR files, retrieves them.
The check "may this user read this file" never runs because the downstream system only ever saw the
platform key. The fix is not "tell the model not to": it is to make the effective authority the
intersection of user and agent, enforced where the data lives (§5, §11).

### 2.2 What to use instead

| Need | Replace the shared key with |
|---|---|
| Agent's own identity | a registered OAuth client or workload identity per agent (and per environment) |
| User-initiated work | token exchange yielding a delegated token with `sub`=user, `act`=agent (§4) |
| Scheduled / autonomous work | client credentials for the agent's *own* narrowly scoped identity, flagged autonomous (§1.2) |
| Secrets for third-party APIs | a credential broker/vault that mints short-lived credentials per call, so the agent runtime never holds the long-lived secret (ch 24 §12.5) |
| Proof the caller is the registered agent | workload attestation (§9) or sender-constrained tokens (§5.4) |

---

## 3. Agent as a distinct principal

> **In plain words.** Give each agent a name, an owner, a version and its own credential, the same way you
> would a new microservice, plus a few agent-specific attributes.
>
> **Real-world example.** `refund-agent` and `faq-agent` are two registered clients. The FAQ one has no
> scopes that touch payments at all, so even a fully hijacked FAQ agent cannot request a payment token.

### 3.1 The agent registry record

Extend the tool registry (the `ai-rag/labs/tool-registry/` lab models tools, not callers) with a sibling
record for agents. Fields worth having:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str            # "agent:refund-agent" -> becomes act.sub / client_id
    version: str             # git SHA or release tag; bump on prompt/tool change
    owner_team: str          # who is paged and who approves scope changes
    tenant_scope: str        # "tenant:acme" or "*" (platform agent)
    max_scopes: frozenset    # ceiling: no token for this agent may exceed it
    allowed_audiences: frozenset
    mode: str                # "delegated" | "autonomous" | "both"
    risk_tier: int           # drives step-up thresholds (§7) and review cadence
    status: str = "active"   # "suspended" = kill switch (§12.3)
```

The `max_scopes` ceiling is the agent-side half of the intersection in §1.2. The authorization server (or
token-exchange policy) refuses to mint anything above it, whatever the user could do.

### 3.2 Ephemeral versus long-lived agent identities

- **Long-lived** (one per deployed agent): stable audit identity, easy to reason about, easy to revoke.
  Right default for the top-level agent.
- **Ephemeral** (one per task or per sub-agent spawn): narrower, dies with the task, limits replay value.
  Right for sub-agents and for code-execution sandboxes. The cost is registration churn, so derive them from
  the parent: the parent requests a child credential that is an attenuation of its own (§5, §6), rather than
  registering each child by hand.

### 3.3 How an agent proves it is that agent

A `client_id` is a name, not proof. Options, from weakest to strongest: a client secret (static, leakable);
`private_key_jwt` client authentication; mTLS client certificates (RFC 8705, §5.4); a workload identity
document such as a SPIFFE SVID, issued after the platform attests the runtime (§9). Prefer the last two:
they remove the long-lived secret that ch 24 §12.5 warns about.

---

## 4. OAuth 2.0 Token Exchange (RFC 8693): subject, actor, `act`, `may_act`

> **In plain words.** Token exchange is an OAuth endpoint call that says "here is a token (the subject's)
> and here is a token proving who I am (the actor's): give me a new token for a specific service". The new
> token carries both identities, so the downstream service sees "agent X acting for user Alice".
>
> **Real-world example.** Alice's login token reaches the inbox agent. The agent posts it to the token
> endpoint as `subject_token`, with its own credential as `actor_token`, asking for audience
> `https://mail.internal`, scope `mail.read`. It receives a 5-minute token with `sub=alice`, `act.sub=inbox-assistant`.

This section describes RFC 8693 (Standards Track, January 2020) from its text and authoritative secondary
descriptions; it is the standard mechanism the agent-specific drafts in §10 build on.

### 4.1 The request

A client POSTs to the token endpoint, `application/x-www-form-urlencoded`, with
`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`. Parameters (RFC 8693 §2.1):

| Parameter | Required | Meaning |
|---|---|---|
| `grant_type` | yes | the URN above |
| `subject_token` | yes | the token representing the party on whose behalf the request is made |
| `subject_token_type` | yes | a type identifier URI for it |
| `actor_token` | no | a token representing the party *acting*, i.e. the agent |
| `actor_token_type` | required if `actor_token` present | type identifier for it |
| `requested_token_type` | no | what to issue; defaults are server policy |
| `audience` | no | logical name of the target service (may repeat) |
| `resource` | no | target URI(s) (RFC 8707-style, may repeat) |
| `scope` | no | space-delimited requested scopes |

Token type identifiers in RFC 8693 §3 include `urn:ietf:params:oauth:token-type:access_token`,
`refresh_token`, `id_token`, `saml1`, `saml2` and `jwt`. The response (§2.2) is a normal token response with
one extra, required field `issued_token_type`; `token_type` is `N_A` when the issued token is not an access
token usable as a bearer token.

### 4.2 Delegation versus impersonation

The decisive difference is whether an `actor_token` is present. RFC 8693 §1.1 defines both:

- **Impersonation:** the issued token says "the subject", and the actor's identity is not visible in it. To
  the resource server the agent *is* Alice. Simple, and exactly what you do **not** want for agents:
  the audit trail and agent-specific policy vanish.
- **Delegation:** the issued token keeps the subject as `sub` and names the actor in the `act` claim. Both
  identities are visible to the resource server. This is the model for agents.

Secondary sources summarising the RFC state that delegation requires an actor token. Treat "send an
`actor_token`" as the line between the two, and make your authorization server *reject* subject-only
exchanges for agent clients.

### 4.3 The `act` claim and delegation chains

RFC 8693 §4.1 defines `act` ("actor") as a JSON object whose members identify the current actor, normally
with a `sub` (and `iss` where the issuer differs). The `act` claim may itself contain an `act`, forming a
chain in which the **outermost is the current actor** and nested ones are previous actors. An access token
produced by user → planner → worker looks like:

```json
{
  "iss": "https://as.example",
  "sub": "user:alice",
  "aud": "https://mail.internal",
  "scope": "mail.read",
  "exp": 1790000300,
  "jti": "7d1f4a1e-...",
  "act": {
    "sub": "agent:worker-17",
    "act": { "sub": "agent:planner" }
  }
}
```

Two rules from the RFC matter for design:

1. **Only the current actor and top-level claims count for access control.** The RFC says a consumer MUST
   consider only the token's top-level claims and the party identified as the current actor by `act`;
   prior actors in nested claims are informational. A five-hop chain is therefore *not* five enforceable
   hops: it is one enforceable actor plus history. If policy must depend on the root agent, put that in an
   explicit claim or make each hop re-exchange and re-authorize.
2. **The RFC does not define interoperable rules for chains**: how to preserve, extend, disclose or
   validate a delegation path across successive exchanges is left open. A 2026 individual draft
   (`draft-mw-oauth-actor-chain`) proposes chain profiles; it is a draft only (§10). In practice, enforce
   `max_depth` yourself (§13) and cap what a child can request at what its parent held.

### 4.4 The `may_act` claim

RFC 8693 §4.4 defines `may_act` as a claim *in the subject token* stating that another party is authorized
to act for the subject; the authorization server uses it to decide whether an exchange is permitted. It is
the subject-side pre-authorization: "this token may be exchanged by `agent:inbox-assistant`".

```json
{ "sub": "user:alice", "may_act": { "sub": "agent:inbox-assistant" } }
```

Use it to bind a user's token to the agent the user consented to, so that a token leaked to a different
agent cannot be exchanged by it. Note that `may_act` only constrains exchanges; it does not itself shrink
scopes (§5).

### 4.5 What the authorization server must enforce at exchange time

Token exchange is a privileged operation, and the server's policy is the actual security boundary:

1. Authenticate the exchanging client (§3.3) and look up its `AgentIdentity`.
2. Validate `subject_token` (signature, `exp`, issuer, not revoked) and `actor_token`.
3. If `may_act` is present, require the actor to match it.
4. Compute the new scope as `requested ∩ subject_token.scope ∩ agent.max_scopes` and refuse if the
   intersection is empty or if the request asked for more than the intersection.
5. Constrain `aud`/`resource` to `agent.allowed_audiences`.
6. Set a short `exp` no later than the subject token's `exp`, mint a fresh `jti`, and nest the new `act`
   around any existing one; refuse if the depth would exceed policy.
7. Log the exchange (user, agent, parent `jti`, new `jti`, scope) as an audit event (§12).

The code in §13.1 shows the request body; §13.2 shows the matching validator on the resource-server side.

---

## 5. Down-scoping and attenuation

> **In plain words.** Every hop should have *less* power than the one before it: narrower scope, one
> audience, short life, and bound to the holder so a copy is useless.
>
> **Real-world example.** The agent's session token (valid 1 hour, many APIs) becomes, for one refund, a
> 2-minute token for the payments API only, saying "refund at most 50 on order O-5512", usable only with the
> agent's private key. A stolen copy is dead in 2 minutes and useless without the key.

### 5.1 Scopes are too coarse: Rich Authorization Requests (RFC 9396)

Scopes like `payments.write` cannot express "up to 50 EUR to this one beneficiary". RFC 9396 adds the
`authorization_details` parameter: a JSON array of objects, each with a required `type` and optional common
fields (`locations`, `actions`, `datatypes`, `identifier`, `privileges`) plus type-specific ones. It is
usable in authorization requests and, relevant here, in token requests including token exchange. Resource
servers receive the granted details in the access token (JWT claim) or via introspection.

```json
[{
  "type": "refund",
  "actions": ["create"],
  "locations": ["https://refunds.internal"],
  "identifier": "order/O-5512",
  "max_amount": {"currency": "EUR", "value": "50.00"}
}]
```

`type` values and the extra fields such as `max_amount` are **defined by you** (or by a profile); RFC 9396
standardizes the envelope, not your payment semantics. The resource server must still enforce the limits:
a token that *says* "max 50" protects nothing if the API ignores it.

### 5.2 Resource indicators (RFC 8707)

The `resource` parameter lets a client name the target API in authorization and token requests, so the
server can issue a token whose `aud` is exactly that API. This is what stops a token minted for the mail API
being replayed against the calendar API (the audience-confusion class of bug). Requesting several resources
is allowed but yields wider tokens; for agents prefer **one token per audience**, even at the cost of more
exchanges.

### 5.3 Short lifetimes and one-shot tokens

For agent-held tokens, 1–15 minutes is a sound default, with per-action tokens (30–120 s) for high-risk
tools. Shortness is also the most dependable revocation mechanism: most revocation schemes are
best-effort (§12), while expiry is guaranteed. Pair with a refresh model that the *agent runtime does not
hold*: a broker holds the refresh token and mints access tokens per tool call.

### 5.4 Sender-constrained tokens: DPoP (RFC 9449) and mTLS (RFC 8705)

A bearer token is valid for whoever holds it, and agent contexts leak. Sender-constraining binds the token
to a key the holder must prove it has.

| | DPoP (RFC 9449) | mTLS-bound tokens (RFC 8705) |
|---|---|---|
| Binding | token carries `cnf.jkt` (thumbprint of the client's public key) | token carries `cnf."x5t#S256"` (thumbprint of the client certificate) |
| Proof | each request carries a `DPoP` header: a JWT signed with the private key, covering HTTP method and URL (`htm`, `htu`), a `jti`, `iat`, and the token hash (`ath`) | the TLS handshake with that client certificate |
| Where it works | application layer, through proxies and TLS terminators | needs mTLS to reach the resource server (hard behind some gateways) |
| Agent fit | good for agents calling SaaS APIs over HTTPS; key can live in the runtime or a broker | good inside a mesh where mTLS already exists |

Neither protects against an attacker who controls the agent process and can *use* the key; they defeat
*exfiltration and replay* of tokens, which is the common leak path (logs, traces, prompt injection that makes
the agent print its token). Keep the private key outside the model's reach (broker or HSM/TPM-style store),
never in the prompt or tool outputs.

### 5.5 Attenuation across sub-agents

Define one invariant and enforce it in the exchange policy (§4.5): **a child's grant is a subset of its
parent's grant on every axis**: scopes, audiences, `authorization_details` limits, lifetime and depth
budget. With token exchange, the authorization server enforces this by comparing the child request with the
parent token. With capability tokens (§6) the property is built into the token format.

---

## 6. Capability tokens as an alternative: macaroons and biscuits

> **In plain words.** An alternative to "ask the server for a narrower token each time": a token you can
> narrow *yourself*, offline, by adding restrictions, but never widen.
>
> **Real-world example.** The orchestrator holds a token for "read this folder". Before handing it to a
> sub-agent it appends "only files ending .pdf, expires in 10 minutes". The sub-agent can add more
> restrictions, but cannot remove any.

These are **alternatives** to the OAuth-centred design above, not part of it, and they are less widely
supported by commercial APIs.

- **Macaroons** (Birgisson et al., Google, NDSS 2014): bearer credentials whose caveats (restrictions) are
  chained with HMACs, so anyone holding a macaroon can add a caveat but cannot remove one. Verification
  needs the root key, so only the issuing service (or one sharing the key) can verify. Third-party caveats
  allow "valid only if this other service vouches".
- **Biscuit** (biscuitsec.org): similar attenuation idea with public-key signatures, so *any* party with the
  public key can verify without the secret, plus a Datalog-based policy language for caveats/checks.

Trade-offs relative to OAuth token exchange: offline delegation with no AS round-trip per hop (a real win
for deep agent trees), and attenuation is structural; against that: no ecosystem of IdPs, consent screens
and introspection, harder revocation (you need short TTLs or a revocation-ID list), and caveat languages are
a new surface to get wrong. A pragmatic hybrid: OAuth for the user → agent grant, a capability format
inside the agent runtime for sub-agent fan-out. Treat this as design inspiration unless you control both
issuer and verifier.

---

## 7. Asynchronous and background agents: CIBA, step-up, consent

> **In plain words.** Many agents run when the user is not looking at a browser. When one needs permission,
> it must reach the human on another channel, say clearly what it wants, and wait.
>
> **Real-world example.** A nightly reconciliation agent finds 3 invoices to write off. It cannot pop up a
> login page. It asks the authorization server for approval; Alice's phone shows "Reconciliation Agent
> (finance team) wants to write off 3 invoices, total 148.20 EUR. Approve?". She taps yes; the agent
> receives a token valid for 5 minutes.

### 7.1 CIBA

**OpenID Connect Client-Initiated Backchannel Authentication (CIBA) Core 1.0** (OpenID Foundation) lets a
client start authentication of a user **without a browser redirect on the client's device**: the client
calls the backchannel authentication endpoint with a hint identifying the user (`login_hint`,
`id_token_hint` or `login_hint_token`), a `scope`, and optionally a `binding_message` and `user_code`; the
OP authenticates and obtains consent on the user's *own* device (push, app); the client then gets tokens by
**poll**, **ping** or **push** delivery mode. The grant type at the token endpoint is
`urn:openid:params:grant-type:ciba`, using the `auth_req_id` returned from the first call. `binding_message`
is the human-readable text that ties the approval prompt to the transaction the client is attempting. CIBA
is an OpenID *Final* specification and is the established fit for out-of-band, consent-gated agent actions.

Design points:

1. **Put the specifics in `binding_message` and/or RAR details**, not just "Agent wants access": action,
   target and amount. A consent that cannot be read is a consent that gets approved blindly.
2. **Name the agent.** The consent screen should show the agent's registered display name, owner and
   version, so users can tell "Reconciliation Agent" from "some app". Do not let the agent supply its own
   free-text description unchecked, or a hijacked agent will write a flattering one.
3. **Bound the grant**: scope or RAR limits, short TTL, single use for destructive actions.
4. **Timeouts fail closed**: no answer means no token and a recorded `expired` outcome, not a retry loop that
   spams the user.
5. **Notification fatigue** is a real attack surface: an agent that triggers many prompts trains users to tap
   yes. Rate-limit approvals per agent and batch related actions into one prompt.

### 7.2 Step-up approval for destructive actions

Combine the PDP (§11) with CIBA or an in-product approval. The PDP returns one of `permit`, `deny`,
`step_up`; the PEP turns `step_up` into an approval request and blocks (or suspends the agent graph via an
interrupt, ch 21) until a human answers. Typical triggers: irreversible operations (delete, send externally,
transfer), amounts over a limit, first use of a new recipient or destination, access to a higher data
classification, or any action taken in a context that ingested untrusted content (the Rule of Two in ch 17
§4.7: untrusted input + sensitive data + external action should need a human). After approval, mint a
**single-use, action-bound** token (RAR `authorization_details` containing the exact action and parameters,
TTL ≤ 5 minutes) so approval for "delete folder X" cannot be reused for "delete folder Y".

### 7.3 Consent that survives the session

For background work launched by a user, convert consent into an explicit durable record: grant ID, user,
agent (and version), scopes/details, expiry, revocation handle. Show it in a "connected agents" page where
the user can see and revoke it. Do not silently reuse the user's interactive refresh token for a background
agent; that is how a 1-hour chat session becomes a standing 90-day power of attorney.

---

## 8. MCP authorization as a concrete case

> **In plain words.** MCP (the Model Context Protocol, ch 26) turned the ideas above into a required part of
> its HTTP transport: an MCP server is an OAuth *resource server*, tells clients where to get tokens, and
> must refuse tokens that were issued for something else.
>
> **Real-world example.** A coding agent connects to a remote issue-tracker MCP server. The server answers
> 401 with a pointer to its metadata; the client discovers the authorization server, runs the OAuth flow
> asking for a token for *that server only*, and every later call carries it. The server never forwards that
> token to GitHub; for GitHub it uses its own credential or a separate exchange.

Facts below are from the MCP authorization specification (revision **2025-11-25**, carried forward into
**2026-07-28**) plus RFC 9728. The 2026-07-28 changelog adds three authorization changes, listed at the end
of this list.

- **Roles.** The MCP server acts as an OAuth 2.1 **resource server**; the MCP client is an OAuth client;
  a separate authorization server issues tokens (it may be the same product, but it is a distinct role).
- **Discovery with Protected Resource Metadata (RFC 9728).** An MCP server must implement discovery of its
  authorization server: either include the metadata document URL in the `WWW-Authenticate` header
  (`resource_metadata=...`) on a `401`, or serve metadata at a well-known URI. The metadata document lists
  `authorization_servers`; if several, the client chooses (per RFC 9728 §7.6).
- **Resource indicators.** Clients MUST send the RFC 8707 `resource` parameter in authorization and token
  requests, naming the MCP server, so the token's audience is that server.
- **Audience validation and no token passthrough.** The server must validate that a token was issued *for
  it* and must not accept tokens minted for other resources. Equally it must **not pass the received token
  through to upstream APIs**. If the MCP server needs an upstream API it acts as a *client* in its own right
  (own credentials, or a token exchange where the user's token is the `subject_token`, §4). Passthrough
  recreates the confused deputy: the upstream cannot tell the MCP server's call from the user's, and a token
  scoped to the MCP server gets honoured by a service it was never meant for.
- **Client registration.** Clients can be pre-registered, use Client ID Metadata Documents (the client ID is
  an HTTPS URL that serves the client's metadata), or use RFC 7591 Dynamic Client Registration. **2026-07-28
  deprecates Dynamic Client Registration** in favour of Client ID Metadata Documents. DCR stays available for
  backwards compatibility with authorization servers that don't support metadata documents.
- **Issuer checks (2026-07-28).** Authorization servers SHOULD include the RFC 9207 `iss` parameter in
  authorization responses. Clients MUST validate a present `iss` against the recorded issuer before redeeming
  the code, which defends against mix-up attacks when a client talks to many authorization servers. Client
  credentials are bound to the authorization server that issued them, and must not be reused with another one.

What MCP authorization does **not** give you: it authenticates a *client* (the agent host application) to a
*server*; it says nothing about which agent sub-component or sub-agent acted, nor about the human-versus-
agent distinction beyond what your authorization server puts in the token. Add the `act` claim (§4), per
-tool policy (§11) and audit (§12) on top. Threats specific to MCP (tool poisoning, rug pulls, SSRF in
discovery) belong in ch 28; the transport and tool model in ch 26.

---

## 9. Workload identity for agent runtimes: SPIFFE/SPIRE and WIMSE

> **In plain words.** Before an agent can exchange tokens it must prove *what it is* without a stored
> secret. Workload identity systems hand each running process a short-lived, automatically rotated
> certificate or token after checking where and how it is running.
>
> **Real-world example.** An agent pod starts on Kubernetes. The platform's agent attests the pod's
> namespace and service account, and gives it an identity `spiffe://corp.example/ns/agents/sa/refund-agent`
> valid for one hour. The token service accepts that as the agent's client authentication; no API key was
> ever deployed.

- **SPIFFE** (Secure Production Identity Framework for Everyone, a CNCF project): defines the identity
  name (a SPIFFE ID, `spiffe://trust-domain/path`) and the document carrying it (an **SVID**, as an X.509
  certificate or a JWT). **SPIRE** is the reference implementation: node and workload attestation, then
  issuing and rotating SVIDs. Workloads fetch SVIDs from a local agent over the Workload API, so there is
  no secret to provision.
- **How it fits agents.** Use the SPIFFE ID as the agent's *runtime* identity and map it to the
  `AgentIdentity` record (§3.1) and OAuth client; accept it for client authentication at the token endpoint
  (e.g. SVID as `private_key_jwt`/mTLS client credentials). Give each agent type its own SPIFFE ID, and
  sandboxes for generated code a *different*, weaker one.
- **What it does not give you.** SPIFFE identifies a workload, not a user or a delegation. It proves "this
  is the refund-agent pod", not "refund-agent is acting for Alice with limit 50". That is why it is the
  *bottom* layer, under token exchange.
- **WIMSE.** The IETF **Workload Identity in Multi System Environments** working group works on
  conveying workload identity and security context across systems (cloud boundaries, multi-hop calls). Its
  architecture draft `draft-ietf-wimse-arch` is an active working-group Internet-Draft (informational; the
  latest revision seen in search was -08, July 2026). Companion drafts address workload-to-workload
  authentication and token formats. As with all drafts: not final, may change; use SPIFFE/OAuth as
  deployed today and follow WIMSE for direction.

---

## 10. Agent-specific IETF drafts: what exists and how settled it is

> **In plain words.** Several people are proposing ways to extend OAuth for agents. They are proposals.
>
> **Real-world example.** If a vendor says "our product implements the IETF agent-delegation standard",
> ask which RFC number. If the answer is a draft name, it is a proposal that may change or expire.

**State of the standards (October 2026): not settled.** No RFC specifically standardizes agent delegation.
What is standardized is the base: RFC 6749/OAuth 2.0, RFC 8693, RFC 8707, RFC 9396, RFC 9449, RFC 8705,
RFC 9728. The following are Internet-Drafts. Drafts expire or change quickly, so check the revision number and
status on datatracker.ietf.org before relying on any of them.

| Draft | What it proposes (per search-result summaries) | Status as observed |
|---|---|---|
| `draft-sweeney-wimse-credential-delegation-00` ("Credential Delegation Protocol for AI Agents in Multi-System Environments", K. Sweeney) | Profiles RFC 8693, RFC 9449, RFC 9396 and CIBA for agents: ephemeral-key agent identity, capability-scoped delegation tokens, wrapped credentials so agents never see underlying OAuth tokens, consent-gated flows, cascading revocation, audit chains. States it adds no new token formats or grant types. | Individual submission, -00, dated July 2026; not adopted by a working group; no IESG state |
| `draft-oauth-ai-agents-on-behalf-of-user` (Dissanayaka et al.) | Extends the authorization-code flow: -00 added a `requested_agent` parameter and a dedicated grant type; -01 switched to `requested_actor` in authorization requests and `actor_token` in token requests; claims record user → agent delegation. Goal: explicit consent and auditability. | Individual draft, versions -00/-01 (May 2025) shown as expired on datatracker, a later -02 mirrored; do not assume it is alive |
| `draft-mw-oauth-actor-chain` | Profiles for preserving, extending and validating delegation chains across successive token exchanges, keeping existing meanings of `sub`, `act`, `may_act` | Individual draft (-01 seen, 2026); gap-filler for the chain-rules gap noted in §4.3 |
| `draft-ietf-wimse-arch` | WIMSE architecture (workload identity across systems) | WG draft, informational, -08 (July 2026) |

Guidance: (1) **don't depend on a draft's wire format**; wrap your use behind your own interface. (2) The
drafts above largely *compose* RFC 8693 + RAR + DPoP + CIBA, which is what §4–§7 already describe, so
building on those RFCs positions you for any of them. (3) Re-check datatracker before relying on
status; names and revisions here will age.

---

## 11. Policy enforcement: PEP, PDP, ReBAC and tenant isolation

> **In plain words.** Split "who enforces" from "who decides". A guard at the door (PEP) asks a rule engine
> (PDP) a yes/no question for every request. The model is on the wrong side of the door.
>
> **Real-world example.** All tool calls pass a gateway. It asks the PDP: "agent refund-agent, for user
> alice in tenant acme, action refund.create on order/O-5512, amount 499.90?". The PDP answers `step_up`
> because the agent's rule caps unattended refunds at 50.

### 11.1 Where decisions live

- **Policy Enforcement Point (PEP):** code on the call path that cannot be skipped: an API gateway, the
  tool executor/pipeline (ch 24 §4.2), the MCP server or an MCP gateway/proxy. It validates the token
  (§13.2), builds the authorization question, calls the PDP, and enforces the answer including `step_up`.
  Put it **outside the model-reachable process**; if the agent can call the tool without the PEP, there is
  no PEP.
- **Policy Decision Point (PDP):** a policy engine given `(principal, actor, action, resource, context)` and
  returning permit/deny (plus obligations like "require approval"). Common choices: **OPA** (Rego, CNCF),
  **Cedar** (AWS's open-source policy language, with default-deny and forbid-overrides-permit semantics),
  or a relationship engine (§11.2). Keep policy as versioned code, reviewed like code, with tests.
- **Policy Information Point (PIP):** where the PDP fetches facts (user's groups, order ownership, data
  classification).

Evaluation rule for agents: **permit only if both** the user's entitlement **and** the agent's grant allow
it, and no forbid applies. §13.3 implements this shape in 30 lines.

### 11.2 Relationship-based access for document-level permissions (RAG plus agents)

RAG makes authorization a retrieval problem: the answer is built from chunks, and every chunk must be
visible to the *requesting user*, or the system leaks content through the model. Document-level ACLs in
a vector store go stale; the scalable model is **relationship-based access control (ReBAC)**, popularised by
Google's **Zanzibar** (USENIX ATC 2019) and available open source as **OpenFGA** (CNCF) and others. Facts are
tuples like `doc:handbook#viewer@group:eng#member`, and permission is a graph query, `check(user, viewer,
doc)`.

Two integration patterns for RAG plus agents:

1. **Pre-filter (preferred).** Resolve the user's accessible set (or a bounded ACL token/group list) first and
   pass it as a metadata filter to the vector search, so unauthorized chunks are never retrieved. Cheap when
   the set is small or expressible as groups; use `list-objects`-style queries where supported.
2. **Post-filter.** Retrieve top-K, then `check` each candidate and drop denied ones. Simple and always
   correct, but wastes recall: if 60% of hits are denied, K=10 leaves 4 usable chunks (arithmetic, not
   measured). Over-fetch (K×3) and re-rank after filtering.

For agents the identity passed to `check` is the **delegating user**, not the agent, and the agent's own
policy applies on top. An autonomous agent has no user; give it its own tuples and its own document grants,
never "all documents". Re-check at tool time as well as at retrieval time: a chunk retrieved last turn may
be revoked this turn, and agents carry memory (ch 25) which must not outlive the permission that created it.

### 11.3 Per-tenant isolation

Identity is the first line of tenant isolation, not the only one: `tenant_id` must be a verified claim (from
the token), never a tool argument; vector namespaces/collections and caches are keyed by it; the agent
registry records `tenant_scope` (§3.1) so a tenant-scoped agent cannot request platform tokens; and the PDP
denies by default when `resource.tenant != principal.tenant`. Ch 24 §5.4 covers per-tenant tool scoping;
the delta here is enforcing it in the *token* so a bug in the tool registry cannot cross tenants.

---

## 12. Audit and revocation

> **In plain words.** Log one record per tool call that ties together the human, the agent, the session and
> the exact token. Make it possible to turn off one agent instantly.
>
> **Real-world example.** A customer disputes a refund. One query on `jti`/`session` shows: Alice asked,
> `refund-agent v3` called `issue_refund`, token `7d1f…` was minted by an exchange at 14:02:11, the PDP
> permitted it under rule R-17, and the downstream refund ID. No guessing.

### 12.1 The audit tuple

Ch 24 §6.5 covers audit logging generally. The identity-specific delta is the join key set:

| Field | Source | Why |
|---|---|---|
| `user` (`sub`) | token | whose authority |
| `agent` (`act.sub`) and `agent_version` | token / registry | who acted, which build |
| `actor_chain` | nested `act` | who delegated to whom (informational, §4.3) |
| `session` / `sid` / trace ID | runtime | groups a whole task; joins to traces (ch 24 §13.3) |
| `jti` | token | exact credential used; joins to exchange logs |
| `tool`, `args_hash`, resource | PEP | what was attempted (hash to keep PII out) |
| `decision`, `policy_version`, `rule_id` | PDP | why allowed or denied |
| `approval_id` | step-up flow | the human consent that unlocked it |
| `outcome` | executor | success, failure, unknown |

Log **denials** and **step-ups** too, and write to append-only storage. Never log token values, only `jti`.
§13.4 builds the row.

### 12.2 Revocation: what works, what is best-effort

- **Expiry** is the only guaranteed mechanism; hence §5.3's short TTLs.
- **Token revocation** (RFC 7009) and **introspection** (RFC 7662) let a resource server ask the AS "is this
  still active"; cost is a round-trip per check (cache for seconds, not minutes). Self-contained JWTs are
  not revoked until `exp` unless the resource server also checks a deny-list keyed by `jti`, or by
  `(agent_id, issued_before)`.
- **Grant revocation**: deleting the durable consent record (§7.3) stops future exchanges and refreshes.

### 12.3 The per-agent kill switch

Because every token carries `act.sub`, you can revoke by agent identity rather than by token:

1. Set `status="suspended"` in the agent registry (§3.1). The AS refuses new exchanges and client
   authentications for it immediately.
2. Publish `agent:refund-agent` plus a `not_before` timestamp to the PEP's deny-list (push or short-poll).
   The PEP rejects any token whose current actor matches and whose `iat` precedes `not_before`.
3. Revoke refresh tokens/grants held by the broker; kill running sandboxes.
4. Keep the switch independent of the agent runtime: the thing being switched off must not be the thing
   that processes the off-signal.

With 5-minute tokens, worst-case residual access after step 1 is ≤ 5 minutes even if step 2 fails
(arithmetic from the TTL). Test the kill switch in game days like any other incident control.

---

## 13. Code: exchange request, validator, toy PDP

Runs with the Python standard library (tested with `python3 -I`). The HS256 JWT here is a demo; production
should use asymmetric keys and a vetted library (PyJWT, authlib) and fetch keys by `kid` from the AS's JWKS.

### 13.1 Token exchange request shape

```python
import json
from urllib.parse import urlencode

GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
AT    = "urn:ietf:params:oauth:token-type:access_token"

def build_exchange_request(subject_token, actor_token, audience, scope,
                           resources=(), authz_details=None):
    """Form body for POST /token (RFC 8693 §2.1, RFC 8707 resource, RFC 9396 details)."""
    pairs = [("grant_type", GRANT),
             ("subject_token", subject_token), ("subject_token_type", AT),
             ("actor_token", actor_token),     ("actor_token_type", AT),
             ("requested_token_type", AT),
             ("audience", audience), ("scope", " ".join(scope))]
    pairs += [("resource", r) for r in resources]
    if authz_details:
        pairs.append(("authorization_details",
                      json.dumps(authz_details, separators=(",", ":"))))
    return urlencode(pairs)
```

Send it with the agent's client authentication (§3.3) and, for DPoP, a `DPoP` header. The response carries
`access_token`, `issued_token_type`, `token_type`, `expires_in` and `scope` when it differs from the request.

### 13.2 The resource-server validator

```python
import base64, hashlib, hmac, json, time

class AuthzError(Exception): ...

def _b64(b):  return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def sign(claims, key: bytes):                       # demo issuer
    h = _b64(json.dumps({"alg": "HS256", "typ": "at+jwt"}).encode())
    p = _b64(json.dumps(claims).encode())
    s = _b64(hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"

def verify(tok, key: bytes):
    h, p, s = tok.split(".")
    good = _b64(hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(s, good): raise ValueError("bad signature")
    return json.loads(_unb64(p))

def act_chain(claims):
    """Outermost act = current actor; deeper = prior actors (RFC 8693 §4.1)."""
    out, a = [], claims.get("act")
    while a is not None:
        out.append(a["sub"]); a = a.get("act")
    return out

def validate(tok, key, *, my_audience, needed_scope, allowed_actors,
             max_depth=2, now=None):
    c = verify(tok, key)                             # also pin alg + iss in real code
    now = now or time.time()
    if c["exp"] <= now:                       raise AuthzError("expired")
    aud = c["aud"] if isinstance(c["aud"], list) else [c["aud"]]
    if my_audience not in aud:                raise AuthzError("wrong audience")
    if not set(needed_scope) <= set(c.get("scope", "").split()):
                                              raise AuthzError("missing scope")
    chain = act_chain(c)
    if not chain:        raise AuthzError("no actor: impersonation-style token refused")
    if len(chain) > max_depth:                raise AuthzError("delegation chain too deep")
    if chain[0] not in allowed_actors:        raise AuthzError("current actor not allowed")
    if "jti" not in c:                        raise AuthzError("no jti")
    return c
```

Design notes: only `chain[0]` (the current actor) is used for the allow decision (§4.3); depth is capped
because the RFC leaves chain semantics open; a missing `act` is rejected because an agent endpoint should
never accept impersonation tokens; add `cnf`/DPoP proof checking (§5.4) and a `jti`/agent deny-list lookup
(§12.3) before returning in production.

### 13.3 A toy PDP: user entitlement AND agent grant, default deny

```python
from dataclasses import dataclass, field

@dataclass
class Rule:
    actor: str; action: str; resource_prefix: str
    max_amount: float | None = None
    effect: str = "permit"                  # "permit" | "forbid"

@dataclass
class PDP:
    rules: list[Rule] = field(default_factory=list)
    def decide(self, *, user_scopes, actor, action, resource, amount=None):
        if action not in user_scopes:
            return "deny", "user lacks scope"                    # user side
        hit = [r for r in self.rules if r.actor == actor and r.action == action
               and resource.startswith(r.resource_prefix)]
        if any(r.effect == "forbid" for r in hit):
            return "deny", "forbid rule"                         # forbid beats permit
        for r in hit:
            if r.max_amount is None or (amount is not None and amount <= r.max_amount):
                return "permit", "ok"                            # agent side
        if hit:
            return "step_up", "over limit: needs human approval"
        return "deny", "no rule"                                 # default deny
```

Production PDPs add tenant checks, ReBAC lookups (§11.2), context (time, risk score, "context contains
untrusted content") and obligations. The semantic shape, default deny + forbid override + both sides must
allow, is the portable part (OPA and Cedar express it natively).

### 13.4 The audit row

```python
import hashlib, json, time

def audit(claims, tool, args, decision):
    return {"ts": time.time(), "user": claims["sub"], "agent": claims["act"]["sub"],
            "session": claims.get("sid"), "jti": claims["jti"], "tool": tool,
            "args_sha256": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest(),
            "decision": decision}
```

Running the combined demo prints the actor chain, a `permit` for a 20-unit refund, `step_up` for 499.9, and
four rejections (wrong audience, missing scope, chain too deep, expired).

### 13.5 Wiring into the tool-registry lab

`ai-rag/labs/tool-registry/` validates tool schemas and annotations before execution (ch 24). Add the
identity checks at the same boundary: on each call, `validate()` the token, build `ctx` from its claims
(never from model arguments, ch 24 §6.1), ask the PDP, then either execute, refuse, or raise an interrupt for
approval. A tool's registry entry already declares required scopes and read-only/destructive hints; map
"destructive" to "PDP must return permit with an `approval_id`", so the registry becomes the policy input
for step-up.

---

## 14. Anti-patterns

1. **One shared API key for all agents** (§2). Over-privilege, no attribution, no per-agent revocation.
2. **Impersonation tokens for agents.** Dropping `actor_token` hides the agent; policy and audit lose it.
3. **Passing the user's token straight to every tool** without audience binding or agent identity (and, in
   MCP, token passthrough, §8).
4. **Scopes broad enough "for convenience"** (`*`, `admin`, `payments.write` for a read task).
5. **Authorization facts as tool arguments** (`user_id`, `role`, `approved_by` chosen by the model).
6. **Trusting nested `act` entries for access control.** Only the current actor is authoritative (§4.3).
7. **Long-lived bearer tokens in the agent's context, logs or traces.** One leak = months of access.
8. **Reusing the interactive session's refresh token for background agents** (§7.3).
9. **Approval prompts that say "Agent wants access"** with no action, target or amount; or that fire so often
   users approve blindly.
10. **Agent-supplied self-description on consent screens.** A hijacked agent will write a trusted-sounding one.
11. **Retrieval-time filtering only** in RAG: permissions change between retrieval and use (§11.2).
12. **Tenant ID from the request body** instead of a verified token claim.
13. **A PEP inside the agent process** the model's code can bypass or monkey-patch.
14. **No tested kill switch**, or one that needs the compromised agent's cooperation.
15. **Building product dependencies on an unadopted draft's wire format** (§10).

---

## 15. Interview questions

**Q1. Why is a shared service-account key for all agents wrong, and what replaces it?**
It carries the union of every agent's needs, hides which user and which agent acted, cannot be revoked per
agent and is a long-lived, portable secret. A prompt-injected agent inherits all of it. Replace with a
per-agent identity (workload identity or registered client), delegated tokens from token exchange for
user-initiated work (`sub`=user, `act`=agent), the agent's own narrow client-credentials identity for truly
autonomous work, and a broker for third-party secrets.

**Q2. Delegation versus impersonation in RFC 8693: which for agents and why?**
Delegation. With an `actor_token`, the issued token keeps the user as `sub` and names the agent in `act`,
so the resource server and audit see both. Impersonation makes the agent indistinguishable from the user,
losing agent-specific policy and attribution.

**Q3. What do `act` and `may_act` mean, and what is a common misreading?**
`act` names the current actor and nests earlier actors; `may_act` sits in the subject token and states who may
act for the subject, so the AS can authorize an exchange. The misreading: treating nested `act` entries as
enforceable. The RFC says access control considers top-level claims and the current actor only; earlier
actors are informational. The RFC also leaves chain-validation rules open, so enforce a depth cap yourself.

**Q4. A planner delegates to three workers. How do you stop a worker from exceeding the planner?**
Make each worker obtain its token by exchange with the planner's token as `subject_token` (or the user's as
subject and the planner as part of the chain), and have the AS enforce child ⊆ parent on scope, audience,
`authorization_details` limits and lifetime, plus a depth budget. Alternatively use attenuable capability
tokens where narrowing is structural, accepting weaker ecosystem support.

**Q5. Scopes versus RAR (RFC 9396)?**
Scopes are coarse strings; RAR carries structured, typed authorization details (type, actions, locations,
identifier, custom limits like `max_amount`). Use RAR for transaction-bound grants such as "refund up to 50
on this order". The resource server must still enforce the values.

**Q6. How do resource indicators and DPoP differ in what they prevent?**
RFC 8707 `resource` fixes the token's audience, preventing replay at a different API. DPoP (RFC 9449) binds
the token to a client key and a request, so a stolen token without the key is useless. Audience stops
cross-service reuse; sender-constraint stops theft-and-replay at the same service. Use both.

**Q7. Design approval for a background agent that wants to delete data.**
PDP returns `step_up` for destructive operations; the PEP starts a CIBA-style out-of-band request with a
`binding_message` naming agent, action and target; the OP authenticates the user on their device; on
approval the AS issues a single-use token bound (via RAR) to that exact action with TTL of minutes. Timeouts
fail closed, prompts are rate-limited, and the approval ID is stored in the audit row.

**Q8. What does MCP require of a server's authorization, and why forbid token passthrough?**
(Revision 2025-11-25.) The server is an OAuth 2.1 resource server; it advertises its authorization server via
RFC 9728 Protected Resource Metadata (the `WWW-Authenticate` `resource_metadata` pointer or a well-known
URI); clients send the RFC 8707 `resource` parameter; the server validates audience and does not forward the
client's token upstream. Passthrough is a confused-deputy hole: upstreams cannot distinguish the server from
the user, and tokens minted for one service get honoured by another.

**Q9. How do SPIFFE and OAuth token exchange relate?**
SPIFFE/SPIRE gives a workload a verifiable runtime identity (SVID) with no stored secret; it answers "what is
this process". Token exchange answers "who is it acting for and with what limits". Use the SVID to
authenticate the agent at the token endpoint and then exchange for a delegated, audience-bound token.

**Q10. Are there IETF standards for agent delegation?**
Not specifically. The base (RFC 8693, 8707, 9396, 9449, 8705, 9728) is standardized. Agent-specific work
(for example an individual draft profiling token exchange, DPoP, RAR and CIBA for agents, and drafts for
actor chains and on-behalf-of flows) is Internet-Draft stage, some expired, none a final standard. Build on
the RFCs and isolate draft-specific details.

**Q11. How do you enforce document-level permissions in RAG with agents?**
ReBAC (Zanzibar-style, e.g. OpenFGA) tuples; the delegating user's identity feeds a pre-filter on the vector
query where possible and a post-filter `check` with over-fetch otherwise; autonomous agents get their own
grants; re-check at tool time and expire agent memory when permissions are revoked.

**Q12. How do you kill one misbehaving agent without taking down the others?**
Suspend its registry record so the AS refuses new exchanges; push `(agent_id, not_before)` to PEP deny-lists;
revoke its grants and refresh tokens; terminate sandboxes. Short token TTLs bound the residual window.
Because tokens carry `act.sub`, revocation is by agent identity, not by hunting tokens.

**Q13. What goes in an agent audit record that a normal service log lacks?**
User, agent and version, nested actor chain, session/trace ID, token `jti`, tool and argument hash,
PDP decision with policy version and rule, and approval ID, so the delegation, the credential and the
human consent are all joinable.

---

## 16. Lab exercises

All use Python stdlib; the reference code is §13 (tested). Put files in a scratch directory.

1. **Run the validator (30 min).** Copy §13.2–§13.4 into `ident.py`. Issue a token with
   `act={"sub":"agent:a","act":{"sub":"agent:planner"}}`. Verify it passes with `max_depth=2` and fails with
   `max_depth=1`, with the wrong audience, a missing scope and a past `exp`. *Expected:* four distinct
   `AuthzError` messages.
2. **Add `may_act` enforcement (45 min).** Write a function `exchange(subject_claims, actor_id, requested_scope,
   agent_registry)` that mints a child token only if `may_act.sub == actor_id`, scope is
   `requested ∩ subject ∩ agent.max_scopes` and the child `exp` ≤ the subject's. Test that agent B cannot
   exchange a token whose `may_act` names agent A, and that requesting a scope above the ceiling fails.
3. **Extend the PDP (45 min).** Add a `tenant` field to principal and resource and a rule that denies on
   mismatch; add a `forbid` rule for recipients outside `@corp.example`. Add a "context_untrusted" flag that
   downgrades any `permit` for external-send to `step_up`. Write 8 table-driven tests.
4. **Kill switch (30 min).** Add an in-memory deny-list `{agent_id: not_before}` checked in `validate()`.
   Show a token minted before `not_before` is rejected and one minted after is not (unless the registry says
   suspended). Measure the worst-case window as TTL.
5. **Join the audit trail (30 min).** Write each `audit()` row as a JSON line. With `jq` or Python, answer:
   "for user X, list agents and tools used in session S" and "which `jti`s did agent A use between times T1
   and T2".
6. **DPoP sketch (60 min).** Using `cryptography` (or `PyJWT` with ES256), build a DPoP proof JWT with
   `htm`, `htu`, `iat`, `jti` and verify it against `cnf.jkt` in the access token; reject replay of a `jti`
   inside a 60-second window. *Expected:* a copied token without the key is rejected.
7. **Document-level filter (45 min).** Build a dictionary ReBAC (`user -> groups -> docs`), fake vector hits
   with scores, and compare pre-filter versus post-filter recall at K=10 when 60% of hits are denied.
   Report the usable count and how over-fetching changes it.

---

## 17. Real-world cases

Each case below is either a publicly documented source or explicitly a composite.

### Case 1 — Illustrative scenario (composite, not a specific company): the shared key and the helpful FAQ bot

**Setup.** A SaaS vendor runs 6 agents on one platform key with read access to all customer records
(needed by only 2 of them). A support FAQ agent reads untrusted ticket text.
**What happens.** A ticket contains injected instructions; the FAQ agent, which holds the platform key,
fetches another customer's records and quotes them in a reply. The audit shows `platform-key`, 1 of 6 agents,
unknown user.
**Arithmetic (illustrative).** If 6 agents each need 3 scopes and the shared key carries the union, with 2
scopes shared, that is up to 16 scopes on every agent versus 3 needed, about a 5× over-grant. This is a
counting example, not a measurement.
**Fix.** Per-agent identities with ceilings (§3.1), delegated tokens with user binding (§4), PDP deny on
tenant mismatch (§11.3). Same injection now yields `deny: tenant mismatch` and a row naming user, agent, `jti`.

### Case 2 — Illustrative scenario (composite): consent that named nothing

**Setup.** A background agent triggers an approval prompt, "An application wants to access your account".
Users approve 94% of prompts in under 3 seconds (illustrative numbers).
**What happens.** A hijacked agent requests a mailbox-delete grant and the prompt is approved by reflex.
**Fix.** CIBA `binding_message` and RAR details naming agent, action and target; single-use token; rate limit
on prompts per agent per day; destructive actions require typed confirmation of the target name.

### Case 3 — Publicly documented class: MCP token passthrough and the confused deputy

The MCP authorization specification and the MCP security best-practices material identify token
passthrough (a server accepting a client token and forwarding it upstream, or accepting tokens not issued
for it) as an explicit anti-pattern and prohibit it; the confused-deputy risk for MCP proxy servers using
static client IDs with third-party authorization servers is also documented there. See the sources for the
specification pages; this chapter did not independently reproduce an exploit.
**Lesson.** Audience validation and a separate upstream credential are protocol requirements, not
hardening extras.

### Case 4 — Illustrative scenario (composite): the kill switch that needed the agent

**Setup.** An agent fleet's "disable" flag is read by the agent's own startup code. A looping agent never
restarts.
**What happens.** Operators flip the flag; nothing changes for 41 minutes until the 1-hour tokens expire.
(41 minutes is an illustrative figure.)
**Fix.** Revoke at the AS and the PEP (§12.3), not in the agent; 5-minute tokens cap exposure; game-day test
the switch quarterly.

---

## Sources

Protocol and standards references. Section numbers refer to the published RFC texts; drafts are cited at the
revision current in 2026-10.

- RFC 8693, OAuth 2.0 Token Exchange: https://datatracker.ietf.org/doc/rfc8693/
- RFC 8707, Resource Indicators for OAuth 2.0: https://datatracker.ietf.org/doc/rfc8707/
- RFC 9396, OAuth 2.0 Rich Authorization Requests: https://datatracker.ietf.org/doc/rfc9396/
- RFC 9449, OAuth 2.0 Demonstrating Proof of Possession (DPoP): https://datatracker.ietf.org/doc/rfc9449/
- RFC 8705, OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens: https://datatracker.ietf.org/doc/rfc8705/
- RFC 9728, OAuth 2.0 Protected Resource Metadata: https://datatracker.ietf.org/doc/rfc9728/
- RFC 7009 (Token Revocation): https://datatracker.ietf.org/doc/rfc7009/ ; RFC 7662 (Token Introspection): https://datatracker.ietf.org/doc/rfc7662/
- OpenID Connect Client-Initiated Backchannel Authentication (CIBA) Core 1.0: https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html
- MCP authorization specification (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- MCP security best practices: https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices
- SPIFFE / SPIRE: https://spiffe.io/
- IETF WIMSE architecture draft: https://datatracker.ietf.org/doc/draft-ietf-wimse-arch/
- Credential Delegation for AI Agents (draft, -00): https://datatracker.ietf.org/doc/draft-sweeney-wimse-credential-delegation/
- OAuth on-behalf-of-user for AI agents (draft): https://datatracker.ietf.org/doc/draft-oauth-ai-agents-on-behalf-of-user/01/
- Actor-chain draft (-01 mirror): https://ietf.potaroo.net/ids/draft-mw-oauth-actor-chain-01.txt
- Zanzibar: Google's Consistent, Global Authorization System (USENIX ATC 2019): https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/
- OpenFGA: https://openfga.dev/ ; Open Policy Agent: https://www.openpolicyagent.org/ ; Cedar: https://www.cedarpolicy.com/
- Macaroons (NDSS 2014): https://research.google/pubs/macaroons-cookies-with-contextual-caveats-for-decentralized-authorization-in-the-cloud/ ; Biscuit: https://www.biscuitsec.org/
