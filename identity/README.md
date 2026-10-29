# identity — Authentication, Authorization and Identity for Humans, Services and AI Agents

Everything between "who is this?" and "may they do that?": passwords and passkeys, sessions and cookies, JWT/JOSE, OAuth
2.x and PKCE, OpenID Connect, `.well-known` discovery, hardening specs (PAR, DPoP, RFC 9207, RAR, token exchange, FAPI),
how to store credentials and tokens safely, enterprise SSO (SAML, SCIM, Shared Signals), authorization models (RBAC,
ABAC, ReBAC/Zanzibar, policy engines), workload and service-to-service identity (mTLS, SPIFFE, CI federation), and identity
for AI agents (MCP authorization, A2A, agent registries, MCP/AI gateways, human-in-the-loop, tokens expiring mid-run).

> **How this track relates to the rest of the repo.** Agent delegation RFC details and capability tokens are covered in
> depth in [`ai-rag/29-agent-identity-and-delegated-authorization.md`](../ai-rag/29-agent-identity-and-delegated-authorization.md);
> MCP and A2A as protocols in [`ai-rag/26`](../ai-rag/26-mcp-and-agent-protocols.md) and [`ai-rag/27`](../ai-rag/27-a2a-and-agent-interoperability.md);
> Kubernetes authN/authZ internals in [`kubernetes/07`](../kubernetes/07-authentication-authorization.md); secrets in
> [`kubernetes/44`](../kubernetes/44-secrets-and-configmaps-deep-dive.md). This track is the general foundation those
> chapters assume, and links to them instead of repeating them.

---

## 1. The one-paragraph version

Every protocol here is a variation of one pattern: **a party the verifier already trusts vouches for a claim, in a form
the verifier can check, for a limited time and a limited audience** (ch 00 §3). The bugs are almost always a confusion
between two things that look alike on the wire: authentication vs authorization, ID token vs access token, user vs the
client or agent acting for them, issuer vs audience, expiry vs revocation. The engineering is mostly about **where state
lives** (sessions, refresh tokens, vaults), **who can read it** (ch 08–09), and **how fast you can take access away**
(revocation, deprovisioning, kill switches).

---

## 2. Reading order

| If you want to… | Read |
|---|---|
| Understand login end to end | 00 → 01 → 02 → 04 → 05 |
| Understand OAuth properly (why PKCE, why codes, why `state`) | 04 → 06 → 07 |
| Secure an API | 03 → 04 §13 → 07 → 11 |
| Store passwords, tokens, API keys, third-party tokens correctly | 01 §2 → 08 → 09 |
| Sell to enterprises (SSO, SCIM, offboarding) | 05 → 10 → 11 §7 → 18 §8 |
| Secure service-to-service traffic and CI | 12 → 07 §5/§7 |
| Build or secure AI agents, MCP servers and A2A | 13 → 14 → 15 (then ai-rag 26–29) |
| Choose or debug a vendor (Okta, Auth0, Entra, Keycloak, Cognito) | 17 → 06 → 03 |
| Design an identity platform / interview prep | 00 → 18, then the chapters it links |

---

## 3. Chapters

