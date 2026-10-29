# Identity — Labs

A learn-by-doing task sheet for the 19 chapters of this track. Every task ends in an observed value, a failing-then-passing
test, or a captured request. Nothing here needs a cloud account.

- **Closed book first.** Answer each chapter's checkpoint from memory before rereading.
- **Predict before you run.** Write the prediction down, then the observation, then why they differ.
- **Capture the wire.** For every flow, save the raw HTTP requests and responses (browser devtools HAR or `mitmproxy`) in
  your lab notebook, **then delete the HAR files**: they contain live tokens (ch 17 §2.5).
- **Space the review.** Redo the checkpoints after 1 day, 1 week and 1 month.

## Setup

| Environment | Used by |
|---|---|
| Docker + Compose v2 | all |
| Python 3.12+ venv: `pyjwt[crypto] cryptography argon2-cffi httpx fastapi uvicorn keyring redis` | all |
| Keycloak 26.x in dev mode (compose below) | 02–07, 10, 11, 13, 14 |
| Redis 7 | 02, 08, 09 |
| Postgres 17 | 08, 11 |
| `mitmproxy` (optional) | 04–07 |
| An MCP client that supports remote servers with OAuth (any current AI desktop app or the MCP Inspector, run locally and patched) | 13–15 |

`compose.yaml`:

```yaml
name: idlab
services:
  keycloak:
    image: quay.io/keycloak/keycloak:26.4
    command: ["start-dev", "--features=dpop"]
    environment:
      KC_BOOTSTRAP_ADMIN_USERNAME: admin
      KC_BOOTSTRAP_ADMIN_PASSWORD: admin
    ports: ["8080:8080"]
  redis:
    image: redis:7
    ports: ["6379:6379"]
  postgres:
    image: postgres:17
    environment: {POSTGRES_PASSWORD: lab}
    ports: ["5432:5432"]
```

Create a realm `lab`, a user `alice`, a confidential client `web-app` and a public client `cli`. Pin the image tag you
actually pull and record it in your notebook; Keycloak feature flags change between releases.

---

## 00 — Mental models

- [ ] Pick three systems you use (a SaaS app, a CLI, a CI pipeline). For each, write the issuer, the audience, the
  credential type and where it's stored. **Verify:** for each you can name who could read the stored credential.
- [ ] Find one confused-deputy risk in a system you work on. **Verify:** a sentence of the form "service X calls Y with
  X's authority on behalf of user U, and Y can't tell".

**Checkpoint:** Why are ID tokens never sent to APIs? What is the revocation lag of a 15-minute JWT? Name the four
checks every verifier performs.

## 01 — Passwords, MFA, passkeys

- [ ] Time `argon2-cffi` at (m=19 MiB, t=2, p=1) and (m=64 MiB, t=3, p=4) on your laptop. **Predict** ms per hash first.
  **Verify:** table of ms and peak RSS for 50 concurrent hashes.
- [ ] Implement the ch 01 TOTP verifier with replay protection; test with the RFC 6238 vector and a replayed code.
  **Verify:** replay rejected.
- [ ] In Keycloak, enable WebAuthn passwordless for `alice`; register a passkey; then try logging in via
  `http://127.0.0.1:8080` instead of `localhost`. **Predict** the outcome. **Verify:** explain using RP ID rules.

**Checkpoint:** Why don't TOTP codes stop AitM phishing? What are NIST's minimum lengths? Why is hard lockout a DoS?

## 02 — Sessions and cookies

- [ ] Build the ch 02 `SessionStore` against Redis; add a login endpoint that rotates the session ID. **Verify:** a
  pre-login session ID no longer works after login (fixation test).
- [ ] Serve a page from `evil.localhost:8001` that auto-submits a POST to your app on `localhost:8000`, with `SameSite`
  absent, `Lax`, `Strict`. **Verify:** record which variants carry the cookie (mind that `*.localhost` and `localhost`
  are different sites, and check what your browser does with default-Lax).
- [ ] Put an access token in `localStorage` in a toy SPA, inject `<img src=x onerror=...>` that exfiltrates it to a local
  listener. Repeat with a BFF cookie. **Verify:** what the attacker gets in each case.

**Checkpoint:** What does `__Host-` enforce? Why does `HttpOnly` matter if XSS can still make requests? Name two CSRF
defenses besides SameSite.

## 03 — JWT and keys

- [ ] Run the ch 03 verifier against Keycloak's JWKS. Craft tokens with `alg: none`, an HS256 token signed with the RSA
  public key, a wrong `aud`, and `typ: JWT`. **Verify:** each rejected, with the reason logged.
