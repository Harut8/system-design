# 05 — OpenID Connect: login on top of OAuth

> **Prerequisites:** [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) (OIDC *is* the
> authorization code flow with extra parts), [`03-jwt-jose-and-signing-keys.md`](03-jwt-jose-and-signing-keys.md)
> (the ID token is a JWT).
>
> **Feeds into:** [`06-well-known-discovery-and-metadata.md`](06-well-known-discovery-and-metadata.md)
> (`/.well-known/openid-configuration`), [`10-enterprise-sso-saml-scim-and-federation.md`](10-enterprise-sso-saml-scim-and-federation.md)
> (OIDC versus SAML for enterprise SSO), [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md).
>
> **THESIS:** OAuth tells a client "you may call this API". It does not tell the client *who the user is*. OpenID
> Connect (OIDC, 2014) adds exactly that: an **ID token** addressed to the client, with the user's stable identifier,
> when and how they authenticated, and a `nonce` tying it to this login attempt. Get four things right and OIDC is
> safe: **validate the ID token fully, key users on `(iss, sub)`, never send the ID token to APIs, and design logout
> as deliberately as login.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [What OIDC adds to OAuth, and why it was needed](#1-what-oidc-adds-to-oauth-and-why-it-was-needed)
2. [The flow: authorization code with `openid` scope](#2-the-flow-authorization-code-with-openid-scope)
3. [The ID token, claim by claim](#3-the-id-token-claim-by-claim)
4. [ID token versus access token versus UserInfo](#4-id-token-versus-access-token-versus-userinfo)
5. [Validating an ID token](#5-validating-an-id-token)
6. [Scopes, claims and identity assurance](#6-scopes-claims-and-identity-assurance)
7. [Logout: RP-initiated, front-channel, back-channel](#7-logout-rp-initiated-front-channel-back-channel)
8. [Multi-tenant IdPs and issuer validation](#8-multi-tenant-idps-and-issuer-validation)
9. [Account linking and identifiers](#9-account-linking-and-identifiers)
10. [Other OIDC parts worth knowing](#10-other-oidc-parts-worth-knowing)
11. [Code: callback handler with full ID token validation](#11-code-callback-handler-with-full-id-token-validation)
12. [Anti-patterns](#12-anti-patterns)
13. [Interview questions](#13-interview-questions)
14. [Sources](#sources)

---

## Start here — the whole chapter in plain words

OIDC is OAuth with one extra token. When the app asks for the scope `openid`, the identity provider returns, along
with the access token, an **ID token**: a signed note addressed *to the app* that says "this is user `248289761001`,
they logged in at 09:02 with a passkey, and this answers your login request number `n-0S6_WzA2Mj`".

The app checks the signature, checks the note is addressed to it, checks the request number matches the one it
sent, and logs the user in. The access token is for calling APIs; the ID token is for the app itself and should
never be sent anywhere.

Logout is the hard part: the user has a session at the app *and* at the IdP, and possibly at ten other apps that
used the same IdP. OIDC defines ways for the IdP to tell each app "end this user's session".

---

## 1. What OIDC adds to OAuth, and why it was needed

Around 2010–2012, many sites implemented "Login with Facebook/Google" with pure OAuth: get an access token, call
`/me`, log in whoever `/me` returns. That pattern has a specific hole:

> **The token substitution attack.** A malicious app `evil-quiz` gets a legitimate access token for Alice (she
> logged into the quiz). Now `evil-quiz` calls *your* app's "login with token" endpoint (common for mobile apps
> that send their token to their backend) with Alice's token. Your backend calls `/me`, gets Alice, logs the
> attacker in as Alice. The access token was never meant for your app, but nothing in it said so.

OIDC's fixes, each answering a gap:

| Gap in "OAuth as login" | OIDC addition |
|---|---|
| Token isn't addressed to the client | ID token `aud` = your `client_id` |
| No binding to this login attempt (replay, injection) | `nonce` you send, echoed in the ID token |
| No standard user identifier | `sub`, unique and stable within `iss` |
| No info on how/when authenticated | `auth_time`, `amr`, `acr` |
| Every provider's `/me` differs | Standard claims + `/userinfo` endpoint |
| No standard discovery | `/.well-known/openid-configuration` (ch 06) |
| No logout | RP-Initiated, Front-Channel and Back-Channel Logout specs |
| Each provider needs manual setup | Dynamic Client Registration (RFC 7591 / OIDC DCR) |

---

## 2. The flow: authorization code with `openid` scope

It is the ch 04 flow with three additions:

```
GET /authorize?response_type=code
    &client_id=web-app
    &redirect_uri=https://app.example.com/cb
    &scope=openid%20profile%20email          ← "openid" makes it OIDC
    &state=af0ifjsldkj
    &nonce=n-0S6_WzA2Mj                       ← binds the ID token to this request
    &code_challenge=...&code_challenge_method=S256
    &prompt=login | consent | none | select_account   (optional)
    &max_age=300                              (optional: re-auth if older than 5 min)
    &acr_values=urn:...:phr                   (optional: ask for phishing-resistant auth)
    &login_hint=alice@acme.com                (optional)
```

Token response adds `id_token`:

```json
{ "access_token": "...", "token_type": "Bearer", "expires_in": 900,
  "refresh_token": "...", "id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6IjFlOWdkazcifQ..." }
```

`prompt=none` asks for **silent** authentication: return immediately with a code if the IdP session is valid, or
`error=login_required` otherwise. It is how apps check "still logged in at the IdP?", though browsers' third-party
cookie restrictions limit using it in hidden iframes (ch 02 §2).

**Other response types** (hybrid flow `code id_token`, implicit `id_token token`) exist in OIDC Core but are
legacy. Use `response_type=code`.

---

## 3. The ID token, claim by claim

```json
{
  "iss": "https://login.example.com",
  "sub": "248289761001",
  "aud": "web-app",
  "azp": "web-app",
  "exp": 1761725700,
  "iat": 1761724800,
  "auth_time": 1761724790,
  "nonce": "n-0S6_WzA2Mj",
  "acr": "urn:example:loa:phr",
  "amr": ["hwk", "user"],
  "sid": "08a5019c-17e1-4977-8f42-65a12843ea02",
  "email": "alice@acme.com",
  "email_verified": true,
  "name": "Alice Example"
}
```

| Claim | Meaning | Use |
|---|---|---|
| `iss` | Issuer | Must exactly equal the configured issuer |
| `sub` | Subject; never reassigned within `iss`; may be pairwise (different per client) | **The** user key, together with `iss` |
| `aud` | Client ID(s) | Must contain your `client_id` |
| `azp` | Authorized party | If `aud` has several values, must equal your `client_id` |
| `exp`, `iat` | Validity | Usual time checks |
| `auth_time` | When the user actually authenticated | Freshness for sensitive actions; required if you sent `max_age` |
| `nonce` | Echo of your request's nonce | Must equal what you stored |
| `acr` | Authentication Context Class Reference: assurance level achieved | Policy: e.g. require phishing-resistant for admin |
| `amr` | Authentication Methods References (RFC 8176): `pwd`, `otp`, `hwk`, `swk`, `face`, `fpt`, `mfa`, `user`, `pin`, ... | Evidence of method; values are provider-specific in practice |
| `sid` | IdP session ID | Correlate back-channel logout |
| `at_hash`, `c_hash` | Hash of access token / code | Only relevant in hybrid/implicit flows |

`sub` is **not** an email and is not portable across issuers. Some IdPs issue **pairwise** `sub`s
(different value per client) for privacy, so two of your apps may see different `sub`s for the same person
unless they share a sector identifier.

---

## 4. ID token versus access token versus UserInfo

| | ID token | Access token | UserInfo response |
|---|---|---|---|
| Audience | **The client** | **The resource server(s)** | The client |
| Purpose | "Who logged in, how, when" | "May call this API" | Fetch profile claims |
| Format | Always JWT | JWT or opaque | JSON (or signed JWT) |
| Who validates | The client | The resource server | The client (via TLS to the IdP) |
| Send to APIs? | **Never** | Yes | — |
| Lifetime | Short; only meaningful at login time | Short | — |

Why not send ID tokens to your APIs "because they have the user ID"?

1. `aud` is the client, so a correctly validating API rejects it; APIs that accept it are skipping `aud` checks,
   which means they'd also accept ID tokens issued to *other* clients.
2. ID tokens can't be scoped or down-scoped; they don't carry the client's granted permissions.
3. They're not designed to be revoked or sender-constrained.

Use an access token whose `aud` is your API. If the IdP can't issue one (common with consumer social IdPs), your
backend validates the ID token at login and then mints its own session or its own access tokens.

**Don't parse access tokens in the client either.** To the client an access token is opaque, even if it happens to
be a JWT today; the AS may change its format.

---

## 5. Validating an ID token

OIDC Core §3.1.3.7, condensed:

1. If encrypted, decrypt (rare).
2. `iss` exactly matches the issuer from discovery.
3. `aud` contains your `client_id`; reject if it contains audiences you don't trust; if several, `azp` = `client_id`.
4. Signature valid with a key from the issuer's JWKS, using the algorithm you registered/expect (RS256 is the OIDC
   default). Received over the authenticated back channel via TLS, the spec permits skipping signature checks for
   the code flow; **don't**, it costs microseconds and removes a class of bugs.
5. `exp` in the future; `iat` reasonable.
6. `nonce` equals the value stored in the session for this flow. Delete it after use.
7. If you sent `max_age`, check `auth_time + max_age ≥ now`.
8. If you need a level, check `acr` (and `amr` if you rely on it).

Then: look up or create the user by **`(iss, sub)`**, rotate the app session ID (ch 02 §3), store `sid` and
`auth_time`/`acr` in the session.

---

## 6. Scopes, claims and identity assurance

Standard scopes: `openid` (required), `profile` (name, family_name, picture...), `email` (+`email_verified`),
`address`, `phone` (+`phone_number_verified`), `offline_access` (request a refresh token usable when the user is not
present).

`email_verified: true` means the IdP verified *that the user controls the mailbox*, at some point. It doesn't mean
the IdP is authoritative for the domain. A random Gmail-style consumer IdP can verify `alice@acme.com` if Alice
proved control once; that doesn't make it Acme's corporate identity. For enterprise login, route by domain to the
customer's own IdP (ch 10 §4).

The `claims` request parameter lets the client request individual claims and mark them essential.
**OpenID Connect for Identity Assurance** (OIDC4IDA, final 2024) adds `verified_claims`: claims plus the evidence and
trust framework under which they were verified (e.g. "name verified against a passport under eIDAS LoA high"), used
in banking and government.

---

## 7. Logout: RP-initiated, front-channel, back-channel

> **In plain words.** Logging out of the app doesn't log you out of the IdP; logging out of the IdP doesn't
> log you out of the apps. OIDC gives you three tools to connect them, and the reliable one is back-channel.

Sessions involved (from ch 02 §1): IdP session, N app sessions, refresh tokens.

| Spec | Direction | How | Reliability |
|---|---|---|---|
| **RP-Initiated Logout 1.0** | App → IdP | Redirect browser to `end_session_endpoint?id_token_hint=...&post_logout_redirect_uri=...&state=...` | Good for ending the IdP session from one app |
| **Front-Channel Logout 1.0** | IdP → apps via browser | IdP's logout page loads each app's `frontchannel_logout_uri` in hidden iframes | **Poor**: depends on third-party cookies (the iframe needs the app's cookie), fails if the browser closes |
| **Back-Channel Logout 1.0** | IdP → app servers directly | IdP POSTs a signed **logout token** to each app's `backchannel_logout_uri` | **Best**: server to server, no browser needed |
| Session Management 1.0 (check_session_iframe) | App polls IdP via iframe | postMessage to an IdP iframe | Broken by third-party cookie blocking; avoid |

Back-channel logout token:

```json
{
  "iss": "https://login.example.com",
  "aud": "web-app",
  "iat": 1761728000,
  "jti": "bWJq",
  "sid": "08a5019c-17e1-4977-8f42-65a12843ea02",
  "sub": "248289761001",
  "events": { "http://schemas.openid.net/event/backchannel-logout": {} }
}
```

Header `typ` should be `logout+jwt`. Validation: signature, `iss`, `aud`, `iat`, `jti` not seen before, the
`events` member present, and **no `nonce`** (its presence would mean someone is replaying an ID token as a logout
token). Then delete every app session with that `sid` (or for that `sub` if no `sid`), and revoke refresh tokens
tied to it. That's why the session row stores `idp_sid` (ch 02 §3).

Design implications:

- The app must be reachable from the IdP (a public or VPN-routable endpoint). For apps behind NAT, fall back to short
  app sessions plus periodic `prompt=none` or refresh-token checks.
- Logout is **eventually consistent**: access tokens already issued remain valid until `exp`. Pick access-token
  lifetimes accordingly, or push revocation to resource servers via Shared Signals/CAEP (ch 10 §6).
- A full "log out of everything" for a user = IdP session kill + back-channel logout to all RPs + refresh-token
  revocation + (optionally) CAEP `session-revoked` events.

---

## 8. Multi-tenant IdPs and issuer validation

Microsoft Entra ID, and multi-tenant setups of Auth0/Okta/Keycloak, let one app accept users from many tenants. The
pitfall: discovery for the multi-tenant endpoint (`https://login.microsoftonline.com/common/v2.0/...`) returns an
issuer **template** like `https://login.microsoftonline.com/{tenantid}/v2.0`, and **every tenant's tokens are
signed with the same keys**. Signature validation alone accepts a token from *any* Entra tenant in the world,
including one the attacker created in five minutes.

Rules:

1. Resolve the template: `iss` must equal `https://login.microsoftonline.com/{tid}/v2.0` where `tid` is the token's
   tenant claim, **and** `tid` must be in your allowlist of customer tenants (or you must map `(tid, oid/sub)` to an
   onboarded customer).
2. Key users on `(iss, sub)` or `(tid, oid)`, never on email or `preferred_username` (mutable, user- or
   admin-controlled; the nOAuth class from ch 01 §6).
3. Single-tenant apps should use their tenant-specific endpoints, not `common`.

The same applies to any SaaS IdP that serves many customers from one key set: always pin the issuer *and* the
tenant.

---

## 9. Account linking and identifiers

Users arrive with several identities: Google today, Microsoft SSO tomorrow, a password from 2019. Rules:

- Internal model: `user (id)` ← `identity (iss, sub, user_id, linked_at, last_login)`, many identities per user.
- **Link only when the user is authenticated with both** (logged into the existing account, then performs the new
  login), or when the new IdP is authoritative for the verified email domain under an explicit enterprise
  configuration.
- **Never auto-link on email match** from a non-authoritative IdP. That turns any IdP that lets a user claim an
  email into a takeover vector.
- Keep the email as a contact attribute, not an identifier; allow it to change.
- For B2B, an enterprise customer enabling SSO usually wants **existing** password accounts with their domain
  converted to SSO-only. Do it as an admin action with domain verification (DNS TXT), then force those users through
  SSO on next login.

---

## 10. Other OIDC parts worth knowing

| Spec | What | When you meet it |
|---|---|---|
| Discovery 1.0 | `/.well-known/openid-configuration` | Always (ch 06) |
| Dynamic Client Registration 1.0 | Clients register via API | Multi-tenant platforms, MCP (deprecated there in favor of CIMD, ch 13) |
| CIBA Core 1.0 | Decoupled flow: client asks AS to authenticate a user on their phone | Call centers, agents needing approval (ch 07 §9, ch 15 §5) |
| OpenID Federation 1.0 (final, Feb 2026) | Trust chains between entities via signed entity statements | National/sector federations (ch 10 §8) |
| FAPI 2.0 | High-security profile (banking) | Open banking (ch 07 §10) |
| Native SSO for Mobile Apps | Share login across apps from the same vendor | Mobile app suites |
| Self-Issued OP v2 / OpenID4VP | Wallet-based login | Verifiable credentials (ch 16) |
| RP Metadata Choices 1.0 | RP can list several supported options in metadata | Recent final spec, interop polish |

---

## 11. Code: callback handler with full ID token validation

Uses the ch 04 `start_login`/`handle_callback` (extended with a `nonce`) and PyJWT.

```python
import secrets, time, jwt
from jwt import PyJWKClient

OIDC = {  # from https://login.example.com/.well-known/openid-configuration (ch 06)
    "issuer": "https://login.example.com",
    "jwks_uri": "https://login.example.com/.well-known/jwks.json",
}
CLIENT_ID = "web-app"
jwks = PyJWKClient(OIDC["jwks_uri"], lifespan=600, cooldown_duration=60)

def add_nonce(session: dict) -> str:
    nonce = secrets.token_urlsafe(16)
    session["oauth"]["nonce"] = nonce
    return nonce          # include as &nonce= in the authorization URL

def validate_id_token(id_token: str, expected_nonce: str, max_age: int | None = None,
                      required_acr: set[str] | None = None) -> dict:
    key = jwks.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token, key.key,
        algorithms=["RS256", "ES256"],          # what you registered for this client
        audience=CLIENT_ID,
        issuer=OIDC["issuer"],
        leeway=30,
        options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
    )
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(aud) > 1 and claims.get("azp") != CLIENT_ID:
        raise PermissionError("azp mismatch")
    if not secrets.compare_digest(claims["nonce"], expected_nonce):
        raise PermissionError("nonce mismatch")
    if max_age is not None:
        if "auth_time" not in claims or claims["auth_time"] + max_age < time.time():
            raise PermissionError("authentication too old")
    if required_acr and claims.get("acr") not in required_acr:
        raise PermissionError("insufficient authentication level")
    return claims

def login_user(db, claims: dict):
    ident = db.identities.get(iss=claims["iss"], sub=claims["sub"])   # never by email
    if ident is None:
        user = db.users.create(email=claims.get("email") if claims.get("email_verified") else None)
        ident = db.identities.create(iss=claims["iss"], sub=claims["sub"], user_id=user.id)
    return ident.user_id
```

Back-channel logout endpoint:

```python
def backchannel_logout(logout_token: str, sessions, seen_jti) -> int:
    header = jwt.get_unverified_header(logout_token)
    if header.get("typ") not in (None, "logout+jwt"):            # typ is RECOMMENDED by the spec
        raise PermissionError("wrong typ")
    key = jwks.get_signing_key_from_jwt(logout_token)
    c = jwt.decode(logout_token, key.key, algorithms=["RS256", "ES256"],
                   audience=CLIENT_ID, issuer=OIDC["issuer"], leeway=30,
                   options={"require": ["iss", "aud", "iat", "jti", "events"]})
    if "nonce" in c:
        raise PermissionError("nonce not allowed in logout token")
    if "http://schemas.openid.net/event/backchannel-logout" not in c["events"]:
        raise PermissionError("not a logout event")
    if not seen_jti.add_if_absent(c["jti"], ttl=600):
        raise PermissionError("replay")
    if "sid" in c:
        return sessions.destroy_by_idp_sid(c["sid"])
    if "sub" in c:
        return sessions.destroy_all_for(c["iss"], c["sub"])
    raise PermissionError("need sid or sub")
```

Return `200` on success, `400` on failure, and send `Cache-Control: no-store`.

---

## 12. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Logging in with a bare access token + `/me` | Token substitution | Validate ID token `aud` = your client |
| Users keyed by email | Account takeover, broken on email change | `(iss, sub)` |
| Sending ID tokens to APIs | Audience confusion | Access tokens with API audience |
| Skipping `nonce` | ID token replay/injection | Always send and check |
| Multi-tenant signature-only validation | Any tenant can log in | Pin issuer + tenant allowlist |
| Logout that only clears app cookie | IdP session silently logs user back in | RP-initiated logout + back-channel |
| Relying on front-channel logout iframes | Broken by cookie restrictions | Back-channel logout |
| Auto-linking accounts on email match | Takeover via weaker IdP | Link only with both authenticated |
| `prompt=none` iframes for session checks | Broken under third-party cookie blocking | Refresh tokens / BFF / back-channel |

---

## 13. Interview questions

**Q1. Why can't you use OAuth access tokens for login?**
Access tokens are addressed to resource servers, not the client; a token issued to another client for the same user
can be replayed into your app (token substitution). OIDC's ID token has `aud` = your client, a `nonce` bound to your
request, and authentication context.

**Q2. What is the `nonce` for if you already use `state` and PKCE?**
`state` protects the redirect step (CSRF) and PKCE binds the code to the client instance. `nonce` binds the *ID
token itself* to the session that started the login, so an ID token obtained elsewhere (logs, another flow) can't
be injected. In the code flow with PKCE the residual risk is small, but `nonce` is cheap and required by many IdPs.

**Q3. Design logout across 12 apps sharing one IdP so that disabling a user ends access within 5 minutes.**
Back-channel logout from IdP to every app (kill sessions by `sid`/`sub`); refresh tokens tied to IdP session and
revoked; access tokens ≤5 minutes; optional CAEP `session-revoked` push to APIs; apps unreachable for back-channel
get ≤5-minute sessions with periodic refresh checks.

**Q4. A multi-tenant SaaS accepts "Sign in with Microsoft". What must it validate beyond the signature?**
The issuer resolved for the token's `tid`, the `tid` against onboarded customers, `aud`, and it must key on
`(tid, oid)` or `(iss, sub)`, never on `email`/`preferred_username`.

---

## Sources

- OpenID Connect Core 1.0 (incorporating errata): https://openid.net/specs/openid-connect-core-1_0.html
- OpenID Connect Discovery 1.0: https://openid.net/specs/openid-connect-discovery-1_0.html
- OpenID Connect RP-Initiated Logout 1.0: https://openid.net/specs/openid-connect-rpinitiated-1_0.html
- OpenID Connect Back-Channel Logout 1.0: https://openid.net/specs/openid-connect-backchannel-1_0.html
- OpenID Connect Front-Channel Logout 1.0: https://openid.net/specs/openid-connect-frontchannel-1_0.html
- RFC 8176, Authentication Method Reference Values: https://datatracker.ietf.org/doc/html/rfc8176
- OpenID Connect for Identity Assurance 1.0: https://openid.net/specs/openid-connect-4-identity-assurance-1_0.html
- Microsoft identity platform, validating tokens and multi-tenant issuer handling: https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens
- OpenID Federation 1.0: https://openid.net/specs/openid-federation-1_0.html
