# Primitive Sheet: FastAPI RBAC

Extracted from [`solutions/fastapi-rbac-design.md`](../solutions/fastapi-rbac-design.md).
Method and template: [`README.md`](README.md).

**The odd one out.** The other four sheets are distributed-systems problems
measured in TB and QPS. This is an *application architecture* problem — 100k
users, one Postgres, one Redis. It teaches modelling, cache invalidation
fan-out, and multi-tenancy, and it is the sheet most likely to be directly
useful in a real job. It is also the shortest, honestly: there is less here.

---

## 0. The meta-primitive: choosing the authorization model

Every authorization system is one of three shapes, and picking the wrong one is
a rewrite, not a refactor.

| Model | Question it answers | Example | Cost |
|---|---|---|---|
| **RBAC** | What *kind of user* are you? | "admins can delete users" | Cannot express per-object rules |
| **ABAC** | What do the *attributes* say? | "editors in the EU during business hours" | Policy engine; hard to audit "who can do X?" |
| **ReBAC** | What is your *relationship* to this object? | "you can edit posts you authored" | Graph store, Zanzibar-class infrastructure |

**Choice:** RBAC.
**Forced by:** Nothing — this is a judgment call, and the solution states it
plainly: **RBAC covers ~90% of cases and is far simpler.**
**In one breath:** Users get roles, roles carry permissions, and a check is
"does this user's permission set contain this string?"
**Cost accepted:** The missing 10% is **per-object authorization**, and it is
not a small 10%. "A user may edit *their own* posts" is inexpressible in pure
RBAC. Every application eventually wants it, and it gets bolted on as ad-hoc
`if resource.owner_id == user.id` checks scattered through the codebase.
**Flips when:** Ownership or sharing semantics appear (documents, projects,
folders). That is ReBAC, and the honest answer is Zanzibar-style relationship
tuples rather than more roles.

**Know where the boundary is.** The strongest thing you can say about an authz
design is not which model you chose, but **the specific requirement that would
force you off it** — and that you'd rather add a second mechanism deliberately
than let `if owner_id ==` spread.

---

## 1. The simplifying constraint: additive-only, no deny

**Choice:** Permissions are purely additive. There are **no deny rules**.
**Forced by:** Nothing external. This is the design buying its own
tractability, and it is the most consequential line in the document.
**In one breath:** Roles only ever grant; nothing takes away — so a user's
permissions are just the union of their roles' permissions.
**The number:** A permission check is one set-membership test on a
`Set[str]`. **O(1), no ordering, no precedence.**
**Cost accepted:** You cannot express "managers can do everything except delete
users." You must instead define a role that never had delete.
**Flips when:** Compliance requires explicit denial (a suspended user, a legal
hold) that must override every grant. Then you need precedence rules — and
you're building a policy engine.

**Why this matters more than it looks.** With deny rules, resolution becomes
order-dependent:

```
Additive:  permissions = union(role.permissions for role in user.roles)
           → commutative, associative, cacheable, trivially correct

With deny: which wins — a deny on a low role or a grant on a high one?
           what about deny-on-parent, grant-on-child?
           → order matters, conflicts need rules, the rules need docs,
             and "why can't this user do X?" becomes a debugging session
```

**This is the same property that makes counters batchable
([`distributed-counter.md`](distributed-counter.md) §5) and CRDTs mergeable:
commutativity.** When an operation is a commutative union, you can compute it
in any order, cache it, and merge it. The moment you add a non-commutative
operator, all of that is gone. **Look for the commutativity decision in every
design — it is usually the one that determines how hard everything else will
be.**

---

## 2. Read-model derivation — flatten the graph at auth time

**Choice:** Resolve `user → roles → role_hierarchy → permissions` once, into a
flat `Set[str]`, and cache it.
**Forced by:** 5 ms P99 on a check that runs on **every single request**.
Traversing the role hierarchy per check would be several joins in the critical
path of everything.
**In one breath:** Walk the role graph once at login, flatten it to a list of
permission strings, and every later check is a set lookup.
**The number:**

```
Stored:   users → user_roles → roles → role_hierarchy → role_permissions
                                                      → permissions
          (5 tables, recursive hierarchy walk)

Resolved: AuthContext.permissions = {"users:read", "users:write", "posts:read"}
          (one set, in memory, O(1) checks)

Cached:   rbac:user:{tenant}:{user}:permissions   TTL 5 min
          rbac:role:{role_id}:permissions         TTL 10 min
```

**Cost accepted:** A permission change takes up to 5 minutes to take effect
unless explicitly invalidated (see §3).
**Flips when:** Permissions become per-object — then there is no finite set to
flatten and you must check per resource at access time.

