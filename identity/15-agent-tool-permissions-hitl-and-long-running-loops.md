# 15 — Agent tool permissions, human-in-the-loop approval, and auth in long-running loops

> **Prerequisites:** [`14-agent-identity-delegation-registries-and-gateways.md`](14-agent-identity-delegation-registries-and-gateways.md)
> (who the agent is, the permission formula), [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md)
> (step-up challenges), [`07-oauth-hardening-and-advanced-specs.md`](07-oauth-hardening-and-advanced-specs.md) (RAR, CIBA,
> RFC 9470), [`08-storing-credentials-and-tokens-at-rest.md`](08-storing-credentials-and-tokens-at-rest.md) (§13.4 safe
> refresh). Background: [`ai-rag/17-safety-guardrails-and-prompt-injection.md`](../ai-rag/17-safety-guardrails-and-prompt-injection.md)
> (Rule of Two, CaMeL), [`ai-rag/21-langgraph-deep-dive.md`](../ai-rag/21-langgraph-deep-dive.md) (interrupts).
>
> **THESIS:** an agent run is a long, branching sequence of tool calls chosen by a model that reads untrusted text. Three
> consequences follow. **Permissions must be decided per tool call**, at an enforcement point the model can't reach, using
> the narrowest token for that call. **Human approval must bind to the exact action** (tool, arguments, resource, amount)
> and be checked by policy, not described in the prompt. And **time is an adversary**: tokens expire, grants get revoked,
> users get offboarded and tool definitions change while the run is in flight, so every step must re-establish
> authority and every side-effecting call must be safe to retry.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Where authorization happens in an agent loop](#1-where-authorization-happens-in-an-agent-loop)
2. [The tool permission model](#2-the-tool-permission-model)
3. [Just-in-time, per-call credentials](#3-just-in-time-per-call-credentials)
4. [Bounded actions with Rich Authorization Requests](#4-bounded-actions-with-rich-authorization-requests)
5. [Human-in-the-loop approval done right](#5-human-in-the-loop-approval-done-right)
6. [Token expiry, revocation and step-up in the middle of a run](#6-token-expiry-revocation-and-step-up-in-the-middle-of-a-run)
7. [The edge-case catalogue](#7-the-edge-case-catalogue)
8. [Prompt injection meets permissions: data-flow rules](#8-prompt-injection-meets-permissions-data-flow-rules)
9. [Code: a tool executor that survives expiry and enforces approvals](#9-code-a-tool-executor-that-survives-expiry-and-enforces-approvals)
10. [Anti-patterns](#10-anti-patterns)
11. [Interview questions](#11-interview-questions)
12. [Sources](#sources)

---

## Start here — the whole chapter in plain words

An agent works in a loop: think, pick a tool, run it, read the result, think again, maybe for minutes or hours.

- **Every tool call is a permission decision.** Before running `send_email`, code outside the model checks: is this
  agent allowed this tool, is this user allowed this action, are these arguments within limits? Then it fetches a
  short-lived key for just that call.
- **Some actions need a human to say yes.** The approval must be for *exactly* the action shown ("pay €80 to Vendor A
  for invoice 4711"), used once, and expire. If the agent later tries to pay €800, the old approval doesn't count.
- **Keys run out mid-task.** A one-hour job will see its 15-minute token expire. The executor refreshes it and retries,
  carefully: if the tool call that failed was "transfer money", retrying blindly could pay twice, so side-effecting calls
  carry an **idempotency key**. If the user's access was revoked in the meantime, the run must stop and say so, not
  quietly continue with a stale key.

---

## 1. Where authorization happens in an agent loop

```
             ┌──────────── untrusted: model output ────────────┐
User task ─▶ │ Model: "call invoice.approve {id: 4711, €80}"   │
             └───────────────┬─────────────────────────────────┘
                             ▼
     Tool executor / MCP gateway (trusted code, outside model)
       1. Parse + validate arguments against the registered schema
       2. PDP: user × agent × tool × args × context  (ch 14 §2.1)
       3. Approval needed? → pause run, request, verify binding (§5)
       4. Get per-call token (exchange / vault), DPoP-bound (§3)
       5. Execute with idempotency key; handle 401/403 (§6)
       6. Scrub output (secrets, PII per data class) → back to model
       7. Audit record
```

Two rules make this safe:

1. **The model never holds credentials and never "authorizes".** Instructions in the prompt like "you may only call X"
   are guidance, not enforcement. Enforcement is steps 1–5.
2. **Authorization is evaluated per call, with current state.** Not once at the start of the run.

---

## 2. The tool permission model

Classify every tool (in the tool registry, ch 14 §6.2):

| Risk class | Examples | Default policy |
|---|---|---|
| Read, own data | `calendar.list`, `files.read` (user's files) | Allow within agent ceiling |
| Read, sensitive data | HR records, financials, secrets | Allow only for agents with that data class; log; limit volume |
| Write, reversible | Create draft, add label, create branch | Allow; audit; rate-limit |
| Write, irreversible / costly | Payments, deletes, merges to `main`, production deploys | **Approval required**; bounded (RAR); single-use tokens |
| External communication | Send email/Slack outside org, HTTP to arbitrary URLs | Approval or strict allowlists; exfiltration risk (§8) |
| Code execution | Run shell, run generated code | Sandbox, no credentials inside (ch 09 §8.4), egress allowlist |
| Identity/permission changes | Grant access, create API keys, change MFA | Never for agents, or approval + step-up |

Beyond allow/deny per tool, constrain **arguments**:

- Schema validation (types, enums, lengths) before anything else; reject unknown fields.
- Resource constraints: `repo` ∈ allowed repos; `path` under workspace root (normalize, no `..`, no symlinks out);
  `recipient` domain ∈ org; `amount` ≤ limit.
- Rate and volume: max calls per run, max rows exported, max spend.
- These live in policy (Cedar/OPA/your PDP), keyed by tool and agent, so security can change them without redeploying the
  agent.

"Always allow this tool" settings in agent UIs are permission grants: store them as scoped, expiring grants
(`user, agent, tool, argument constraints, expires_at`) in the same consent store, show them in a revocation UI, and never
let them cover the irreversible class.

---

## 3. Just-in-time, per-call credentials

Long-lived, broad tokens held by the agent runtime for the whole run are the agent version of the shared service key.
Instead:

- The executor requests a token **per call or per short phase**: `aud` = the one tool server, `scope` = what this call
  needs, lifetime = minutes, sender-constrained to the runtime key (DPoP).
- Cache per `(user, agent, audience, scope)` for the token's lifetime to avoid AS load; never across users.
- For third-party SaaS where you hold the user's refresh token (ch 08 §10), the vault returns an access token, not the
  refresh token, and only to the executor.

Effect: a prompt-injected agent that persuades the executor to call a permitted tool still gets only that tool's narrow
token; a leaked token is useless elsewhere and soon expired.

---

## 4. Bounded actions with Rich Authorization Requests

Scopes say "may approve invoices". Agents need "may approve **this** invoice for **this** amount". RAR (RFC 9396, ch 07 §6)
expresses it:

```json
"authorization_details": [{
  "type": "invoice_approval",
  "invoice_id": "4711",
  "max_amount": {"currency": "EUR", "value": "80.00"},
  "vendor_id": "V-118",
  "approval_ref": "apr_7f3c"
}]
```

The executor requests a token with these details right before the call; the resource server enforces them; the token is
single-use (`jti` recorded on use). Combined with an approval (§5), you get: the human saw exactly this, the token permits
exactly this, the API enforces exactly this.

---

## 5. Human-in-the-loop approval done right

### 5.1 When to ask

| Factor | Ask when |
|---|---|
| Reversibility | Irreversible or costly to undo |
| Blast radius | Affects others (external email, shared resources, production) |
| Value | Above a threshold (money, data volume) |
| Novelty | First time this agent does this for this user; new recipient/vendor/domain |
| Confidence signals | Run touched untrusted content before proposing the action (§8) |
| Policy | Agent risk tier, data class, regulation (e.g. payments need SCA) |

Too many prompts produce **approval fatigue** (users click yes reflexively, the same failure as MFA push bombing). Batch
low-risk actions into a plan the user approves once with bounded parameters, and reserve per-action approval for the
irreversible class.

### 5.2 What an approval must bind to

An approval is a **security token**, not a UI event. Record:

```
approval_id, requester (user + agent + run), approver (user id, auth time, amr),
action: tool id + tool definition hash, canonicalized arguments hash, resource ids, amounts,
display text shown to the approver (exact), created_at, expires_at (minutes), single_use, used_at
```

At execution, the executor recomputes the hash of the canonical arguments it is about to send and requires an unused,
unexpired approval with the same hash, tool hash and run. That defeats:

- **Argument swapping**: the model proposes €80 for approval, then calls with €800.
- **Replay**: reusing an old approval for a new action.
- **Tool swap**: the server changed the tool's behaviour after approval (definition hash).
- **Social engineering in text**: "the user already approved this" inside a document or A2A message counts for nothing.

### 5.3 Who approves, and how they're authenticated

- The approver must be **authenticated now** (fresh session or step-up for high-value approvals, RFC 9470 semantics:
  `acr`, `auth_time`).
- **Approver ≠ requester** for separation-of-duties actions (the finance agent acting for Alice can't have Alice's
  approval satisfy a policy that requires a second person).
- Show what will happen in plain language **and** the raw parameters (recipient, amount, diff for code changes).

### 5.4 Channels

| Situation | Channel |
|---|---|
| User is in the chat/IDE | Inline approval (LangGraph `interrupt`, IDE permission prompt) |
| User away, background run | Push/CIBA to their device with `binding_message` (ch 07 §9); run pauses |
| MCP server needs user input | MCP 2026-07-28 multi round-trip requests (`input_required` result carrying elicitation requests; URL-mode elicitation for out-of-band flows such as a third-party OAuth consent); never ask for credentials inside elicitation |
| Remote A2A agent needs auth | Task enters `auth-required`; requester obtains authorization out of band and resumes |
| Approval by someone else (manager) | Workflow system creates an approval task; agent run waits on its signed outcome |

### 5.5 Waiting safely

- Pausing must **checkpoint** the run state without credentials (references only, ch 08 §10).
- Timeouts default to **deny**; the run resumes with "not approved" and the model must handle it.
- On resume, **re-evaluate everything** (§6): the approval may be valid but the user may have lost access in the meantime.

---

## 6. Token expiry, revocation and step-up in the middle of a run

> **In plain words.** A long agent run will outlive its tokens. Expiry is normal and handled by refreshing. Revocation
> is a signal to stop. The executor must tell the two apart, retry only what is safe to retry, and never let the model
> improvise around an authorization error.

### 6.1 The failure taxonomy and the correct reaction

| Signal | Meaning | Reaction |
|---|---|---|
| Token near expiry (proactive) | Normal | Refresh/exchange before the call (refresh at ~80% lifetime) |
| `401 invalid_token` (expired) | Normal | Refresh once, retry the call **only if idempotent or carrying an idempotency key** |
| Refresh returns `invalid_grant` | Grant revoked, refresh token expired, user disabled, rotation conflict | **Stop**: mark connected account `needs_reauth`, pause run, notify user. Don't retry in a loop |
| Token exchange denied | Agent suspended, consent withdrawn, user lost entitlement, policy changed | Stop the affected branch; report reason to user (not to the model as something to work around) |
| `403 insufficient_scope` (MCP step-up) | Needs more permission | Pause; request incremental consent (user present) or fail (background); cap attempts |
| `401 insufficient_user_authentication` (RFC 9470) | Needs fresher/stronger login | Pause; ask user to re-authenticate with requested `acr`/`max_age` |
| `403` from resource policy | User or agent not allowed this object | Don't retry; return a clean denial to the model ("not permitted"), log |
| AS unavailable | Infra | Use still-valid cached token; otherwise back off with jitter; pause run after budget; never fall back to a broader credential |
| Approval expired before execution | Normal in slow flows | Re-request approval; never extend silently |
| DPoP `use_dpop_nonce` | Server wants a nonce | Retry once with the nonce |
| Clock skew errors (`iat` in future) | Runtime clock wrong | Use server `expires_in` relative to receipt; fix NTP |

### 6.2 Retries and idempotency: the real danger

The worst mid-loop auth bug isn't failing; it's **doing a side effect twice**. Sequence:

```
executor → payments API: POST /transfers (token T1)  ──▶ API performs transfer, response lost / 401 on a later hop
executor sees error → refreshes token → retries POST /transfers (T2) → second transfer
```

Rules:
- Every side-effecting tool call gets an **idempotency key** derived from `(run_id, step_id)` (not random per attempt), sent
  as `Idempotency-Key` (or the tool's equivalent) so the server deduplicates.
- Classify tools as idempotent / idempotent-with-key / non-idempotent in the registry. For non-idempotent tools, after an
  ambiguous failure **query state** ("did transfer for step 7 happen?") before retrying, or escalate to a human.
- Retry auth errors **once** after refresh; never let the model "try again" with a loop of its own.

### 6.3 Re-check authority at every step (time-of-check vs time-of-use)

A run that started at 09:00 under Alice's permissions may still be executing at 11:00 after Alice moved teams or was
offboarded. Short-lived per-call tokens (§3) give this for free: each exchange re-evaluates consent, agent status and
coarse entitlements; each resource server re-evaluates object-level rights. What breaks it: a single long-lived token
minted at run start, cached tool results reused across steps without re-authorization, and sub-agents holding tokens
longer than the parent.

Also bound **run duration** (e.g. 24 h max) and **token lifetime ≤ remaining run budget**. Background runs that need to live
longer should re-confirm with the user periodically.

### 6.4 Never hand auth failures to the model as a puzzle

Return a structured, minimal error ("tool unavailable: re-authorization required; the user has been notified") and stop
that branch. Detailed `WWW-Authenticate` contents, token fragments or URLs in tool results invite the model (or an
injection) to "fix" auth, e.g. by trying other tools or asking the user to paste a token into chat. Credential prompts in
chat are an anti-pattern; MCP explicitly forbids requesting sensitive credentials through elicitation.

---

## 7. The edge-case catalogue

| # | Edge case | What goes wrong | Handling |
|---|---|---|---|
| 1 | Parallel tool calls refresh the same rotating refresh token | Provider sees reuse, revokes whole grant | Single-flight refresh per account; row lock (ch 08 §13.4) |
| 2 | Sub-agent token outlives parent run | Orphaned authority | Child `exp` ≤ parent; revoke children on parent cancel |
| 3 | Approval granted, token for execution expired | Execution fails after human waited | Approval valid for N minutes independent of token; mint token at execution |
| 4 | Multi-step operation half done when auth fails | Inconsistent state (draft created, not sent) | Saga-style compensations; record step state; resume after re-auth |
| 5 | User logs out mid-run | Run continues with stale authority | Decide policy: background runs continue under standing grant, interactive runs stop; back-channel logout cancels interactive runs |
| 6 | User offboarded mid-run | Run continues | SCIM cascade suspends runs; exchanges fail (ch 10 §5) |
| 7 | Agent version upgraded mid-run | New ceiling or tools differ | Pin run to version at start; new version applies to new runs |
| 8 | Tool definition changed mid-run (rug pull) | Approved semantics changed | Definition hash check per call; block on change |
| 9 | MCP server's AS changes | Stored credentials invalid or sent to wrong AS | Credentials keyed by issuer; re-register (ch 13 §4.2) |
| 10 | Tenant switch in the same session | Agent carries data/permissions across tenants | Runs bound to one tenant; new run on switch |
| 11 | Cached tool results reused after access revoked | Data shown to user who lost access | Cache keyed by user and short TTL; re-authorize on reuse |
| 12 | Agent memory contains another user's data | Cross-user leakage | Memory partitioned by `(tenant, user, agent)`; never global by default |
| 13 | Shared agent (team assistant) used by several people | Whose authority applies? | Per-invocation user context; or agent's own identity with team-scoped grants; never "creator's token for everyone" |
| 14 | Long A2A task waiting in `auth-required` | Remote holds context indefinitely | Timeouts; cancel; re-auth link expiry |
| 15 | Streaming response cut mid-tool-call | Unknown whether side effect happened | Idempotency keys; query-before-retry |
| 16 | Model retries a denied action with different phrasing/tools | Policy probing | Deny counter per run; escalate or stop after N denials |
| 17 | Approval prompt contains attacker-controlled text | User approves misleading description | Render from structured args, not model prose; show raw params |
| 18 | Rate limit hit during refresh storm | Cascading failures | Jittered refresh, token cache, backoff |
| 19 | Clock skew on edge runtimes | Spurious expiry | `expires_in` from receipt; NTP |
| 20 | Consent expires during a scheduled weekly run | Silent failure | Expiry reminders; pre-flight check at schedule time |

---

## 8. Prompt injection meets permissions: data-flow rules

Permissions per tool aren't enough when tools combine. Simon Willison's **lethal trifecta**: an agent with (1) access to
private data, (2) exposure to untrusted content, and (3) a way to communicate externally can be made to exfiltrate. Meta's
**Agents Rule of Two** turns that into a design rule: in one session, allow at most two of {untrusted input, sensitive
data/systems, ability to change state or communicate externally} without human approval. Google DeepMind's **CaMeL**
separates a privileged planner that never sees untrusted data from a quarantined model that processes it, with
capability-tagged values.

Practical controls in the executor:

- **Taint tracking**: mark the run (or values) as tainted once untrusted content (web pages, emails, tickets, A2A messages,
  tool outputs from external servers) enters context.
- **Policy on taint**: after taint, external-communication and irreversible tools require approval, even if normally
  allowed.
- **Egress allowlists** at the network level for runtimes and sandboxes; no arbitrary URL fetches with credentials.
- **No credentials in reach** of the model (§1), so injection can at most misuse tools within policy, never steal keys.

Details and evaluations: [ai-rag ch 17](../ai-rag/17-safety-guardrails-and-prompt-injection.md) and
[ch 28](../ai-rag/28-agentic-security-owasp-and-mcp-threats.md).

---

## 9. Code: a tool executor that survives expiry and enforces approvals

```python
import hashlib, json, time
from dataclasses import dataclass

class NeedsReauth(Exception): ...
class NeedsApproval(Exception):
    def __init__(self, request): self.request = request
class Denied(Exception): ...

@dataclass
class Approval:
    id: str
    run_id: str
    tool_id: str
    tool_hash: str
    args_hash: str
    approver: str
    expires_at: float
    used_at: float | None = None

def canonical_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

class ToolExecutor:
    def __init__(self, registry, pdp, tokens, approvals, http, audit):
        self.registry, self.pdp, self.tokens = registry, pdp, tokens
        self.approvals, self.http, self.audit = approvals, http, audit

    def call(self, ctx, step_id: str, tool_id: str, args: dict, tainted: bool) -> dict:
        tool = self.registry.tool(tool_id)
        tool.validate(args)                                            # schema first; rejects unknown fields
        if self.registry.live_definition_hash(tool_id) != tool.definition_hash:
            raise Denied("tool definition changed since registration")
        ok, reason = self.pdp.decide(ctx.user, ctx.agent, tool_id, args)   # user × agent × tool × args
        if not ok:
            self.audit.log("deny", ctx=ctx, tool=tool_id, reason=reason)
            raise Denied(reason)

        args_hash = canonical_hash(args)
        if tool.approval_required or (tainted and tool.side_effects in ("external-communication", "write-irreversible")):
            appr = self.approvals.find_unused(ctx.run_id, tool_id, tool.definition_hash, args_hash)
            if appr is None or appr.expires_at < time.time():
                raise NeedsApproval({"tool": tool_id, "args": args, "args_hash": args_hash,
                                     "display": tool.render_for_human(args)})   # rendered from args, not model prose
            if not self.approvals.mark_used(appr.id):                 # atomic single use
                raise Denied("approval already used")

        idem = f"{ctx.run_id}:{step_id}"                               # stable across retries
        for attempt in (1, 2):
            token = self.tokens.for_call(ctx, audience=tool.server, scopes=tool.scopes_required,
                                         details=tool.authorization_details(args))
            resp = self.http.post(tool.server, tool_id, args, token=token,
                                  headers={"Idempotency-Key": idem})
            if resp.status == 401 and resp.error == "invalid_token" and attempt == 1:
                self.tokens.invalidate(ctx, tool.server)               # expired: refresh once and retry
                continue
            if resp.status == 401 and resp.error == "insufficient_user_authentication":
                raise NeedsReauth("step-up authentication required")
            if resp.status == 403 and resp.error == "insufficient_scope":
                raise NeedsReauth("additional consent required")
            if resp.status in (401, 403):
                raise Denied("not permitted")
            self.audit.log("call", ctx=ctx, tool=tool_id, args_hash=args_hash, status=resp.status, idem=idem)
            return tool.scrub(resp.body)                               # strip secrets / out-of-class data
        raise NeedsReauth("token could not be refreshed")
```

`tokens.for_call` raises `NeedsReauth` when the refresh or exchange returns `invalid_grant` / denial; the run loop catches
`NeedsReauth` and `NeedsApproval`, checkpoints (without secrets), notifies the user, and pauses. The model receives only
"paused: approval requested" or "stopped: re-authorization required".

---

## 10. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Permissions described only in the system prompt | Injection bypasses them | Executor/gateway enforcement |
| One broad token minted at run start | Stale authority for hours; large blast radius | Per-call, short, audience-bound tokens |
| Approval = "user clicked yes on a message" | Argument swapping, replay | Approval bound to tool hash + args hash, single use, expiring |
| Approval text written by the model | Misleading prompts | Render from structured arguments |
| Retrying side effects after auth errors without idempotency | Double payments/emails | Stable idempotency keys; query-before-retry |
| Letting the model handle 401/403 | Loops, credential prompts in chat | Executor handles; model gets a minimal status |
| Background runs that never re-check consent | Offboarded users' agents keep working | Per-call exchange re-evaluates; SCIM suspends runs |
| "Always allow" without scope or expiry | Permanent silent grants | Scoped, expiring, revocable grants |
| Same treatment for tainted and clean runs | Exfiltration via trifecta | Taint-aware policy |

---

## 11. Interview questions

**Q1. An agent's run lasts three hours; tokens last 15 minutes. Design the auth handling.**
Per-call tokens from exchange (re-evaluating consent and agent status each time), proactive refresh, single retry on
expired tokens, idempotency keys for side effects, distinct handling of revoked grants (stop + notify), step-up and
insufficient-scope (pause + ask), checkpoints without secrets, max run duration, and audit.

**Q2. How do you make sure a human approval can't be reused or bait-and-switched?**
Store approvals bound to run, tool definition hash and canonical arguments hash, with expiry and atomic single use; render
the approval from the structured arguments; at execution recompute the hash and require a match; optionally mint a RAR
token embedding the approved parameters so the resource server enforces them too.

**Q3. What's the difference between `invalid_token`, `insufficient_scope` and `insufficient_user_authentication`, and
how should an agent executor react to each?**
Expired/invalid token: refresh once and retry if safe. Insufficient scope: needs more permission, pause for incremental
consent (or fail for background clients). Insufficient user authentication: needs a fresher or stronger login, pause and ask
the user to re-authenticate. None should be handed to the model to resolve.

**Q4. When must an agent ask for approval even for a normally allowed tool?**
When the action is irreversible, high-value or affects others, when it's novel for this user/agent, and when the run has
ingested untrusted content and the tool can communicate externally or change state (lethal trifecta / Rule of Two).

---

## Sources

- RFC 9396 (RAR): https://datatracker.ietf.org/doc/html/rfc9396 ; RFC 9470 (Step-up): https://datatracker.ietf.org/doc/html/rfc9470
- OpenID Connect CIBA Core 1.0: https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html
- MCP 2026-07-28: Authorization (scope challenge handling) and Multi Round-Trip Requests: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- A2A Protocol (task states including `auth-required`): https://a2a-protocol.org/latest/specification/
- S. Willison, "The lethal trifecta for AI agents" (2025): https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
- Meta AI, "Agents Rule of Two: A Practical Approach to AI Agent Security" (2025): https://ai.meta.com/blog/practical-ai-agent-security/
- E. Debenedetti et al., "Defeating Prompt Injections by Design" (CaMeL, 2025): https://arxiv.org/abs/2503.18813
- IETF draft, The Idempotency-Key HTTP Header Field: https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
- LangGraph human-in-the-loop (interrupts): https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/