| Doc | Topic |
|---|---|
| [`00-mental-models.md`](00-mental-models.md) | AuthN vs AuthZ, vocabulary, **what an IdP is and why it exists**, the trusted-issuer pattern, confusable pairs, principals, stateful vs stateless, confused deputy, lifecycle, protocol map |
| [`01-passwords-mfa-and-passkeys.md`](01-passwords-mfa-and-passkeys.md) | NIST 800-63B-4, password rules and hashing, TOTP, SMS/push weaknesses, AitM phishing, WebAuthn/passkeys, magic links, social login traps, recovery, ATO defenses |
| [`02-sessions-cookies-and-browser-security.md`](02-sessions-cookies-and-browser-security.md) | Cookie attributes and prefixes, server sessions, CSRF, XSS vs token storage, BFF vs browser-only SPAs, CORS, session theft and DBSC, timeouts |
| [`03-jwt-jose-and-signing-keys.md`](03-jwt-jose-and-signing-keys.md) | JWS/JWE/JWK/JWA/JWT, claims, algorithms, `alg:none`/confusion/`kid`/`jku` attacks, verification checklist, **JWKS and key rotation**, JWT vs opaque |
| [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) | Roles and channels, **deriving the code flow attack by attack**, **PKCE in depth and why**, grants, scopes, refresh rotation, redirect URIs, native/CLI, OAuth 2.1 |
| [`05-openid-connect.md`](05-openid-connect.md) | Why OIDC exists, ID token claims and validation, ID vs access token, logout (RP-initiated, front/back-channel), multi-tenant issuer validation, account linking |
| [`06-well-known-discovery-and-metadata.md`](06-well-known-discovery-and-metadata.md) | **What `.well-known` is and why it's necessary**, RFC 8414, RFC 9728, catalogue of identity well-knowns, path insertion, discovery chain, SSRF and trust |
| [`07-oauth-hardening-and-advanced-specs.md`](07-oauth-hardening-and-advanced-specs.md) | RFC 9700, PAR/JAR/JARM, mix-up and RFC 9207, resource indicators, DPoP and mTLS, RAR, token exchange, introspection/revocation/step-up, CIBA, FAPI 2.0 |
| [`08-storing-credentials-and-tokens-at-rest.md`](08-storing-credentials-and-tokens-at-rest.md) | **Hash vs encrypt per secret type**, Argon2id, SHA-256 for high-entropy tokens, envelope encryption with AAD, API keys, schemas, leak paths, agent/MCP token vaults, best/worst practices |
| [`09-where-clients-keep-tokens.md`](09-where-clients-keep-tokens.md) | **Token storage per client**: server apps, BFF, SPA, iOS/Android, desktop/CLI, local MCP configs, workloads, four shapes of AI agents, edge cases |
| [`10-enterprise-sso-saml-scim-and-federation.md`](10-enterprise-sso-saml-scim-and-federation.md) | SAML and XML signature wrapping, SAML vs OIDC, B2B home realm discovery and tenant trust, SCIM deprovisioning cascade, Shared Signals/CAEP/RISC, ID-JAG, OpenID Federation, Kerberos/AD |
| [`11-authorization-models-and-policy-engines.md`](11-authorization-models-and-policy-engines.md) | PEP/PDP, RBAC, ABAC, ReBAC/Zanzibar, OPA/Cedar/OpenFGA/SpiceDB, AuthZEN, multi-tenancy and RLS, BOLA/BFLA, testing authorization |
| [`12-workload-and-service-identity.md`](12-workload-and-service-identity.md) | **Service-to-service trust models**, mTLS, SPIFFE/SPIRE, meshes, CI/cloud workload identity federation, Kubernetes SA tokens, user-context propagation, webhooks, zero trust |
| [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md) | **In-process tools vs local stdio MCP vs remote HTTP MCP**, MCP 2026-07-28 authorization end to end, CIMD vs DCR, no passthrough, scope step-up, enterprise-managed auth, A2A security |
| [`14-agent-identity-delegation-registries-and-gateways.md`](14-agent-identity-delegation-registries-and-gateways.md) | **User × agent identity** (same user, different agents, different access), delegated tokens, sub-agents and attenuation, A2A trust, **agent/tool/MCP registries**, **AI gateways, MCP gateways**, kill switches |
| [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md) | Per-call tool authorization, just-in-time tokens, RAR-bounded actions, **human-in-the-loop approvals bound to actions**, **token expiry/revocation mid-loop**, idempotent retries, 20 edge cases, data-flow rules |
| [`16-decentralized-identity-and-verifiable-credentials.md`](16-decentralized-identity-and-verifiable-credentials.md) | Issuer–holder–verifier, SD-JWT (RFC 9901), mdoc, OpenID4VCI/VP, Digital Credentials API, trust and status lists, DIDs, EUDI wallet |
| [`17-identity-products-mapped-to-protocols.md`](17-identity-products-mapped-to-protocols.md) | **Okta, Auth0, Entra ID, Keycloak, Cognito**, Google and others translated into protocol terms; their "flows", metadata/JWKS locations, agent features, portability, pitfalls |
| [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md) | MVP → Growth → Scale, build vs buy, reference architecture, capacity math, availability, key ops, ATO defense, deprovisioning/GDPR, SLOs, agent-readiness, CI guardrails, interview walk-through |
| [`LABS.md`](LABS.md) | Hands-on tasks per chapter on a laptop (Keycloak + Python), with predictions, measurements and checkpoint questions |

Every chapter has: a plain-words start, real-world examples, code that was run while writing, anti-patterns, interview
questions and primary sources (RFCs, OpenID specs, the MCP specification, vendor docs, incident reports).

---

## 4. State of the standards (October 2026)

| Standard | Status |
|---|---|
| OAuth 2.0 (RFC 6749/6750), PKCE (RFC 7636), Security BCP (RFC 9700, Jan 2025) | Published |
| OAuth 2.1 | Internet-Draft (`draft-ietf-oauth-v2-1-16`, Sept 2026); IESG submission planned Dec 2026 |
| RFC 9728 Protected Resource Metadata (2025), RFC 9207, RFC 9449 DPoP, RFC 9396 RAR, RFC 8693 token exchange | Published |
| SD-JWT | RFC 9901 (Nov 2025) |
| OpenID Connect Core, Discovery, logout specs; FAPI 2.0 Security Profile (Feb 2025) | Final |
| Shared Signals Framework, CAEP, RISC 1.0 | Final (Sept 2025) |
| AuthZEN Authorization API 1.0 | Final (Jan 2026) |
| OpenID Federation 1.0 | Final (Feb 2026) |
| OpenID4VP 1.0, OpenID4VCI 1.0, HAIP 1.0 | Final (2025) |
| NIST SP 800-63-4 | Final (2025) |
| MCP specification | Revision 2026-07-28 (DCR deprecated in favour of CIMD; RFC 9207 `iss` validation required of clients) |
| MCP authorization extensions (Enterprise-Managed Authorization / ID-JAG, OAuth Client Credentials) | Stable extensions, versioned with 2026-07-28 |
| Client ID Metadata Document, ID-JAG, Transaction Tokens, Token Status List, SD-JWT VC | IETF Internet-Drafts |

Drafts change. Re-check datatracker.ietf.org and openid.net before relying on a draft's wire format.