**This is a materialized view** (Tier 1 #20), the same primitive as Instagram's
precomputed feed and Twitter's prefix ZSETs: *the stored shape is normalised
for correctness, the read shape is denormalised for speed, and a cache with a
TTL bridges them.* The role hierarchy is a graph in Postgres and a flat set in
RAM.

**The two-level cache is worth noting:** role→permissions is cached separately
(10 min) from user→permissions (5 min). Roles change far less often than role
assignments, so the more stable data gets the longer TTL. **Cache TTL should
track the mutation rate of the thing cached, not be a global constant.**

---

## 3. Cache invalidation fan-out — the real hard part

**Choice:** Event-based invalidation, not TTL-only.
**Forced by:** Security. A revoked admin who keeps their permissions for
5 minutes is a real incident, not a stale-data annoyance.
**In one breath:** When permissions change, actively delete the affected cache
entries instead of waiting for them to expire.
**The number:**

| Event | Invalidation scope |
|---|---|
| User's roles change | 1 key — the user's |
| **Role's permissions change** | **Every user holding that role** |
| Permission definition changes | Entire cache |

**Cost accepted:** Middle row. A role held by 50,000 users means 50,000 cache
deletions from one admin action.
**Flips when:** Staleness is acceptable — then TTL-only, and all this
disappears.

**This is the weak point of the source design, and it is exactly what an
interviewer would push on.** The document specifies *what* to invalidate but
not *how* to do it at scale. Two standard answers worth having ready:

- **Generation counters.** Store `rbac:role:{id}:version`; include the version
  in the user's cache key or in the cached payload. Bumping the role version
  invalidates every dependent entry with **one write**, and stale entries age
  out naturally. This is the answer for the 50,000-user case.
- **Cache tags / dependency sets.** Track which user keys derive from which
  role and delete precisely. Exact, but you now maintain the reverse index —
  and that index has its own consistency problem.

**Generalise: invalidation cost scales with fan-out, and there are only three
answers** — accept staleness (TTL), pay the fan-out (delete each), or make the
cached value self-invalidating (version stamp). Instagram picked the first
([`instagram-feed.md`](instagram-feed.md) §4, lazy filtering); this design picks
the second and would need the third to scale.

---

## 4. Multi-tenancy

**Choice:** Shared schema with a `tenant_id` column on every table, filtered on
every query.
**Forced by:** 100k users across many tenants. Database-per-tenant doesn't
scale operationally at that count; schema-per-tenant makes migrations
combinatorial.
**In one breath:** One database, one set of tables, and a tenant column that
must appear in every single query.
**The number:**

```
Tenant resolution, in priority order:
  1. JWT `tenant_id` claim   ← preferred: signed, unforgeable
  2. X-Tenant-ID header      ← must be authorized against the token
  3. Subdomain

Roles: tenant_id = NULL  → global role
       tenant_id = X     → tenant-scoped role
```

**Cost accepted:** **Isolation is enforced by discipline, not by the database.**
One forgotten `WHERE tenant_id = ?` is a cross-tenant data leak — the worst bug
class this system can produce.
**Flips when:** A tenant demands physical isolation (regulatory, or an
enterprise contract) → database-per-tenant for that tenant, hybrid model.

**The mitigation the document doesn't name, and should:** enforce it at the
database, not in application code. **Postgres row-level security** with a
session variable makes tenant isolation structural — a query that forgets the
filter returns nothing rather than everything. Failing that, a repository base
class that injects the filter, with a lint rule banning raw queries.

**The ordering of tenant resolution is a security decision.** JWT first because
it's signed. A header is client-controlled and must be *checked against* the
token, never trusted on its own — accepting `X-Tenant-ID` blindly is a
one-header privilege escalation.

---

## 5. Stateless tokens with server-side permissions

**Choice:** JWT carries **identity**; permissions are resolved server-side per
request.
**Forced by:** Revocation. Permissions inside the token cannot be withdrawn
before it expires.
**In one breath:** The token says who you are; the server decides, fresh, what
you're allowed to do.
**The number:**

```
JWT claims:      user_id, tenant_id, exp        ← small, stable
NOT in the JWT:  permissions                    ← would be unrevocable

Revocation latency:  cache TTL (5 min) or immediate with event invalidation
                     vs. token lifetime (hours) if permissions were embedded
```

**Cost accepted:** A cache lookup (and occasionally a DB query) on every
request, instead of a pure signature verification. The system is not truly
stateless.
**Flips when:** Permissions genuinely never change during a token's life, and
you need zero-dependency verification (edge auth, offline validation). Then
embed them and accept short token lifetimes as the revocation mechanism.

**This is the JWT trade-off that gets missed constantly.** "Stateless JWT" is
sold as needing no server state, but that property is exactly what makes
revocation impossible. **Putting only stable identity in the token and keeping
volatile authorization server-side gets the scalability of JWT with the
revocability of sessions** — and it is the right default for anything where
permissions can be withdrawn.

Note the parallel to [`twitter-search.md`](twitter-search.md) §5: *don't embed a
volatile field in an artefact that is expensive to rewrite.* A JWT is an
immutable, signed artefact; permissions are volatile. Same rule, different
surface.

---

## 6. Where the check happens

**Choice:** FastAPI dependency injection —
`Depends(require_permission("users:read"))` — over middleware.
**Forced by:** Testability and explicitness.
**In one breath:** Declare the required permission on the route itself, so it's
visible in the signature and injectable in tests.
**Cost accepted:** Every route must remember to declare it. Middleware would
catch routes you forgot; DI will not.
**Flips when:** You need a blanket default-deny — then middleware (or a router
that requires an explicit `public=True` opt-out) is safer, because forgetting
becomes fail-closed instead of fail-open.

**Defense in depth is the mitigation:** check at the route, at the service
layer, and at the data layer. Route checks alone miss every path that doesn't
go through a route — background jobs, admin scripts, message consumers, and
internal service calls. Those are exactly the paths where authorization bugs
survive longest, because nobody tests them.

**The permission code convention `{resource}:{action}`** is small and worth
copying. Flat strings are greppable, cacheable as a set, easy to enumerate for
docs, and simple to validate. The alternative — structured permission objects —
is more expressive and immediately loses O(1) checks and easy caching.

---

## 7. What this design does *not* handle

Stated plainly, because knowing an approach's limits is most of the value.

- **Per-object permissions.** The 10% RBAC misses (§0). The single most common
  reason teams outgrow it.
- **Invalidation at fan-out scale.** §3 — needs generation counters.
- **Deny rules / suspension.** §1 — additive-only is a hard architectural
  boundary.
- **Permission migration.** Renaming `users:write` when it's referenced in
  50 route decorators and 10,000 database rows. The production checklist names
  it; the design doesn't solve it. (Aliasing plus a deprecation window is the
  usual answer.)
