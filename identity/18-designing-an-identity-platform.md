# 18 — Designing an identity platform: architecture, scale, operations

> **Prerequisites:** the rest of this track. This chapter assembles it into one system and adds the numbers.
>
> **Related designs:** [`solutions/fastapi-rbac-design.md`](../solutions/fastapi-rbac-design.md),
> [`solutions/api-gateway-rate-limiter-design.md`](../solutions/api-gateway-rate-limiter-design.md),
> [`solutions/ai-agent-platform-design.md`](../solutions/ai-agent-platform-design.md).
>
> **THESIS:** identity is **tier-0**: when it is down, nothing works; when it is wrong, everything is exposed. That pushes
> the design in a consistent direction: keep the **hot path stateless and local** (JWT verification against cached keys,
> sessions in a fast store), keep the **issuing path small, boring, and multi-region**, keep **secrets out of reach**
> (KMS, hashing, vaults), make **revocation and deprovisioning first-class flows**, and **buy** the commodity parts unless
> identity is your product. Most teams should spend their engineering on authorization, tenant isolation, and the
> lifecycle edges, not on reimplementing login.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Requirements and phases: MVP, Growth, Scale](#1-requirements-and-phases-mvp-growth-scale)
2. [Build versus buy](#2-build-versus-buy)
3. [Reference architecture](#3-reference-architecture)
4. [Capacity planning: the numbers that matter](#4-capacity-planning-the-numbers-that-matter)
5. [Availability and failure modes](#5-availability-and-failure-modes)
6. [Key management operations](#6-key-management-operations)
7. [Account-takeover defense at scale](#7-account-takeover-defense-at-scale)
8. [Lifecycle: deprovisioning, retention and GDPR](#8-lifecycle-deprovisioning-retention-and-gdpr)
9. [Observability and SLOs](#9-observability-and-slos)
10. [Security operations: admins, break-glass, incidents](#10-security-operations-admins-break-glass-incidents)
11. [Making the platform agent-ready](#11-making-the-platform-agent-ready)
12. [CI/CD guardrails for identity code](#12-cicd-guardrails-for-identity-code)
13. [Interview walk-through: auth for 100M users](#13-interview-walk-through-auth-for-100m-users)
14. [Sources](#sources)

---

## Start here — the whole chapter in plain words

An identity platform is several systems that must work together:

- a **login service** (passwords, passkeys, MFA, social and enterprise SSO),
- a **token service** (issues and refreshes tokens, publishes keys),
- **session storage**,
- **user and tenant data**,
- **authorization** (who may do what),
- **lifecycle** (sign-up, provisioning, recovery, offboarding, deletion),
- and **audit**.

Because every request in your product depends on it, it must be fast (checking a token should never require a network
call), always available (an outage logs everyone out), and very careful with secrets. Most companies buy the login and
token parts from a vendor or run an open-source IdP, and build authorization and lifecycle glue themselves.

---

## 1. Requirements and phases: MVP, Growth, Scale

| Phase | Users / tenants | Must have | Defer |
|---|---|---|---|
| **MVP** | ≤100k users, first customers | Hosted IdP (managed or Keycloak); OIDC code + PKCE; BFF sessions; passkeys + password/TOTP; RBAC in app; tenant ID on every row + RLS; audit log table; secrets in a manager; short access tokens | SCIM, SAML, custom policy engine, multi-region |
| **Growth** | 100k–10M, first enterprise deals | SAML/OIDC per tenant, verified domains, SCIM with cascading deprovisioning; admin roles; API keys done right; ATO defenses (rate limits, breached-password checks, risk signals); token exchange for service calls; SIEM export | Own IdP, global active-active |
| **Scale** | 10M+ users, regulated customers | Multi-region identity with failover; ReBAC/policy engine; DPoP/mTLS for sensitive APIs; Shared Signals/CAEP; FAPI for payments; data residency; agent registry, token vault, MCP gateway | — |

The SMB lens: low-friction adoption means **passkeys and social login for consumers**, **"Sign in with Google/Microsoft" and
email magic links for small businesses**, and **SAML/SCIM only when a customer asks**, ideally via a broker so it doesn't
reshape the product.

---

## 2. Build versus buy

| Component | Buy / adopt | Build | Why |
|---|---|---|---|
| Login UI, MFA, passkeys, social/enterprise federation | ✔ (Okta/Auth0/Entra/Cognito/Keycloak/Zitadel/WorkOS) | Rarely | Commodity, security-critical, constant protocol churn |
| Token service / OAuth AS | ✔ | Only if identity is your product | Same |
| SCIM server for your product | Partly (WorkOS-style APIs) | ✔ usually | Must cascade into your data model |
| Authorization (product permissions) | Engine (OpenFGA/Cedar/OPA) | ✔ model + integration | Depends on your domain |
| Tenant isolation | — | ✔ | Your data |
| Token vault for integrations/agents | Possibly | ✔ often | Close to your data and audit |
| Workload identity | ✔ (cloud, SPIRE, mesh) | No | Platform concern |
| Audit pipeline | Components | ✔ glue | Your events |

Cost of buying: per-MAU pricing that can dominate at consumer scale; feature lock-in (ch 17 §11). Cost of self-hosting
(Keycloak etc.): on-call for tier-0, upgrades, CVEs, HA. Building your own AS from scratch is justified rarely (you are
an IdP vendor, or you have extreme scale/customization needs and a dedicated team).

---

## 3. Reference architecture

```
                         ┌──────────────────────────── Control plane ─────────────────────────────┐
                         │ Admin console · Tenant config (connections, domains, SSO enforcement)   │
                         │ Policy store (roles, relationships, agent registry, tool registry)      │
                         │ KMS/HSM (signing keys, KEKs) · Secrets manager · SIEM                    │
                         └────────────────────────────────────────────────────────────────────────┘
 Browser/app ─▶ CDN/WAF ─▶ BFF / Web app ──────────────┐
                 │  (rate limit, bot mgmt)              │ cookie session (Redis)
                 ▼                                       ▼
            Identity service (IdP / AS) ◀── upstream IdPs (customer SAML/OIDC, Google, Microsoft)
             ├─ /authorize /token /par /revoke /introspect /jwks /.well-known/*
             ├─ Credential store (Postgres: users, identities, password hashes, passkeys, encrypted TOTP)
             ├─ Session store (Redis, hashed IDs) · Refresh token store (hashed, families)
             ├─ Risk engine (IP/ASN reputation, device, velocity, breached passwords)
             ├─ SCIM server ◀── customer IdPs        ├─ SSF/CAEP transmitter & receiver
             └─ Audit events ──▶ stream ──▶ SIEM / audit store (immutable)
                 │ JWT access tokens (5–15 min), JWKS cached everywhere
                 ▼
 API gateway (validate JWT locally, coarse authz) ─▶ Services (object-level authz via PDP/library; RLS backstop)
                                                       │ mTLS (mesh/SPIFFE) + token exchange for downstream calls
                                                       ▼
                                          Agent runtime ─▶ AI gateway / MCP gateway (ch 14 §7) ─▶ tools
                                                       └─▶ Token vault (encrypted third-party tokens)
```

Design properties:

- **Hot path**: gateways and services verify JWTs locally with cached JWKS: zero calls to the identity service per request.
- **Issuance path**: `/token` and login are the only synchronous dependencies on the identity service; scale them
  horizontally; keep their dependencies minimal (DB, Redis, KMS).
- **State**: users/identities/credentials in Postgres (strongly consistent, regional primary); sessions and short-lived
  artefacts (codes, PKCE challenges, DPoP `jti`s, rate-limit counters) in Redis with TTLs.
- **Isolation**: identity service runs in its own accounts/projects/clusters with separate admin access.

---

## 4. Capacity planning: the numbers that matter

Assume 100M registered users, 20M DAU, peak factor 3×.

| Quantity | Estimate | Notes |
|---|---|---|
| Logins/day | ~10M (most users ride long sessions) | Spikes at 9 a.m. local and after incidents/forced logouts |
| Peak logins/s | 10M / 86,400 × 3 ≈ **350/s**; after a mass logout, 10–50× | Design for the thundering herd, not the average |
| Password hash cost | Argon2id m=19 MiB, ~50 ms CPU | 350/s × 0.05 s = ~18 cores; memory 350 × 19 MiB ≈ 6.6 GiB in flight. A 30× herd = ~500 cores: rate-limit and queue, prefer passkeys (signature verify ≈ 0.1 ms) |
| Token refreshes | 20M DAU × 1 per 15 min active × ~2 h active ≈ 160M/day ≈ **1.9k/s avg, ~6k/s peak** | Each refresh: DB lookup by hash + write (rotation) + JWT sign. Signing via KMS per token is too slow/expensive at this rate: use in-memory keys loaded from KMS (ch 08 §7) |
| JWT verifications | Every API call: e.g. **200k/s** | ES256 verify ≈ tens of μs per core: a few cores fleet-wide. Never call the IdP per request |
| JWKS fetches | Cached 10 min per instance; 2,000 instances → ~3/s | Serve from CDN |
| Session store | 20M active sessions × ~1 KB ≈ 20 GB | Redis cluster, replicated per region |
| Refresh token rows | ~50M live families × ~300 B ≈ 15 GB + indexes | Partition by user or time; TTL cleanup jobs |
| Audit events | ~20 events/user/day → 400M/day ≈ 5k/s | Stream (Kafka) → object storage + SIEM; retention policy |

Takeaways: the expensive operation is **password hashing under bursts**; the high-volume operation is **refresh**; the
highest-QPS operation (**verification**) is cheap if and only if it stays local.

---

## 5. Availability and failure modes

| Failure | Impact if naive | Design response |
|---|---|---|
| Identity service down | No logins; refresh fails → users logged out at AT expiry | Multi-AZ, multi-region failover for `/token` and login; existing **sessions in BFFs keep working**; consider slightly longer AT lifetimes during declared incidents (trade-off with revocation lag) |
| JWKS endpoint down | Verifiers can't load new keys | Verifiers cache last-known-good; CDN with stale-if-error; never rotate during an incident |
| KMS unavailable | Can't sign / unwrap | Keys loaded in memory at start with bounded lifetime; DEK caches |
| Redis session store lost | Mass logout → login herd | Replicas, persistence for sessions; rate-limited re-login; passkeys reduce hash load |
| Upstream customer IdP down | That tenant can't log in | Clear error; tenant admin break-glass account (strong MFA) per policy |
| Email/SMS provider down | No magic links, OTPs, resets | Secondary provider; passkeys/TOTP not dependent on it |
| Clock skew | Token validity errors | NTP everywhere; small leeway |
| Bad config push (e.g. wrong issuer, dropped key) | Global auth failure | Config validation in CI, canary tenants/regions, instant rollback |

**Multi-region**: user and credential data need a write-primary (or carefully partitioned by user/tenant home region for
residency); login in a non-home region can proxy to the home region or use read replicas for verification with writes
(rotation, lockout counters) routed home. Token signing keys are per region or shared via KMS multi-region keys; JWKS
publishes all active keys from all regions so any verifier accepts any region's tokens.

---

## 6. Key management operations

- Signing keys: ES256 or RS256/PS256, generated in KMS/HSM; rotate on schedule (e.g. every 90 days) with the
  publish → wait → sign → wait → retire protocol (ch 03 §7); automate it, alert if a key is older than policy.
- Separate keys per token type, per environment, ideally per region.
- KEKs for envelope encryption in KMS with automatic rotation; re-wrap DEKs lazily (ch 08 §5).
- Client authentication: prefer `private_key_jwt` for your own confidential clients; rotate their keys via JWKS URLs they host.
- Runbook for **key compromise**: remove from JWKS, rotate, force refresh, investigate forged-token use in audit logs.

---

## 7. Account-takeover defense at scale

Layers, outermost first:

1. **Edge**: WAF and bot management on login, sign-up, reset, OTP endpoints; per-IP/ASN rate limits; CAPTCHA/proof-of-work
   on risk.
2. **Credential checks**: breached-password screening at set and (sampled) at login; passkeys first; phishing-resistant MFA
   for admins and high-value accounts.
3. **Risk engine** per attempt: device fingerprint/cookie, IP reputation, impossible travel, velocity per account/IP/
   password hash, known-bad infrastructure. Outputs allow / step-up / deny, and influence session lifetime.
4. **Post-login**: anomaly detection on sessions (new ASN mid-session), re-auth for sensitive actions, notifications on
   new device login, password/MFA change.
5. **Recovery hardening**: cooling-off periods, notifications to all channels, help-desk verification (ch 01 §7).
6. **Signals sharing**: consume and emit Shared Signals (RISC `credential-compromise`, CAEP `session-revoked`, ch 10 §6).

Metrics to watch: login failure rate by reason, unique accounts targeted per IP/ASN, breached-password hit rate, MFA
challenge pass rate, reset requests per hour, new-device logins per account.

---

## 8. Lifecycle: deprovisioning, retention and GDPR

**Deprovisioning is the most important lifecycle flow**, because forgotten access is the most common way former users,
leaked credentials and abandoned integrations turn into incidents. A user, service account or agent being removed must
cascade to (ch 10 §5):

```
disable principal ─┬─ sessions (all devices)          ─┬─ API keys / PATs owned
                   ├─ refresh tokens, OAuth grants      ├─ connected accounts (revoke at providers, ch 08 §10)
                   ├─ outstanding JWTs (tokens_valid_after / CAEP)
                   ├─ agent grants, scheduled agent runs, registry entries they own (reassign owner)
                   ├─ SCIM-provisioned downstream accounts (via your outbound SCIM or webhooks)
                   └─ audit event + report to tenant admin
```

Also: **orphan detection** jobs (API keys whose owner is disabled, OAuth grants unused for 90 days, service accounts without
owners, agents without an active owner team) with automatic expiry.

**Retention and GDPR:**

| Data | Retention | Notes |
|---|---|---|
| Account and identity records | Life of account + legal hold | Delete or anonymize on erasure request |
| Credentials (hashes, passkeys, TOTP) | Life of credential | Delete immediately on account deletion |
| Sessions, refresh tokens | Until expiry/revocation | TTL cleanup |
| Login/audit events | e.g. 1 year hot, longer cold for compliance | Pseudonymize user identifiers where possible; access-controlled |
| Connected-account tokens | Until disconnect | Revoke at provider on deletion |
| Backups | Per policy | Crypto-shredding via per-user DEKs (ch 08 §12) |

Data subject access requests should include identity data (linked identities, devices/passkeys, sessions, consents and agent
grants). Consents (including per-agent grants, ch 14 §2) are records you must be able to show and revoke.

---

## 9. Observability and SLOs

**SLIs/SLOs (examples):**

| SLI | SLO |
|---|---|
| Login success for valid credentials (excluding user error) | 99.95% / 30 days |
| `/token` availability | 99.99% |
| `/token` latency p99 | < 300 ms (refresh), < 500 ms (code exchange) |
| JWKS availability (from CDN) | 99.999% |
| SCIM deprovisioning end-to-end (event → sessions revoked) | 99% < 60 s |
| Back-channel logout delivery | 99% < 30 s |

**Metrics**: logins by method/outcome/reason, MFA prompts and failures, refresh outcomes (`invalid_grant` rate is an early
signal of rotation bugs or attacks), token exchange denials by reason (agent suspended, consent missing), JWKS fetch errors
at verifiers, key age, session count, SCIM operation lag, vault unwrap rate per agent.

**Logs/audit**: structured events with `tenant`, `user`, `client_id`, `agent_id`, `act` chain, IP/ASN, device, method,
outcome; **never tokens, passwords or OTPs** (ch 08 §9). **Tracing**: propagate trace context through login and token
flows, but redact `Authorization` headers from spans.

Detail on SLO engineering and alerting: [sre-observability ch 13](../sre-observability/13-slo-engineering.md),
[ch 12](../sre-observability/12-alerting.md).

---

## 10. Security operations: admins, break-glass, incidents

- **Admin access**: separate admin identities, phishing-resistant MFA, device-bound sessions, short session lifetimes,
  just-in-time elevation with approval, all actions audited and alerted on (new admin, policy change, MFA reset, key
  operations).
- **Break-glass accounts**: a small number, hardware keys stored offline, monitored for any use, tested quarterly.
- **Customer support tooling**: no ability to view credentials; impersonation ("login as user") only with customer consent,
  time limits, banners and audit; never via sharing tokens.
- **Incident runbooks**: credential stuffing wave; session-cookie theft (infostealer); signing key compromise; vendor IdP
  breach; mass token revocation at third-party providers (ch 08 §11); compromised agent/MCP server (suspend in registry,
  revoke grants, rotate vault entries).

---

## 11. Making the platform agent-ready

Additions that let the same platform serve AI agents without a parallel identity system (ch 13–15):

1. **Agent registry** and **tool registry** in the control plane, consulted by the AS and gateways.
2. **Token exchange** at the AS with agent-aware policy (`sub` = user, `act` = agent, per-agent consent, ceilings).
3. **MCP authorization server** support: RFC 9728 metadata for your MCP servers, CIMD support for MCP clients, RFC 9207
   `iss`, audience-bound tokens.
4. **Token vault** for third-party tokens, with a broker in the tool runtime.
5. **Approval service** (in-app + CIBA/push) with approvals bound to action hashes.
6. **AI gateway and MCP gateway** as PEPs, with per-agent budgets and audit.
7. **Kill switches** in registry and AS, exercised in game days.

---

## 12. CI/CD guardrails for identity code

| Check | Tooling |
|---|---|
| Lint/format/type-check | ruff, mypy/pyright (or language equivalents) |
| Unit tests for validators (JWT, PKCE, SAML, DPoP) with negative cases | pytest; RFC test vectors |
| Authorization matrix and cross-tenant tests | Generated test suites (ch 11 §11) |
| Policy tests | `opa test`, Cedar validation, `fga model test` |
| Secret scanning (pre-commit + CI + push protection) | gitleaks/TruffleHog, GitHub push protection |
| SAST rules for auth anti-patterns (`verify_signature: False`, `algorithms` from header, tokens in logs, routes without authz helper) | Semgrep custom rules |
| Dependency scanning (SAML/JWT libraries especially) | Dependabot/Renovate, OSV-Scanner |
| Container image scanning, signed images | Trivy/Grype, cosign |
| OIDC/OAuth conformance (if you run an OP) | OpenID Foundation conformance suite |
| Config validation (issuers, redirect URIs, key IDs) before deploy | Schema checks, canary |

---

## 13. Interview walk-through: auth for 100M users

**Prompt:** "Design authentication and authorization for a consumer + SMB product with 100M users, web and mobile apps,
a public API, and AI agents that act for users."

1. **Clarify**: consumer vs business accounts, SSO needs, regulatory regions, API consumers (first-party only vs third-party
   developers), agent features, availability targets.
2. **Identity**: managed IdP or Keycloak at Growth; passkeys first, password + TOTP fallback, social login; business tenants
   with Google/Microsoft SSO and SAML/SCIM on demand via broker; custom domains for issuer and passkey RP ID.
3. **Clients**: web via BFF (cookies), mobile via code + PKCE in system browser with Keychain/Keystore storage and DPoP,
   third-party developers via OAuth code flow/client credentials with PAR and `private_key_jwt`, CLI via loopback/device
   flow.
4. **Tokens**: JWT access tokens (10 min, `aud` per API, `typ at+jwt`), rotating refresh tokens, JWKS on CDN, keys in KMS
   loaded to memory, rotation automated.
5. **Authorization**: RBAC within tenant + ReBAC for sharing (OpenFGA), RLS backstop, BOLA tests in CI.
6. **Services**: mesh mTLS with SPIFFE identities; token exchange for user context to sensitive services.
7. **Agents**: agent registry, per-agent consent, token exchange with `act`, MCP gateway + token vault, approvals bound to
   actions, kill switches.
8. **Scale numbers** (§4): password hashing herd, refresh volume, local verification.
9. **Availability** (§5): multi-region `/token`, cached JWKS, sessions survive IdP outage.
10. **Security ops**: ATO layers (§7), deprovisioning cascade (§8), audit/SIEM, incident runbooks.
11. **Phases**: what ships in MVP vs Growth vs Scale (§1), and what you deliberately defer.

---

## Sources

- NIST SP 800-63-4 Digital Identity Guidelines: https://pages.nist.gov/800-63-4/
- RFC 9700, OAuth 2.0 Security BCP: https://datatracker.ietf.org/doc/html/rfc9700
- OWASP Application Security Verification Standard (V2 Authentication, V3 Session, V4 Access Control): https://owasp.org/www-project-application-security-verification-standard/
- Google SRE book, "Managing Critical State" and dependency chapters: https://sre.google/sre-book/table-of-contents/
- OpenID Foundation certification and conformance testing: https://openid.net/certification/
- OpenID Shared Signals Framework: https://openid.net/wg/sharedsignals/
- GDPR (Regulation (EU) 2016/679): https://eur-lex.europa.eu/eli/reg/2016/679/oj
