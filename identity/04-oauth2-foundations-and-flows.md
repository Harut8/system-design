# 04 — OAuth 2.0 foundations and flows, and why they look the way they do

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (§2.1 what an IdP is; §4 the confusable pairs),
> [`03-jwt-jose-and-signing-keys.md`](03-jwt-jose-and-signing-keys.md) (access tokens are often JWTs).
>
> **Feeds into:** [`05-openid-connect.md`](05-openid-connect.md) (OIDC is a layer on this),
> [`06-well-known-discovery-and-metadata.md`](06-well-known-discovery-and-metadata.md) (how clients find the
> endpoints), [`07-oauth-hardening-and-advanced-specs.md`](07-oauth-hardening-and-advanced-specs.md) (the attacks
> and the extensions that close them), [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md)
> (MCP is an OAuth 2.1 profile).
>
> **THESIS:** OAuth is a protocol for **delegated authorization**: letting a piece of software (the client) call an
> API (the resource server) with *some* of a user's authority, without ever learning the user's password. Every odd
> detail of the flow (the redirect, the `state` parameter, the one-time code, the back-channel exchange, PKCE)
> exists because of a specific attack against a simpler design. If you learn the flow as "the simplest thing that
> survives these attacks", you never have to memorize it. And OAuth is **not** an authentication protocol; using a
> bare access token as proof of login is a bug that OIDC (ch 05) was created to fix.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The problem OAuth solves: the password anti-pattern](#1-the-problem-oauth-solves-the-password-anti-pattern)
2. [The four roles and the two channels](#2-the-four-roles-and-the-two-channels)
3. [Deriving the authorization code flow, one attack at a time](#3-deriving-the-authorization-code-flow-one-attack-at-a-time)
4. [PKCE in depth: what it is and why it exists](#4-pkce-in-depth-what-it-is-and-why-it-exists)
5. [The full authorization code + PKCE flow on the wire](#5-the-full-authorization-code--pkce-flow-on-the-wire)
6. [Client types: confidential versus public, and client authentication](#6-client-types-confidential-versus-public-and-client-authentication)
7. [The other grants: client credentials, device, refresh, and the deprecated ones](#7-the-other-grants-client-credentials-device-refresh-and-the-deprecated-ones)
8. [Scopes, consent and the effective permission](#8-scopes-consent-and-the-effective-permission)
9. [Refresh tokens: lifetimes, rotation and reuse detection](#9-refresh-tokens-lifetimes-rotation-and-reuse-detection)
10. [Redirect URIs: the most attacked parameter](#10-redirect-uris-the-most-attacked-parameter)
11. [Native apps, CLIs and loopback redirects](#11-native-apps-clis-and-loopback-redirects)
12. [OAuth 2.1: what it consolidates](#12-oauth-21-what-it-consolidates)
13. [Errors and the resource server's side](#13-errors-and-the-resource-servers-side)
14. [Code: a complete client and a resource-server check](#14-code-a-complete-client-and-a-resource-server-check)
15. [Anti-patterns](#15-anti-patterns)
16. [Interview questions](#16-interview-questions)
17. [Sources](#sources)

---

## Start here — the whole chapter in plain words

You want a printing website to print photos from your Google Photos. The bad old way: you give the printing site
your Google password. Now it can read your email too, forever, and the only way to stop it is to change your
password everywhere.

OAuth's way:

1. The printing site sends your browser to **Google** with a note: "the printing site would like *read access to
   photos*".
2. You log in **at Google** (the printing site never sees your password) and click **Allow**.
3. Google sends your browser back to the printing site with a **one-time code**.
4. The printing site, server to server, trades that code at Google for an **access token**: a key that opens only
   "read photos", only for you, only for an hour.
5. The printing site calls the Photos API with that token. You can revoke it at Google any time without changing
   your password.

**PKCE** ("pixy") is a small addition that makes step 4 safe even when the "site" is a mobile app or a SPA that
cannot keep a secret: before step 1 the app invents a random secret, sends only its **hash** in step 1, and must
reveal the secret itself in step 4. Someone who steals the code in step 3 doesn't have the secret, so the code is
useless to them.

---

## 1. The problem OAuth solves: the password anti-pattern

Before OAuth (2007), "connect your account" meant password sharing, which had five properties nobody wanted:

| Property of password sharing | Consequence | OAuth's answer |
|---|---|---|
| The third party sees the password | It can be leaked or misused anywhere | User authenticates only at the AS |
| Full account authority | Printing site can read email | **Scopes** limit authority |
| No expiry | Access forever | **Short-lived access tokens** |
| Revoking one app = changing password for all | Can't revoke selectively | Per-client **grants** revocable at the AS |
| MFA breaks it | Third party can't do your MFA | MFA happens at the AS during login |

OAuth 1.0 (RFC 5849, 2010) solved this with request signing, which was hard to implement. OAuth 2.0
(RFC 6749, 2012) replaced signing with **bearer tokens over TLS** (RFC 6750) and a set of **grant types** for
different client situations. Its simplicity drove adoption; its flexibility drove a decade of security BCPs
(RFC 6819, then RFC 9700, then OAuth 2.1).

---

## 2. The four roles and the two channels

| Role | Example | Holds |
|---|---|---|
| **Resource owner** | You | The authority being delegated |
| **Client** | The printing site, a mobile app, a CLI, an AI agent host | Client ID (and maybe a credential); receives tokens |
| **Authorization server (AS)** | Google's OAuth endpoints, Okta, Keycloak | Users, clients, grants, signing keys |
| **Resource server (RS)** | Photos API, an MCP server | The protected data; accepts access tokens |

Two **channels** carry messages, and the distinction explains most of the protocol:

| Channel | Path | Properties |
|---|---|---|
| **Front channel** | Through the user's browser via HTTP redirects (`302 Location: ...?code=...`) | Lets the AS interact with the user (login, consent). But: visible in browser history, logs, `Referer` headers, to browser extensions, to malicious apps registered for the same URL scheme. **Not confidential, not integrity-protected end to end.** |
| **Back channel** | Direct HTTPS from client server to AS | Confidential, client can authenticate, response not seen by the browser |

Design rule that follows: **only send things through the front channel that are useless on their own.** That is
why the front channel carries a short-lived, one-time, client-bound **code**, and the **token** travels only on
the back channel.

The AS exposes two endpoints:

- **Authorization endpoint** (front channel): `GET /authorize?...`. Where the user logs in and consents.
- **Token endpoint** (back channel): `POST /token`. Where codes, refresh tokens and other grants are exchanged for
  tokens.

Plus, usually: `/revoke` (RFC 7009), `/introspect` (RFC 7662), `/par` (RFC 9126), `/jwks`, and a metadata document
at a well-known URL (ch 06).

---

## 3. Deriving the authorization code flow, one attack at a time

The fastest way to understand the flow is to design it yourself, starting naive.

### Attempt 1: the AS redirects back with the token

```
Client ──redirect──▶ AS /authorize?client_id=print&redirect_uri=https://print.example/cb
User logs in, consents
AS ──redirect──▶ https://print.example/cb#access_token=eyJ...
```

This is roughly the **implicit grant** of OAuth 2.0. Problems:

- The token sits in the browser URL: history, extensions, logs, `Referer` leakage.
- The client can't prove to itself that *it* asked for this token. An attacker can inject a token obtained for a
  different client (**token injection / substitution**).
- No way to authenticate the client; anyone can claim `client_id=print`.

**Fix → put a code in the front channel instead, exchange it on the back channel.**

### Attempt 2: authorization code

```
AS ──redirect──▶ https://print.example/cb?code=SplxlOBeZQQYbYS6WxSbIA
Client ──POST /token {code, client_id, client_secret, redirect_uri}──▶ AS
AS ──▶ {access_token, refresh_token, expires_in}
```

Now the token never touches the browser, and a stolen code is useless without the client secret. Remaining
attacks:

**(a) CSRF on the callback ("login CSRF" / session swapping).** The attacker starts a flow with *their own* account,
stops before the redirect, and tricks the victim's browser into loading
`https://print.example/cb?code=ATTACKER_CODE`. The victim's printing-site session is now connected to the
attacker's photos; the victim uploads private documents into the attacker's account.

**Fix → `state`.** Before redirecting, the client generates a random value, stores it in the user's session, and
sends it as `state`. The callback must return the same `state`. An attacker can't know the victim's `state`.
(PKCE, below, also defeats this when the AS enforces it, which is why OAuth 2.1 lets PKCE replace `state` for
CSRF; keep `state` anyway for app-level redirect context.)

**(b) Redirect URI manipulation.** The attacker sends the victim to
`/authorize?client_id=print&redirect_uri=https://evil.example/cb`. If the AS honors it, the code goes to the
attacker. **Fix → the AS compares `redirect_uri` against pre-registered values by exact string match** (§10), and
the token endpoint requires the same `redirect_uri` the code was issued for.

**(c) Code theft by a public client's neighbor.** Mobile and desktop apps can't hold a secret (anyone can
decompile the binary), so for them "a stolen code is useless without the secret" is false. On Android/iOS, a
malicious app can register the **same custom URL scheme** (`com.print.app://cb`) and receive the redirect. On
desktops, another local process can race to listen on the loopback port. Browser-based SPAs leak codes through
history and logs. **Fix → PKCE** (§4).

**(d) Code replay.** A code seen in a log is used later. **Fix → codes are single-use and short-lived** (the AS
should expire them within about a minute and, if a code is presented twice, revoke tokens already issued from
it).

**(e) Mix-up.** A client that talks to several ASes is tricked into sending a code from AS-A to AS-B's token
endpoint (controlled by the attacker). **Fix → issuer identification** (`iss` in the authorization response,
RFC 9207, ch 07 §3), or a distinct redirect URI per AS.

What remains after all fixes is the modern flow: **authorization code + PKCE + exact redirect URI + `state` (or
PKCE-as-CSRF) + issuer check + single-use codes.** Nothing in it is ceremony; each part answers one of the attacks
above.

---

## 4. PKCE in depth: what it is and why it exists

> **In plain words.** PKCE (Proof Key for Code Exchange, RFC 7636, pronounced "pixy") makes the authorization code
> useless to anyone except the client instance that started the flow. That instance picks a random secret, sends
> its hash at the start, and reveals the secret at the end. The AS checks that the hash matches.
>
> **Real-world example.** A mobile banking app opens the system browser to the bank's login page. A malicious app
> on the same phone has registered the same URL scheme and catches the redirect with the code. It calls the token
> endpoint with the code, but it doesn't know the `code_verifier`, which never left the banking app's memory. The
> AS refuses. The code is worthless.

### 4.1 The mechanics

```
code_verifier  = random 43..128 chars from [A-Z a-z 0-9 - . _ ~]   (use 32 random bytes → base64url = 43 chars)
code_challenge = BASE64URL( SHA256( ASCII(code_verifier) ) )        (method "S256")
```

1. **Authorization request** (front channel) carries `code_challenge` and `code_challenge_method=S256`. The AS
   stores the challenge alongside the code it issues.
2. **Token request** (back channel) carries `code_verifier`. The AS computes `S256(code_verifier)` and compares it
   to the stored challenge. Mismatch → `invalid_grant`.

RFC 7636 Appendix B gives a worked example (verifier octets → verifier string → challenge). Copy it from the RFC
into your client's unit tests; a base64 vs base64url or padding bug is the most common PKCE implementation error
and that test catches it.

### 4.2 Why a hash, and why it works

- The front channel is observable. If the client sent the verifier itself at the start, an attacker who sees the
  authorization request would know it. Sending `SHA256(verifier)` reveals nothing usable: you can't invert the hash
  and the verifier has 256 bits of entropy, so you can't guess it.
- The verifier is created **per authorization request** and lives only in the client instance's memory (or session
  for a server-side client). It's a **dynamic, per-flow secret**, which is exactly what a public client lacks: it
  can't hold a long-term secret, but it can hold a one-minute one.
- So PKCE binds three things together: *the client instance that started the flow* = *the one that receives the
  tokens*.

### 4.3 Why `plain` is not enough

`code_challenge_method=plain` sends the verifier as the challenge. It only protects against attackers who see the
redirect *response* but not the *request*. On a device where the attacker can observe the request (e.g. via logs or
an intent filter), `plain` gives nothing. **Always S256**; ASes should reject `plain` (RFC 9700).

### 4.4 Why confidential clients use PKCE too

Originally PKCE was "for mobile apps". The OAuth Security BCP (RFC 9700) and OAuth 2.1 require it for **all**
clients using the code flow, including web servers with a client secret, because it also defends against:

- **Authorization code injection**: an attacker who stole a code (e.g. from logs) injects it into *their own*
  session with the legitimate client; the client, holding a valid secret, would happily redeem it. With PKCE, the
  attacker's session holds a different verifier, so redemption fails.
- **CSRF**, as noted in §3(a), when the AS enforces PKCE.

### 4.5 PKCE downgrade

If the AS accepts a token request **without** `code_verifier` for a code that was issued **with** a challenge, an
attacker can strip PKCE. The AS must require the verifier whenever a challenge was present, and should require PKCE
for all clients. The AS advertises support via `code_challenge_methods_supported` in its metadata (ch 06); MCP
clients, for instance, MUST refuse to proceed if `S256` is not listed.

---

## 5. The full authorization code + PKCE flow on the wire

```
Browser/User            Client (backend or app)                 Authorization Server            Resource Server
     │                          │                                        │                              │
     │  click "Connect"         │                                        │                              │
     ├─────────────────────────▶│ verifier=rand(32B); challenge=S256(v)  │                              │
     │                          │ state=rand(16B); nonce (OIDC)          │                              │
     │                          │ store {v, state, nonce, iss} in session│                              │
     │◀── 302 Location: https://as.example/authorize?                    │                              │
     │      response_type=code&client_id=print&redirect_uri=https://print.example/cb                     │
     │      &scope=photos.read&state=...&code_challenge=...&code_challenge_method=S256                   │
     │      &resource=https://photos.example (RFC 8707, optional)        │                              │
     ├──────────────────────────────────────────────────────────────────▶│ authenticate user (+MFA)     │
     │                                                                   │ show consent: "print wants   │
     │                                                                   │  photos.read"                │
     │◀── 302 Location: https://print.example/cb?code=abc&state=...&iss=https://as.example              │
     ├─────────────────────────▶│ check state == session.state           │                              │
     │                          │ check iss == session.iss (RFC 9207)    │                              │
     │                          │ POST /token                            │                              │
     │                          │  grant_type=authorization_code&code=abc│                              │
     │                          │  &redirect_uri=...&code_verifier=v     │                              │
     │                          │  + client auth (private_key_jwt/secret)│                              │
     │                          ├───────────────────────────────────────▶│ code valid, unused, for this │
     │                          │                                        │ client & redirect_uri;       │
     │                          │                                        │ S256(v)==challenge           │
     │                          │◀── {access_token, token_type, expires_in, refresh_token, scope}      │
     │                          │ GET /photos  Authorization: Bearer AT  │                              │
     │                          ├──────────────────────────────────────────────────────────────────────▶│
     │                          │                                        │   validate AT (sig/introspect,│
     │                          │                                        │   aud, exp, scope) → 200      │
```

Token response (RFC 6749 §5.1):

```json
{
  "access_token": "eyJhbGciOiJFUzI1NiIsInR5cCI6ImF0K2p3dCJ9...",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_token": "8xLOxBtZp8",
  "scope": "photos.read"
}
```

Note `scope` in the response: the AS may grant **less** than requested (the user unticked a box, policy denied
one). Clients must read it rather than assume.

---

## 6. Client types: confidential versus public, and client authentication

| Type | Can keep a secret? | Examples | Authenticates at token endpoint with |
|---|---|---|---|
| **Confidential** | Yes, on a server you control | Web backend, BFF, backend service, server-side agent | `client_secret_basic`/`post` (shared secret), **`private_key_jwt`** (RFC 7523 assertion signed with client's key), **mTLS** (RFC 8705) |
| **Public** | No; code is on the user's device | SPA, mobile app, desktop app, CLI, local AI agent host | Nothing (`none`); relies on PKCE, redirect URI, and refresh-token rotation/binding |

Prefer **asymmetric client authentication** (`private_key_jwt` or mTLS) over shared secrets for confidential
clients: the AS stores only a public key, secrets don't travel, and keys can live in a KMS. FAPI 2.0 requires one of
the two.

`private_key_jwt` assertion (sent as `client_assertion` with `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`):

```json
{ "iss": "print", "sub": "print", "aud": "https://as.example", "jti": "uuid", "iat": 1761724800, "exp": 1761724860 }
```

The AS must check `jti` uniqueness within the validity window (replay) and that `aud` identifies itself. In early
2025 the OpenID Foundation coordinated a disclosure on exactly this: because `aud` was allowed to be several
values (token endpoint URL, issuer, other endpoints), a malicious AS that a client also talks to could get the
client to produce an assertion that an honest AS would accept, then replay it there. The fix being standardized in
`draft-ietf-oauth-rfc7523bis` is that `aud` must be exactly the AS's issuer identifier and nothing else.

**Client identity is weaker for public clients.** A `client_id` for a public client is a *claim*, not a proof:
anybody can send `client_id=official-mobile-app`. Redirect URI checks (and on mobile, claimed HTTPS links / app
attestation) are what make it meaningful. That is a recurring theme in agent and MCP clients (ch 13): open
ecosystems of public clients need another way to establish "who is this client", hence Client ID Metadata
Documents.

---

## 7. The other grants: client credentials, device, refresh, and the deprecated ones

| Grant | `grant_type` | Who acts | Use for | Notes |
|---|---|---|---|---|
| Authorization code (+PKCE) | `authorization_code` | Client **for a user** | Anything with a user and a browser | The default |
| Client credentials | `client_credentials` | Client **as itself** | Service-to-service, cron jobs, autonomous agents with their own authority | No user, no refresh token; authenticate the client strongly |
| Device authorization (RFC 8628) | `urn:ietf:params:oauth:grant-type:device_code` | Client for a user, on a device without a good browser | TVs, CLIs on remote servers, IoT | User enters a code on another device; phishable ("device code phishing"), see below |
| Refresh token | `refresh_token` | Same as original grant | Getting new access tokens | §9 |
| Token exchange (RFC 8693) | `urn:ietf:params:oauth:grant-type:token-exchange` | Service for a user or agent for a user | Delegation chains, down-scoping | ch 07 §7, ch 13 |
| JWT bearer (RFC 7523) | `urn:ietf:params:oauth:grant-type:jwt-bearer` | Holder of an assertion from a trusted issuer | Cross-domain trust, ID-JAG | ch 10 §7 |
| CIBA | `urn:openid:params:grant-type:ciba` | Client for a user who approves on their phone | Call centers, agents needing approval | ch 07 §9, ch 15 §5 |
| ~~Implicit~~ | `response_type=token` | — | **Removed in OAuth 2.1** | Tokens in URL fragment; no binding |
| ~~Resource owner password~~ | `password` | — | **Removed in OAuth 2.1** | Client sees password; no MFA; trains phishing |

**Device flow detail and its phishing risk.** The device requests `/device_authorization` and gets a
`device_code` (secret, kept by the device) and a short `user_code` + `verification_uri` shown to the user. The user
visits the URI on a phone, logs in, enters the code. The device polls `/token` (respecting `interval` and
`slow_down`) until it gets tokens. Attackers abuse this: they start a device flow themselves and send the victim
"enter code ABCD-EFGH at microsoft.com/devicelogin to view the document". The victim authenticates, with MFA, at
the real site, and the attacker's device receives tokens. Since 2024–2025 this has been a widely reported
technique against Microsoft 365. Mitigations: disable device flow where not needed, restrict it by conditional
access, show the requesting client and location prominently on the verification page, and short `user_code`
lifetimes.

---

## 8. Scopes, consent and the effective permission

- A **scope** is a string the AS and RS agree on (`photos.read`). It limits **the client**, not the user.
- **Effective permission = user's own permissions ∩ scopes granted to the client ∩ RS policy.** A token with
  `invoices.write` for a user who can't write invoices must still be refused by the RS. Forgetting the user half of
  the intersection is a frequent bug.
- Scopes are coarse. For "transfer up to €50 to IBAN X" use **Rich Authorization Requests** (`authorization_details`,
  RFC 9396, ch 07 §6).
- **Consent** must name the client (verified name and logo where possible), the scopes in human language, and the
  resource. Consent screens are a phishing surface: "illicit consent grants" (an attacker-registered app named
  "Microsoft Security Update" asking for `Mail.ReadWrite offline_access`) are a standard enterprise attack. Admin
  consent policies, publisher verification and app governance are the enterprise defenses.
- **Incremental authorization**: request minimal scopes at login and more when a feature needs them. MCP formalizes
  this as step-up on `insufficient_scope` (ch 13 §6).

---

## 9. Refresh tokens: lifetimes, rotation and reuse detection

Why two tokens? Access tokens are presented to many resource servers and checked locally, so they must be short-lived
(their lifetime is your revocation lag). Refresh tokens are presented **only** to the AS, which checks them against
its database on every use, so they can live longer and still be revoked instantly.

**Rotation with reuse detection** (required for public clients by RFC 9700 unless the refresh token is
sender-constrained):

```
RT1 used → AS issues AT2 + RT2, marks RT1 used (same "family")
RT2 used → AT3 + RT3
Attacker replays stolen RT1 (or RT2 after legit client used it)
→ AS sees a used token in the family → revoke the entire family → both attacker and user must re-authenticate
```

Edge cases to design for:

- **Concurrent refresh** from two tabs/threads of the legitimate client: the second presents an already-rotated
  token and nukes the family. Fix: single-flight refresh in the client (one in-flight refresh, others wait), and
  on the AS a short **grace period** (e.g. 10–60 s) during which the previous RT returns the same new pair.
- **Network failure after the AS rotated** but before the client stored RT2: the client still holds RT1, which is
  now "used". The grace period handles this too.
- **Lifetimes**: sliding (each use extends) plus an absolute cap (re-login every N days). Tie refresh-token validity
  to the user's IdP session for workforce apps, so disabling the user ends it.
- **Storage**: refresh tokens are long-lived bearer credentials. Server side, store them hashed (ch 08 §4); client
  side, see ch 09.

---

## 10. Redirect URIs: the most attacked parameter

Rules (RFC 9700 and OAuth 2.1):

1. **Pre-register** every redirect URI.
2. **Exact string matching**, no wildcards, no prefix matching, no "same host any path". Exception: for loopback
   (`http://127.0.0.1`), the **port** may vary (§11).
3. No open redirectors on the client: if `https://print.example/cb?next=...` redirects anywhere, an attacker can chain
   it to leak codes via `Referer` or fragments.
4. `https` only, except loopback.
5. The token request must repeat the `redirect_uri` (2.0) or the AS must otherwise bind the code to it.

Real-world breaches from loose matching: prefix-matching allowed `https://client.example.com.evil.com`; path
matching allowed `https://client.example.com/open-redirect?u=evil`; subdomain wildcards allowed a hijacked
subdomain to receive codes.

---

## 11. Native apps, CLIs and loopback redirects

RFC 8252 (OAuth 2.0 for Native Apps):

- Use the **system browser** (ASWebAuthenticationSession on iOS/macOS, Custom Tabs on Android), never an embedded
  webview. In a webview the app can read the password field and the user can't see the URL bar, and the user's
  existing IdP session (SSO) isn't available.
- Redirect options, best first: **claimed HTTPS URLs** (Universal Links / App Links, verified through
  `apple-app-site-association` / `assetlinks.json` well-known files, ch 06 §5), **loopback**
  (`http://127.0.0.1:{random_port}/callback`, for desktop/CLI), **private-use URI scheme** (`com.example.app:/cb`,
  reverse-DNS, last resort because other apps can claim it).
- Always PKCE.

**CLI pattern** (what `gh`, `gcloud`, `az`, and coding agents do): start a local HTTP server on `127.0.0.1` with an
ephemeral port, open the browser to `/authorize` with that redirect, receive the code, exchange it with PKCE, store
the refresh token in the OS keychain (ch 09 §6). Over SSH or in containers with no browser, fall back to the device
flow.

---

## 12. OAuth 2.1: what it consolidates

OAuth 2.1 (`draft-ietf-oauth-v2-1`, revision -16 as of September 2026, still an Internet-Draft with an IESG
submission milestone of December 2026) is not new protocol; it folds RFC 6749, RFC 6750, RFC 7636, RFC 8252, the
browser-apps guidance and RFC 9700 into one document:

| Change from 2.0 | Reason |
|---|---|
| PKCE required for all authorization code clients | §4.4 |
| Implicit grant removed | §3, attempt 1 |
| Password grant removed | Credential exposure, no MFA |
| Exact redirect URI matching | §10 |
| Bearer tokens not allowed in query strings | Logged everywhere |
| Refresh tokens for public clients must be sender-constrained or rotated | §9 |
| `state` can be replaced by PKCE for CSRF protection when the AS supports PKCE | §3(a) |

MCP's authorization spec is written against OAuth 2.1 (ch 13). In practice "OAuth 2.1" in a product description means
"OAuth 2.0 configured per RFC 9700".

---

## 13. Errors and the resource server's side

Token endpoint errors (RFC 6749 §5.2): `invalid_request`, `invalid_client` (401), `invalid_grant` (bad/expired/used
code or refresh token), `unauthorized_client`, `unsupported_grant_type`, `invalid_scope`.

Resource server responses (RFC 6750 §3):

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer error="invalid_token", error_description="expired"

HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope", scope="photos.write"
```

Modern RSes add `resource_metadata="https://.../.well-known/oauth-protected-resource"` (RFC 9728) so clients can
discover which AS to use. That's the hook MCP builds on (ch 06 §4, ch 13 §3).

RS validation for each request: JWT checks of ch 03 §6 (or introspection), then scope, then **object-level**
authorization against the user (ch 11). A valid token is the start of authorization, not the end.

---

## 14. Code: a complete client and a resource-server check

Client side (confidential web client with PKCE; framework-agnostic, `session` is your server-side session dict):

```python
import base64, hashlib, secrets, urllib.parse
import httpx

AS = {
    "issuer": "https://as.example",
    "authorization_endpoint": "https://as.example/authorize",
    "token_endpoint": "https://as.example/token",
}  # in production: fetched from /.well-known/oauth-authorization-server (ch 06)
CLIENT_ID, REDIRECT_URI = "print", "https://print.example/cb"

def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def start_login(session: dict, scope: str = "photos.read") -> str:
    verifier = b64url(secrets.token_bytes(32))                    # 43 chars, 256 bits
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))
    session["oauth"] = {"verifier": verifier, "state": state, "iss": AS["issuer"]}
    q = {
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
        "scope": scope, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return AS["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)

def handle_callback(session: dict, params: dict, client_auth) -> dict:
    flow = session.pop("oauth", None)                              # one-shot: can't be replayed
    if not flow or not secrets.compare_digest(params.get("state", ""), flow["state"]):
        raise PermissionError("state mismatch (CSRF or stale flow)")
    if "iss" in params and params["iss"] != flow["iss"]:           # RFC 9207 mix-up defense
        raise PermissionError("issuer mismatch")
    if "error" in params:
        raise PermissionError(params["error"])
    r = httpx.post(AS["token_endpoint"], data={
        "grant_type": "authorization_code", "code": params["code"],
        "redirect_uri": REDIRECT_URI, "code_verifier": flow["verifier"],
    }, auth=client_auth, timeout=10)
    r.raise_for_status()
    tokens = r.json()
    if tokens.get("token_type", "").lower() not in ("bearer", "dpop"):
        raise PermissionError("unexpected token type")
    return tokens   # store server-side; refresh token hashed/encrypted (ch 08)
```

Resource server side (FastAPI dependency; `verifier` is the ch 03 `Verifier`):

```python
from fastapi import Depends, HTTPException, Request

RESOURCE_METADATA = "https://photos.example/.well-known/oauth-protected-resource"

def require_scope(needed: str):
    def dep(request: Request) -> dict:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{RESOURCE_METADATA}"'})
        try:
            claims = verifier.verify(auth[7:])
        except Exception:
            raise HTTPException(401, headers={
                "WWW-Authenticate": 'Bearer error="invalid_token"'})
        if needed not in claims.get("scope", "").split():
            raise HTTPException(403, headers={
                "WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{needed}"'})
        return claims
    return dep

@app.get("/photos/{photo_id}")
def get_photo(photo_id: str, claims: dict = Depends(require_scope("photos.read"))):
    photo = repo.get(photo_id)
    if photo is None or photo.owner_sub != claims["sub"]:   # object-level authorization
        raise HTTPException(404)                             # 404, not 403: don't confirm existence
    return photo
```

---

## 15. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Using OAuth access token as proof of login ("if I can call /me, it's that user") | Token from another client can log in as victim | OIDC ID token with `aud` = your client (ch 05) |
| No `state` / no PKCE | Login CSRF, code injection | PKCE S256 always + `state` |
| Wildcard or prefix redirect URIs | Code theft | Exact match |
| Implicit flow in SPAs | Tokens in URL | Code + PKCE, or BFF |
| Password grant for "our own first-party app" | No MFA, trains users to type passwords into apps | Code + PKCE in system browser |
| Embedded webview for login | App can read credentials, no SSO | System browser (RFC 8252) |
| Requesting every scope at sign-up | Consent fatigue, overprivileged tokens | Incremental authorization |
| Treating scopes as user permissions | Users exceed their own rights | Scope ∩ user permission ∩ policy |
| Non-rotating long-lived refresh tokens in public clients | Theft = indefinite access | Rotation with reuse detection, or DPoP |
| Same client registration for web, mobile, CLI | Can't apply per-platform policy | One client per platform |

---

## 16. Interview questions

**Q1. Why does the authorization code flow use a code instead of returning the token directly?**
The front channel (browser redirects) is observable and can't authenticate the client. A short-lived, single-use
code bound to the client, redirect URI and PKCE verifier is useless if leaked; the token is delivered only on the
authenticated back channel.

**Q2. What does PKCE protect against, and why do confidential clients need it too?**
Interception of the code by another app or log reader (the thief lacks the verifier), authorization code injection
into another session (that session's verifier differs) and CSRF. Confidential clients are vulnerable to injection
because their secret would redeem any injected code.

**Q3. Walk through refresh-token rotation and a concurrency bug it causes.**
Each use returns a new refresh token and invalidates the old one; reuse of an old one revokes the family. Two
parallel refreshes from the same legitimate client make the second look like reuse; fix with single-flight refresh
and an AS grace window.

**Q4. A TV app needs to access a user's account. Which grant, and what's the risk?**
Device authorization grant. Risk: device-code phishing, where an attacker initiates the flow and gets the victim to
enter the code. Mitigate with clear client/location display, short code lifetimes, and disabling the grant where
unused.

**Q5. Why is OAuth not an authentication protocol?**
The access token is meant for the resource server, not the client, has no standard audience pointing to the client,
and says nothing about when or how the user authenticated. A malicious client could replay a token it received for
itself into your app. OIDC adds the ID token, addressed to the client, with `nonce`, `auth_time` and `acr`.

---

## Sources

- RFC 6749, The OAuth 2.0 Authorization Framework: https://datatracker.ietf.org/doc/html/rfc6749
- RFC 6750, Bearer Token Usage: https://datatracker.ietf.org/doc/html/rfc6750
- RFC 7636, Proof Key for Code Exchange (PKCE): https://datatracker.ietf.org/doc/html/rfc7636
- RFC 8252, OAuth 2.0 for Native Apps: https://datatracker.ietf.org/doc/html/rfc8252
- RFC 8628, OAuth 2.0 Device Authorization Grant: https://datatracker.ietf.org/doc/html/rfc8628
- RFC 9700, Best Current Practice for OAuth 2.0 Security (January 2025): https://datatracker.ietf.org/doc/html/rfc9700
- RFC 7523, JWT Profile for OAuth 2.0 Client Authentication and Authorization Grants: https://datatracker.ietf.org/doc/html/rfc7523
- The OAuth 2.1 Authorization Framework, draft-ietf-oauth-v2-1-16 (September 2026): https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/
- RFC 6819, OAuth 2.0 Threat Model and Security Considerations: https://datatracker.ietf.org/doc/html/rfc6819
- RFC 9207, OAuth 2.0 Authorization Server Issuer Identification: https://datatracker.ietf.org/doc/html/rfc9207
- D. Fett, R. Küsters, G. Schmitz, "A Comprehensive Formal Security Analysis of OAuth 2.0" (CCS 2016; source of the mix-up analysis): https://arxiv.org/abs/1601.01229