- **Delegation and impersonation.** "Support can act as this user" — needs an
  audit-visible principal-vs-subject distinction the `AuthContext` doesn't have.

---

## The index card

```
SCALE     100k users · 1k roles · 10k permissions · 10k checks/s/instance
          Permission check P99 ≤ 5 ms — in the critical path of EVERY request

MODEL     RBAC (~90% coverage). Boundary = PER-OBJECT rules ("edit your own")
          → that's ReBAC/Zanzibar, not more roles. Know the boundary.
ADDITIVE  NO DENY RULES. permissions = union(roles) → commutative → cacheable,
          order-independent, O(1). Deny would make resolution order-dependent.
          Same property that makes counters batchable and CRDTs mergeable.
RESOLVE   Flatten user→roles→hierarchy→permissions into a Set[str] at auth.
          5 tables → 1 set. Materialized view, same as feed/autocomplete.
CACHE     L1 request-scoped → L2 Redis → Postgres
          user perms 5 min · role perms 10 min · permission defs 1 h
          TTL tracks MUTATION RATE of the thing cached, not a global constant.
INVALID.  Event-based, not TTL-only (security, not freshness).
          HARD CASE: role perms change → invalidate all users with that role.
          50k users = 50k deletes. FIX = generation counter on the role: one
          write invalidates everything. Three options only:
          accept staleness · pay fan-out · self-invalidating version stamp.
TENANCY   Shared schema + tenant_id on every query.
          Isolation by DISCIPLINE → enforce with Postgres RLS instead.
          Resolution: JWT claim (signed) > header (must authorize!) > subdomain
JWT       Identity in the token; PERMISSIONS RESOLVED SERVER-SIDE.
          Embedded permissions = unrevocable until expiry.
          Same rule as "don't index a volatile field."
CHECK     DI over middleware: explicit + testable, but fail-OPEN if forgotten.
          Defense in depth: route + service + data layer. Background jobs
          bypass route checks — that's where authz bugs live.
CODES     {resource}:{action} — flat strings: greppable, set-cacheable, O(1)

GAPS      per-object perms · invalidation fan-out · deny/suspension ·
          permission renaming · delegation/impersonation
```