- [ ] Rotation drill: with the ch 03 `KeyRing` serving a JWKS locally, start signing with a new key **before** verifiers
  refresh. **Predict** the failure window given `cooldown_duration`. **Verify:** measured window matches.
- [ ] Rotate Keycloak's realm keys (Realm settings → Keys) while a client keeps verifying tokens. **Verify:** no failures if
  you add the new key as active and keep the old one passive until old tokens expire.

**Checkpoint:** Why pin the algorithm per issuer? What's the safe order of key rotation steps? When would you use JWE?

## 04 — OAuth and PKCE

- [ ] Run the ch 04 client against Keycloak's `web-app` client. Capture authorize and token requests. **Verify:** you can
  label every parameter with the attack it prevents.
- [ ] Code interception: copy a code from the redirect and redeem it from another process without the verifier.
  **Verify:** `invalid_grant`.
- [ ] Remove `state` checking and perform login CSRF against yourself with a second Keycloak user. **Verify:** your
  session ends up bound to the other account. Restore the check.
- [ ] Enable refresh-token rotation (Keycloak: "Revoke Refresh Token", max reuse 0). Refresh twice in parallel.
  **Verify:** the second fails; explain the grace-period fix.
- [ ] CLI loopback flow with the `cli` public client on `http://127.0.0.1:{random}/callback`; store the refresh token with
  `keyring` (ch 09 §11.1).

**Checkpoint:** Why a code instead of a token in the redirect? What does PKCE stop that a client secret doesn't? List what
OAuth 2.1 removes.

## 05 — OpenID Connect

- [ ] Validate Keycloak ID tokens with the ch 05 `validate_id_token`, including `nonce` and `max_age`. **Verify:** stale
  `auth_time` rejected.
- [ ] Configure back-channel logout in Keycloak to your app; log out from the Keycloak account console. **Verify:** your
  app's session for that `sid` is gone within seconds.
- [ ] Multi-tenant trap: create a second realm, issue a token there, and point a verifier that only checks signature at
  both realms' keys. **Verify:** it accepts the wrong realm; then pin issuer and it doesn't.

**Checkpoint:** What does OIDC add to OAuth? Why key users on `(iss, sub)`? Which logout mechanism is reliable and why?

## 06 — `.well-known` discovery

- [ ] Fetch Keycloak's `openid-configuration`; list every field you'd use for a security decision. **Verify:** matches
  ch 06 §3 table.
- [ ] Run the ch 06 discovery client against: a path issuer (`/realms/lab`), a server serving a mismatched `issuer`, and
  `https://169.254.169.254/`. **Verify:** first succeeds via the correct URL order, others rejected.
- [ ] Serve an RFC 9728 document for a toy API and return `401` with `resource_metadata`. **Verify:** an MCP-capable client
  discovers the AS from it.

**Checkpoint:** Why does RFC 8414 insert the path while OIDC appends it? What makes it safe to try several metadata URLs?
What is the trust anchor of a well-known document?

## 07 — Hardening

- [ ] Enable DPoP in Keycloak for a client; obtain a DPoP-bound token with the ch 07 `DPoPClient`; replay the token with
  `Authorization: Bearer` and no proof against an RS using `DPoPVerifier`. **Verify:** rejected.
- [ ] Token exchange in Keycloak (standard token exchange): exchange `alice`'s token from `web-app` for a token with
  audience `orders-api` and a narrower scope. **Verify:** decoded claims, and that up-scoping is refused.
- [ ] Use PAR (`/protocol/openid-connect/ext/par/request`) and confirm the browser URL now carries only `request_uri`.

**Checkpoint:** What does DPoP not protect against? Delegation vs impersonation? Explain mix-up and RFC 9207.

## 08 — Storage at rest

- [ ] Implement `api_keys`, `one_time_tokens` and `connected_accounts` (ch 08 §8) in Postgres; run the §13 code.
  **Verify:** swapping two rows' ciphertexts makes decryption fail (AAD).
- [ ] Grep a running app's logs, Sentry-like error payloads and a `pg_dump` for any token value you issued. **Verify:** zero
  hits after adding redaction.
- [ ] Simulate two workers refreshing one connected account concurrently with and without `FOR UPDATE`. **Verify:** without
  the lock, the provider (Keycloak with rotation) revokes the family.

**Checkpoint:** Which secrets get Argon2id, which SHA-256, which encryption? What is AAD for? Name five leak paths outside the
main table.

## 09 — Client storage

- [ ] For your OS, store and read a token with `keyring`; then find where the plaintext fallback would go if no keyring
  existed. **Verify:** file permissions.
