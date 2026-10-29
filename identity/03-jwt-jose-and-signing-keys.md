# 03 — JWT, JOSE and signing keys

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (§3: the verifier's checks).
>
> **Feeds into:** [`04`](04-oauth2-foundations-and-flows.md) and [`05`](05-openid-connect.md) (access and ID tokens
> are usually JWTs), [`06`](06-well-known-discovery-and-metadata.md) (JWKS discovery),
> [`08`](08-storing-credentials-and-tokens-at-rest.md) (where signing keys live), [`15`](16-decentralized-identity-and-verifiable-credentials.md)
> (SD-JWT).
>
> **THESIS:** a JWT is a signed JSON object, nothing more. It is neither encrypted nor revocable by default, and its
> header is attacker-controlled input. Nearly every JWT vulnerability comes from letting that header decide how the
> token is verified. **Pin the algorithm, the issuer, the audience and the key source in the verifier's
> configuration; treat everything inside the token as a claim to check, never as an instruction.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The JOSE family](#1-the-jose-family)
2. [Anatomy of a JWT](#2-anatomy-of-a-jwt)
3. [Registered claims and what each one defends against](#3-registered-claims-and-what-each-one-defends-against)
4. [Algorithms: which to use and why](#4-algorithms-which-to-use-and-why)
5. [The classic JWT attacks](#5-the-classic-jwt-attacks)
6. [The verification checklist](#6-the-verification-checklist)
7. [JWKS, key IDs and key rotation](#7-jwks-key-ids-and-key-rotation)
8. [JWE: when you actually need encryption](#8-jwe-when-you-actually-need-encryption)
9. [JWT versus opaque tokens](#9-jwt-versus-opaque-tokens)
10. [Code: a strict verifier and a rotating signer](#10-code-a-strict-verifier-and-a-rotating-signer)
11. [Anti-patterns](#11-anti-patterns)
12. [Interview questions](#12-interview-questions)
13. [Sources](#sources)

---

## Start here — the whole chapter in plain words

A JWT (pronounced "jot") is three base64url pieces joined by dots: `header.payload.signature`. The payload is
plain JSON anyone can read, for example `{"sub": "alice", "aud": "invoices-api", "exp": 1767225600}`. The
signature proves that whoever holds the issuer's private key produced exactly this header and payload.

A service that receives a JWT checks: is the signature valid with the issuer's public key; is the issuer one I
trust; is the token meant for me (`aud`); has it expired (`exp`)? If all pass, it believes the claims inside.

The danger: the header says which algorithm and key to use (`"alg": "RS256", "kid": "key-7"`), and the attacker
writes the header. Libraries that obeyed it accepted unsigned tokens (`"alg": "none"`), or verified an RSA
token with an HMAC using the *public* key as the secret. Your verifier must decide the algorithm itself.

---

## 1. The JOSE family

JOSE = JSON Object Signing and Encryption, an IETF working group that produced:

| Spec | RFC | What |
|---|---|---|
| JWS | 7515 | Signed content (compact `a.b.c` or JSON serialization) |
| JWE | 7516 | Encrypted content (compact form has 5 parts) |
| JWK | 7517 | A key as JSON; JWK Set (JWKS) = `{"keys": [...]}` |
| JWA | 7518 | Algorithm names (`RS256`, `ES256`, `EdDSA`, `A256GCM`, ...) |
| JWT | 7519 | Claims set conventions, carried in a JWS or JWE |
| JWT BCP | 8725 | Best current practices for JWT (read it; it lists the attacks below) |
| JWK thumbprint | 7638 | A stable hash of a public key (used by DPoP) |
| JWT access token profile | 9068 | Standard claims and `typ: at+jwt` for OAuth access tokens |
| SD-JWT | 9901 | Selective disclosure (ch 16) |

**Why JSON and not XML?** SAML's XML signatures need canonicalization (XML-DSig C14N), which spawned a decade of
signature-wrapping bugs (ch 10 §2). JWS signs the exact base64url bytes, so there is nothing to canonicalize: the
verifier checks the bytes it received. That design choice is the main reason JWT displaced SAML assertions for
new protocols.

---

## 2. Anatomy of a JWT

```
eyJhbGciOiJFUzI1NiIsImtpZCI6IjIwMjYtMTAiLCJ0eXAiOiJhdCtqd3QifQ   ← header
.eyJpc3MiOiJodHRwczovL2xvZ2luLmV4YW1wbGUuY29tIiwic3ViIjoi...       ← payload
.MEUCIQD...                                                        ← signature
```

```json
// header
{ "alg": "ES256", "kid": "2026-10", "typ": "at+jwt" }
// payload
{
  "iss": "https://login.example.com",
  "sub": "248289761001",
  "aud": "https://api.example.com/invoices",
  "client_id": "web-app",
  "scope": "invoices.read",
  "iat": 1761724800, "nbf": 1761724800, "exp": 1761725700,
  "jti": "8f1c0e6a-...",
  "tenant": "acme"
}
```

The signature is computed over `ASCII(base64url(header) || "." || base64url(payload))`. Note what this implies:

- The payload is **readable by anyone** who holds the token (browser, logs, proxies). Never put secrets or
  sensitive PII in a JWS.
- **Size**: each claim adds bytes to every request. A token with 200 group names in it can exceed header size
  limits (8–16 KB on many proxies). Put large or volatile data behind a lookup, not in the token.

---

## 3. Registered claims and what each one defends against

| Claim | Meaning | Attack it prevents if checked |
|---|---|---|
| `iss` | Issuer | Accepting tokens from an issuer you don't trust (or from another tenant of a multi-tenant IdP) |
| `sub` | Subject; unique **within** `iss` | Identity confusion; always key users on `(iss, sub)` |
| `aud` | Intended recipient(s) | Cross-service replay: a token for API A used at API B |
| `exp` | Expiry | Indefinite replay of stolen tokens |
| `nbf` | Not before | Use of pre-issued tokens |
| `iat` | Issued at | Detecting tokens issued before a password change / revocation event |
| `jti` | Unique ID | Replay (if you track used `jti`s for one-time tokens) |
| `typ` (header) | Token type, e.g. `at+jwt`, `JWT`, `dpop+jwt` | **Cross-JWT confusion**: an ID token accepted as an access token, a DPoP proof accepted as something else |
| `azp` | Authorized party (OIDC) | Which client the ID token was issued to, when `aud` has several values |
| `cnf` | Confirmation (key binding) | Use of a sender-constrained token without the key (ch 07) |
| `act` | Actor (RFC 8693) | Losing track of delegation (ch 13) |

**Explicit typing** (RFC 8725 §3.11) is the cheap, under-used defense: every kind of JWT your system issues
should have a distinct `typ`, and every verifier should require the one it expects. Otherwise any JWT signed by
the same key (ID tokens, logout tokens, email-verification tokens) is a candidate access token.

---

## 4. Algorithms: which to use and why

| `alg` | Type | Key | Verdict |
|---|---|---|---|
| `HS256` | HMAC-SHA256 | Shared secret | Only when issuer and verifier are **the same service**. Anyone who can verify can also forge |
| `RS256` | RSA PKCS#1 v1.5 + SHA-256 | RSA ≥2048 | Universally supported; large signatures (256 B at 2048-bit); fine |
| `PS256` | RSA-PSS | RSA ≥2048 | Preferred RSA mode (FAPI 2.0 allows PS256, ES256, EdDSA) |
| `ES256` | ECDSA P-256 + SHA-256 | EC P-256 | Small (64 B), fast to sign; needs good randomness per signature; good default |
| `EdDSA` (Ed25519) | Edwards-curve | Ed25519 | Deterministic, fast, small; check library and IdP support |
| `none` | Unsigned | — | Never accept |

**Why asymmetric for anything distributed:** with HMAC every verifier holds the signing secret, so every
verifier can mint tokens for every other verifier. With RS/ES/EdDSA the issuer holds the private key and
verifiers get public keys from a JWKS URL. The verifier set can grow without growing the set of parties that can
forge.

**Implementation pitfalls by algorithm:**
- ECDSA needs a fresh unpredictable nonce per signature; reused or biased nonces leak the private key (the PS3
  key leak, 2010). Use a library with RFC 6979 deterministic nonces or a KMS.
- ECDSA verifiers must reject `r = 0` or `s = 0`. Java 15–18's "psychic signatures" bug (CVE-2022-21449)
  accepted an all-zero ES256 signature for any message.
- Compare HMACs in constant time.

---

## 5. The classic JWT attacks

### 5.1 `alg: none`

The header says `"alg": "none"`, the signature part is empty, and a library that dispatches on the header
accepts it as valid. Fix: the verifier passes an explicit algorithm allowlist; `none` is never in it.

### 5.2 Algorithm confusion (RS256 → HS256)

```
Attacker takes the server's RSA *public* key (public, in the JWKS),
writes header {"alg": "HS256"}, and computes HMAC-SHA256(public_key_bytes, header.payload).
A library that does verify(token, key) and lets the header pick the algorithm
uses the public key bytes as the HMAC secret → valid.
```

Fix: bind algorithm to key. Each key in your verifier config has exactly one allowed `alg`; use a library API
that takes the algorithm list explicitly.

### 5.3 `kid` injection

`kid` is a string the attacker controls. Seen in the wild: `kid` used as a file path (`../../dev/null` → empty
key), in an SQL query, or in a shell command. Fix: `kid` is only ever a lookup key into an in-memory map of
keys you loaded from a trusted source.

### 5.4 `jku` / `x5u` / embedded `jwk`

These header parameters tell the verifier where to fetch the key, or include the key itself. Following them
means the attacker supplies their own key. Fix: ignore them; keys come from configuration or from the
issuer's discovery document (ch 06), never from the token.

### 5.5 Unverified decode

`jwt.decode(token, options={"verify_signature": False})` used to "just read the user ID" and then trusted.
Search your codebase for it. Reading claims before verification is fine only for routing (e.g. pick the issuer
config by `iss`), and the token must then be fully verified with *that issuer's* pinned settings.

### 5.6 Cross-service and cross-type confusion

A valid token for the wrong audience, or an ID token sent as an access token. Fix: check `aud` and `typ` every
time; issue different `typ`s.

### 5.7 Multi-tenant issuer confusion

Multi-tenant IdPs (Entra ID's `common` endpoint, Auth0/Okta custom domains) sign tokens for **all** tenants with
the same keys. Validating only the signature against the shared JWKS accepts tokens from any tenant in the world.
Fix: validate `iss` (and the tenant ID claim, e.g. `tid`) against your allowlist, not just the signature (ch 05 §8).

### 5.8 Key compromise

Real-world: in 2023 an attacker group (Storm-0558) obtained a Microsoft consumer signing key and forged tokens
that Exchange Online accepted for enterprise mailboxes, because a validation path accepted the consumer key for
enterprise tokens. Lessons: scope keys to issuers and audiences, keep signing keys in HSMs, and make rotation
routine so an emergency rotation is not your first one.

---

## 6. The verification checklist

For every incoming JWT, in this order:

1. **Parse** strictly: three parts, valid base64url, valid JSON, size limit (e.g. 8 KB).
2. **Select issuer config** by expected issuer (or by `iss`, matched against an allowlist).
3. **`typ`** equals the expected type (`at+jwt` for RFC 9068 access tokens).
4. **`alg`** is in the allowlist **for this issuer** (never `none`).
5. **Key**: look up `kid` in that issuer's cached JWKS; if unknown, refresh JWKS once (rate-limited), then fail.
6. **Signature** verifies.
7. **`iss`** equals the configured issuer exactly (string comparison, no normalization).
8. **`aud`** contains this service's identifier.
9. **`exp`** > now − skew, **`nbf`** ≤ now + skew, **`iat`** not in the future; skew ≤ 60 s.
10. **Binding**: if `cnf` present, require and verify DPoP proof or mTLS certificate (ch 07 §5).
11. **Revocation hooks** (optional): `iat` after user's `tokens_valid_after`; `jti` not in denylist.
12. **Then** authorize on `sub`, `scope`, `client_id`, tenant, `act`.

---

## 7. JWKS, key IDs and key rotation

The issuer publishes public keys at `jwks_uri` (found via discovery, ch 06):

```json
{ "keys": [
  { "kty": "EC", "crv": "P-256", "kid": "2026-10", "use": "sig", "alg": "ES256", "x": "...", "y": "..." },
  { "kty": "EC", "crv": "P-256", "kid": "2026-07", "use": "sig", "alg": "ES256", "x": "...", "y": "..." }
]}
```

**Rotation without downtime** follows from one constraint: verifiers cache the JWKS, so a new key must be
published *before* it signs anything, and an old key must stay published until every token it signed has expired.

```
t0  publish K2 in JWKS (K1 still signing)          ← wait ≥ max JWKS cache TTL
t1  start signing with K2                          ← wait ≥ max token lifetime (incl. refresh-issued ID tokens)
t2  remove K1 from JWKS
```

Verifier side:
- Cache the JWKS respecting `Cache-Control` with a floor and ceiling (e.g. 5 min to 24 h).
- On unknown `kid`, refetch once, **rate-limited** (e.g. at most once per minute per issuer), or an attacker
  sending random `kid`s turns every request into a JWKS fetch.
- On fetch failure, keep serving with the last known good JWKS.

**Emergency rotation** (key leaked): remove the key from JWKS immediately and accept that verifiers will reject
tokens signed by it as soon as their cache expires; all sessions relying on those tokens must refresh. This is
why short JWKS cache TTLs are a security property, not just an availability one.

Where the private key lives: an HSM or cloud KMS (sign via API, the key never leaves), covered in ch 08 §7.

---

## 8. JWE: when you actually need encryption

JWE encrypts the payload to a recipient's public key (`alg` = key management such as `RSA-OAEP-256` or
`ECDH-ES`; `enc` = content encryption such as `A256GCM`). Use it when a token must transit an untrusted party
(the browser, a partner) and carries data that party must not read, e.g. an ID token containing regulated PII.

Most of the time you don't need it: if the client must not read the token, give it an **opaque reference token**
and let the API introspect (§9). Nested JWT (sign, then encrypt) is the correct order when you need both.

---

## 9. JWT versus opaque tokens

| | JWT access token | Opaque access token + introspection (RFC 7662) |
|---|---|---|
| Verify | Local signature check | Call AS `/introspect` (cache result for seconds) |
| Revocation | Wait for `exp` (or denylist) | Immediate |
| Privacy | Claims visible to client | Claims hidden |
| AS load | Issuance only | Every validation (minus cache) |
| Cross-org APIs | Easy | Each RS needs introspection credentials |
| Typical | Internal microservices, high QPS | Public APIs, high-risk scopes, third-party clients |

A common hybrid is the **phantom token** pattern: clients outside get opaque tokens; the API gateway introspects
once and forwards a short-lived internal JWT to services behind it. Clients can't read claims; internal services
verify locally.

---

## 10. Code: a strict verifier and a rotating signer

```python
import jwt                                  # PyJWT 2.x (PyJWKClient cooldown_duration needs a recent 2.x)
from jwt import PyJWKClient, InvalidTokenError

class IssuerConfig:
    def __init__(self, issuer: str, jwks_uri: str, audience: str,
                 algorithms: list[str], typ: str = "at+jwt", leeway: int = 30):
        self.issuer, self.audience, self.algorithms = issuer, audience, algorithms
        self.typ, self.leeway = typ, leeway
        # Caches the JWKS for `lifespan` seconds; on an unknown kid it refetches once,
        # but not more often than every `cooldown_duration` seconds (random-kid DoS guard).
        self.jwks = PyJWKClient(jwks_uri, cache_jwk_set=True, lifespan=600,
                                cooldown_duration=60, timeout=5)

class Verifier:
    def __init__(self, issuers: dict[str, IssuerConfig], max_len: int = 8192):
        self.issuers, self.max_len = issuers, max_len

    def verify(self, token: str) -> dict:
        if len(token) > self.max_len:
            raise InvalidTokenError("token too large")
        header = jwt.get_unverified_header(token)
        # Reading iss before verification is only for picking a config from an allowlist.
        iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
        cfg = self.issuers.get(iss)
        if cfg is None:
            raise InvalidTokenError("untrusted issuer")
        if header.get("typ", "").lower() not in (cfg.typ, "application/" + cfg.typ):
            raise InvalidTokenError("wrong token type")
        if header.get("alg") not in cfg.algorithms:          # never 'none', never header-chosen
            raise InvalidTokenError("algorithm not allowed")
        try:
            key = cfg.jwks.get_signing_key_from_jwt(token)    # kid is only a map lookup
        except jwt.PyJWKClientError as e:
            raise InvalidTokenError("unknown signing key") from e
        return jwt.decode(
            token, key.key,
            algorithms=cfg.algorithms,                        # pinned per issuer
            audience=cfg.audience,
            issuer=cfg.issuer,
            leeway=cfg.leeway,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
```

Signer side with an overlap window (keys would live in a KMS in production; generated here for clarity):

```python
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
import json, time, uuid, jwt

class KeyRing:
    def __init__(self):
        self.keys: list[tuple[str, ec.EllipticCurvePrivateKey, float]] = []  # (kid, key, created)
        self.active_kid: str | None = None

    def add_key(self) -> str:                 # step t0: publish before use
        kid = time.strftime("%Y-%m-%d") + "-" + uuid.uuid4().hex[:6]
        self.keys.append((kid, ec.generate_private_key(ec.SECP256R1()), time.time()))
        return kid

    def activate(self, kid: str) -> None:     # step t1: after verifiers' JWKS cache TTL has passed
        self.active_kid = kid

    def retire(self, kid: str) -> None:       # step t2: after the max token lifetime has passed
        assert kid != self.active_kid
        self.keys = [k for k in self.keys if k[0] != kid]

    def jwks(self) -> dict:
        out = []
        for kid, key, _ in self.keys:
            jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
            jwk.update(kid=kid, use="sig", alg="ES256")
            out.append(jwk)
        return {"keys": out}

    def sign(self, claims: dict, typ: str = "at+jwt") -> str:
        kid, key, _ = next(k for k in self.keys if k[0] == self.active_kid)
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid, "typ": typ})
```

---

## 11. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| `algorithms` taken from the token header | `none` / confusion attacks | Pinned allowlist per issuer |
| HS256 shared across many services | Every verifier can forge | Asymmetric signing, JWKS |
| No `aud` check | Cross-service replay | Always check `aud` |
| Same key and no `typ` for ID, access, reset, logout tokens | Token type confusion | Distinct `typ` (and ideally keys) per token kind |
| PII or secrets in JWT payload | Readable by client and logs | Opaque token, or JWE, or lookup |
| 24-hour JWT access tokens with no revocation | Day-long revocation lag | 5–15 min + refresh tokens / introspection |
| Fetching JWKS on every request | Latency, IdP rate limits, DoS amplification | Cache + rate-limited refresh |
| Rotating by swapping the key in place | All in-flight tokens fail at once | Publish → wait → sign → wait → retire |
| Logging full tokens | Logs become a credential store | Log `jti`, `sub`, `client_id`, never the token |

---

## 12. Interview questions

**Q1. Explain the RS256→HS256 algorithm confusion attack and the fix.**
The verifier lets the token header choose the algorithm; the attacker selects HS256 and signs with the RSA public
key bytes as the HMAC secret; a naive `verify(token, key)` accepts. Fix: the verifier decides the algorithm per
key, from configuration.

**Q2. You need to revoke a user's access immediately but use JWT access tokens. Options?**
Short token lifetimes plus refresh-token revocation (lag = AT lifetime); a per-user `tokens_valid_after`
timestamp checked against `iat` (a small, cacheable lookup); a `jti` denylist distributed to verifiers; or
opaque tokens with introspection for high-risk APIs. Shared Signals/CAEP events (ch 10) can push revocation to
resource servers.

**Q3. Why is `typ` important?**
It prevents a validly signed token of one kind from being accepted as another kind when the same issuer key signs
several token types (RFC 8725 §3.11, RFC 9068 `at+jwt`).

**Q4. Describe zero-downtime signing key rotation.**
Publish the new public key first and wait for the maximum JWKS cache lifetime; switch signing; keep the old key
published until the longest-lived token signed by it expires; remove it. Verifiers refetch JWKS on unknown
`kid` with rate limiting.

---

## Sources

- RFC 7515 (JWS), 7516 (JWE), 7517 (JWK), 7518 (JWA), 7519 (JWT): https://datatracker.ietf.org/doc/html/rfc7519
- RFC 8725, JSON Web Token Best Current Practices: https://datatracker.ietf.org/doc/html/rfc8725
- RFC 9068, JWT Profile for OAuth 2.0 Access Tokens: https://datatracker.ietf.org/doc/html/rfc9068
- RFC 7638, JWK Thumbprint: https://datatracker.ietf.org/doc/html/rfc7638
- RFC 9901, Selective Disclosure for JWTs (SD-JWT): https://www.rfc-editor.org/rfc/rfc9901
- Microsoft, "Results of major technical investigations for Storm-0558 key acquisition" (2023): https://msrc.microsoft.com/blog/2023/09/results-of-major-technical-investigations-for-storm-0558-key-acquisition/
- CVE-2022-21449 (Java ECDSA "psychic signatures"): https://nvd.nist.gov/vuln/detail/CVE-2022-21449
- Auth0, "Critical vulnerabilities in JSON Web Token libraries" (2015, the original `none`/confusion write-up): https://auth0.com/blog/critical-vulnerabilities-in-json-web-token-libraries/
- PyJWT documentation: https://pyjwt.readthedocs.io/
