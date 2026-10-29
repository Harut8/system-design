# 17 — Identity products mapped to protocols: Okta, Auth0, Entra ID, Keycloak, Cognito and friends

> **Prerequisites:** chapters [`03`](03-jwt-jose-and-signing-keys.md)–[`07`](07-oauth-hardening-and-advanced-specs.md)
> (the protocols), [`10`](10-enterprise-sso-saml-scim-and-federation.md) (SSO, SCIM), [`12`](12-workload-and-service-identity.md)
> (SPIFFE, workload identity).
>
> **Feeds into:** [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md) (build versus buy).
>
> **THESIS:** "Okta flows" are not a different thing from OAuth. Every mainstream identity product implements the same
> standards (OAuth 2.x, OIDC, JWT/JWK, SAML, SCIM, and increasingly DPoP, PAR, token exchange, Shared Signals) and then
> wraps them in its own vocabulary, policy engines, SDKs and a few proprietary extensions. Learn to translate product terms
> back into protocol terms and you can (a) debug any of them with the same mental model, (b) spot the handful of
> product-specific traps (wrong authorization server, missing `audience`, tokens you aren't supposed to validate), and
> (c) keep your architecture portable by depending on the standard surface, not the proprietary one.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The translation table](#1-the-translation-table)
2. [Okta (Workforce Identity)](#2-okta-workforce-identity)
3. [Auth0 (Okta Customer Identity)](#3-auth0-okta-customer-identity)
4. [Microsoft Entra ID](#4-microsoft-entra-id)
5. [Keycloak](#5-keycloak)
6. [Amazon Cognito](#6-amazon-cognito)
7. [Google, Ping and the rest of the field](#7-google-ping-and-the-rest-of-the-field)
8. [Workload and infrastructure identity products](#8-workload-and-infrastructure-identity-products)
9. [Where each product publishes metadata and keys](#9-where-each-product-publishes-metadata-and-keys)
10. [Agent-identity features in products](#10-agent-identity-features-in-products)
11. [Portability: staying on the standard surface](#11-portability-staying-on-the-standard-surface)
12. [Product-specific pitfalls](#12-product-specific-pitfalls)
13. [Interview questions](#13-interview-questions)
14. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Okta, Auth0, Microsoft Entra ID, Keycloak and Amazon Cognito are all **identity providers** (ch 00 §2.1). Under the hood
they speak the same languages: OIDC for login, OAuth for API access, JWTs as tokens, JWKS for public keys, SAML for older
enterprise apps, SCIM for user provisioning.

What differs is the vocabulary ("application", "client", "app registration" all mean OAuth client), the admin UI, the
policy features (who must use MFA, from which devices), and a few product-only features. When documentation says "use the
Okta authorization code flow", read: OAuth 2.0 authorization code grant with PKCE, against an Okta authorization server.

---

## 1. The translation table

| Protocol concept | Okta | Auth0 | Entra ID | Keycloak | Cognito |
|---|---|---|---|---|---|
| Identity provider / OP | Okta org | Tenant | Tenant | Realm | User pool |
| Authorization server (issuer) | Org AS or **custom AS** (`/oauth2/{id}`) | Tenant (custom domain) | Tenant v2.0 endpoint | Realm | User pool |
| OAuth client | App integration (OIDC app, API service app) | Application | App registration (+ service principal / enterprise app) | Client | App client |
| Resource server / API | Custom AS audience + scopes | **API** (identifier = `audience`) | App registration exposing an API (App ID URI) | Client (bearer-only) / audience | Resource server |
| Scopes | Custom scopes | API permissions | Delegated permissions (`scp`) | Client scopes | Custom scopes |
| App roles in token | Groups/claims rules | RBAC permissions (`permissions` claim) | App roles (`roles`) | Realm/client roles | Groups (`cognito:groups`) |
| Claim customization | Claims on custom AS, token inline hooks | Actions | Claims mapping, optional claims | Protocol mappers | Pre-token-generation Lambda |
| Login policy / MFA | Global session + authentication policies | Rules/Actions, MFA settings | Conditional Access | Authentication flows | MFA settings, adaptive auth |
| Upstream federation | IdP routing rules | Connections (social, enterprise) | External identities / B2B | Identity brokering | Identity providers |
| B2B multi-tenancy | Orgs (Okta CIC) / multiple IdPs | **Organizations** | Multi-tenant apps, cross-tenant access | **Organizations** feature | One pool per tenant or custom attributes |
| Provisioning | SCIM (Okta as SCIM client), Lifecycle Management | SCIM inbound for enterprise connections | SCIM provisioning service | SCIM via extensions | — |
| Machine-to-machine | API service app, client credentials | M2M application | Client credentials, managed identities | Service accounts (client credentials) | App client with client credentials |
| Token exchange | Supported (incl. ID-JAG / Cross App Access) | Custom token exchange; Token Vault | On-Behalf-Of flow (proprietary profile) | Standard token exchange (RFC 8693) | — |
| DPoP | Supported | Supported | (Proof-of-possession via its own mechanisms) | Supported | — |
| Continuous access / signals | Identity Threat Protection, Shared Signals | — | Continuous Access Evaluation | — | — |

Treat the table as orientation; features move quickly, so check current docs before relying on a cell.

---

## 2. Okta (Workforce Identity)

### 2.1 Two kinds of authorization server: the trap everyone hits once

| | **Org authorization server** | **Custom authorization server** |
|---|---|---|
| Issuer | `https://{yourOktaDomain}` | `https://{yourOktaDomain}/oauth2/{authServerId}` (built-in one: `/oauth2/default`) |
| Purpose | SSO into Okta and tokens for **Okta's own APIs** | Tokens for **your** APIs |
| Access tokens | Meant for Okta; **your APIs shouldn't validate them** (format/keys are Okta-internal) | JWTs with your `aud`, custom scopes and claims, validated with this AS's JWKS |
| Custom scopes/claims, access policies | No | Yes |

So: the ID token from the org AS is fine for logging into your app; for calling **your** API, request tokens from a custom
AS (`/oauth2/default` or one you create, with `aud` = your API). A frequent bug is validating org-AS access tokens in an API
and then "fixing" the resulting failures by disabling checks.

### 2.2 Endpoints (custom AS)

```
/oauth2/{id}/.well-known/openid-configuration      and  /.well-known/oauth-authorization-server
/oauth2/{id}/v1/authorize    /v1/token    /v1/keys (JWKS)    /v1/userinfo
/oauth2/{id}/v1/introspect   /v1/revoke   /v1/logout         /v1/device/authorize
```

### 2.3 What Okta's "flows" are in protocol terms

| Okta guide name | Protocol |
|---|---|
| "Authorization Code flow with PKCE" (SPA, native) | RFC 6749 code grant + RFC 7636 |
| "Authorization Code flow" (web app) | Code grant, confidential client (secret or `private_key_jwt`) |
| "Client Credentials flow" (API service app) | `client_credentials`; Okta recommends `private_key_jwt` |
| "Device Authorization Grant" | RFC 8628 |
| "Token exchange" / "Cross App Access" | RFC 8693; ID-JAG (ch 10 §7) |
| "Refresh token rotation" | Rotation with reuse detection (ch 04 §9), with a configurable grace period |
| "DPoP" | RFC 9449 |
| "Interaction Code grant" (Identity Engine embedded sign-in) | **Okta-specific** grant for apps that render their own sign-in UI using Okta's Identity Engine; not portable |
| "Resource Owner Password" | Legacy password grant; avoid (ch 04 §7) |

### 2.4 Okta concepts that aren't OAuth

- **Okta Identity Engine (OIE)** vs Classic: the policy engine; authentication policies per app evaluate user, group,
  device and network, and select **authenticators**.
- **Okta Verify / FastPass**: Okta's authenticator app; FastPass is device-bound, phishing-resistant passwordless
  (platform key + device signals), equivalent in spirit to passkeys (ch 01 §5); push with **number challenge** against
  MFA fatigue.
- **Policies**: global session policy (Okta session), authentication policies (per app), **authorization server access
  policies** (which clients get which scopes/grants, token lifetimes).
- **Hooks**: *inline hooks* (synchronous: e.g. add claims at token issuance, like Auth0 Actions) and *event hooks*
  (asynchronous webhooks on System Log events).
- **System Log**: audit events; stream to your SIEM.
- **Lifecycle Management**: SCIM *client* that provisions users into apps (your SaaS implements the SCIM *server*, ch 10 §5).
- **Okta API authentication**: legacy **API tokens** (`Authorization: SSWS <token>`, long-lived, tied to an admin) versus
  **OAuth 2.0 for Okta APIs** (service app, `private_key_jwt`, scoped `okta.users.read` etc.). Prefer OAuth; SSWS tokens are
  exactly the static secrets of ch 12 §1.

### 2.5 A lesson from Okta's own incident

In October 2023, an attacker accessed Okta's customer-support case system and downloaded **HAR files** customers had
uploaded for troubleshooting. HAR files capture full HTTP traffic, including **session cookies and tokens**; some were then
used to hijack admin sessions at customers. Lessons that generalize (ch 08 §9): HAR files, debug logs and support uploads
are credential stores; sanitize them; bind admin sessions to devices and re-authenticate for admin actions; alert on admin
sessions from new ASNs.

---

## 3. Auth0 (Okta Customer Identity)

- **Tenant** = issuer (`https://{tenant}.{region}.auth0.com/` or your custom domain). Note the **trailing slash** in Auth0's
  issuer; exact string comparison (ch 03 §6) will fail if your config omits it.
- **APIs** have an **identifier** you pass as the **`audience`** parameter. **Gotcha:** without `audience`, Auth0 returns an
  **opaque access token usable only for `/userinfo`**; developers then try to validate it as a JWT in their API. Always send
  `audience` for API access (Auth0's pre-RFC 8707 equivalent of `resource`).
- **Applications**: SPA, regular web, native, M2M (client credentials, per-API grants).
- **Actions**: serverless hooks in the login/token pipeline (add claims, call risk services, deny logins).
- **Organizations**: B2B tenants-within-a-tenant with per-org connections (SAML/OIDC), invitations and roles (ch 10 §4).
- **Connections**: database (Auth0-hosted users), social, enterprise (SAML, OIDC, Entra, Google Workspace), passwordless.
- **Auth0 FGA**: managed OpenFGA (ReBAC, ch 11 §4).
- **Auth0 for AI Agents**: features aimed at agents, including a **Token Vault** (Auth0 stores users' third-party tokens and
  exchanges them for agents, the vault/broker pattern of ch 08 §10), asynchronous authorization (CIBA-style approvals, ch 07
  §9) and FGA for RAG document filtering.

---

## 4. Microsoft Entra ID

- **Endpoints**: `https://login.microsoftonline.com/{tenant}/v2.0` (prefer v2.0). `{tenant}` = tenant ID/domain for
  single-tenant apps; `common`, `organizations`, `consumers` for multi-tenant (ch 05 §8: resolve and pin `tid`).
- **App registration** (the global OAuth client definition) vs **enterprise application / service principal** (its instance
  in each tenant where it's consented). Consent and assignment live on the service principal.
- **Tokens for Microsoft Graph are not for you to validate**. They may be in formats or signed in ways your app shouldn't
  depend on; validate only tokens whose `aud` is **your** API (App ID URI or client ID).
- Claims: `scp` (delegated permissions, user present), `roles` (app roles, or application permissions for daemon apps),
  `oid` (stable user object ID), `tid`, `azp`/`appid`. Key users on `(tid, oid)`.
- **On-Behalf-Of (OBO)** flow: a middle-tier API exchanges the incoming user token for a downstream token (jwt-bearer grant
  with `requested_token_use=on_behalf_of`). Conceptually RFC 8693 delegation, but Entra's own profile, with no `act` claim.
- **Conditional Access**: the policy engine (MFA, device compliance, location, risk). **Continuous Access Evaluation (CAE)**:
  Microsoft's implementation of the continuous-access idea (ch 10 §6), letting resource providers reject tokens after
  critical events before expiry; clients must handle **claims challenges** (a 401 asking for a new token with specific
  claims).
- **Managed identities** (Azure workloads get tokens from IMDS, no secrets) and **workload identity federation** (trust
  GitHub/Kubernetes OIDC tokens, ch 12 §5).
- **Entra Agent ID** (announced 2025): Microsoft's model for registering AI agents as identities in Entra, with
  governance and Conditional Access applying to them; the product counterpart of ch 14's agent registry. Check current
  availability.

---

## 5. Keycloak

Open-source (CNCF incubating) IdP; the usual self-hosted choice.

- **Realm** = issuer `https://host/realms/{realm}`; discovery at `/realms/{realm}/.well-known/openid-configuration`; JWKS at
  `/realms/{realm}/protocol/openid-connect/certs`.
- **Clients** (confidential/public, service accounts for client credentials), **client scopes**, **protocol mappers**
  (claims), **roles** (realm and client), **groups**.
- **Identity brokering** (upstream OIDC/SAML/social) and **user federation** (LDAP/AD, Kerberos).
- **Organizations** (B2B: per-org members and IdPs) in recent releases.
- **Standard token exchange (RFC 8693)**: supported in the 26.x line (the earlier "legacy" token exchange was a preview
  feature with non-standard behaviour); also DPoP, PAR, FAPI 2.0 client policies, CIBA, passkeys.
- **Authorization Services**: resource/policy/permission model with UMA 2.0; powerful but complex; many teams use
  OPA/OpenFGA instead (ch 11).
- Operating it: you own HA (Infinispan caches, database), upgrades, key rotation, backups, and its CVE stream. That's the
  price of control and cost.

---

## 6. Amazon Cognito

- **User pools**: an OIDC IdP (hosted UI / managed login, social and SAML/OIDC federation, MFA, Lambda triggers).
  Issuer `https://cognito-idp.{region}.amazonaws.com/{userPoolId}`.
- **Identity pools**: a separate service that exchanges identities (user pool tokens, social, SAML) for **temporary AWS
  credentials** (STS); this is how mobile apps call AWS services directly.
- **Gotcha: access tokens have no `aud`.** Cognito access tokens carry `client_id` and `token_use: "access"`; ID tokens
  carry `aud` and `token_use: "id"`. A generic JWT validator that requires `aud` will reject access tokens, and one that
  skips audience checks accepts ID tokens as access tokens. Check `token_use` and `client_id` explicitly (use AWS's
  `aws-jwt-verify` library or equivalent).
- Customization via Lambda triggers (pre-token generation to add claims/scopes, pre-sign-up, custom auth challenges).
- Multi-tenancy: pool-per-tenant (strong isolation, quotas on number of pools) or shared pool with tenant attributes and
  per-tenant app clients/IdPs.

---

## 7. Google, Ping and the rest of the field

| Product | Positioning | Notes |
|---|---|---|
| Google Identity (Sign in with Google) | Consumer social login, Workspace SSO | OIDC; issuer `https://accounts.google.com`; check `hd` claim for Workspace domain, key on `sub` |
| Google Cloud Identity Platform | CIAM on GCP (Firebase Auth upgrade) | Multi-tenancy, SAML/OIDC |
| Ping Identity (incl. ForgeRock) | Enterprise workforce/CIAM, strong in finance | FAPI, PingFederate (SAML/OIDC broker), PingAM |
| WorkOS | B2B "enterprise readiness" API | SSO (SAML/OIDC) + Directory Sync (SCIM) normalized behind one API |
| Zitadel | Open-source and cloud, multi-tenant native | Organizations, actions, OIDC/SAML |
| Ory (Hydra, Kratos, Keto, Oathkeeper) | Open-source, modular | Hydra = OAuth2/OIDC server; Kratos = identity/login; Keto = Zanzibar-style authorization |
| Authentik, Authelia, Dex | Self-hosted | Dex is a federating OIDC broker often used for Kubernetes |
| Stytch, Descope, Clerk, FusionAuth, SuperTokens | Developer-focused CIAM | Passkeys, B2B orgs, agent-auth features vary |
| JumpCloud, OneLogin, Duo | Workforce/SMB, MFA | SAML/OIDC/RADIUS/LDAP |

---

## 8. Workload and infrastructure identity products

| Product | Standard it implements | Role |
|---|---|---|
| **SPIRE** | SPIFFE (X.509-SVID, JWT-SVID, Workload API, federation) | Workload identity issuer (ch 12 §4) |
| Istio / Linkerd / Cilium | SPIFFE-format identities, mTLS | Mesh identity and authorization policies |
| Kubernetes | OIDC discovery for service account issuer, projected tokens | Workload identity source for clouds/Vault |
| AWS IAM / STS, GCP IAM, Azure managed identities | OIDC federation, STS token exchange | Cloud workload identity and federation |
| HashiCorp Vault / OpenBao | JWT/OIDC/Kubernetes auth methods, dynamic secrets, Transit (KMS-like) | Secrets broker, envelope encryption (ch 08 §5) |
| Teleport, Boundary | Short-lived certificates (SSH/X.509), OIDC/SAML SSO | Human-to-infrastructure access |
| cert-manager | ACME, private CAs | Certificate lifecycle for mTLS |

---

## 9. Where each product publishes metadata and keys

| Product | Discovery | JWKS |
|---|---|---|
| Okta custom AS | `https://{domain}/oauth2/{id}/.well-known/openid-configuration` | `https://{domain}/oauth2/{id}/v1/keys` |
| Okta org AS | `https://{domain}/.well-known/openid-configuration` | `https://{domain}/oauth2/v1/keys` |
| Auth0 | `https://{tenant}/.well-known/openid-configuration` | `https://{tenant}/.well-known/jwks.json` |
| Entra ID | `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration` | `https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys` |
| Keycloak | `https://{host}/realms/{realm}/.well-known/openid-configuration` | `https://{host}/realms/{realm}/protocol/openid-connect/certs` |
| Cognito | `https://cognito-idp.{region}.amazonaws.com/{poolId}/.well-known/openid-configuration` | `…/{poolId}/.well-known/jwks.json` |
| Google | `https://accounts.google.com/.well-known/openid-configuration` | `https://www.googleapis.com/oauth2/v3/certs` |
| GitHub Actions OIDC | `https://token.actions.githubusercontent.com/.well-known/openid-configuration` | `…/.well-known/jwks` |
| Kubernetes SA issuer | `{issuer}/.well-known/openid-configuration` | `{issuer}/openid/v1/jwks` |

The table is for recognition; production code should read `jwks_uri` from discovery (ch 06), because these paths are the
product's choice and can change.

---

## 10. Agent-identity features in products

Vendors moved fast in 2025–2026 to cover ch 13–15 patterns. Map features back to the patterns before buying:

| Pattern (chapter) | Product features that implement it |
|---|---|
| Agent as a registered principal (ch 14 §6) | Entra Agent ID; Okta's agent identity/governance offerings; agent registries in AI platforms |
| Token vault / credential broker (ch 08 §10) | Auth0 Token Vault; Composio/Arcade-style tool auth platforms; MCP gateways with vaults |
| Async human approval (ch 15 §5) | CIBA support in Auth0, Okta, Keycloak, Ping |
| Fine-grained authorization for RAG/agents (ch 11) | Auth0 FGA/OpenFGA, SpiceDB, Cedar (Amazon Verified Permissions, Bedrock AgentCore policies) |
| Enterprise-managed MCP access (ch 13 §7) | Okta Cross App Access (ID-JAG) |
| MCP authorization server (ch 13 §3) | Most IdPs now document "use us as the AS for your MCP server" (PRM + CIMD/DCR support varies: check `client_id_metadata_document_supported`) |
| Workload identity for agent runtimes (ch 12) | SPIRE, cloud managed identities, Kubernetes projected tokens |

Questions to ask any vendor: Does the token carry the agent distinctly from the user (`act` or equivalent)? Can I set a
per-agent ceiling independent of the user? Is consent per agent? Are tokens audience-bound per tool server and
sender-constrained? Can I suspend an agent everywhere in seconds? Is CIMD supported for MCP clients?

---

## 11. Portability: staying on the standard surface

Lock-in in identity is expensive because **users and their credentials** are hard to move. Reduce it:

1. **Apps integrate via standard OIDC/OAuth** with discovery, not vendor-only SDK flows (Interaction Code, proprietary
   embedded APIs) unless you accept the lock-in knowingly.
2. **Your own user ID** in your database, mapped to `(iss, sub)` identities (ch 08 §8). Never use the vendor's user ID as
   your primary key everywhere.
3. **Authorization in your system** (or a portable engine), not encoded in vendor-specific claim rules nobody can export.
4. **Password hash export**: confirm you can export hashes in a standard format (bcrypt/Argon2 with parameters) or plan
   lazy migration (users log in through the old IdP once, you capture and rehash). Some vendors don't export hashes at all.
5. **Passkeys are bound to the RP ID (your domain)**, not to the vendor, *if* the vendor uses your domain as RP ID (custom
   domain). Passkeys registered against a vendor's domain don't move. Use a custom domain from day one.
6. **Custom domain for the issuer** (`login.yourcompany.com`): issuer URL changes break every client, every stored
   `(iss, sub)` mapping and every SAML trust; a custom domain lets you switch vendors behind it.

---

## 12. Product-specific pitfalls

| Product | Pitfall | Fix |
|---|---|---|
| Okta | Validating **org AS** access tokens in your API | Use a custom AS with your `aud` |
| Okta | SSWS API tokens for automation | OAuth service apps with `private_key_jwt` and scoped Okta API scopes |
| Okta | HAR files / support uploads with live sessions | Sanitize; device-bound admin sessions |
| Auth0 | No `audience` → opaque token | Always pass `audience` for APIs |
| Auth0 | Issuer trailing slash mismatch | Copy issuer from discovery exactly |
| Entra | Accepting tokens from `common` without pinning `tid` | Tenant allowlist + issuer per `tid` |
| Entra | Validating Graph tokens | Validate only tokens for your API |
| Entra | Keying users on `email`/`preferred_username` | `(tid, oid)` |
| Entra | Ignoring CAE claims challenges | Handle 401 with `claims` → re-acquire token |
| Keycloak | Using legacy (preview) token exchange semantics | Standard token exchange in current versions |
| Keycloak | Default realm settings in prod (long tokens, no brute-force detection) | Harden realm: lifetimes, brute force, PKCE required, refresh rotation |
| Cognito | Requiring `aud` on access tokens / not checking `token_use` | Check `token_use` + `client_id` |
| All | Vendor-domain passkeys and issuers | Custom domains from day one |
| All | Admin console without phishing-resistant MFA | Hardware keys / passkeys for admins |

---

## 13. Interview questions

**Q1. A developer says "we use the Okta flow, not OAuth". What do you tell them?**
Okta's flows are OAuth 2.0/OIDC grants (code + PKCE, client credentials, device, token exchange) against an Okta
authorization server, with a few Okta-specific extensions such as the Interaction Code grant. Debug them with standard
tools: discovery documents, JWKS, token claims.

**Q2. Your API rejects Okta access tokens with "signature invalid" but ID tokens validate. Why?**
Most likely the tokens come from Okta's org authorization server, which issues access tokens for Okta's own APIs. Your
API should accept tokens from a custom authorization server whose audience is your API, validated with that server's JWKS.

**Q3. How do you keep the option to leave your identity vendor?**
Custom issuer and passkey domains; standard OIDC integrations; own user IDs mapped to `(iss, sub)`; authorization in your
own system; verify password-hash export or plan lazy migration; avoid proprietary embedded-login APIs where possible.

**Q4. What's different about Cognito access tokens?**
They have no `aud` claim; they carry `client_id` and `token_use: access`. Validators must check those fields rather than
`aud`, and must reject ID tokens presented as access tokens.

---

## Sources

- Okta Developer, authorization servers (org vs custom): https://developer.okta.com/docs/concepts/auth-servers/
- Okta Developer, OAuth 2.0 and OIDC overview: https://developer.okta.com/docs/concepts/oauth-openid/
- Okta, "Unauthorized access to Okta's support case management system: root cause and remediation" (2023): https://sec.okta.com/harfiles
- Auth0 docs, Get access tokens (audience) and Organizations: https://auth0.com/docs/secure/tokens/access-tokens/get-access-tokens
- Auth0 for AI Agents: https://auth0.com/ai
- Microsoft identity platform, access tokens and token validation: https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens
- Microsoft, On-Behalf-Of flow: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow
- Microsoft, Continuous Access Evaluation: https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation
- Keycloak documentation (server admin, token exchange): https://www.keycloak.org/documentation
- Amazon Cognito, verifying JSON web tokens: https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html
- Google Identity, OpenID Connect: https://developers.google.com/identity/openid-connect/openid-connect
- SPIFFE/SPIRE: https://spiffe.io/ ; HashiCorp Vault auth methods: https://developer.hashicorp.com/vault/docs/auth