- [ ] Build the ch 09 `TokenCache` and hammer it with 1,000 concurrent `get()` calls at expiry. **Verify:** one fetch.
- [ ] Credential-injecting proxy: run a sandboxed script (container with no secrets) that calls an API through a proxy which
  adds `Authorization` for one host only. **Verify:** the script can't read the token and can't use it for other hosts.

**Checkpoint:** Rank `localStorage`, memory, `HttpOnly` cookie, IndexedDB non-extractable key for a SPA. Where do tokens
live in each of the four agent shapes?

## 10 — Enterprise SSO

- [ ] Configure a Keycloak realm as SAML IdP for a toy SP (use a maintained SAML library). Tamper with the assertion
  (change `NameID`, keep signature). **Verify:** rejected; then intentionally read attributes from the raw XML instead of the
  verified object and see the risk.
- [ ] Implement a SCIM `PATCH active=false` handler with the ch 10 cascade. **Verify:** sessions, refresh tokens and API
  keys of the user are all dead within one second.

**Checkpoint:** What is XSW? Why must an IdP only log users into its own tenant? List the deprovisioning cascade.

## 11 — Authorization

- [ ] Run the ch 11 PDP and toy ReBAC; write a generated matrix test (role × action × same/other tenant). **Verify:** all
  cross-tenant cases deny.
- [ ] Enable Postgres RLS (ch 11 §12.2); run queries with and without `SET LOCAL`, and through a pooled connection.
  **Verify:** no cross-tenant rows ever.
- [ ] Optional: model the same rules in OpenFGA or Cedar; run their test tooling.

**Checkpoint:** When RBAC → ReBAC? What's the new-enemy problem? Why 404 instead of 403 cross-tenant?

## 12 — Workload identity

- [ ] Run SPIRE quickstart (server + agent in Docker); fetch an X.509-SVID for a workload; inspect the URI SAN.
- [ ] Use the ch 12 `authorize_ci` against a GitHub Actions OIDC token (from a test repo's workflow that prints a token to a
  step output in a private repo, then revoke), or construct one with a local issuer. **Verify:** wrong environment rejected.
- [ ] Webhook verifier: rotate secrets with two active; send a replayed old request. **Verify:** rejected.

**Checkpoint:** What does node attestation prove vs workload attestation? What's the classic CI federation
misconfiguration? Options for propagating user context?

## 13–15 — MCP and agents

- [ ] Run the ch 13 FastAPI MCP guard with Keycloak as AS; point an MCP client at it. **Verify:** 401 → discovery → login →
  tool call works; `files.write` triggers a step-up 403 and a re-consent.
- [ ] Try to call the MCP server with a Keycloak token whose `aud` is another client. **Verify:** rejected.
- [ ] Implement the ch 14 exchange policy; register three agents for `alice`. **Verify:** same user, different agents,
  different allowed scopes; a sub-agent can't exceed its parent.
- [ ] Run the ch 15 executor with a fake payments tool that times out after performing the action. **Verify:** with the
  idempotency key, one payment; without it, two.
- [ ] Revoke `alice`'s consent mid-run. **Verify:** the next tool call stops the run with "re-authorization required",
  and the model never sees token details.

**Checkpoint:** Compare in-process, stdio and remote MCP trust models. Why is token passthrough forbidden? What must an
approval bind to? What are the three different 401/403 errors and reactions?

## 16–18 — VCs, products, platform

- [ ] Run the ch 16 SD-JWT verifier with a self-made issuer; reveal only `age_over_18`. **Verify:** hidden claims absent,
  wrong nonce rejected.
- [ ] Using ch 17 §9, fetch discovery documents from three real IdPs (e.g. Google, Microsoft common, your Keycloak). **Verify:**
  you can name issuer, JWKS URI, PKCE support and DPoP support for each.
- [ ] Do the ch 18 capacity math for your own product's numbers. **Verify:** peak hashing cores and refresh QPS written down.

**Checkpoint:** When VCs instead of OIDC? The org vs custom authorization server trap in Okta? The three numbers that drive
identity capacity planning?

---

## Capstones

**A. Production-grade login for a SaaS (1–2 weeks).** BFF + Keycloak, passkeys + TOTP, back-channel logout, refresh rotation,
SCIM deprovisioning, RLS tenant isolation, audit events, CI with secret scanning and authorization matrix tests.

**B. Agent platform identity (2 weeks).** Agent registry, token exchange with `act`, MCP server (ch 13 guard) behind an MCP
gateway that exchanges per backend, token vault for a third-party OAuth provider, approval service bound to argument hashes,
kill switch drill: suspend an agent mid-run and measure time to full stop.

**C. Break it.** Take capstone A and, with a teammate, run the attacks of chapters 02–07 and 10 against it. Keep a table:
attack, expected defense, observed result, fix.
