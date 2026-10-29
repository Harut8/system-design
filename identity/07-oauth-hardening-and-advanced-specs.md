# 07 — OAuth hardening and the advanced specs

> **Prerequisites:** [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) (the base flow and its
> attacks), [`06-well-known-discovery-and-metadata.md`](06-well-known-discovery-and-metadata.md) (capabilities are
> negotiated through metadata).
>
> **Feeds into:** [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md) and
> [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md)
> (token exchange, RAR, step-up and CIBA are the agent toolkit), [`12-workload-and-service-identity.md`](12-workload-and-service-identity.md)
> (mTLS-bound tokens).
>
> **THESIS:** plain OAuth 2.0 bearer tokens have three structural weaknesses: **anyone holding a token can use it**,
> **front-channel parameters can be tampered with or mixed up**, and **scopes are too coarse to express "this
> payment, this amount"**. The specs in this chapter each close one of these, and together (as profiled by FAPI 2.0)
> they are what "high-security OAuth" means. You do not need all of them everywhere. You need to know which attack each
> one stops, so you can buy exactly the protection your risk requires.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [RFC 9700: the threat model in one table](#1-rfc-9700-the-threat-model-in-one-table)
2. [Protecting the authorization request: PAR, JAR, JARM](#2-protecting-the-authorization-request-par-jar-jarm)
3. [Mix-up attacks and issuer identification (RFC 9207)](#3-mix-up-attacks-and-issuer-identification-rfc-9207)
4. [Audience restriction with resource indicators (RFC 8707)](#4-audience-restriction-with-resource-indicators-rfc-8707)
5. [Sender-constrained tokens: DPoP and mTLS](#5-sender-constrained-tokens-dpop-and-mtls)
6. [Fine-grained permissions: Rich Authorization Requests (RFC 9396)](#6-fine-grained-permissions-rich-authorization-requests-rfc-9396)
7. [Token exchange (RFC 8693): delegation, impersonation, down-scoping](#7-token-exchange-rfc-8693-delegation-impersonation-down-scoping)
8. [Introspection, revocation and step-up authentication](#8-introspection-revocation-and-step-up-authentication)
9. [CIBA: decoupled approval on another device](#9-ciba-decoupled-approval-on-another-device)
10. [FAPI 2.0: putting it together](#10-fapi-20-putting-it-together)
11. [Choosing what to adopt](#11-choosing-what-to-adopt)
12. [Code: DPoP proof creation and verification](#12-code-dpop-proof-creation-and-verification)
13. [Anti-patterns](#13-anti-patterns)
14. [Interview questions](#14-interview-questions)
15. [Sources](#sources)

---

## Start here — the whole chapter in plain words

A normal OAuth access token is like cash: whoever holds it can spend it. If it leaks through a log file, a proxy, or
malware, the thief uses it from anywhere. **DPoP** turns it into a card with a PIN: the token is tied to a private key
in the client, and every request must carry a fresh signature from that key. A stolen token alone is useless.

The authorization request travels through the browser, so an attacker can tamper with it. **PAR** moves the request
to a direct server-to-server call and sends only a short reference through the browser.

Clients that talk to several authorization servers can be tricked into sending a code from one to the other. The
**`iss` parameter** (RFC 9207) lets the client check who actually answered.

Scopes like `payments` are too broad. **Rich Authorization Requests** let the client ask for "one payment of €45.00 to
this IBAN", and the user sees exactly that on the consent screen.

When a service needs to call another service *for* a user, **token exchange** trades the incoming token for a new,
narrower one, recording who is acting for whom.

---

## 1. RFC 9700: the threat model in one table

RFC 9700 (*Best Current Practice for OAuth 2.0 Security*, January 2025) is the canonical list. Condensed:

| Attack | Mechanism | Required countermeasure |
|---|---|---|
| Authorization code interception | Code leaks via redirect, logs, malicious app | PKCE (S256) |
| Authorization code injection | Stolen code inserted into attacker's session | PKCE (or OIDC `nonce`) |
| CSRF on redirect | Attacker's code in victim's session | PKCE or `state` |
| Mix-up | Code sent to wrong AS | RFC 9207 `iss`, or per-AS redirect URIs |
| Redirect URI manipulation | Loose matching | Exact matching |
| Token leakage in URLs | Implicit grant, tokens in query | Code flow only; no tokens in URLs |
| Access token replay | Stolen bearer token | Sender constraining (DPoP, mTLS) where risk justifies |
| Access token privilege | Token valid at many RSes | Audience restriction (`resource`), minimal scopes |
| Refresh token theft (public clients) | Long-lived bearer RT | Rotation with reuse detection, or sender constraining |
| Resource owner password grant | Credential exposure | Must not be used |
| Open redirectors | Chained to leak codes | No open redirects on client or AS |
| Clickjacking of consent | Framed AS pages | `frame-ancestors` CSP on AS |
| Client impersonation | Weak client auth | Asymmetric client authentication |

The rest of this chapter is the "how" for the rows that need more than one line.

---

## 2. Protecting the authorization request: PAR, JAR, JARM

> **In plain words.** The authorization request (`/authorize?client_id=...&scope=...&redirect_uri=...`) is assembled by
> the client but delivered by the browser, so anyone in the middle (malware, an extension, a user editing the URL) can
> change it, and it can grow past URL length limits. PAR makes the client hand the request to the AS directly.

**PAR, Pushed Authorization Requests (RFC 9126):**

```
Client ──POST /par (client auth + all authorization params)──▶ AS
Client ◀── {"request_uri": "urn:ietf:params:oauth:request_uri:6esc_11ACC5bwc014ltc14eY22c", "expires_in": 60}
Browser ──GET /authorize?client_id=web-app&request_uri=urn:...──▶ AS
```

What it buys:

- **Integrity and confidentiality** of the request: parameters never pass through the browser.
- **Client authentication before the user sees anything**: the AS knows the request came from the real client, so a
  phishing client can't start a flow using a legitimate `client_id` with a malicious `redirect_uri` (the redirect URI
  can even be unregistered and still be trusted because the client authenticated, per RFC 9126).
- **No URL size limit**: RAR `authorization_details` can be large.
- **Early rejection**: invalid requests fail at the back channel, not on a user-facing error page.

Cost: one extra round trip. FAPI 2.0 requires PAR; AS metadata `require_pushed_authorization_requests` can force it.

**JAR, JWT-Secured Authorization Request (RFC 9101):** the request parameters are a signed (optionally encrypted)
JWT, passed by value (`request=`) or by reference. Gives integrity and non-repudiation even when sent through the
browser. With PAR, JAR is less necessary; FAPI 2.0 Message Signing combines them for non-repudiation.

**JARM, JWT-Secured Authorization Response Mode:** the AS returns the authorization response (`code`, `state`, `iss`)
as a signed JWT, protecting it against tampering and providing issuer identification. RFC 9207 is the lightweight
alternative for the issuer part.

---

## 3. Mix-up attacks and issuer identification (RFC 9207)

> **In plain words.** A client that works with several authorization servers keeps state about "which one am I
> talking to in this flow". The mix-up attack confuses that state so the client sends an honest AS's code to the
> attacker's token endpoint.
>
> **Real-world example.** An MCP client (an AI app) talks to dozens of authorization servers chosen by MCP servers
> that users add, some of them malicious. That is exactly the multi-AS setting where mix-up applies, which is why the
> MCP 2026-07-28 revision made `iss` validation a MUST for clients.

The attack (simplified): the user starts a flow with attacker-AS; the attacker redirects the browser to honest-AS's
authorization endpoint with the client's `client_id` registered there; the user authenticates at honest-AS; the code
comes back to the client's redirect URI; the client still believes this flow belongs to attacker-AS and sends the
code (plus PKCE verifier) to attacker-AS's token endpoint. The attacker now holds a valid honest-AS code and verifier.

Fix (RFC 9207): the AS adds `iss=<issuer>` to every authorization response (including errors) and advertises
`authorization_response_iss_parameter_supported: true`. The client:

1. Records the expected issuer **before** redirecting, in the same per-flow record as the PKCE verifier.
2. On the response, compares `iss` with the record using **exact string comparison** (no case folding, no trailing
   slash or default-port normalization; MCP spells this out).
3. If metadata says `iss` is supported and it's absent, or if it differs, rejects the response without displaying the
   error parameters (an attacker could inject misleading error text).

Alternative without RFC 9207: a distinct redirect URI per AS, and checking that the callback path matches the flow's AS.

---

## 4. Audience restriction with resource indicators (RFC 8707)

By default an access token's audience is whatever the AS decides, often "all APIs of this AS". A token for the
calendar API that also works at the payroll API turns a leak at the least sensitive service into a breach of the most
sensitive one.

RFC 8707 adds `resource=<absolute URI>` to authorization and token requests. The AS issues tokens whose `aud` is that
resource; the client can request separate tokens for separate resources using the same refresh token:

```
POST /token
grant_type=refresh_token&refresh_token=...&resource=https://calendar.example/api
POST /token
grant_type=refresh_token&refresh_token=...&resource=https://payroll.example/api
```

Each RS checks `aud` contains its own identifier (ch 03 §6). MCP makes `resource` mandatory: clients MUST send the
canonical MCP server URI in both requests, and servers MUST reject tokens not issued for them (ch 13 §5).

---

## 5. Sender-constrained tokens: DPoP and mTLS

> **In plain words.** A sender-constrained token contains a fingerprint of a public key. The resource server accepts
> it only from a caller who proves they hold the matching private key. Steal the token without the key and you get
> nothing.

### 5.1 DPoP (RFC 9449)

The client generates a key pair (ideally non-extractable: WebCrypto with `extractable: false`, Android Keystore,
Secure Enclave, a TPM). On each request to the token endpoint **and** to resource servers, it sends a **DPoP proof**: a
fresh JWT signed with the private key.

```
DPoP proof header: {"typ": "dpop+jwt", "alg": "ES256", "jwk": {public key}}
DPoP proof claims: {"jti": "unique", "htm": "GET", "htu": "https://api.example/photos",
                    "iat": 1761724800, "ath": base64url(SHA-256(access_token)),   ← on RS calls
                    "nonce": "server-provided"}                                    ← if the server demands one
```

Flow:

1. Token request carries `DPoP: <proof>`. The AS binds the issued token to the key: `cnf.jkt` = JWK SHA-256
   thumbprint (RFC 7638) and returns `token_type: DPoP`.
2. API request: `Authorization: DPoP <access_token>` and `DPoP: <proof>` with `ath`.
3. RS verifies: the proof's signature with the embedded `jwk`; `typ`; `htm`/`htu` match this request; `iat` fresh;
   `jti` not seen recently; `ath` matches the token; **thumbprint of `jwk` equals the token's `cnf.jkt`**.
4. Server nonces (`DPoP-Nonce` response header, `use_dpop_nonce` error) limit pre-generated proofs.

What DPoP stops: replay of a leaked access or refresh token from another machine. What it doesn't stop: an attacker
who runs code *in* the client (XSS, malware in the process) and asks it to sign, which is why non-extractable keys and
the BFF pattern still matter. DPoP also binds **refresh tokens** for public clients, which RFC 9700 accepts instead of
rotation.

### 5.2 mTLS (RFC 8705)

The client authenticates with an X.509 certificate in the TLS handshake; the AS puts the certificate's SHA-256
thumbprint in `cnf.x5t#S256`; the RS checks the presented client certificate matches. Strong and simple between
servers you control; awkward for browsers and mobile apps, and broken by TLS-terminating proxies unless they forward
the certificate (with a trusted header) to the application.

| | DPoP | mTLS |
|---|---|---|
| Works in browsers/SPAs | Yes (WebCrypto) | Poorly (client certs UX) |
| Works through TLS-terminating LBs | Yes (application layer) | Needs cert forwarding |
| Per-request cost | Sign + verify a JWT, `jti` cache | TLS handshake (amortized) |
| Typical | Public clients, mobile, SPAs, agents | Server-to-server, banking, service mesh |

---

## 6. Fine-grained permissions: Rich Authorization Requests (RFC 9396)

Scopes are flat strings; real permissions have structure. RAR adds `authorization_details`, a JSON array of typed
objects:

```json
"authorization_details": [{
  "type": "payment_initiation",
  "actions": ["initiate"],
  "locations": ["https://bank.example/payments"],
  "instructedAmount": {"currency": "EUR", "amount": "45.00"},
  "creditorName": "Merchant A",
  "creditorAccount": {"iban": "DE02100100109307118603"}
}]
```

- The AS renders consent from it ("Pay €45.00 to Merchant A"), the user approves, and the issued token (or its
  introspection response) carries the approved `authorization_details`.
- The RS enforces the details: amount, creditor, action.
- Types are defined by your API; register the schemas and validate them at the AS.
- Because the payload can be large, send it via PAR.

RAR is the natural shape for agent permissions ("may book a flight under €400 on 2026-11-12", ch 15 §4).

---

## 7. Token exchange (RFC 8693): delegation, impersonation, down-scoping

> **In plain words.** A service holding a token from its caller asks the AS for a *different* token: for another
> audience, with fewer permissions, or recording that it is acting on the caller's behalf.

```
POST /token
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<the user's access token>
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&actor_token=<the calling service's own token>              (optional: delegation)
&actor_token_type=urn:ietf:params:oauth:token-type:jwt
&resource=https://ledger.internal/api
&scope=ledger.read
```

Response contains `access_token` and `issued_token_type`. Two modes:

| Mode | Request | Resulting token | Use |
|---|---|---|---|
| **Impersonation** | No `actor_token` | `sub` = user; indistinguishable from the user | Legacy downstreams that only understand user tokens |
| **Delegation** | With `actor_token` | `sub` = user, `act: {"sub": "svc-orders"}` | Audit and policy can see both; **preferred** |

Nested `act` claims record chains (`act: {sub: B, act: {sub: A}}`); RFC 8693 says only the outermost actor is
relevant for access control, earlier ones are informational. `may_act` in the subject token states who is allowed to act
for the subject.

The AS **must** enforce policy at exchange time: is this actor allowed to exchange tokens for this subject; is the
requested audience allowed; is the requested scope a subset of the subject token's (no up-scoping)? Token exchange
without that policy is a privilege-escalation endpoint.

Where you meet it: microservice call chains propagating user identity (ch 12 §7), cloud workload identity federation
(STS endpoints are token exchanges), MCP servers calling upstream APIs on the user's behalf instead of passing tokens
through (ch 13 §5), agents and sub-agents (ch 14), ID-JAG (ch 10 §7).

---

## 8. Introspection, revocation and step-up authentication

**Introspection (RFC 7662):** `POST /introspect token=...` (with RS authentication) → `{"active": true, "scope": ...,
"sub": ..., "aud": ..., "exp": ..., "cnf": ...}` or `{"active": false}`. Use for opaque tokens or when an RS needs
real-time revocation status. Cache positive results for seconds (that's your revocation lag); never cache past `exp`.
RFC 9701 lets the AS return a signed JWT introspection response.

**Revocation (RFC 7009):** `POST /revoke token=...&token_type_hint=refresh_token`. Revoking a refresh token should
revoke the access tokens derived from it where the AS can (opaque tokens: immediate; JWTs: at expiry). Call it on
logout, on "disconnect app", and when a client detects compromise.

**Step-up authentication (RFC 9470):** an RS signals that the token's authentication is too weak or too old for this
operation:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer error="insufficient_user_authentication",
  error_description="A different authentication level is required",
  acr_values="urn:example:phr", max_age="300"
```

The client starts a new authorization request with `acr_values` / `max_age`; the AS re-authenticates the user; the new
token carries `acr` and `auth_time` that the RS checks. Compare with MCP's **scope** step-up (`403 insufficient_scope`
with `scope=`), which asks for more *permission* rather than stronger *authentication* (ch 13 §6). An agent may need
both.

---

## 9. CIBA: decoupled approval on another device

OpenID Connect **Client-Initiated Backchannel Authentication** lets a client ask the AS to authenticate and get
consent from a user **who isn't at the client's screen**:

```
Client ──POST /bc-authorize (client auth, login_hint=alice, binding_message="Refund #4711 €80",
                             scope / authorization_details)──▶ AS
Client ◀── {auth_req_id, expires_in, interval}
AS ──push──▶ Alice's phone: "Support agent bot asks: Refund #4711 €80. Approve?"
Client polls /token (grant_type=urn:openid:params:grant-type:ciba, auth_req_id) — or receives ping/push callback
Client ◀── tokens after Alice approves
```

Use cases: a call-center agent asks the customer to approve on their banking app; an **AI agent running in the
background** needs a human to approve a sensitive action (ch 15 §5). `binding_message` shows the same short text on both
sides so the user knows which request they're approving. Risks: approval fatigue (like push MFA), so rate-limit,
make messages specific, and never use CIBA approval for "approve everything for 30 days".

---

## 10. FAPI 2.0: putting it together

The OpenID Foundation's **FAPI 2.0 Security Profile** (final, February 2025) is "OAuth for when money moves". It
profiles the specs above into one interoperable set, and has a formal attacker model behind it:

- Authorization code flow only, **PKCE S256**, **PAR required**.
- Confidential clients only, authenticated with **`private_key_jwt` or mTLS**.
- Sender-constrained access tokens: **DPoP or mTLS**.
- `iss` in authorization responses (RFC 9207).
- Strict redirect URI handling, short code lifetime (≤60 s).
- Allowed algorithms PS256, ES256, EdDSA (Ed25519).
- Refresh tokens: no rotation required because they are bound to an authenticated confidential client.

FAPI 2.0 Message Signing adds signed requests/responses for non-repudiation. Open banking regimes (UK, Brazil,
Australia CDR, Saudi Arabia, and others) build on FAPI profiles, with OpenID Foundation conformance tests and
certification.

---

## 11. Choosing what to adopt

| Your situation | Add on top of code+PKCE+exact redirects |
|---|---|
| Ordinary SaaS, first-party apps | `iss` (RFC 9207), audience-restricted tokens, refresh rotation, short ATs |
| Third-party developer ecosystem | + PAR, asymmetric client auth, consent with verified publisher |
| Mobile/SPA handling sensitive data | + DPoP (non-extractable key), or move to BFF |
| Service mesh / internal APIs | mTLS identities (ch 12) + token exchange for user context |
| Payments, health records, regulated | FAPI 2.0 (PAR, DPoP/mTLS, `private_key_jwt`, RAR) |
| AI agents acting for users | Token exchange with `act`, RAR for bounded actions, CIBA/step-up for approvals, DPoP for agent runtimes (ch 13–15) |
| Any client talking to many ASes (MCP clients) | RFC 9207 mandatory, per-AS client registration state |

---

## 12. Code: DPoP proof creation and verification

```python
import base64, hashlib, json, time, uuid
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def jwk_thumbprint(jwk: dict) -> str:
    # RFC 7638: required members only, lexicographic order, no whitespace.
    members = {k: jwk[k] for k in ("crv", "kty", "x", "y")}
    return b64url(hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode()).digest())

class DPoPClient:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())    # in a browser: WebCrypto, extractable=false
        self.jwk = json.loads(ECAlgorithm.to_jwk(self.key.public_key()))
        self.nonce: str | None = None

    def proof(self, method: str, url: str, access_token: str | None = None) -> str:
        claims = {"jti": str(uuid.uuid4()), "htm": method, "htu": url.split("?")[0].split("#")[0],
                  "iat": int(time.time())}
        if access_token:
            claims["ath"] = b64url(hashlib.sha256(access_token.encode()).digest())
        if self.nonce:
            claims["nonce"] = self.nonce
        return jwt.encode(claims, self.key, algorithm="ES256",
                          headers={"typ": "dpop+jwt", "jwk": self.jwk})

class DPoPVerifier:
    def __init__(self, jti_cache, max_age: int = 60):
        self.jti_cache, self.max_age = jti_cache, max_age

    def verify(self, proof: str, method: str, url: str, access_token: str, token_claims: dict) -> None:
        header = jwt.get_unverified_header(proof)
        if header.get("typ") != "dpop+jwt" or header.get("alg") not in ("ES256", "EdDSA", "PS256"):
            raise PermissionError("bad DPoP header")
        jwk = header.get("jwk") or {}
        if any(k in jwk for k in ("d", "p", "q")):                       # private key material
            raise PermissionError("private key in proof")
        public = ECAlgorithm.from_jwk(json.dumps(jwk))
        c = jwt.decode(proof, public, algorithms=[header["alg"]],
                       options={"require": ["jti", "htm", "htu", "iat"]})
        if c["htm"] != method or c["htu"] != url.split("?")[0].split("#")[0]:
            raise PermissionError("proof not for this request")
        if abs(time.time() - c["iat"]) > self.max_age:
            raise PermissionError("stale proof")
        if c.get("ath") != b64url(hashlib.sha256(access_token.encode()).digest()):
            raise PermissionError("proof not bound to this token")
        if token_claims.get("cnf", {}).get("jkt") != jwk_thumbprint(jwk):
            raise PermissionError("token not bound to this key")
        if not self.jti_cache.add_if_absent(c["jti"], ttl=self.max_age * 2):
            raise PermissionError("replayed proof")
```

The RS runs this **after** validating the access token itself (ch 03 §6) and requires it whenever the token has
`cnf.jkt` or the `Authorization` scheme is `DPoP`. Rejecting a DPoP-bound token sent as `Bearer` closes the
downgrade path.

---

## 13. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Accepting a DPoP-bound token with `Authorization: Bearer` and no proof | Binding bypassed | Require proof whenever `cnf` present |
| Token exchange endpoint without exchange policy | Any service can mint tokens for any user/audience | Explicit `(actor, subject, audience, scope)` policy |
| Up-scoping in token exchange | Escalation | Requested scope ⊆ subject token scope |
| Using impersonation by default | Audit can't tell service from user | Delegation with `act` |
| Introspection cache longer than revocation SLO | Revoked tokens keep working | Cache seconds, honor `exp` |
| RAR details not enforced at RS | Consent theatre | RS checks amount/creditor/action |
| CIBA approvals with vague `binding_message` | Users approve the wrong thing | Specific, human-readable, matching on both sides |
| Multi-AS client without `iss` checks | Mix-up | RFC 9207 + per-AS state |
| Extractable DPoP keys in `localStorage` | XSS steals key + token | Non-extractable WebCrypto keys in IndexedDB |

---

## 14. Interview questions

**Q1. What does DPoP protect against and what doesn't it?**
It prevents use of a stolen access or refresh token by anyone without the client's private key. It doesn't protect
against an attacker executing inside the client who can request signatures, and it requires RSes to verify proofs and
track `jti`s.

**Q2. When would you use PAR?**
When request integrity matters (high-value flows), when requests are large (RAR), or when you want the AS to
authenticate the client before user interaction. FAPI 2.0 requires it.

**Q3. Service A receives a user's token and must call service B. What are the options, and which do you pick?**
Pass the token through (only if B is in the token's audience, generally avoid); call B with A's own identity (loses
user context, creates a deputy); or token exchange for a B-audience token with `sub`=user and `act`=A, down-scoped.
Pick token exchange, with AS policy on which actors may exchange for which audiences.

**Q4. Explain the mix-up attack and two mitigations.**
A multi-AS client is tricked into sending a code from honest-AS to attacker-AS's token endpoint. Mitigate with RFC 9207
`iss` in the response checked against the recorded issuer, or distinct redirect URIs per AS.

---

## Sources

- RFC 9700, Best Current Practice for OAuth 2.0 Security: https://datatracker.ietf.org/doc/html/rfc9700
- RFC 9126, OAuth 2.0 Pushed Authorization Requests: https://datatracker.ietf.org/doc/html/rfc9126
- RFC 9101, JWT-Secured Authorization Request (JAR): https://datatracker.ietf.org/doc/html/rfc9101
- JWT Secured Authorization Response Mode (JARM): https://openid.net/specs/oauth-v2-jarm.html
- RFC 9207, OAuth 2.0 Authorization Server Issuer Identification: https://datatracker.ietf.org/doc/html/rfc9207
- RFC 8707, Resource Indicators for OAuth 2.0: https://datatracker.ietf.org/doc/html/rfc8707
- RFC 9449, OAuth 2.0 Demonstrating Proof of Possession (DPoP): https://datatracker.ietf.org/doc/html/rfc9449
- RFC 8705, OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens: https://datatracker.ietf.org/doc/html/rfc8705
- RFC 9396, OAuth 2.0 Rich Authorization Requests: https://datatracker.ietf.org/doc/html/rfc9396
- RFC 8693, OAuth 2.0 Token Exchange: https://datatracker.ietf.org/doc/html/rfc8693
- RFC 7662 (Introspection), RFC 7009 (Revocation), RFC 9701 (JWT introspection response)
- RFC 9470, OAuth 2.0 Step Up Authentication Challenge Protocol: https://datatracker.ietf.org/doc/html/rfc9470
- OpenID Connect CIBA Core 1.0: https://openid.net/specs/openid-client-initiated-backchannel-authentication-core-1_0.html
- FAPI 2.0 Security Profile: https://openid.net/specs/fapi-security-profile-2_0-final.html
