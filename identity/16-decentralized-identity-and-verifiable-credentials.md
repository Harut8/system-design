# 16 — Decentralized identity and verifiable credentials

> **Prerequisites:** [`03-jwt-jose-and-signing-keys.md`](03-jwt-jose-and-signing-keys.md) (JWS, key binding),
> [`05-openid-connect.md`](05-openid-connect.md), [`06-well-known-discovery-and-metadata.md`](06-well-known-discovery-and-metadata.md)
> (issuer metadata).
>
> **Feeds into:** [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md) (when to accept wallet
> credentials).
>
> **THESIS:** federated login (ch 05) makes the identity provider a participant in every login: it learns where you sign in
> and can be compelled or breached. Verifiable credentials invert that: an **issuer** signs claims once into a
> **wallet** the user holds; the user **presents** them to verifiers, revealing only what's needed, without the issuer
> learning of the presentation. The cryptography is mature; what decides success is **trust frameworks** (which issuers a
> verifier accepts), **revocation**, **holder binding**, and **wallet availability**. In 2026 this is no longer
> speculative: SD-JWT is an RFC, the OpenID4VC protocols are final, ISO mobile driving licences ship in phones, and the
> EU requires member states to offer digital identity wallets.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The issuer–holder–verifier model](#1-the-issuerholderverifier-model)
2. [Credential formats: SD-JWT VC, mdoc, W3C VC](#2-credential-formats-sd-jwt-vc-mdoc-w3c-vc)
3. [Selective disclosure with SD-JWT (RFC 9901)](#3-selective-disclosure-with-sd-jwt-rfc-9901)
4. [Protocols: OpenID4VCI, OpenID4VP, the Digital Credentials API](#4-protocols-openid4vci-openid4vp-the-digital-credentials-api)
5. [Trust, revocation and holder binding](#5-trust-revocation-and-holder-binding)
6. [DIDs: where they fit and where they don't](#6-dids-where-they-fit-and-where-they-dont)
7. [Deployments: mDL, EUDI wallet, workforce credentials](#7-deployments-mdl-eudi-wallet-workforce-credentials)
8. [When to use verifiable credentials versus OIDC](#8-when-to-use-verifiable-credentials-versus-oidc)
9. [Code: verifying an SD-JWT presentation (simplified)](#9-code-verifying-an-sd-jwt-presentation-simplified)
10. [Anti-patterns](#10-anti-patterns)
11. [Interview questions](#11-interview-questions)
12. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Think of a physical driving licence. The government (issuer) gives it to you once. When a bar wants to know you're over 18,
you show it; the government isn't called, and doesn't learn you went to a bar. But the bartender also sees your address
and exact birth date, which they don't need.

A **verifiable credential** is the digital version, held in a phone **wallet**. It's signed by the issuer, so the bar's
app can check it's genuine. With **selective disclosure** you can show only "age over 18: true" and nothing else. And the
credential is tied to a key in your phone, so a copy of it is useless to someone else.

---

## 1. The issuer–holder–verifier model

```
Issuer (government, university, employer, bank)
   │  issues signed credential, bound to holder's key       (OpenID4VCI)
   ▼
Holder (wallet app on the user's device, keys in secure hardware)
   │  presents selected claims + proof of key possession    (OpenID4VP / ISO 18013-5 / Digital Credentials API)
   ▼
Verifier (website, app, border control, bar)
      checks: issuer signature, issuer is trusted (trust list), not revoked, holder binding, freshness (nonce)
```

Comparison with federated login:

| | Federated (OIDC) | Verifiable credentials |
|---|---|---|
| Issuer online at use time | Yes, every login | No |
| Issuer learns where you log in | Yes | No (with care about revocation checks) |
| Selective disclosure | IdP decides claims per client | Holder chooses per presentation |
| Offline / in-person | No | Yes (ISO mDL proximity) |
| Revocation | Session/token based | Status lists, short validity |
| Maturity for web login | Very high | Emerging; strong for high-assurance attribute proofs |

---

## 2. Credential formats: SD-JWT VC, mdoc, W3C VC

| Format | Encoding | Selective disclosure | Where used |
|---|---|---|---|
| **SD-JWT VC** (IETF draft profile on RFC 9901) | JWT + salted hashed disclosures | Yes (per claim) | EUDI wallet PID and attestations, many new deployments |
| **mdoc / mDL** (ISO/IEC 18013-5, 18013-7) | CBOR/COSE, Mobile Security Object with digests | Yes (per data element) | Mobile driving licences, EUDI PID, Apple/Google wallet IDs |
| **W3C Verifiable Credentials Data Model 2.0** | JSON-LD, secured with Data Integrity proofs or JOSE/COSE | With BBS+ cryptosuites (unlinkable) or SD-JWT securing | Education, research, some government |

The format choice is mostly made for you by the ecosystem (EUDI: SD-JWT VC and mdoc; driving licences: mdoc).

---

## 3. Selective disclosure with SD-JWT (RFC 9901)

RFC 9901 (November 2025) defines how to make individual claims of a JWS-signed JSON object disclosable or not.

Issuance:

1. For each selectively disclosable claim, the issuer creates a **disclosure**: `base64url(JSON [salt, name, value])`,
   e.g. `["2GLC42sKQveCfGfryNRN9w", "birthdate", "1990-04-12"]`.
2. It puts `SHA-256(disclosure)` (base64url) into an `_sd` array in the payload instead of the claim, and signs the JWT.
3. The holder receives `<issuer-JWT>~<disclosure1>~<disclosure2>~...~`.

Presentation:

1. The holder sends the issuer JWT plus **only the disclosures it chooses**.
2. With **key binding**: the issuer JWT contains `cnf` (the holder's public key); the holder appends a **KB-JWT** signed
   with that key over `nonce`, `aud` (the verifier), `iat` and `sd_hash` (hash of the presented SD-JWT), proving
   possession and freshness and preventing replay to another verifier.
3. The verifier checks the issuer signature, hashes each presented disclosure and checks it appears in `_sd`, reconstructs
   the visible claims, and verifies the KB-JWT.

Salts prevent guessing hidden values from their hashes (without salt, `hash(["birthdate","1990-04-12"])` could be
brute-forced). Decoy digests hide how many claims exist. **Linkability**: the same issuer signature is presented to every
verifier, so colluding verifiers can link presentations; issuers mitigate with **batch issuance** (many single-use copies).
BBS-based W3C credentials give unlinkability cryptographically at higher complexity.

---

## 4. Protocols: OpenID4VCI, OpenID4VP, the Digital Credentials API

- **OpenID for Verifiable Credential Issuance (OpenID4VCI) 1.0** (final, 2025): an OAuth-protected credential endpoint.
  Issuer metadata at `/.well-known/openid-credential-issuer`; pre-authorized code flow (issuer already knows you, e.g.
  via a QR code after in-person proofing) or authorization code flow; the wallet proves key possession and receives the
  credential.
- **OpenID for Verifiable Presentations (OpenID4VP) 1.0** (final, July 2025): the verifier sends a request (with
  **DCQL**, the Digital Credentials Query Language, describing which credentials and claims it needs), the wallet returns
  a presentation; supports same-device and cross-device (QR) flows, and requests signed by the verifier so the wallet can
  show who is asking.
- **HAIP** (OpenID4VC High Assurance Interoperability Profile 1.0, final): profiles the above for high-assurance use
  (EUDI).
- **W3C Digital Credentials API**: browser API (`navigator.credentials.get({digital: {...}})`) letting a website request a
  credential from wallets installed on the device, with the OS mediating wallet choice; carries OpenID4VP or ISO
  18013-7 requests. Shipping in Chrome and Safari in 2025 releases (check current status); it replaces fragile
  custom-scheme deep links and QR-code hand-offs.

---

## 5. Trust, revocation and holder binding

- **Trust**: a verifier must decide which issuers it accepts. Mechanisms: **trust lists** (EU trusted lists for EUDI
  providers), **IACA** root certificates for mDL issuers (AAMVA's Digital Trust Service in North America), **OpenID
  Federation** trust chains (ch 10 §8), X.509 certificate chains in issuer metadata. A valid signature from an unknown
  issuer is worth nothing.
- **Revocation and status**: **Token Status List** (IETF OAuth WG draft): the issuer publishes a compressed bitstring; each
  credential references its index; verifiers fetch the list (cacheable, so the issuer doesn't learn which credential was
  checked). Alternatives: short-lived credentials re-issued frequently, W3C Bitstring Status List.
- **Holder binding**: `cnf` key in the credential, private key in the device's secure element / TEE; KB-JWT or mdoc device
  signature at presentation. Wallets add **wallet attestation** (the wallet app proves it's a certified wallet) so issuers
  only issue high-assurance credentials into trustworthy wallets.
- **Verifier authentication**: wallets should only release data to verifiers that authenticate (signed requests,
  registered relying parties), and show the user who's asking and why.

---

## 6. DIDs: where they fit and where they don't

**Decentralized Identifiers** (W3C DID Core 1.0) are URIs (`did:method:...`) that resolve to a DID document with public
keys. `did:web:example.com` resolves via `https://example.com/.well-known/did.json`, i.e. it is **domain-anchored** like
everything in ch 06; `did:key` embeds the key itself; ledger-based methods anchor on blockchains.

The large deployments (EUDI, mDL) mostly don't depend on DIDs: they use X.509 and trust lists. Treat DIDs as one way to
name and find issuer keys, not as a prerequisite, and be wary of methods whose resolution depends on infrastructure you
can't evaluate.

---

## 7. Deployments: mDL, EUDI wallet, workforce credentials

- **Mobile driving licences (ISO 18013-5)** are live in many US states and other countries, in Apple Wallet, Google Wallet
  and state apps; accepted at some airport security checkpoints and increasingly online via 18013-7 / the Digital
  Credentials API.
- **EU Digital Identity Wallet**: the revised eIDAS regulation (eIDAS 2.0, Regulation (EU) 2024/1183) requires member states
  to offer wallets to citizens by 2026 under the implementing acts and the Architecture and Reference Framework (ARF),
  carrying person identification data (PID) and attestations in SD-JWT VC and mdoc formats, with OpenID4VCI/VP and HAIP.
  Large-scale pilots ran 2023–2025; regulated relying parties (banks, large platforms) are required to accept the wallet
  for strong authentication in the following years. Check national timelines.
- **Workforce and B2B**: employment credentials, verified-employee proofs for help-desk identity verification (a direct
  defence against the help-desk social-engineering pattern of ch 01 §7), and onboarding KYC re-use.

---

## 8. When to use verifiable credentials versus OIDC

| Need | Use |
|---|---|
| Users log in to your app every day | OIDC / passkeys |
| Prove an attribute from an authoritative source once (age, licence, degree, employment, KYC) | Verifiable credentials |
| In-person or offline verification | mdoc proximity |
| Minimize data you collect and store (GDPR) | VC with selective disclosure (e.g. `age_over_18`) |
| High-assurance identity proofing for account recovery | VC from a government wallet, plus liveness where required |
| You are the issuer of record (university, employer) | Issue VCs via OpenID4VCI alongside your OIDC IdP |

Data minimization is the underrated benefit: verifying `age_over_18: true` from a trusted issuer means you never store a
birth date or a document scan.

---

## 9. Code: verifying an SD-JWT presentation (simplified)

Illustrative only: production verifiers should use a maintained SD-JWT library and also handle nested/array disclosures,
decoys, status lists and trust frameworks.

```python
import base64, hashlib, json, time
import jwt

def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def b64url_sha256(s: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(s.encode()).digest()).rstrip(b"=").decode()

def verify_sd_jwt(presentation: str, issuer_key, trusted_issuers: set[str],
                  expected_aud: str, expected_nonce: str) -> dict:
    parts = presentation.split("~")
    issuer_jwt, disclosures, kb_jwt = parts[0], [d for d in parts[1:-1] if d], parts[-1]
    claims = jwt.decode(issuer_jwt, issuer_key, algorithms=["ES256"],
                        options={"verify_aud": False, "require": ["iss", "_sd_alg", "cnf"]})
    if claims["iss"] not in trusted_issuers:
        raise PermissionError("issuer not in trust list")
    if claims["_sd_alg"] != "sha-256":
        raise PermissionError("unsupported digest alg")
    digests = set(claims.get("_sd", []))
    revealed = {}
    for d in disclosures:
        if b64url_sha256(d) not in digests:
            raise PermissionError("disclosure not committed by issuer")
        salt, name, value = json.loads(b64url_decode(d))
        if name in ("_sd", "...") or name in claims:
            raise PermissionError("illegal disclosure name")
        revealed[name] = value
    # Key binding: proves the presenter holds the key in cnf, for this verifier, now.
    holder_key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(claims["cnf"]["jwk"]))
    kb = jwt.decode(kb_jwt, holder_key, algorithms=["ES256"], audience=expected_aud,
                    options={"require": ["iat", "nonce", "sd_hash"]})
    if jwt.get_unverified_header(kb_jwt).get("typ") != "kb+jwt":
        raise PermissionError("wrong KB-JWT typ")
    if kb["nonce"] != expected_nonce or abs(time.time() - kb["iat"]) > 300:
        raise PermissionError("stale or replayed presentation")
    if kb["sd_hash"] != b64url_sha256(presentation[: presentation.rfind("~") + 1]):
        raise PermissionError("KB-JWT not bound to this presentation")
    visible = {k: v for k, v in claims.items() if k not in ("_sd", "_sd_alg", "cnf")}
    return {**visible, **revealed}
```

---

## 10. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Accepting any validly signed credential | Self-issued fakes | Trust lists / federation |
| No nonce/audience in presentations | Replay to other verifiers | KB-JWT / device signature with nonce + aud |
| Requesting full credentials when one attribute suffices | Over-collection, GDPR exposure | Request specific claims (DCQL), derived claims like `age_over_18` |
| Online revocation checks that call the issuer per presentation | Issuer tracks usage | Status lists, short-lived credentials |
| Storing presented credentials "for audit" | Becomes a PII honeypot | Store verification result, issuer, time, minimal claims |
| Building login on VCs before users have wallets | No adoption | OIDC/passkeys for login; VCs for attribute proofs |

---

## 11. Interview questions

**Q1. How does SD-JWT hide claims while letting the issuer's signature cover them?**
The issuer signs salted hashes of each disclosure; the holder reveals chosen disclosures; the verifier hashes them and finds
the digests in the signed payload. Salts stop guessing; key binding stops replay.

**Q2. Why doesn't a verifiable-credential verifier need to contact the issuer?**
The credential carries the issuer's signature and the verifier has the issuer's key via a trust framework; revocation uses
cached status lists or short validity. This preserves privacy and works offline.

**Q3. Where would you use VCs in a B2B SaaS today?**
Identity verification for account recovery and help-desk calls, KYC/age attributes without storing documents, and possibly
issuing credentials (certifications, employment) to users. Keep daily login on OIDC/passkeys.

---

## Sources

- RFC 9901, Selective Disclosure for JWTs (SD-JWT): https://www.rfc-editor.org/rfc/rfc9901
- SD-JWT-based Verifiable Credentials (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-sd-jwt-vc/
- OpenID for Verifiable Credential Issuance 1.0: https://openid.net/specs/openid-4-verifiable-credential-issuance-1_0.html
- OpenID for Verifiable Presentations 1.0: https://openid.net/specs/openid-4-verifiable-presentations-1_0.html
- OpenID4VC High Assurance Interoperability Profile 1.0: https://openid.net/specs/openid4vc-high-assurance-interoperability-profile-1_0.html
- W3C Digital Credentials API: https://www.w3.org/TR/digital-credentials/
- W3C Verifiable Credentials Data Model 2.0: https://www.w3.org/TR/vc-data-model-2.0/
- Token Status List (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-status-list/
- ISO/IEC 18013-5:2021 (mDL): https://www.iso.org/standard/69084.html
- EU Digital Identity Wallet Architecture and Reference Framework: https://eu-digital-identity-wallet.github.io/eudi-doc-architecture-and-reference-framework/
- Regulation (EU) 2024/1183 (eIDAS 2.0): https://eur-lex.europa.eu/eli/reg/2024/1183/oj
