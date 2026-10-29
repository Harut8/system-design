# 11 — Authorization models and policy engines

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (§1: authorization needs context authentication
> doesn't have), [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) (§8: scopes limit the
> client, not the user).
>
> **Feeds into:** [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md)
> (agent permissions are an intersection of these models), [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md).
> Worked design: [`solutions/fastapi-rbac-design.md`](../solutions/fastapi-rbac-design.md).
>
> **THESIS:** authentication is mostly a solved, buyable problem; authorization is not, because it depends on *your*
> data model. The most common serious API vulnerability is not a crypto bug but a missing object-level check
> (`GET /invoices/4711` returns any tenant's invoice). Pick the simplest model that expresses your sharing rules
> (RBAC for "what kind of user", ReBAC for "who has access to this object", ABAC for conditions), **decide in one place,
> enforce at every entry point, default to deny, and test authorization as a matrix**, not as a side effect of feature
> tests.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The architecture of an authorization decision](#1-the-architecture-of-an-authorization-decision)
2. [RBAC: roles, and where they stop scaling](#2-rbac-roles-and-where-they-stop-scaling)
3. [ABAC: attributes and conditions](#3-abac-attributes-and-conditions)
4. [ReBAC and Zanzibar: permissions as a graph](#4-rebac-and-zanzibar-permissions-as-a-graph)
5. [Policy engines compared](#5-policy-engines-compared)
6. [AuthZEN: a standard API between PEP and PDP](#6-authzen-a-standard-api-between-pep-and-pdp)
7. [Multi-tenancy and the list-filtering problem](#7-multi-tenancy-and-the-list-filtering-problem)
8. [The OWASP API authorization failures](#8-the-owasp-api-authorization-failures)
9. [Performance, caching and consistency](#9-performance-caching-and-consistency)
10. [Authorization for delegated callers and agents](#10-authorization-for-delegated-callers-and-agents)
11. [Testing authorization](#11-testing-authorization)
12. [Code: a PDP with RBAC + ownership, Postgres RLS, a toy ReBAC check](#12-code-a-pdp-with-rbac--ownership-postgres-rls-a-toy-rebac-check)
13. [Anti-patterns](#13-anti-patterns)
14. [Interview questions](#14-interview-questions)
15. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Once you know *who* is calling, you must decide *what they may do*. Three ways to describe the rules:

- **RBAC (roles)**: "Admins can delete projects. Viewers can read." Easy to understand; breaks when every customer
  wants slightly different roles, or when access depends on the specific object.
- **ReBAC (relationships)**: "You can edit this document because you're in the team that owns the folder it's in."
  This is how Google Drive, GitHub and Notion sharing work. Google described its system, Zanzibar, in 2019.
- **ABAC (attributes)**: "Doctors may read records of patients in their own ward during their shift." Rules over
  properties of the user, the object and the context.

Whatever the model, the structure is the same: code at the entry point (**enforcement point**) asks a **decision
point** "may subject S do action A on resource R?", gets yes/no, and refuses by default if anything is unclear.

The bug that matters most: checking "is logged in" or "has role" but not "owns *this* record". Every endpoint that
takes an ID must check the caller's right to that specific ID.

---

## 1. The architecture of an authorization decision

XACML-era vocabulary that is still the cleanest way to talk about it:

| Component | Role | Example |
|---|---|---|
| **PEP** (enforcement point) | Intercepts the request, asks, enforces | API gateway, middleware, service method, database RLS |
| **PDP** (decision point) | Evaluates policy, returns decision | OPA, Cedar, OpenFGA, a library in-process |
| **PAP** (administration point) | Where policies are written and managed | Git repo of Rego/Cedar, admin UI for roles |
| **PIP** (information point) | Supplies attributes the PDP needs | User directory, resource DB, relationship store |

Decision request shape (identical across engines, and standardized by AuthZEN, §6):

```
subject:  {type: user, id: alice, attrs: {tenant: acme, roles: [editor], dept: finance}}
action:   {name: invoice.approve}
resource: {type: invoice, id: 4711, attrs: {tenant: acme, owner: bob, amount: 1200, state: draft}}
context:  {time, ip, mfa_age_s: 120, via_agent: "agent:inbox", client_id: "web-app"}
→ {decision: true|false, reasons, obligations}
```

Placement options:

| | Central PDP service | Sidecar / library PDP with distributed policy | Inline code |
|---|---|---|---|
| Latency | Network hop (~1–5 ms) | Local (μs) | Local |
| Consistency of policy | One version | Eventual (bundle distribution) | Per deploy |
| Data | PDP fetches or is sent | Data pushed with bundles, or sent in request | Direct DB access |
| Use | ReBAC with large relationship graph | Policy-as-code across many services | Small apps, very hot paths |

Coarse checks belong at the edge (gateway: "is this token allowed to call `/admin/*` at all?"); fine-grained checks
belong where the data is (service: "may Alice approve invoice 4711?"). Doing only the edge check is the root of BOLA (§8).

---

## 2. RBAC: roles, and where they stop scaling

Model: users ↔ roles ↔ permissions. NIST's RBAC standard adds role hierarchies (senior roles inherit junior ones) and
separation-of-duty constraints (no one holds both "create vendor" and "approve payment").

Works well when: permissions depend on *job function*, the number of roles is small and stable, and resources are
uniform within a tenant.

Breaks when:

- **Role explosion**: "editor-of-project-17", "viewer-in-EU-only", custom roles per customer. Hundreds of roles that
  encode resource IDs or conditions are a sign you need ReBAC or ABAC.
- **Per-object sharing**: "share this doc with Carol" can't be a role.
- **Ownership**: "users can edit their own profile" is a relationship, not a role.

Production RBAC design:

- Permissions are **verbs on resource types** (`invoice.read`, `invoice.approve`); roles are bundles of permissions;
  code checks **permissions, never role names** (`if user.role == "admin"` scattered in code makes roles impossible to
  change).
- Roles are **scoped**: a role binding is `(subject, role, scope)` where scope is tenant, project or org unit. "Admin of
  project X" is a binding, not a role.
- Tenant-defined custom roles = tenant-defined bundles of *your* permissions.
- Map IdP groups to role bindings at login/SCIM time (ch 10 §5), not by trusting arbitrary group names in tokens.

---

## 3. ABAC: attributes and conditions

Policies are boolean expressions over subject, resource, action and context attributes:

```
permit invoice.approve if
    subject.tenant == resource.tenant
and "finance" in subject.departments
and resource.amount <= subject.approval_limit
and resource.state == "submitted"
and resource.created_by != subject.id          # separation of duties
and context.mfa_age_s < 900                     # recent strong auth
```

Strength: expresses conditions (amount limits, time, location, data classification, device posture) that roles can't.
Weaknesses: attribute **freshness** (stale attributes cached in tokens), difficulty answering "who can access X?" (you'd
have to evaluate the policy for every user), and policies that nobody can read once they grow. Most real systems are
**RBAC or ReBAC with ABAC conditions** layered on top.

---

## 4. ReBAC and Zanzibar: permissions as a graph

> **In plain words.** Store facts like "Alice is a member of team Eng", "team Eng is an editor of folder F", "doc D is in
> folder F". Then "can Alice edit D?" is a graph question: is there a path from Alice to `editor` on D following the
> rules?
>
> **Real-world example.** Google Zanzibar (USENIX ATC 2019) serves authorization for Drive, YouTube, Calendar, Photos and
> more: trillions of relationship tuples, millions of checks per second, p95 under 10 ms, per the paper.

**Tuples:** `object#relation@subject`

```
doc:d1#parent@folder:f1
folder:f1#editor@team:eng#member
team:eng#member@user:alice
```

**Schema (OpenFGA-style DSL):**

```
model
  schema 1.1
type user
type team
  relations
    define member: [user]
type folder
  relations
    define editor: [user, team#member]
    define viewer: [user, team#member] or editor
type doc
  relations
    define parent: [folder]
    define owner: [user]
    define editor: [user, team#member] or owner or editor from parent
    define viewer: [user, team#member] or editor or viewer from parent
```

**APIs:** `Check(doc:d1, editor, user:alice)` → true; `ListObjects(user:alice, viewer, type doc)`; `ListUsers` /
`Expand` for "who can see this?" (which ABAC can't answer cheaply).

**Consistency: the "new enemy" problem.** Alice removes Bob from a folder, then adds a secret doc to it. If the
authorization system evaluates Bob's access to the new doc against a *stale* snapshot taken before the removal, Bob sees
the secret. Zanzibar solves this with **zookies**: a consistency token returned when content changes, passed to later
checks so they evaluate at a snapshot at least that fresh. SpiceDB calls them **ZedTokens**; OpenFGA offers
consistency preferences. If you build ReBAC yourself, you need an equivalent.

Implementations: **OpenFGA** (CNCF, from Auth0/Okta), **SpiceDB** (AuthZed), **Ory Keto**, **Permify**; AWS Verified
Permissions uses Cedar (policy-based, with entity hierarchies) rather than tuples.

---

## 5. Policy engines compared

| Engine | Model | Language | Strengths | Watch out for |
|---|---|---|---|---|
| **OPA** (CNCF graduated) | General policy (ABAC/RBAC, anything) | Rego (Datalog-like) | Ubiquitous: Kubernetes admission (Gatekeeper), Envoy ext_authz, Terraform, CI; bundles; decision logs | Rego learning curve; data must be loaded or sent; partial evaluation for list filtering is complex |
| **Cedar** (AWS, open source) | RBAC + ABAC with entity hierarchy | Cedar | Formally analyzable (verified with Lean/Dafny), readable, fast, `forbid` overrides `permit`; AWS Verified Permissions, Amazon Bedrock AgentCore policies | Younger ecosystem |
| **OpenFGA** | ReBAC (Zanzibar) | Modeling DSL | Graph queries, ListObjects, SDKs, consistency options | Needs tuple sync from your data |
| **SpiceDB** | ReBAC (Zanzibar) | Schema language | Strong consistency (ZedTokens), caveats (ABAC conditions on edges) | Operating a separate datastore |
| **Casbin** | Configurable (ACL/RBAC/ABAC) | PERM model config | Embedded library in many languages | Ad hoc models get messy |
| **Database RLS** | Row filters | SQL policies | Enforced at the data layer for every query | Only as good as the session variables you set (§12.2) |

Cedar example:

```cedar
permit (
  principal in Role::"finance",
  action == Action::"invoice.approve",
  resource
) when {
  resource.tenant == principal.tenant &&
  resource.amount <= principal.approvalLimit &&
  resource.createdBy != principal
};

forbid (principal, action, resource) when { principal.suspended };
```

Rego equivalent of the same rule:

```rego
package invoices

default allow := false

allow if {
    input.action == "invoice.approve"
    "finance" in input.subject.roles
    input.resource.tenant == input.subject.tenant
    input.resource.amount <= input.subject.approval_limit
    input.resource.created_by != input.subject.id
    not input.subject.suspended
}
```

Decision guide: one service with simple roles → in-process code with a permission table. Many services, policy-as-code
across infra → OPA. Product-level per-object sharing (docs, projects, folders) → OpenFGA/SpiceDB. Product authorization
with conditions and a desire for analyzability → Cedar. Always combine with DB-level tenant isolation (§7).

---

## 6. AuthZEN: a standard API between PEP and PDP

The OpenID Foundation's **Authorization API 1.0** (AuthZEN working group, approved as a Final Specification in
January 2026) standardizes the PEP→PDP call, so gateways and apps can swap PDPs without rewriting integrations:

```http
POST /access/v1/evaluation
{
  "subject":  {"type": "user", "id": "alice@acme.com"},
  "action":   {"name": "can_read"},
  "resource": {"type": "invoice", "id": "4711"},
  "context":  {"time": "2026-10-29T09:00:00Z"}
}
→ {"decision": true}
```

It also defines batch evaluation (`/access/v1/evaluations`) and search APIs (which subjects/resources/actions match) and
a PDP metadata document. Vendors and open-source PDPs (Topaz, Cerbos, OpenFGA adapters, and others) participate in its
interop events. If you're designing the interface between your gateway and your policy service, adopt its shape.

---

## 7. Multi-tenancy and the list-filtering problem

**Tenant isolation is the authorization rule you must never get wrong**, so enforce it in at least two layers:

1. Application: every query carries `tenant_id` from the authenticated context (never from the request body/URL).
2. Database: **Row-Level Security** in Postgres (`USING (tenant_id = current_setting('app.tenant_id')::uuid)`), or
   schema/database per tenant for high-isolation customers.

**The list-filtering problem.** `Check(user, resource)` works for one object. `GET /invoices?status=open` must return
*only* invoices the user may see, with pagination. Options:

| Approach | How | When |
|---|---|---|
| Pre-filter in SQL | Translate policy to `WHERE` (tenant, owner, team membership joins) | Simple models; fastest |
| Policy partial evaluation | OPA `compile` API / Cedar partial evaluation generates residual conditions → SQL | Policy engines with complex rules |
| ReBAC ListObjects then fetch | Ask OpenFGA for IDs, then `WHERE id IN (...)` | Moderate result sizes |
| Post-filter with Check | Fetch a page, check each, fetch more if needed | Small pages, rare denials; breaks counts/pagination |
| Materialized ACL | Denormalize "who can see what" into a table/index (e.g. search index with ACL field) | Search over large corpora; RAG retrieval (see [ai-rag ch 29 §11](../ai-rag/29-agent-identity-and-delegated-authorization.md)) |

---

## 8. The OWASP API authorization failures

OWASP API Security Top 10 (2023), the authorization-related entries:

| ID | Name | What it looks like | Fix |
|---|---|---|---|
| API1 | **Broken Object Level Authorization (BOLA / IDOR)** | `GET /api/orders/1002` returns someone else's order | Object-level check on **every** access by ID; unguessable IDs help but aren't the fix |
| API3 | **Broken Object Property Level Authorization** | Response includes `is_admin`, `ssn`; or **mass assignment**: `PATCH /me {"role": "admin"}` accepted | Explicit response schemas; allowlist writable fields per role |
| API5 | **Broken Function Level Authorization (BFLA)** | Regular user calls `POST /admin/users` because the UI hid the button but the API didn't check | Permission check on every function; deny by default for admin routes |
| API2 | Broken Authentication | (authentication, but adjacent) | Chapters 01–07 |

BOLA notes:
- Return **404** rather than 403 for objects in other tenants, so attackers can't enumerate existence.
- Check on **writes and deletes** too, including bulk endpoints, export jobs, GraphQL resolvers (each field resolver
  that loads by ID), webhooks that accept IDs, and background jobs triggered with user-supplied IDs.
- File downloads via signed URLs: sign `(object, user, expiry)` and check before signing.

---

## 9. Performance, caching and consistency

- **Decision caching** keyed by `(subject, action, resource, policy_version, relationship_snapshot)`; TTL short
  (seconds); invalidate on role/relationship change; never cache denials so long that newly granted access looks broken.
- **Attribute caching in tokens**: roles and groups in JWTs are fast but stale until refresh. Acceptable for coarse roles
  with short tokens; not for "removed from the project five seconds ago" (use ReBAC with consistency tokens).
- **Fail closed**: PDP unreachable → deny (with a clear error), except for explicitly designated low-risk reads where
  you've decided availability wins and logged that decision.
- **Latency budgets**: a page that checks 200 objects needs batch evaluation (AuthZEN `evaluations`, OpenFGA
  `BatchCheck`) or list filtering, not 200 sequential network calls.
- **Decision logs**: log every decision (subject, action, resource, decision, policy version, reason). They are your
  audit trail and your debugging tool, and they are high-volume: sample allows, keep all denies and all
  sensitive-resource decisions.

---

## 10. Authorization for delegated callers and agents

When a client (or agent) acts for a user, the effective permission is an **intersection**:

```
allowed = user_may(action, resource)                 # the human's own rights (RBAC/ReBAC/ABAC)
      AND client_granted(action, resource)           # OAuth scopes / RAR authorization_details for this client
      AND agent_policy(agent_id, action, resource)   # what this agent type may ever do (ch 14 §2)
      AND context_ok(step_up, approval, budget)      # freshness, HITL approval, rate/budget limits
```

Common bug: implementing only the second term (scopes) and letting a token with `files.write` write files the user
can't write. Another: an agent with its own service identity doing work "for" users, checked only against the agent's
permissions, which makes it a confused deputy for every user (ch 00 §7). Pass both identities to the PDP (`sub` and
`act`, ch 07 §7) and write policies that use both.

---

## 11. Testing authorization

- **Authorization matrix tests**: for each endpoint × role/relationship × tenant (same/other) × ownership, assert
  allow/deny. Generate the matrix; don't hand-write 400 tests.
- **Cross-tenant tests in CI**: create two tenants with identical data; run every endpoint as tenant A against tenant B's
  IDs; everything must 404.
- **Policy unit tests**: OPA `opa test`, Cedar's validator and test harnesses, OpenFGA model tests (`fga model test`)
  with tuples and expected checks.
- **Static analysis**: lint for route handlers that load by ID without calling the authz helper (custom Semgrep rules).
- **Production canaries**: run new policy versions in **shadow mode**, logging decisions that would differ, before
  enforcing.

---

## 12. Code: a PDP with RBAC + ownership, Postgres RLS, a toy ReBAC check

### 12.1 In-process PDP with permissions, scoped role bindings and object checks

```python
from dataclasses import dataclass, field

ROLE_PERMISSIONS = {
    "viewer":  {"invoice.read"},
    "editor":  {"invoice.read", "invoice.create", "invoice.update"},
    "finance": {"invoice.read", "invoice.approve"},
    "admin":   {"invoice.read", "invoice.create", "invoice.update", "invoice.delete", "invoice.approve"},
}

@dataclass
class Subject:
    id: str
    tenant: str
    bindings: list[tuple[str, str]]            # (role, scope) e.g. ("editor", "tenant:acme"), ("admin", "project:p1")
    approval_limit: int = 0
    granted_scopes: set[str] | None = None     # OAuth scopes if acting via a client/agent; None = first-party session
    attrs: dict = field(default_factory=dict)

@dataclass
class Invoice:
    id: str
    tenant: str
    project: str
    created_by: str
    amount: int
    state: str

def has_permission(s: Subject, perm: str, scopes: set[str]) -> bool:
    return any(perm in ROLE_PERMISSIONS.get(role, ()) and scope in scopes for role, scope in s.bindings)

def authorize(s: Subject, action: str, inv: Invoice) -> tuple[bool, str]:
    if s.tenant != inv.tenant:
        return False, "cross-tenant"                                   # caller should return 404
    if s.granted_scopes is not None and action not in s.granted_scopes:
        return False, "client not granted this scope"                  # delegated-caller intersection
    if not has_permission(s, action, {f"tenant:{inv.tenant}", f"project:{inv.project}"}):
        return False, "no permission"
    if action == "invoice.approve":
        if inv.state != "submitted":
            return False, "wrong state"
        if inv.created_by == s.id:
            return False, "separation of duties"
        if inv.amount > s.approval_limit:
            return False, "over approval limit"
    return True, "ok"
```

### 12.2 Postgres Row-Level Security as the tenant backstop

```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE ROW LEVEL SECURITY;         -- applies to the table owner too

CREATE POLICY tenant_isolation ON invoices
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- Per request/transaction, from the *authenticated* context, never from user input:
BEGIN;
SET LOCAL app.tenant_id = '6f1c...';                   -- SET LOCAL: scoped to this transaction (pool-safe)
SELECT * FROM invoices WHERE id = $1;                  -- other tenants' rows are invisible
COMMIT;
```

Pitfalls: connection pools that reuse a session with a leftover `SET` (use `SET LOCAL` inside a transaction); app roles
with `BYPASSRLS` or superuser; views and functions declared `SECURITY DEFINER` that skip policies; `current_setting`
missing → `NULL` → no rows (good: fails closed).

### 12.3 Toy ReBAC check (for intuition, not production)

```python
from collections import defaultdict

tuples = defaultdict(set)       # (object, relation) -> {subject or "obj#rel" usersets}
def write(obj, rel, subj): tuples[(obj, rel)].add(subj)

# rewrite rules: relation -> list of ("this",) | ("computed", rel) | ("tupleset", tupleset_rel, target_rel)
RULES = {
    ("doc", "viewer"): [("this",), ("computed", "editor"), ("tupleset", "parent", "viewer")],
    ("doc", "editor"): [("this",), ("computed", "owner"), ("tupleset", "parent", "editor")],
    ("doc", "owner"): [("this",)],
    ("folder", "viewer"): [("this",), ("computed", "editor")],
    ("folder", "editor"): [("this",)],
    ("team", "member"): [("this",)],
}

def check(obj: str, rel: str, user: str, depth: int = 0) -> bool:
    if depth > 25:
        return False                                            # cycle/depth guard
    otype = obj.split(":")[0]
    for rule in RULES.get((otype, rel), []):
        if rule[0] == "this":
            for s in tuples[(obj, rel)]:
                if s == user:
                    return True
                if "#" in s:                                    # userset, e.g. team:eng#member
                    o, r = s.split("#")
                    if check(o, r, user, depth + 1):
                        return True
        elif rule[0] == "computed" and check(obj, rule[1], user, depth + 1):
            return True
        elif rule[0] == "tupleset":
            for parent in tuples[(obj, rule[1])]:
                if check(parent, rule[2], user, depth + 1):
                    return True
    return False

write("team:eng", "member", "user:alice")
write("folder:f1", "editor", "team:eng#member")
write("doc:d1", "parent", "folder:f1")
assert check("doc:d1", "editor", "user:alice") and check("doc:d1", "viewer", "user:alice")
assert not check("doc:d1", "editor", "user:bob")
```

---

## 13. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Authorization only at the gateway | BOLA everywhere behind it | Object-level checks in services |
| `if user.role == "admin"` scattered in code | Can't change roles; inconsistent | Check permissions via one helper/PDP |
| Tenant ID taken from request body/URL | Cross-tenant access | From authenticated context; RLS backstop |
| Roles per resource ("editor-project-17") | Role explosion | Scoped role bindings or ReBAC |
| Trusting group names from any IdP token | Customer admin grants themselves your admin role | Map configured groups per tenant to bindings |
| Scopes treated as the whole permission | Clients/agents exceed user rights | Intersection with user permissions |
| 403 for other tenants' objects | Enumeration | 404 |
| Mass assignment via ORM `update(**body)` | Users set their own role/tenant | Allowlisted writable fields |
| No consistency tokens in ReBAC | New-enemy problem | Zookies/ZedTokens or read-after-write consistency |
| PDP fails open | Outage = everyone allowed | Fail closed; explicit exceptions |
| Authorization tests only via happy-path feature tests | Gaps go unnoticed | Generated matrix + cross-tenant suites |

---

## 14. Interview questions

**Q1. When would you move from RBAC to ReBAC?**
When access depends on relationships to specific objects (sharing, ownership, hierarchy of folders/projects/orgs), and
you see role explosion or per-object role names. Keep RBAC for job-function permissions within a scope.

**Q2. Explain the new-enemy problem.**
A permission revocation followed by a content change can be evaluated out of order if checks use a stale snapshot,
exposing new content to the revoked user. Zanzibar uses zookies so checks after a change use a snapshot at least as new
as the change.

**Q3. How do you implement "list all documents I can see" with pagination over a ReBAC system?**
ListObjects for moderate sets then filter in SQL; or maintain a denormalized ACL index (search index field) updated from
relationship changes; post-filter only for small pages. Discuss staleness and consistency trade-offs.

**Q4. Design tenant isolation for a shared-schema SaaS database.**
Tenant ID from auth context on every query, Postgres RLS with `SET LOCAL` per transaction as a backstop, no `BYPASSRLS`
in app roles, cross-tenant CI tests, 404 on cross-tenant IDs, and per-tenant encryption keys for sensitive fields.

---

## Sources

- Zanzibar: Google's Consistent, Global Authorization System (USENIX ATC 2019): https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/
- NIST RBAC model (INCITS 359): https://csrc.nist.gov/projects/role-based-access-control
- NIST SP 800-162, Guide to ABAC: https://csrc.nist.gov/pubs/sp/800/162/upd2/final
- OpenID AuthZEN Authorization API 1.0 (final, January 2026): https://openid.net/specs/authorization-api-1_0.html
- Open Policy Agent: https://www.openpolicyagent.org/docs/latest/
- Cedar policy language: https://docs.cedarpolicy.com/
- OpenFGA: https://openfga.dev/docs ; SpiceDB: https://authzed.com/docs
- OWASP API Security Top 10 (2023): https://owasp.org/API-Security/editions/2023/en/0x11-t10/
- PostgreSQL Row Security Policies: https://www.postgresql.org/docs/current/ddl-rowsecurity.html
