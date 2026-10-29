# 00 — Identity mental models

> **Prerequisites:** none. This is the entry point of the identity track.
>
> **Feeds into:** every other chapter here. The vocabulary in §2 (principal, credential, assertion, token,
> session, grant) is used everywhere without being redefined.
>
> **THESIS:** almost every identity bug is a mix-up between two things that look the same on the wire.
> Authentication versus authorization. An ID token versus an access token. The user versus the client acting
> for the user. The session at the identity provider versus the session at the app. A token's issuer versus its
> audience. If you can name which of the pair a given byte string is, and who is allowed to trust it, most
> protocols in this track become variations on one pattern: **someone the verifier already trusts vouches for a
> claim, in a form the verifier can check, for a limited time and a limited audience.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Identity, authentication, authorization, accounting](#1-identity-authentication-authorization-accounting)
2. [The vocabulary](#2-the-vocabulary)
3. [The one pattern: trusted issuer, checkable claim, bounded scope](#3-the-one-pattern-trusted-issuer-checkable-claim-bounded-scope)
4. [The confusable pairs](#4-the-confusable-pairs)
5. [Kinds of principals: humans, workloads, devices, agents](#5-kinds-of-principals-humans-workloads-devices-agents)
6. [Where state lives: stateful sessions versus self-contained tokens](#6-where-state-lives-stateful-sessions-versus-self-contained-tokens)
7. [Trust boundaries and the confused deputy](#7-trust-boundaries-and-the-confused-deputy)
8. [The identity lifecycle](#8-the-identity-lifecycle)
9. [The protocol map](#9-the-protocol-map)
10. [Decision cheat sheet](#10-decision-cheat-sheet)
11. [Interview questions](#11-interview-questions)
12. [Sources](#sources)

---

## Start here — the whole chapter in plain words

**Identity** answers "who is this?". **Authentication** (AuthN) is proving it. **Authorization** (AuthZ) is
deciding what that proven party may do. They are separate steps, done by separate code, often owned by separate
teams, and the common failure is letting one stand in for the other: "the request had a valid token, so it may
do this" skips authorization entirely.

A real login, end to end:

1. Alice opens `app.example.com`. The app has no session for her, so it redirects her to the company's
   **identity provider** (IdP), for example Okta, Entra ID, Keycloak or Google.
2. The IdP **authenticates** her: password plus passkey, or a passkey alone.
3. The IdP sends her browser back to the app with a one-time **authorization code**. The app swaps that code,
   server to server, for an **ID token** (a signed statement: "this is Alice, I checked at 09:02") and an
   **access token** (a key that lets the app call APIs for her).
4. The app checks the ID token's signature and audience, creates its own **session**, and sets a cookie.
5. When the app calls the invoices API, it sends the access token. The API checks the token was issued by the
   IdP *for the invoices API*, then **authorizes**: may Alice read invoice 4711 in tenant `acme`?

Every chapter in this track zooms into one of those five steps, or into a variant where the caller is a
service, a CI job, a device or an AI agent instead of Alice.

---

## 1. Identity, authentication, authorization, accounting

| Step | Question | Output | Typical owner |
|---|---|---|---|
| Identification | Who do you claim to be? | An identifier (`alice@acme.com`, `spiffe://prod/ns/billing/sa/api`) | Directory, HR system, registry |
| Authentication | Prove it | A verified identity plus *how* it was verified (method, strength, time) | IdP, auth service |
| Authorization | May you do X to Y? | Allow / deny (plus obligations: "allow, but log", "allow, mask PII") | Each service, or a central policy engine |
| Accounting / audit | What did you do? | An attributable, tamper-evident record | Logging, SIEM |

Two consequences engineers forget:

- **Authentication has a strength and an age.** "Alice logged in" is incomplete. "Alice logged in with a
  phishing-resistant passkey 4 minutes ago" lets you decide whether she may wire money without re-checking. OIDC
  carries this as `acr`, `amr` and `auth_time` (ch 05); OAuth step-up carries it as an error that asks for more
  (ch 07).
- **Authorization needs context authentication does not have.** The IdP knows Alice is in the `finance` group. It
  does not know that invoice 4711 belongs to tenant `acme` and is in "draft" state. Fine-grained authorization
  therefore almost always lives near the data, not in the IdP (ch 11).

---

## 2. The vocabulary

| Term | Meaning | Example |
|---|---|---|
| **Principal** | Any entity that can be authenticated and given permissions | A user, a service, a device, an agent |
| **Subject** | The principal a statement is about (`sub` claim) | `sub = 248289761001` |
| **Identifier** | A name for a principal, stable or not | Email (unstable), opaque user ID (stable) |
| **Credential** | Something presented to prove identity | Password, private key, client secret, certificate |
| **Authenticator** | The thing that holds or produces the credential | A passkey on a phone, a YubiKey, a TOTP app |
| **Assertion** | A signed statement from an issuer about a subject | SAML assertion, OIDC ID token, SPIFFE JWT-SVID |
| **Token** | A string presented to get access; may be self-contained (JWT) or a reference (opaque) | Access token, refresh token |
| **Bearer token** | A token anyone holding it can use | Most OAuth access tokens |
| **Sender-constrained token** | A token usable only with a proof of a private key | DPoP, mTLS-bound tokens |
| **Session** | Server- or cookie-held state saying "this browser already authenticated" | `session_id` cookie |
| **Grant** | A way of obtaining a token, and the permission it represents | Authorization code grant, client credentials |
| **Scope** | A coarse permission label on a token | `invoices.read` |
| **Audience** | Who a token is for (`aud`) | `https://api.example.com/invoices` |
| **Issuer** | Who made a token (`iss`) | `https://login.example.com` |
| **Relying party (RP) / service provider (SP)** | The app that relies on an IdP's assertion | Your web app |
| **Identity provider (IdP) / OpenID provider (OP)** | The system that authenticates and asserts | Okta, Entra ID, Keycloak, Auth0 |
| **Authorization server (AS)** | OAuth's token issuer | Often the same product as the IdP |
| **Resource server (RS)** | An API that accepts access tokens | Invoices API, an MCP server |
| **Client** | The application asking for tokens | SPA, mobile app, backend, CLI, AI agent host |
| **Federation** | Trusting identities asserted by another domain | Logging into Slack with Google |
| **Provisioning** | Creating, updating and removing accounts in downstream systems | SCIM |

### 2.1 What an identity provider is, and why it exists

> **In plain words.** An identity provider (IdP) is the one system whose job is to log people in and then tell
> other systems, in a signed message, who logged in. Apps stop handling passwords at all; they trust the IdP.
>
> **Real-world example.** "Sign in with Google" on a small SaaS app: the app never sees your Google password.
> Google (the IdP) checks it, then hands the app a signed ID token saying `sub=1084...`, `email=you@gmail.com`,
> `email_verified=true`. The app trusts that statement because it is signed by a key Google publishes.

**How we got here.** The design makes sense once you see the three stages it replaced:

| Stage | How login worked | Why it broke |
|---|---|---|
| 1. Every app has its own user table | Each app stores passwords, builds reset flows, MFA, lockout | N apps = N password databases to leak, N implementations of MFA, users reuse one password everywhere, leavers keep N accounts |
| 2. Shared directory (LDAP / Active Directory) | Apps send the username and password to the directory to check (LDAP bind) | Every app still *sees* the password; works only inside one network; no MFA in the protocol; no third-party apps |
| 3. Federated IdP (Kerberos inside a network; SAML, then OIDC on the web) | The user authenticates **only at the IdP**; apps receive a signed assertion | This is the model this track describes |

Stage 3 wins for reasons that are each worth naming, because they explain the shape of every protocol later:

1. **Passwords exist in one place.** Apps receive assertions, never credentials. A compromised app leaks
   sessions for that app, not passwords for everything. This is why OAuth and OIDC go through **browser
   redirects** to the IdP instead of having the app show a password form: the whole point is that the app never
   touches the secret.
2. **One place to make authentication strong.** Passkeys, MFA, risk scoring, breached-password checks and
   lockout are built once, at the IdP, and every app inherits them.
3. **One place to remove access.** Disable the user at the IdP and new logins to every app stop. (Existing app
   sessions do not stop by themselves; that gap is chapter 05 §7 and chapter 10.)
4. **Single sign-on.** The IdP keeps its *own* session. The second app you open redirects to the IdP, which
   sees its session cookie and answers immediately without asking for the password again.
5. **Trust is checkable offline.** The assertion is signed. An app verifies it with the IdP's public key
   (fetched from a well-known URL, chapter 06) without calling the IdP on every request.

What an IdP is **not**: it is not where fine-grained authorization lives (it knows Alice's groups, not which
invoices she may approve, see §1), and it is not automatically the OAuth *authorization server* for your APIs,
though most products (Okta, Entra ID, Auth0, Keycloak, Cognito, Zitadel, Ory) play both roles. When the IdP
speaks OIDC it is called an **OpenID Provider (OP)**; when it speaks SAML, an **IdP** paired with a **service
provider (SP)**; the app that trusts it is a **relying party (RP)**.

Common IdP topologies:

```
Consumer app               Workforce SaaS                     B2B SaaS (your product)
user ─▶ your IdP           employee ─▶ Okta/Entra ─▶ Slack    customer employee ─▶ customer's Okta
        (Auth0/Keycloak/                         ─▶ GitHub                         ─▶ your IdP (broker)
         Cognito/self-built)                     ─▶ AWS                               ─▶ your app
```

The third column is **identity brokering**: your IdP trusts many upstream IdPs (one per enterprise customer)
and issues uniform tokens to your app, so the app only ever integrates with one issuer (chapter 10 §4).

---

## 3. The one pattern: trusted issuer, checkable claim, bounded scope

Nearly every protocol in this track, from Kerberos to SAML to OIDC to SPIFFE to MCP, is this:

```
          (1) prove who I am            (2) signed claim, short-lived,
Principal ───────────────────▶ Issuer ──────── for one audience ───────▶ Principal
    │                                                                        │
    │                     (3) present claim                                  │
    └───────────────────────────────────────────────────────▶ Verifier ◀────┘
                                                              │
                         (4) verifier checks: issuer is one I trust
                             signature valid with issuer's key
                             audience is me
                             not expired, not replayed
                             (optionally) presenter holds the bound key
```

The checks in step 4 are the whole security of the system. Each has a famous failure:

| Check skipped | Classic failure |
|---|---|
| Trusted issuer | Accepting a token from any issuer whose JWKS you can fetch (multi-tenant IdPs, ch 05 §8) |
| Signature | `alg: none`, algorithm confusion, unverified decode (ch 03) |
| Audience | Token for API A replayed at API B; MCP token passthrough (ch 13) |
| Expiry / replay | Stolen long-lived tokens used for months (ch 07, real-world cases) |
| Key binding | Stolen bearer token works from any machine (ch 07, DPoP) |

What varies between protocols is only: the format of the claim (XML, JWT, X.509, CBOR), the way the principal
reaches the issuer (browser redirect, back channel, local socket), and how the verifier gets the issuer's key
(metadata URL, certificate chain, pre-shared bundle).

---

## 4. The confusable pairs

| Looks like | Actually | Why it matters |
|---|---|---|
| Authentication vs authorization | Proving identity vs deciding permission | "Has a valid token" is not "may do this" |
| **ID token vs access token** | ID token tells the *client* who logged in; access token lets the client call an *API* | Never send ID tokens to APIs; never use an access token to log someone into the client (ch 05 §4) |
| User vs client | The human vs the software acting for them | Scopes limit the client; the user's own permissions still apply. Effective permission = intersection |
| IdP session vs app session | Cookie at `login.example.com` vs cookie at `app.example.com` | Logging out of one does not log out of the other (ch 05 §7) |
| Issuer vs audience | Who signed it vs who should accept it | A token is safe only at its audience |
| Delegation vs impersonation | "Agent acting for Alice" vs "agent pretending to be Alice" | Audit and policy need both identities (ch 13) |
| Authentication strength vs freshness | *How* vs *when* | Sensitive actions need both |
| Encryption vs signing | Hides content vs proves origin | Most JWTs are signed, not encrypted: anyone can read them |
| Revocation vs expiry | Stop now vs stop eventually | Self-contained tokens are only stopped by expiry unless you add state |
| Account vs identity | A record in one system vs the person behind it | One person, many accounts; account linking is a security decision |

---

## 5. Kinds of principals: humans, workloads, devices, agents

| Principal | Typical credential | Typical protocol | Chapter |
|---|---|---|---|
| Human, consumer | Password + MFA, passkey, social login | OIDC, WebAuthn | 01, 05 |
| Human, workforce | SSO with corporate IdP, device-bound passkey | SAML, OIDC, SCIM | 10 |
| Service / workload | Platform-attested short-lived certificate or token | mTLS, SPIFFE, cloud workload identity | 12 |
| CI/CD job | OIDC token from the CI platform | Workload identity federation | 12 |
| Device | Device certificate, TPM-backed key | mTLS, device posture signals | 12 |
| Third-party app | OAuth client registration | OAuth 2.x | 04, 07 |
| AI agent | Its own client identity plus delegated user tokens | OAuth token exchange, MCP authorization, A2A | 13–15 |

**Non-human identities outnumber humans by a wide margin** in most cloud estates: every service account, CI job,
API key and agent is one. They are also the ones most often authenticated by a long-lived static secret, which is
why chapter 12 exists.

---

## 6. Where state lives: stateful sessions versus self-contained tokens

The most important architectural choice in identity is **where the truth about "this caller is authenticated"
lives**.

| | Reference (stateful) | Self-contained (stateless) |
|---|---|---|
| Example | Opaque session ID → row in Redis; opaque access token → introspection (RFC 7662) | JWT access token, SAML assertion, X.509 certificate |
| Verify cost | A lookup per request (cache-able) | A signature check, local, microseconds |
| Revocation | Instant: delete the row | Only at expiry, unless you add a denylist (which is state again) |
| Size on the wire | ~32 bytes | 0.5–4 KB, grows with claims |
| Privacy | Contents hidden from client | Contents readable by anyone holding it (unless encrypted) |
| Failure mode | Session store outage = everyone logged out | Key compromise = forge anything until rotated |
| Scale | Store must scale with request rate | Scales with CPU; issuer only involved at issuance |

The production answer is usually **both, in layers**: short-lived self-contained access tokens (5–15 minutes) for
service-to-service calls, backed by a stateful refresh token or session that can be revoked instantly. The
access-token lifetime is your **maximum revocation lag**. Pick it on purpose. Chapter 02 covers the browser side,
chapter 07 the API side, chapter 08 how each of these is stored in your database (hashed, encrypted or neither),
and chapter 09 where each kind of client (backend, SPA, mobile, CLI, AI agent) keeps its tokens.

---

## 7. Trust boundaries and the confused deputy

A **confused deputy** is a program with authority that is tricked into using it for someone who lacks it. The
term comes from Norm Hardy's 1988 paper about a compiler that could write to a billing file. Identity systems
create deputies everywhere:

- A backend that calls a downstream API with **its own** service credential on behalf of any user ends up
  letting every user act with the service's authority. Fix: propagate the user's identity (token exchange) and
  authorize on it.
- An OAuth client that accepts an authorization code from the wrong authorization server sends that code to an
  attacker-controlled token endpoint (the **mix-up attack**). Fix: issuer identification (RFC 9207).
- An MCP server that forwards the user's token to an upstream API lets the upstream be called with authority
  meant for the MCP server (ch 13).
- An AI agent with a broad key that reads attacker-controlled text is a deputy whose instructions come from the
  attacker (ch 13, and [ai-rag ch 17](../ai-rag/17-safety-guardrails-and-prompt-injection.md)).

Rule: **at every hop, ask whose authority this call carries, and whether the callee can see that.** If the
answer is "the service's" and the action was requested by a user, you have a deputy.

---

## 8. The identity lifecycle

```
 Joiner ──▶ Provision accounts ──▶ Authenticate (daily) ──▶ Authorize (per request)
   │            (SCIM, JIT)               │                        │
   │                                      ▼                        ▼
   │                               Session / tokens           Audit records
   ▼
 Mover (role change) ──▶ Re-provision, revoke old grants   ◀── access reviews
   ▼
 Leaver ──▶ Deprovision ──▶ revoke sessions, tokens, keys ──▶ delete or anonymize data (GDPR)
```

The leaver step is where real breaches come from: a contractor's API key that outlived their contract, an OAuth
grant to an app nobody uses any more, a service account whose owner left. Design deprovisioning first; it is
the hard direction (ch 10 §5, ch 18 §8).

---

## 9. The protocol map

| Layer | Standards | What it does | Chapter |
|---|---|---|---|
| Credentials | Passwords (NIST SP 800-63B), TOTP (RFC 6238), WebAuthn / FIDO2 / passkeys | Prove a human is present | 01 |
| Browser state | Cookies (RFC 6265 and its update), SameSite, CSRF defenses | Keep a human logged in safely | 02 |
| Token formats | JWT (RFC 7519), JWS, JWE, JWK, SD-JWT (RFC 9901) | Carry claims in a checkable form | 03, 16 |
| Delegated authorization | OAuth 2.0 (RFC 6749), PKCE (RFC 7636), OAuth 2.1 (draft), Security BCP (RFC 9700) | Let a client call an API for a user | 04 |
| Federated login | OpenID Connect Core 1.0, Discovery, logout specs | Tell the client who the user is | 05 |
| Discovery | RFC 8615 `.well-known`, RFC 8414, OIDC Discovery, RFC 9728, JWKS | Find endpoints and keys without configuration | 06 |
| Hardening | PAR, JAR, RFC 9207, DPoP, mTLS-bound tokens, RAR, token exchange, step-up, FAPI 2.0 | Close the known attacks | 07 |
| Storage | Argon2id/scrypt/bcrypt, HMAC/SHA-256 for high-entropy tokens, envelope encryption with a KMS | Keep secrets safe at rest and in clients | 08, 09 |
| Enterprise | SAML 2.0, SCIM 2.0, OpenID Federation, Shared Signals/CAEP, ID-JAG (draft) | SSO, provisioning, cross-domain trust | 10 |
| Authorization models | RBAC, ABAC, ReBAC (Zanzibar), OPA, Cedar, OpenFGA, AuthZEN | Decide what is allowed | 11 |
| Workloads | mTLS, SPIFFE/SPIRE, cloud workload identity federation, Kubernetes service account tokens | Identity for software | 12 |
| Agents | MCP authorization, A2A security schemes, agent registries and gateways, token exchange, human-in-the-loop approval | Identity and permissions for AI agents and tools | 13–15 |
| Decentralized | W3C VC, SD-JWT VC, OpenID4VCI/VP, ISO mDL, EUDI wallet | User-held credentials | 16 |
| Products | Okta, Auth0, Entra ID, Keycloak, Cognito, Google, Ping, SPIRE, Vault | Which product implements which protocol, and their own flow names | 17 |
| Architecture | Build vs buy, availability, scale, ATO defense, phases | Putting it together | 18 |

---

## 10. Decision cheat sheet

| Situation | Default |
|---|---|
| New consumer web app | Buy or self-host an IdP; OIDC authorization code + PKCE; passkeys first, password + MFA fallback |
| SPA calling your own API | Backend-for-frontend (BFF) with HttpOnly session cookie; no tokens in browser JavaScript (ch 02 §6) |
| Mobile app | OIDC code + PKCE via system browser (RFC 8252); never an embedded webview |
| B2B SaaS selling to enterprises | OIDC + SAML per tenant, SCIM provisioning, per-tenant IdP routing (ch 10) |
| Service-to-service inside your platform | mTLS with platform-issued identities (SPIFFE or mesh); plus user context via token exchange if acting for a user |
| CI deploying to cloud | Workload identity federation (CI OIDC token → cloud role), no stored cloud keys (ch 12 §5) |
| Public API for developers | OAuth client credentials for server-to-server; API keys only for low-risk read access, hashed at rest (ch 12 §2) |
| Remote MCP server | OAuth 2.1 resource server per the MCP authorization spec; audience-checked tokens; no passthrough (ch 13) |
| Fine-grained, per-object sharing | ReBAC (OpenFGA/SpiceDB) or a policy engine; not hundreds of roles (ch 11) |

---

## 11. Interview questions

**Q1. What is the difference between authentication and authorization, and give a bug where they are confused.**
Authentication proves identity; authorization decides permission. The canonical bug is an API that checks the
token is valid and then returns `/invoices/{id}` for any `id`: authenticated, never authorized. This is OWASP API
Security's #1, Broken Object Level Authorization.

**Q2. Why are short-lived JWT access tokens paired with refresh tokens instead of just long-lived JWTs?**
A JWT cannot be revoked without extra state; its lifetime is the revocation lag. Short access tokens bound that lag
to minutes while the refresh token, which is checked against the authorization server on every use, can be revoked
immediately and rotated to detect theft.

**Q3. Give two examples of a confused deputy in identity systems.**
A backend calling a downstream API with its own broad credential for every user; an MCP server forwarding the
user's token to an upstream API. Both let one party's authority be exercised for another without the callee being
able to tell.

**Q4. What checks must a verifier perform on any signed token?**
Issuer is trusted; signature verifies with that issuer's current key and the expected algorithm; audience is this
verifier; time claims (`exp`, `nbf`, `iat`) are valid with small skew; and if the token is sender-constrained,
the presenter proves possession of the bound key.

---

## Sources

- N. Hardy, "The Confused Deputy (or why capabilities might have been invented)", ACM SIGOPS Operating Systems
  Review, 1988: https://dl.acm.org/doi/10.1145/54289.871709
- RFC 6749, The OAuth 2.0 Authorization Framework: https://datatracker.ietf.org/doc/html/rfc6749
- OpenID Connect Core 1.0: https://openid.net/specs/openid-connect-core-1_0.html
- RFC 7662, OAuth 2.0 Token Introspection: https://datatracker.ietf.org/doc/html/rfc7662
- NIST SP 800-63-4, Digital Identity Guidelines (2025): https://pages.nist.gov/800-63-4/
- OWASP API Security Top 10 (2023): https://owasp.org/API-Security/editions/2023/en/0x11-t10/
