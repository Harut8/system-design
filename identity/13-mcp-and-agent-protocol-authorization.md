# 13 — MCP and agent protocol authorization: in-process tools, local and remote MCP servers, A2A

> **Prerequisites:** [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md),
> [`06-well-known-discovery-and-metadata.md`](06-well-known-discovery-and-metadata.md) (the discovery chain MCP uses),
> [`07-oauth-hardening-and-advanced-specs.md`](07-oauth-hardening-and-advanced-specs.md) (resource indicators, RFC 9207,
> token exchange). Protocol background: [`ai-rag/26-mcp-and-agent-protocols.md`](../ai-rag/26-mcp-and-agent-protocols.md),
> [`ai-rag/27-a2a-and-agent-interoperability.md`](../ai-rag/27-a2a-and-agent-interoperability.md); threat catalogue:
> [`ai-rag/28-agentic-security-owasp-and-mcp-threats.md`](../ai-rag/28-agentic-security-owasp-and-mcp-threats.md).
>
> **Feeds into:** [`14-agent-identity-delegation-registries-and-gateways.md`](14-agent-identity-delegation-registries-and-gateways.md)
> (who the agent is, and the gateways/registries around it), [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md)
> (what it may do, approvals, expiry mid-run).
>
> **THESIS:** "how does an agent authenticate to a tool?" has three different answers depending on **where the tool
> runs**. An in-process tool is a function call: it has no identity of its own and inherits the host's credentials, so
> the question is *what the host lets the model invoke*. A local stdio MCP server is a child process: it inherits the
> user's OS authority and whatever secrets are in its environment, so the question is *sandboxing and secret hygiene*.
> A remote HTTP MCP server is an ordinary networked API: it is an **OAuth 2.1 resource server**, and the MCP
> authorization spec (2026-07-28) tells both sides exactly what to do. A2A is the same shape between agents. Most
> real incidents come from treating one kind of tool as if it were another: a local server exposed on a port with no
> auth, a remote server forwarding the user's token upstream, an in-process tool that quietly holds an admin key.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Why agent protocols needed their own authorization story](#1-why-agent-protocols-needed-their-own-authorization-story)
2. [In-process tools vs local stdio MCP vs remote HTTP MCP](#2-in-process-tools-vs-local-stdio-mcp-vs-remote-http-mcp)
3. [The MCP authorization flow, end to end](#3-the-mcp-authorization-flow-end-to-end)
4. [Client registration: pre-registration, CIMD, and deprecated DCR](#4-client-registration-pre-registration-cimd-and-deprecated-dcr)
5. [Tokens: audience binding, no passthrough, and calling upstream APIs](#5-tokens-audience-binding-no-passthrough-and-calling-upstream-apis)
6. [Scopes and step-up authorization](#6-scopes-and-step-up-authorization)
7. [Authorization extensions: enterprise-managed authorization and client credentials](#7-authorization-extensions-enterprise-managed-authorization-and-client-credentials)
8. [A2A: authentication between agents](#8-a2a-authentication-between-agents)
9. [Auth-specific threats in agent protocols](#9-auth-specific-threats-in-agent-protocols)
10. [Code: an MCP resource-server guard](#10-code-an-mcp-resource-server-guard)
11. [Anti-patterns](#11-anti-patterns)
12. [Interview questions](#12-interview-questions)
13. [Sources](#sources)

---

## Start here — the whole chapter in plain words

An AI agent uses **tools**: functions that read files, query databases, open tickets. Tools can live in three places.

1. **Inside the agent's own program** (an in-process tool). Calling it is a normal function call. If it needs to call an
   API, the agent program supplies the key. Nobody "logs in"; the important question is which tools the model is
   allowed to pick, and with which arguments.
2. **In a small helper program on your computer** (a local MCP server, talking over stdin/stdout). It runs as you, so
   it can do whatever you can do on that machine. It usually gets its API keys from a config file. The risks are a
   malicious helper and leaked keys.
3. **On someone's server across the internet** (a remote MCP server, over HTTPS). This is just an API, so it uses
   OAuth: the AI app sends you to that service's login page, you approve, and the AI app gets a token that only works
   for that one server. The server must refuse tokens meant for anything else, and must never pass your token on to
   other services.

A2A (agent-to-agent) is the same idea as option 3, but the "tool" is another agent. Each agent publishes a card saying
how to authenticate to it.

---

## 1. Why agent protocols needed their own authorization story

OAuth was designed for a world where a developer registers a client with a provider in advance, and the client knows its
providers. Agent ecosystems break three of those assumptions:

| Assumption in classic OAuth | Agent reality | MCP's answer |
|---|---|---|
| Client and AS have a prior relationship | Any AI app may connect to any MCP server a user pastes in | Client ID Metadata Documents (URL as client ID); DCR (deprecated) |
| Client knows its AS | Client only knows the MCP server URL | RFC 9728 protected resource metadata → RFC 8414 |
| A few providers per client | Dozens of ASes per client, some malicious | RFC 9207 `iss` validation; credentials keyed by issuer |
| Requests come from code the developer wrote | Requests are chosen by a model that reads untrusted text | Audience-bound tokens; no passthrough; least-privilege scopes; human approval (ch 15) |

---

## 2. In-process tools vs local stdio MCP vs remote HTTP MCP

| | In-process tool | Local MCP server (stdio) | Remote MCP server (Streamable HTTP) |
|---|---|---|---|
| What it is | Function in the agent host | Child process launched by the host | Network service |
| Trust boundary with host | None (same process) | Process boundary, same OS user | Network + organization boundary |
| Who authenticates whom | Nobody; the host *is* the caller | Nobody at protocol level; the host launched it | Client ↔ server via **OAuth 2.1** (MCP authorization spec) |
| MCP spec guidance | n/a | "SHOULD NOT follow" the HTTP authorization spec; retrieve credentials from the environment | "SHOULD conform" to the authorization spec |
| Where credentials live | Host's memory / vault / broker | Env vars or config of the child process; OS keychain if well built | MCP client: OAuth tokens per server (keyed by issuer); MCP server: its own upstream credentials |
| User identity at the tool | Host's run context | Implicit: the OS user | Explicit: `sub` in the access token |
| Blast radius if compromised | The whole host process | Everything the OS user can access (files, SSH keys, cloud CLIs) | That server's data + any upstream access it holds for users |
| Typical failures | Tool holds an over-privileged key; model calls it with attacker-chosen args | Malicious package; secrets in committed config; server exposed on a port with no auth; DNS rebinding | Token passthrough; accepting tokens for other audiences; weak consent; SSRF in discovery |
| Controls | Tool allowlists per agent, argument validation, broker-held credentials, approvals | Sandboxing (filesystem, network egress), signed/pinned packages, keychain-held secrets, never bind to `0.0.0.0` | Full OAuth RS validation, scopes, audit, rate limits, gateway (ch 14 §7) |

Three details people miss:

- **A local server is not automatically local-only.** If a "local" MCP server listens on HTTP (some do, for
  multi-client use) it must bind to `127.0.0.1`, validate the `Origin` header (the MCP transport spec requires this,
  because of **DNS rebinding**: a web page can make your browser send requests to `localhost` under an attacker
  domain), and should require authentication. Several 2025 advisories for local MCP tooling (e.g. MCP Inspector's
  proxy, CVE-2025-49596) were exactly "local HTTP service with no auth, reachable from a malicious web page".
- **stdio servers have no auth in the protocol, by design.** The host launched them and is talking over pipes; there's
  nobody else on the channel. The security question moves to *what binary did the host launch and with what
  environment*: pin versions, verify publishers (registries, signatures), avoid `npx some-package@latest` in configs.
- **In-process tools still need authorization**, just not authentication: the host must decide whether *this agent*,
  *for this user*, may call *this tool* with *these arguments*. That's ch 14–15.

---

## 3. The MCP authorization flow, end to end

From the MCP 2026-07-28 authorization specification (structure unchanged since 2025-06-18, with clarifications in
2025-11-25 and 2026-07-28):

**Roles.** MCP server = OAuth 2.1 **resource server**. MCP client = OAuth 2.1 **client**. A separate (possibly
co-hosted) **authorization server** issues tokens.

**Requirements on each side (abridged, normative words kept):**

| Party | MUST / SHOULD |
|---|---|
| Authorization server | MUST implement OAuth 2.1 for confidential and public clients; MUST provide RFC 8414 or OIDC Discovery metadata; SHOULD include RFC 9207 `iss` in authorization responses (and advertise it) |
| MCP server | MUST implement RFC 9728 Protected Resource Metadata (with at least one `authorization_servers` entry), via `WWW-Authenticate` `resource_metadata` and/or the well-known URI; MUST validate tokens were issued for it; MUST NOT accept or transit other tokens; SHOULD include `scope` in 401 challenges |
| MCP client | MUST use RFC 9728 for AS discovery; MUST support both RFC 8414 and OIDC discovery (trying URLs in priority order, ch 06 §6); MUST validate metadata `issuer`; MUST use PKCE and refuse to proceed if `code_challenge_methods_supported` is absent; MUST send RFC 8707 `resource` in authorization and token requests; MUST validate `iss` in the response (when present) before redeeming the code; MUST key stored credentials by issuer; MUST send `Authorization: Bearer` on every request, never in the query string |

**Sequence:**

```
MCP client                         MCP server (RS)                       Authorization server
   │ POST /mcp (no token) ─────────────▶│                                        │
   │◀── 401 WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp",
   │                                  scope="files:read"                        │
   │ GET PRM ──────────────────────────▶│ {resource, authorization_servers:[AS]} │
   │ check resource == server URL                                               │
   │ GET AS metadata (8414 / OIDC, priority order) ─────────────────────────────▶│
   │ check issuer, S256 support, client_id_metadata_document_supported, iss support
   │ obtain client_id (pre-registered | CIMD URL | DCR)                         │
   │ record {issuer, code_verifier, state}                                      │
   │ browser → /authorize?…&code_challenge=…&resource=https://mcp.example.com/mcp&scope=files:read
   │                                                                  user login + consent
   │◀── redirect ?code=…&state=…&iss=https://auth.example.com                   │
   │ check iss == recorded issuer (exact), state                                │
   │ POST /token code, code_verifier, resource ─────────────────────────────────▶│
   │◀── access_token (aud = MCP server), refresh_token (rotated for public clients)
   │ POST /mcp  Authorization: Bearer … ─▶│ validate: signature/introspection, iss, aud == me, exp, scope
```

**Canonical server URI.** The `resource` value is the MCP server's canonical URI: lowercase scheme and host, no fragment,
most specific path, preferably no trailing slash (`https://mcp.example.com/mcp`). Clients MUST send it even if the AS
doesn't advertise support, so tokens become audience-bound as soon as the AS supports it.

**Stateless transport, same auth.** The 2026-07-28 revision removed protocol-level sessions (`Mcp-Session-Id`) and the
`initialize` handshake; every request carries its own context. For auth this is a simplification: there's no session ID
that could be hijacked and confused with authentication; **every HTTP request must carry the access token**, and the
server authorizes each request on its own.

---

## 4. Client registration: pre-registration, CIMD, and deprecated DCR

The problem: the AS must know something about a client it has never seen (name to show on consent, allowed redirect
URIs). Three mechanisms, in the spec's priority order for clients that support all of them:

1. **Pre-registered client** for this AS, if the client has one (enterprise deployments, first-party pairs).
2. **Client ID Metadata Documents (CIMD)**, if AS metadata says `client_id_metadata_document_supported: true`.
3. **Dynamic Client Registration (RFC 7591)**, if the AS has a `registration_endpoint`. **Deprecated in 2026-07-28**,
   kept for backward compatibility.
4. Otherwise prompt the user to enter client details.

### 4.1 CIMD: the client ID is a URL

```
client_id = https://app.example.com/oauth/client-metadata.json
```

The AS fetches that URL (HTTPS, must have a path) and gets:

```json
{
  "client_id": "https://app.example.com/oauth/client-metadata.json",
  "client_name": "Example AI Assistant",
  "client_uri": "https://app.example.com",
  "logo_uri": "https://app.example.com/logo.png",
  "redirect_uris": ["http://127.0.0.1:3000/callback", "http://localhost:3000/callback"],
  "grant_types": ["authorization_code"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

Rules: the document's `client_id` must equal the URL exactly; the AS must validate the request's `redirect_uri` against
the document; it should cache per HTTP cache headers.

**Why CIMD replaced DCR as the preferred option:** with DCR, every client instance registers itself and gets a new random
`client_id`, so the AS accumulates millions of unverifiable registrations, consent screens show self-asserted names, and
there's no stable identity to allow- or block-list. With CIMD the client's identity **is a domain it controls** (the same
domain-control trust anchor as ch 06 §1): an AS can say "trust clients hosted on `*.anthropic.com` / `vscode.dev`", show
the verified domain on consent, and revoke a client by URL.

**What CIMD does not solve:**

- **Localhost redirect impersonation.** A native/CLI client uses `http://127.0.0.1:PORT/callback`. Any malicious local
  app can claim to be "Example AI Assistant" by using that public `client_id` and a localhost redirect, and receive the
  code on its own port. The spec says ASes SHOULD warn for localhost-only redirect URIs, MUST display the redirect host
  during authorization, and MAY require attestation. PKCE stops *other* processes from redeeming *your* code, but not an
  impostor that started its own flow.
- **SSRF.** The AS fetches attacker-supplied URLs; apply ch 06 §8.2 guards.
- **Trust.** A valid document proves domain control, not good intent. ASes may implement domain trust policies.

### 4.2 Authorization server binding

Credentials (client ID from DCR, client secrets, tokens) belong to the AS that issued them. Clients MUST store them keyed
by issuer, MUST NOT reuse them with another AS, and MUST re-register if a server's AS changes. This closes a class of
mix-up and credential-confusion bugs in clients that talk to many servers.

---

## 5. Tokens: audience binding, no passthrough, and calling upstream APIs

> **In plain words.** The token the AI app gives your MCP server is *for your MCP server only*. If your server needs to
> call GitHub, it gets *its own* GitHub token. Forwarding the user's MCP token to GitHub (or accepting a GitHub token at
> your MCP server) is forbidden.

**Audience binding.** MCP servers MUST reject tokens that don't name them as audience. Without this, a token issued
for a low-value MCP server (or leaked from it) works at a high-value one sharing the same AS.

**No token passthrough.** The spec forbids an MCP server from accepting a token not issued for it and from passing the
client's token to upstream APIs. Why passthrough is dangerous:

- It bypasses the MCP server's own controls (rate limits, validation, audit): the upstream sees the user's token and
  can't tell the MCP server was in the middle.
- The upstream's audience check either fails (correct) or was skipped (then any token works anywhere).
- Accountability breaks: logs at the upstream show the user, not "user via MCP server X".
- A compromised MCP server becomes a token-laundering point.

**What to do instead when the MCP server needs an upstream API** (e.g. a "GitHub MCP server"):

| Pattern | How | Notes |
|---|---|---|
| **MCP server as its own OAuth client to the upstream** | During the user's MCP authorization, the MCP server's AS (or the server) runs a *second* OAuth flow to GitHub, stores the user's GitHub tokens server-side (encrypted, ch 08 §5) keyed to the MCP `sub` | Most common for third-party SaaS wrapped as MCP |
| **Token exchange** | Server exchanges the incoming MCP token (subject) for an upstream-audience token at an AS both trust, with `act` = MCP server | Best inside one organization/trust domain |
| **Server's own identity** | Server calls upstream as itself, enforcing per-user policy | Only for data that isn't per-user; otherwise confused deputy |

**Proxy MCP servers and the confused deputy.** An MCP server that fronts a third-party API using a *single static
client ID* at that third party, while letting arbitrary MCP clients register with it dynamically, can be abused: the
third-party AS remembers the user's consent for the static client ID, so an attacker's dynamically registered client can
get a code without the user seeing a new consent. The spec: such proxy servers MUST obtain user consent for each
dynamically registered client before forwarding to the third-party AS.

**Token lifetime and storage.** ASes SHOULD issue short-lived access tokens; for public clients they MUST rotate refresh
tokens. Clients and servers MUST store tokens securely (ch 08, ch 09).

---

## 6. Scopes and step-up authorization

**Initial scope selection (client):** use the `scope` from the 401 challenge if present; otherwise all of
`scopes_supported` from PRM (which is meant to be the minimal set for basic functionality); omit `scope` if neither is
available.

**Runtime step-up (server → client):**

```http
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope", scope="files:read files:write",
                  resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
                  error_description="Writing files requires files:write"
```

- The server SHOULD include all scopes needed for the operation (and may include already-granted ones so the client
  doesn't lose them on re-authorization).
- Clients acting for a user SHOULD re-authorize with the union of existing and challenged scopes; `client_credentials`
  clients re-request or fail.
- Clients SHOULD cap retries and track upgrade attempts to avoid loops; servers MUST account for scope hierarchies (a
  broader scope implying a narrower one) when checking.

This is **incremental consent**, and it fits agents well: start read-only; ask for write when the plan reaches a write
step, which is a natural human-in-the-loop moment (ch 15 §5). Combine with **authentication** step-up (RFC 9470,
`insufficient_user_authentication`, ch 07 §8) for operations that need a fresh, strong login rather than more scope.

---

## 7. Authorization extensions: enterprise-managed authorization and client credentials

MCP publishes optional, additive authorization extensions in the `ext-auth` repository. Two are **stable** and versioned
with 2026-07-28:

### 7.1 Enterprise-Managed Authorization (ID-JAG / Cross App Access)

When the MCP client and MCP server both use the same enterprise IdP for SSO:

1. User signs in to the MCP client via the enterprise IdP (OIDC; or SAML, then exchanged for a refresh token).
2. MCP client → IdP: **token exchange** with `requested_token_type=urn:ietf:params:oauth:token-type:id-jag`,
   `audience` = the MCP server's AS issuer (MUST), optional `resource` = MCP server, `scope`, `subject_token` = ID token.
   The IdP evaluates **admin policy** ("engineering group may use the AI code editor with the source-control MCP server,
   read-only").
3. MCP client → MCP server's AS: **JWT bearer grant** (RFC 7523) with the ID-JAG. The AS validates it and issues an access
   token **audience-restricted to the MCP server** named in the ID-JAG.
4. Discovery: AS metadata lists `urn:ietf:params:oauth:grant-profile:id-jag` in `authorization_grant_profiles_supported`.

Outcome: no per-server consent clicks for users, central visibility and revocation for admins. Limitation (stated in the
extension): the IdP sees token issuance, not the MCP traffic afterwards; fine-grained control depends on how granular the
server's scopes are.

### 7.2 OAuth Client Credentials

For machine-to-machine MCP (no user): pre-registered clients, `client_credentials` grant, authenticated with
`private_key_jwt` (RECOMMENDED) or a client secret; AS metadata must list a supported auth method. Use for backend agents
that act as themselves; don't use it to let a backend agent act *for* users (that needs delegation, ch 14 §3).

---

## 8. A2A: authentication between agents

The Agent2Agent protocol (Linux Foundation project since 2025) lets agents delegate tasks to other agents. Its
auth design deliberately reuses web standards rather than inventing a protocol:

- **Agent Card** at `/.well-known/agent-card.json` (ch 06 §5): the remote agent's name, skills, endpoints, and
  **`securitySchemes`** (OpenAPI-style: `apiKey`, `http` bearer, `oauth2` with flows, `openIdConnect`, `mutualTLS`) plus
  `security` requirements saying which ones apply.
- **Credentials travel in HTTP headers** (e.g. `Authorization: Bearer`) obtained out of band via the declared scheme, not
  inside A2A JSON-RPC payloads.
- **Authenticated extended card**: an agent can expose more skills/details to authenticated callers.
- **Card signatures**: Agent Cards can carry JWS signatures so a client can verify who published the card, rather than
  trusting whatever is served at the URL. Cards are otherwise self-asserted; an unsigned card is a claim.
- **In-task authentication**: a task can move to an `auth-required` state when the remote agent needs additional
  credentials (e.g. the user must authorize access to a third-party system), and resume once supplied.

What A2A leaves to you (and ch 14 covers): **which** identity the calling agent presents (its own workload identity? a
token carrying the end user?), how the receiving agent authorizes the *combination* of calling agent and user, and how
delegation chains across several agents are recorded. Typical production setup:

```
Agent A (planner, org X) ──mTLS (A's SPIFFE ID)──▶ Agent B (travel agent, org Y)
   Authorization: Bearer <token from B's AS: sub=alice, act={sub: agent-A}, aud=agent-B, scope=trips.book>
   obtained via token exchange at an AS B trusts (or ID-JAG if both use Alice's enterprise IdP)
```

---

## 9. Auth-specific threats in agent protocols

| Threat | Description | Control |
|---|---|---|
| Token passthrough | MCP server forwards client token upstream or accepts foreign tokens | Audience validation; own upstream credentials / token exchange (§5) |
| Malicious authorization server | Attacker MCP server lists attacker AS in PRM; user sees a consent page from the attacker | Show AS identity; enterprise allowlists; ID-JAG under IdP policy |
| Mix-up across many ASes | Code sent to wrong token endpoint | RFC 9207 `iss`, credentials keyed by issuer (§4.2) |
| CIMD/localhost client impersonation | Malicious local app reuses a public client ID | Show redirect host; attestation; warn on localhost-only clients |
| SSRF via discovery and CIMD | PRM/AS metadata/client URLs point at internal services | Fetch guards (ch 06 §8.2) |
| DNS rebinding of local HTTP servers | Web page reaches `localhost` MCP server | Bind 127.0.0.1, validate `Origin`, require auth |
| Over-broad scopes at first consent | "Allow everything" tokens | `scope` in challenges, minimal `scopes_supported`, step-up |
| Confused deputy via proxy MCP servers | Static upstream client ID + dynamic MCP clients | Per-client consent before forwarding (§5) |
| Credential exposure to the model | Tokens in tool results or prompts | Broker injects at runtime; scrub outputs (ch 08 §10) |
| Rug pull / tool definition change after approval | Server changes tool behaviour or scope needs later | Pin tool definitions (hash) and re-approve on change; registry governance (ch 14 §6) |

---

## 10. Code: an MCP resource-server guard

FastAPI middleware for a remote MCP server: serves PRM, challenges with `resource_metadata` and `scope`, validates
audience/issuer with the ch 03 `Verifier`, maps tools to scopes, and returns step-up challenges.

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RESOURCE = "https://mcp.example.com/mcp"                       # canonical server URI = token audience
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
AS_ISSUER = "https://auth.example.com"
TOOL_SCOPES = {"files.list": {"files:read"}, "files.read": {"files:read"}, "files.write": {"files:write"}}
BASE_SCOPES = {"files:read"}

app = FastAPI()

@app.get("/.well-known/oauth-protected-resource/mcp")
def prm():
    return {"resource": RESOURCE, "authorization_servers": [AS_ISSUER],
            "scopes_supported": sorted(BASE_SCOPES),            # minimal set; more via step-up
            "bearer_methods_supported": ["header"], "resource_name": "Example Files MCP"}

def challenge(status: int, error: str | None, scopes: set[str], desc: str = "") -> JSONResponse:
    parts = [f'resource_metadata="{PRM_URL}"', f'scope="{" ".join(sorted(scopes))}"']
    if error:
        parts.insert(0, f'error="{error}"')
    if desc:
        parts.append(f'error_description="{desc}"')
    return JSONResponse({"error": error or "unauthorized"}, status_code=status,
                        headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)})

@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path != "/mcp":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return challenge(401, None, BASE_SCOPES)
    try:
        claims = verifier.verify(auth[7:])                      # checks iss == AS_ISSUER, aud == RESOURCE, exp, typ
    except Exception:
        return challenge(401, "invalid_token", BASE_SCOPES)
    granted = set(claims.get("scope", "").split())
    body = await request.json()
    if body.get("method") == "tools/call":
        tool = body.get("params", {}).get("name", "")
        needed = TOOL_SCOPES.get(tool)
        if needed is None:
            return JSONResponse({"error": "unknown tool"}, status_code=404)
        if not needed <= granted:                               # step-up: ask for union of granted + needed
            return challenge(403, "insufficient_scope", granted | needed, f"{tool} requires {' '.join(sorted(needed))}")
    request.state.principal = {"sub": claims["sub"], "client_id": claims.get("client_id"),
                               "act": claims.get("act"), "scopes": granted}
    return await call_next(request)
```

Notes: the guard authorizes **per request** (the 2026-07-28 transport is stateless); object-level checks (may this `sub`
read this file?) still happen inside the tool (ch 11 §8); upstream calls use the server's own credentials or token
exchange (§5), never `auth[7:]`.

---

## 11. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Remote MCP server with a static shared API key for all users | No user identity, no revocation, key leaks to every client | OAuth per spec |
| Accepting any token the AS signed | Cross-server replay | `aud` == canonical server URI |
| Forwarding the client's token upstream | Confused deputy, audit loss | Own upstream credentials / token exchange |
| Local MCP server over HTTP on `0.0.0.0` with no auth | Anyone on the network or any web page via DNS rebinding | stdio, or 127.0.0.1 + Origin check + auth |
| `npx pkg@latest` MCP servers in shared configs | Supply-chain swap | Pinned versions, registry verification |
| Client storing one token per server URL regardless of AS | Credential confusion when server's AS changes | Key by issuer; re-register on change |
| Requesting every scope up front | Over-privileged agents | Challenge scopes + step-up |
| Implementing DCR only, in 2026 | Deprecated; registration sprawl | CIMD (+ DCR fallback) |
| Treating A2A agent cards as trusted because they're at a well-known URL | Impersonation via compromised hosting | Verify card signatures and TLS identity; allowlists |

---

## 12. Interview questions

**Q1. Compare the security model of an in-process tool, a local stdio MCP server, and a remote MCP server.**
In-process: no authentication, host credentials, risk is over-privileged tools and model-chosen arguments; control with
allowlists, argument validation, brokered credentials. Local stdio: runs as the user, credentials from environment, risk is
malicious packages and secret leakage; control with sandboxing, pinning, keychain secrets. Remote: OAuth 2.1 resource
server with audience-bound tokens, no passthrough, scopes and step-up.

**Q2. Why did MCP move from Dynamic Client Registration to Client ID Metadata Documents?**
DCR creates unverifiable per-instance registrations and self-asserted names. CIMD makes the client ID an HTTPS URL on a
domain the client controls, giving a stable, verifiable identity ASes can display, allowlist and revoke, with no
registration endpoint. It doesn't solve localhost impersonation or SSRF, which need separate controls.

**Q3. Your MCP server wraps the GitHub API. How do users' GitHub permissions flow?**
The MCP client gets a token for the MCP server (aud = server). The server holds per-user GitHub tokens from its own OAuth
flow with GitHub (encrypted, keyed to the MCP `sub`), or performs token exchange in a shared trust domain. It never
forwards the MCP token to GitHub and never uses one org-wide GitHub token for all users' actions.

**Q4. What does enterprise-managed authorization change for an AI client connecting to 30 internal MCP servers?**
Users don't consent per server; the client exchanges the user's SSO ID token at the enterprise IdP for an ID-JAG per
server's AS under admin policy, then redeems it for audience-restricted access tokens. Admins control and revoke
client-server pairs centrally.

---

## Sources

- MCP specification 2026-07-28, Authorization (overview, discovery, client registration, security considerations): https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- MCP specification 2026-07-28, Key Changes (changelog): https://modelcontextprotocol.io/specification/2026-07-28/changelog
- MCP authorization extensions (Enterprise-Managed Authorization, OAuth Client Credentials): https://github.com/modelcontextprotocol/ext-auth
- OAuth Client ID Metadata Document (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-client-id-metadata-document/
- RFC 9728 (Protected Resource Metadata), RFC 8707 (Resource Indicators), RFC 9207 (Issuer Identification), RFC 7591 (DCR), RFC 7523 (JWT grants)
- Identity Assertion JWT Authorization Grant, draft-ietf-oauth-identity-assertion-authz-grant-04: https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/
- A2A Protocol specification: https://a2a-protocol.org/latest/specification/
- NVD, CVE-2025-49596 (MCP Inspector, unauthenticated local proxy): https://nvd.nist.gov/vuln/detail/CVE-2025-49596
