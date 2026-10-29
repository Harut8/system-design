# 08 — Storing credentials and tokens at rest: hashing, encryption, and the mistakes everyone makes

> **Prerequisites:** [`01-passwords-mfa-and-passkeys.md`](01-passwords-mfa-and-passkeys.md) (what passwords, TOTP
> seeds and passkeys are), [`02-sessions-cookies-and-browser-security.md`](02-sessions-cookies-and-browser-security.md)
> (session IDs), [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) (codes, refresh tokens,
> client secrets).
>
> **Feeds into:** [`09-where-clients-keep-tokens.md`](09-where-clients-keep-tokens.md) (the client side of the same
> question), [`12-workload-and-service-identity.md`](12-workload-and-service-identity.md) (API keys and secrets
> managers), [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md)
> (token vaults for agents), [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md).
>
> **THESIS:** for every secret you store, answer two questions: **do I ever need to read it back**, and **how much
> entropy does it have**? Those two answers fully determine the storage method. Verify-only, low entropy (passwords):
> slow salted hash. Verify-only, high entropy (session IDs, API keys, refresh tokens, reset links): a fast hash is
> correct and a slow one is waste. Must read back (third-party OAuth tokens, TOTP seeds, webhook secrets): authenticated
> encryption with keys held in a KMS, never next to the data. Public (passkey public keys): store as is. Nearly every
> credential leak in production is not a broken algorithm but a **copy**: the token in a log line, a backup, a CDC
> stream, an error tracker, an analytics replica, or, now, an LLM prompt and its trace.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The decision table: what to do with each kind of secret](#1-the-decision-table-what-to-do-with-each-kind-of-secret)
2. [Hashing versus encryption versus encoding](#2-hashing-versus-encryption-versus-encoding)
3. [Passwords: slow, salted, memory-hard](#3-passwords-slow-salted-memory-hard)
4. [High-entropy tokens: why a fast hash is right](#4-high-entropy-tokens-why-a-fast-hash-is-right)
5. [Secrets you must read back: envelope encryption](#5-secrets-you-must-read-back-envelope-encryption)
6. [API keys: format, storage and lifecycle](#6-api-keys-format-storage-and-lifecycle)
7. [Signing keys and other private keys](#7-signing-keys-and-other-private-keys)
8. [Schemas: a reference identity data model](#8-schemas-a-reference-identity-data-model)
9. [Where secrets leak: the copies you forgot](#9-where-secrets-leak-the-copies-you-forgot)
10. [Agents, MCP and tools: storage-specific risks](#10-agents-mcp-and-tools-storage-specific-risks)
11. [Rotation, migration and incident response](#11-rotation-migration-and-incident-response)
12. [GDPR and data minimization for identity data](#12-gdpr-and-data-minimization-for-identity-data)
13. [Code: API keys, token hashing, envelope encryption, token vault](#13-code-api-keys-token-hashing-envelope-encryption-token-vault)
14. [Best and worst practices, side by side](#14-best-and-worst-practices-side-by-side)
15. [Interview questions](#15-interview-questions)
16. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Assume your database will be stolen one day: a backup bucket left public, an SQL injection, a replica an intern
copied to a laptop. The question is what the thief can do with it.

- **Passwords** you never need to see again. You only check "does what the user typed match?". So store a
  **hash**, a one-way fingerprint. Because people pick guessable passwords, the hash must be deliberately **slow**
  (hundreds of milliseconds' worth of work and lots of memory) so a thief can't try billions of guesses.
- **Session IDs, API keys, password-reset tokens, refresh tokens** you also only check. But you generated them
  randomly with 256 bits, so nobody can guess them no matter how fast the hash. Store a plain **SHA-256** of them. A
  stolen table of SHA-256 hashes of random tokens is useless.
- **A user's Google refresh token** that your app (or AI agent) uses to read their calendar: you must *send* it to
  Google, so you need the original back. That calls for **encryption**, and the encryption key must live somewhere
  else (a key management service), so that stealing the database alone isn't enough.
- **Passkey public keys** are public. Store them as they are.

Then look for the copies: the same token printed in a log, captured by your error tracker, copied into a data
warehouse, or pasted into an LLM prompt that is now stored in a tracing tool. Those copies are how most tokens
actually leak.

---

## 1. The decision table: what to do with each kind of secret

| Secret | Read back? | Entropy | Store as | Notes |
|---|---|---|---|---|
| User password | No | Low | **Argon2id** (or scrypt/bcrypt) with per-user salt; optional pepper | §3 |
| PIN, security answer (avoid these) | No | Very low | Argon2id **plus** strict rate limiting | A 4-digit PIN falls to 10⁴ guesses regardless of hash |
| Recovery codes | No | Medium (≥50 bits) | Argon2id or HMAC-SHA-256 | Single-use; mark used |
| Session ID | No | High (256 bits) | **SHA-256** | §4; lookup by hash |
| Refresh token you issued | No | High | SHA-256 (+ family ID, rotation state) | Store `jti`/family for reuse detection |
| Authorization code | No | High | SHA-256, TTL ≤ 60 s, single use | Also store PKCE challenge, client, redirect URI |
| Password reset / magic link / email verify / invite token | No | High | SHA-256, TTL minutes, single use | Invalidate on password change |
| API key you issued | No | High | SHA-256 or HMAC-SHA-256 + visible prefix | §6 |
| OAuth client secret you issued | No | High | SHA-256 (better: no secrets, use `private_key_jwt`) | |
| Access tokens you issued (JWT) | No | — | **Don't store**; store `jti` + metadata if you need audit/denylist | |
| Access/refresh tokens you **hold** for third parties (Google, GitHub, Salesforce, upstream APIs for MCP/agents) | **Yes** | High | **AES-256-GCM envelope encryption**, KEK in KMS, AAD bound to row | §5, §10 |
| TOTP seed | **Yes** (to compute codes) | High | Envelope encryption | Hashing is impossible |
| Webhook signing secret (you sign outbound) | **Yes** | High | Envelope encryption | |
| Integration credentials (customer's DB password, SMTP password) | **Yes** | Varies | Envelope encryption, or store in a secrets manager and keep only a reference | |
| WebAuthn credential public key | Yes (public) | — | Plain | Integrity matters; protect against tampering via DB permissions |
| Your token-signing private key | Used, not read | — | **HSM / cloud KMS**, non-exportable | §7 |
| Your TLS / mTLS private keys | Used | — | KMS/HSM, or short-lived certs from SPIRE/mesh in memory | ch 12 |
| Encryption keys (DEKs) | Used | — | Wrapped by KEK in KMS; stored alongside data only in wrapped form | §5 |
| Sensitive PII (national ID, DOB) | Yes | Low | Field-level encryption + blind index if you need lookup | §12 |

---

## 2. Hashing versus encryption versus encoding

| | Hashing | Encryption | Encoding |
|---|---|---|---|
| Reversible | No | Yes, with the key | Yes, by anyone |
| Needs a key | No (HMAC: yes) | Yes | No |
| Use for | Verify-only secrets | Secrets you must use later | Transport format only |
| Examples | Argon2id, SHA-256, HMAC-SHA-256 | AES-256-GCM, ChaCha20-Poly1305 | base64, hex, URL-encoding |
| Classic mistake | Using a fast hash for passwords | Encrypting passwords (reversible = leakable) | "We store it base64-encoded" as if that were protection |

Three precise terms:

- **Salt**: random, per-record, stored with the hash. Defeats precomputed (rainbow) tables and makes identical
  passwords hash differently. Not secret.
- **Pepper**: one secret value, **not** stored in the database (in a KMS/HSM or app secret store), mixed in via HMAC.
  Makes a database-only leak unverifiable offline. Costs: rotation requires keeping old peppers or rehashing at login.
- **Key derivation (KDF)**: a function that turns a low-entropy input into a key or verifier with tunable cost
  (Argon2, scrypt, PBKDF2). Password hashing is a KDF use case.

---

## 3. Passwords: slow, salted, memory-hard

**Why slow.** A GPU computes on the order of 10⁹–10¹¹ SHA-256 hashes per second. Human passwords have maybe 20–40
bits of real entropy. That's seconds to minutes per password with a fast hash, and the whole breach in a day. A hash
tuned to ~100 ms and ~19–64 MiB per attempt cuts the attacker's rate by a factor of millions, and memory-hardness stops
GPUs/ASICs from parallelizing cheaply.

**Algorithm choice (OWASP Password Storage Cheat Sheet, current):**

| Algorithm | Minimum settings | When |
|---|---|---|
| **Argon2id** | m = 19 MiB, t = 2, p = 1 (or m = 46 MiB, t = 1, p = 1; or more memory, fewer iterations) | Default for new systems |
| scrypt | N = 2¹⁷, r = 8, p = 1 | If Argon2 unavailable |
| bcrypt | cost ≥ 10 | Legacy; **72-byte input limit** |
| PBKDF2-HMAC-SHA-256 | 600,000 iterations | FIPS-140 environments |

**Store the encoded string**, which carries algorithm, version, parameters and salt:

```
$argon2id$v=19$m=19456,t=2,p=1$c2FsdHNhbHRzYWx0$J7Y3...hash...
```

That lets you raise parameters later and **rehash on next successful login** (`check_needs_rehash`, ch 01 §9).

**Capacity planning matters.** At 19 MiB per hash, 200 concurrent logins hold ~3.8 GiB. A login burst (or a
credential-stuffing attack) can OOM your auth service. Put password hashing behind a bounded worker pool, rate-limit
before hashing, and size hosts for the peak. This is an availability attack surface: unauthenticated requests that cost
you 100 ms of CPU each.

**bcrypt's 72-byte trap.** bcrypt ignores input after 72 bytes. A "pre-hash" fix (`bcrypt(sha256(pw))`) must encode
the digest (base64), because raw digests contain NUL bytes that some bcrypt implementations treat as terminators,
shrinking the effective password. Easier: use Argon2id.

**Pepper, done properly:** `argon2id(HMAC-SHA-256(pepper, password))`, with the pepper in a KMS or HSM (or at least a
secrets manager, not the DB or the repo). Store a `pepper_version` column so you can rotate.

**Migrating legacy hashes** (MD5/SHA-1 from an acquired company): you can't wait for every user to log in. Wrap:
`new = argon2id(md5(password))` for all rows now (`hash_scheme = 'argon2(md5)'`), then on each login verify by
computing `argon2id(md5(input))` and replace it with a plain `argon2id(input)`. Every row is protected immediately.

---

## 4. High-entropy tokens: why a fast hash is right

> **In plain words.** Slow hashing exists to stop *guessing*. A 256-bit random token can't be guessed, so a slow hash
> only burns your CPU. A plain SHA-256 is enough to make a stolen table useless.

The pattern for any verify-only random token (session IDs, refresh tokens, reset links, API keys, auth codes):

1. Generate with a CSPRNG, ≥128 bits (prefer 256): `secrets.token_urlsafe(32)`.
2. Store `SHA-256(token)` (or `HMAC-SHA-256(server_key, token)`) in an indexed column.
3. On presentation, hash the input and **look up by hash**. The equality is done by the database index, so there is
   no meaningful timing side channel on the secret.
4. Enforce TTL, single use, and binding (user, client, purpose) in the row.

Why it matters: in 2022–2024 several breaches of SaaS support systems and CI platforms (e.g. the CircleCI incident in
January 2023, where an engineer's laptop malware stole a valid SSO session and the attacker then exfiltrated customer
secrets and environment variables) turned into customer-wide incidents because stored tokens and secrets were usable
by whoever obtained the data. If a table of session IDs or refresh tokens is stored hashed, a read of that table is not
a set of usable credentials.

HMAC instead of plain SHA-256 is a cheap upgrade: it also stops an attacker who can **write** to the table from
inserting a known token's hash (they'd need the HMAC key), and it stops offline confirmation of tokens found elsewhere.

What **not** to do: store the raw token "because we need to show it again". If a UI needs to show it, show it **once**
at creation. If the system needs to send it somewhere, it's not verify-only, use §5.

---

## 5. Secrets you must read back: envelope encryption

> **In plain words.** Encrypt each secret with a data key; encrypt (wrap) the data key with a master key that lives in
> a KMS and never leaves it. Stealing the database gives the attacker ciphertext and wrapped keys, which are useless
> without calling the KMS, and calling the KMS needs your service's identity and leaves an audit trail.

```
                 ┌──────────── KMS / HSM ────────────┐
                 │  KEK (key-encryption key), never   │
                 │  exported; IAM-controlled; audited │
                 └───────▲───────────────┬────────────┘
           wrap(DEK)     │               │ unwrap(wrapped DEK) → DEK (in memory, cached briefly)
                         │               ▼
plaintext secret ──AES-256-GCM(DEK, nonce, AAD)──▶ ciphertext ── stored in DB row with:
                                                    key_id, wrapped_dek (or tenant DEK ref), nonce, version
```

Design decisions:

| Decision | Recommendation | Why |
|---|---|---|
| Cipher | AES-256-GCM or ChaCha20-Poly1305 (AEAD) | Confidentiality **and** integrity; never ECB, never CBC without MAC |
| Nonce | 96-bit random per encryption (GCM) | Never reuse a nonce with the same key; random 96-bit nonces are fine up to ~2³² messages per key, so rotate DEKs |
| **AAD** (associated data) | Bind to `tenant_id`, `row_id`, `column`, `provider` | Stops ciphertext **swapping**: an attacker with DB write access copying Alice's encrypted token into Mallory's row fails to decrypt |
| DEK granularity | Per tenant (common), per user (enables crypto-shredding), or per row | Blast radius and erasure (§12) |
| KEK location | Cloud KMS (AWS KMS, GCP KMS, Azure Key Vault), Vault Transit, HSM | Keys never in app config or DB |
| Caching | Cache unwrapped DEKs in memory for minutes, bounded | KMS latency and rate limits |
| Rotation | New KEK version wraps new DEKs; re-wrap old DEKs lazily; re-encrypt data only when DEK compromised | Rewrapping is cheap; re-encrypting is expensive |
| Access | Only the service that *uses* the secret can unwrap; analytics/BI roles cannot | Least privilege |

Database-native options and their limits:

- **Transparent data encryption / encrypted disks** (RDS encryption, Postgres TDE products): protects against stolen
  disks and snapshots, **not** against SQL injection, a compromised app, or a DBA. Necessary, not sufficient.
- **`pgcrypto` with the key in the query**: the key appears in query logs and `pg_stat_statements`. Avoid.
- **Application-level (field-level) encryption** as above: protects against everything that reads the DB without the
  app's KMS permission. This is the one that matters for tokens.

**Alternative: don't store it, reference it.** Put the secret in a secrets manager (Vault, AWS Secrets Manager, GCP
Secret Manager) and store only its path. Good for a modest number of high-value integration secrets; too slow and
costly for millions of per-user OAuth tokens, where envelope encryption in your DB is the norm.

---

## 6. API keys: format, storage and lifecycle

A good API key is designed for the day it leaks.

**Format:** `<prefix>_<env>_<random>_<checksum>`, e.g. `acme_live_9Qz...K3_f02c`.

| Part | Purpose |
|---|---|
| Vendor prefix (`acme_`) | Secret scanners (GitHub push protection, GitGuardian, TruffleHog) can recognize it; you can register the pattern with GitHub's secret scanning partner program to get notified of leaks in public repos |
| Environment (`live`/`test`) | Prevent test keys working in prod; visible to humans |
| Random part, ≥128 bits | Unguessable |
| Checksum (e.g. CRC32, as in GitHub's token format) | Scanners and your API can reject typos and fake matches without a DB lookup |

**Storage:**

```
api_keys(
  id uuid pk, key_prefix text,            -- first ~8 chars of random part, for display "acme_live_9Qz…"
  key_hash bytea unique,                  -- HMAC-SHA-256(server_key, full_key)
  owner_type text, owner_id uuid, tenant_id uuid,
  scopes text[], allowed_ips cidr[] null,
  created_by uuid, created_at, last_used_at, last_used_ip, expires_at null, revoked_at null,
  description text
)
```

**Lifecycle rules:**

1. Show the full key **once** at creation; afterward only the prefix.
2. Scope keys (read-only vs write, per resource), and bind to an owner that can be deprovisioned (a service account,
   not a person's account that disappears when they leave, or a person, if you want it to disappear).
3. Expiry by default (90–365 days) with rotation reminders; allow two active keys per owner for zero-downtime
   rotation.
4. Update `last_used_at` (sampled/batched, not a write per request) so you can find and kill unused keys.
5. Revoke automatically on leak notification from secret scanning.
6. Rate-limit per key; log key ID (never the key) on every request.

API keys are bearer credentials with no expiry, audience or binding by default. For server-to-server integration
prefer OAuth client credentials with `private_key_jwt` (short-lived tokens, asymmetric), or workload identity
federation (no stored secret at all, ch 12). Keep API keys for simple developer onboarding and low-risk access.

---

## 7. Signing keys and other private keys

Private keys that sign tokens (your AS's JWT keys, SAML signing keys) are the most valuable secret you have: with one,
an attacker mints any identity (Storm-0558, ch 03 §5.8).

- Keep them in an **HSM or cloud KMS as non-exportable asymmetric keys**; sign via API (`kms.sign(key_id, digest)`).
  Latency is a few milliseconds; batch or cache signatures where the protocol permits (e.g. sign longer-lived
  internal tokens, not every request).
- If you must hold the key in process (very high issuance rates), load it from the KMS-encrypted form at startup, keep
  it only in memory, never on disk or in the DB, and rotate frequently.
- Separate keys per purpose (ID tokens, access tokens, logout tokens) and per environment.
- Alert on any KMS `Sign` call from an unexpected principal.

---

## 8. Schemas: a reference identity data model

```sql
-- People
CREATE TABLE users (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  email         citext,                         -- contact attribute, not an identifier
  email_verified_at timestamptz,
  status        text NOT NULL DEFAULT 'active', -- active | disabled | deleted
  tokens_valid_after timestamptz NOT NULL DEFAULT now(),  -- bump to invalidate all JWTs (iat < this)
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- How they log in (one user, many)
CREATE TABLE password_credentials (
  user_id       uuid PRIMARY KEY REFERENCES users,
  hash          text NOT NULL,                  -- $argon2id$v=19$m=...,t=...,p=...$salt$hash
  pepper_version int,
  updated_at    timestamptz NOT NULL,
  compromised_at timestamptz                    -- set by breach screening -> force reset
);
CREATE TABLE webauthn_credentials (
  credential_id bytea PRIMARY KEY,
  user_id       uuid NOT NULL REFERENCES users,
  public_key    bytea NOT NULL,                 -- COSE key, public
  sign_count    bigint NOT NULL,
  backup_eligible bool, backup_state bool, aaguid uuid, transports text[],
  nickname      text, created_at timestamptz, last_used_at timestamptz
);
CREATE TABLE totp_credentials (
  user_id       uuid PRIMARY KEY REFERENCES users,
  secret_ct     bytea NOT NULL, secret_nonce bytea NOT NULL, dek_id text NOT NULL,  -- encrypted
  last_used_step bigint NOT NULL DEFAULT 0      -- replay protection
);
CREATE TABLE recovery_codes (
  user_id uuid REFERENCES users, code_hash bytea, used_at timestamptz,
  PRIMARY KEY (user_id, code_hash)
);
CREATE TABLE federated_identities (           -- (iss, sub) is the key, never email
  iss text, sub text, user_id uuid NOT NULL REFERENCES users,
  linked_at timestamptz, last_login_at timestamptz,
  PRIMARY KEY (iss, sub)
);

-- Things you issued (verify-only => hashed)
CREATE TABLE sessions (
  id_hash bytea PRIMARY KEY,                    -- sha256(session_id)
  user_id uuid NOT NULL, idp_sid text, auth_time timestamptz, amr text[],
  created_at timestamptz, last_seen_at timestamptz, absolute_expiry timestamptz, revoked_at timestamptz
);
CREATE TABLE refresh_tokens (
  token_hash bytea PRIMARY KEY,
  family_id uuid NOT NULL, user_id uuid, client_id text, scopes text[], resource text,
  dpop_jkt text,                                 -- sender-constrained? store thumbprint
  issued_at timestamptz, expires_at timestamptz, used_at timestamptz, revoked_at timestamptz
);
CREATE INDEX ON refresh_tokens (family_id);
CREATE TABLE one_time_tokens (                  -- reset, magic link, email verify, invite
  token_hash bytea PRIMARY KEY, purpose text NOT NULL, user_id uuid,
  expires_at timestamptz NOT NULL, used_at timestamptz
);

-- Things you hold for others (must read back => encrypted)
CREATE TABLE connected_accounts (               -- user-delegated tokens for integrations/agents
  id uuid PRIMARY KEY, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
  provider text NOT NULL, provider_issuer text NOT NULL, provider_subject text NOT NULL,
  scopes text[] NOT NULL,
  access_token_ct bytea, access_token_expires_at timestamptz,
  refresh_token_ct bytea,
  nonce_at bytea, nonce_rt bytea, dek_id text NOT NULL, aad_version int NOT NULL,
  status text NOT NULL,                          -- active | needs_reauth | revoked
  version bigint NOT NULL DEFAULT 0,             -- optimistic concurrency for refresh
  created_at timestamptz, updated_at timestamptz,
  UNIQUE (tenant_id, user_id, provider, provider_subject)
);
```

Access control on the database itself: the auth service's DB role can read `password_credentials`; nothing else can.
Analytics replicas exclude credential tables entirely (not "mask the columns": exclude).

---

## 9. Where secrets leak: the copies you forgot

The table is protected; the copies aren't. A checklist of real leak paths:

| Copy | How the secret gets there | Defense |
|---|---|---|
| Application logs | Logging request headers, `Authorization`, full URLs with tokens, `repr()` of objects | Structured logging with redaction allowlist; never log headers wholesale; redact `token`, `secret`, `password`, `authorization`, `cookie` keys |
| Access logs (nginx, ALB, CDN) | Tokens in query strings | Never put tokens in URLs (OAuth 2.1 forbids it) |
| Error trackers (Sentry etc.) | Request bodies, local variables in stack frames | Configure scrubbing; `send_default_pii=False` |
| APM / tracing | Span attributes with headers or SQL params | Attribute allowlists |
| CDC / event streams | Debezium streaming the `connected_accounts` table to Kafka, then to the warehouse | Exclude credential tables/columns from CDC |
| Data warehouse / BI | Nightly full-table copies | Exclude; separate schemas |
| Backups and snapshots | Full copies, often with broader access and longer retention | Encrypted, access-controlled, retention-limited; field encryption means they hold ciphertext |
| Developer laptops | `pg_dump` of prod for debugging | Masked datasets; no prod credentials tables in dev |
| ORM model `__repr__`/`toString` | Debug output prints the secret field | Mark secret fields; `SecretStr` types (pydantic) that render as `**********` |
| Caches (Redis) | Cached user objects including tokens | Don't cache secrets; or encrypt; short TTL |
| Browser storage, mobile storage | Ch 09 | Ch 09 |
| CI logs | `echo $TOKEN`, verbose HTTP clients | Masking, OIDC federation instead of stored secrets |
| Support tooling | Admin panels showing user records | Never render secrets; audited break-glass |
| **LLM prompts, traces, memory** | Tool results, config, or user pastes containing tokens go into the model context and get stored by observability tools | §10 |

---

## 10. Agents, MCP and tools: storage-specific risks

> **In plain words.** An AI agent is a program that writes down almost everything it sees: prompts, tool calls, tool
> results, memories, traces. If a token is ever *in the model's context*, assume it is now in five storage systems,
> and that a prompt injection can make the model repeat it to an attacker.
>
> **Real-world example.** In August 2025, attackers used stolen OAuth refresh/access tokens belonging to the Salesloft
> Drift chatbot integration to export data from many companies' Salesforce instances, then searched the stolen data for
> more secrets (AWS keys, Snowflake tokens). The integration held long-lived tokens with broad scopes for every customer
> in one place. That is exactly the shape of an agent platform's "connected accounts" store.

Rules for any system where agents use tools:

1. **The model never sees credentials.** Tools receive tokens from a **credential broker / token vault** at execution
   time, inside the tool runtime. The model sees `call_tool("calendar.list", {...})`, not a bearer token. Tool results
   are scrubbed before they return to the context (strip `Authorization` echoes, `Set-Cookie`, keys in error messages).
2. **Encrypt the vault** (§5), keyed per tenant at least, with AAD binding `(tenant, user, provider, provider_subject)`.
   Only the broker can unwrap; the agent orchestrator cannot.
3. **Store the narrowest token you can.** Request minimal scopes; prefer per-user delegated tokens over one
   org-wide integration token; prefer short-lived tokens obtained by token exchange over long-lived refresh tokens
   (ch 14 §3).
4. **Refresh safely.** Two agent runs refreshing the same refresh token at once trigger rotation reuse detection at the
   provider and kill the grant (ch 04 §9). Serialize refresh per `connected_accounts` row (row lock or a distributed
   lock), and use optimistic concurrency (`version`) when writing the new pair (code in §13.4).
5. **Don't persist secrets in agent memory or checkpoints.** LangGraph-style checkpointers, conversation history, vector
   memories and scratchpads are durable stores. Store references (`connected_account_id`), never values. Scan memory
   writes for secret patterns.
6. **Observability for LLMs is a data store.** Prompt/trace tools (Langfuse, LangSmith, Phoenix, vendor consoles)
   persist full prompts and tool I/O. Redact before export, and treat trace retention like log retention.
7. **Local MCP server configs** (`claude_desktop_config.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, `.env` next to a
   repo) often contain plaintext API keys in `env` blocks. They get committed, synced and read by other local tools.
   Prefer MCP servers that use OAuth (tokens in the OS keychain), or reference secrets from the OS keychain / a
   secrets manager instead of inline values (ch 09 §6).
8. **Revoke on disconnect.** "Disconnect Google" must call the provider's revocation endpoint and delete the row, not
   just set a flag. Deprovisioned users' connected accounts must be revoked too (ch 18 §8).
9. **Separate "can use" from "can read".** A tool may *use* a GitHub token to open a PR; no tool should be able to
   *return* the token. If a tool's output schema could contain the credential, the tool is wrong.
10. **Audit every unwrap**: `(agent_id, run_id, user_id, connected_account_id, tool, time)`. Unusual unwrap volume is
    your earliest exfiltration signal.

---

## 11. Rotation, migration and incident response

| Event | Action |
|---|---|
| Raise Argon2 cost | Rehash at next login (`check_needs_rehash`) |
| Legacy weak hashes | Wrap immediately (§3), unwrap at login |
| KEK rotation (scheduled) | New KEK version; re-wrap DEKs; old version kept for decrypt until done |
| DEK suspected compromised | Re-encrypt affected rows under a new DEK; revoke underlying third-party tokens if plaintext may have leaked |
| DB leak of hashed passwords | Force reset for all (or for weak-hash cohort), invalidate sessions, notify, monitor for stuffing |
| DB leak of hashed session/refresh tokens | Low risk if hashes were of 256-bit tokens; rotate anyway (bump `tokens_valid_after`) |
| DB + KMS access leaked (encrypted third-party tokens readable) | **Revoke at every provider** (bulk revocation APIs), notify customers, rotate KEK |
| API key found on GitHub | Auto-revoke via secret-scanning webhook, notify owner |
| Signing key leak | Remove from JWKS, rotate, invalidate all sessions relying on it, investigate forged-token use |

Practice the last two. The first time you bulk-revoke 100,000 third-party grants shouldn't be during an incident.

---

## 12. GDPR and data minimization for identity data

- **Minimize**: don't collect DOB, phone, address unless a feature needs them. Each attribute is breach liability.
- **Purpose limitation**: the IdP's user store isn't your analytics source; give analytics pseudonymous IDs.
- **Erasure**: delete user rows, federated identities, sessions, refresh tokens, connected accounts (after revoking
  them at providers), and passkey public keys. **Crypto-shredding**: with per-user DEKs, deleting the DEK makes all that
  user's encrypted fields in backups unreadable, which is often the only practical way to "erase" from backups.
- **Lookup on encrypted fields**: a **blind index** (`HMAC(index_key, normalize(email))`) lets you query by email
  without storing it in plaintext; it leaks equality, so use it only where needed.
- **Logs**: user IDs instead of emails; retention limits; access controls.
- **Data residency**: tenant-pinned regions for identity data in regulated markets; keep the IdP regional or split
  user stores per region.
- **Breach notification**: GDPR's 72-hour window starts when you become aware; having credential tables hashed and
  encrypted changes the risk assessment and sometimes whether data subjects must be notified.

---

## 13. Code: API keys, token hashing, envelope encryption, token vault

### 13.1 API keys with prefix, checksum and HMAC storage

```python
import base64, hashlib, hmac, secrets, zlib

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def _b62(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        n, r = divmod(n, 62)
        out.append(ALPHABET[r])
    return "".join(reversed(out))

def new_api_key(env: str = "live") -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(32))      # ~190 bits
    checksum = _b62(zlib.crc32(body.encode()), 6)
    return f"acme_{env}_{body}{checksum}"

def well_formed(key: str) -> bool:
    try:
        _, env, rest = key.split("_", 2)
    except ValueError:
        return False
    body, checksum = rest[:-6], rest[-6:]
    return env in ("live", "test") and len(body) == 32 and _b62(zlib.crc32(body.encode()), 6) == checksum

def key_hash(key: str, server_key: bytes) -> bytes:                 # server_key from KMS/secret store
    return hmac.new(server_key, key.encode(), hashlib.sha256).digest()

def authenticate(key: str, server_key: bytes, db):
    if not well_formed(key):                                         # cheap reject, no DB hit
        return None
    row = db.api_keys.find_one(key_hash=key_hash(key, server_key))  # indexed equality lookup
    if row is None or row.revoked_at or (row.expires_at and row.expires_at < db.now()):
        return None
    return row
```

### 13.2 One-time tokens (reset links)

```python
def issue_reset(db, user_id) -> str:
    token = secrets.token_urlsafe(32)
    db.one_time_tokens.insert(token_hash=hashlib.sha256(token.encode()).digest(),
                              purpose="password_reset", user_id=user_id,
                              expires_at=db.now_plus(minutes=20))
    return token                                    # goes only into the email link

def redeem_reset(db, token: str, purpose: str = "password_reset"):
    h = hashlib.sha256(token.encode()).digest()
    # Atomic single use: the UPDATE succeeds once.
    row = db.execute("""UPDATE one_time_tokens SET used_at = now()
                        WHERE token_hash = %s AND purpose = %s AND used_at IS NULL AND expires_at > now()
                        RETURNING user_id""", (h, purpose)).fetchone()
    return row.user_id if row else None
```

### 13.3 Envelope encryption with AAD (KMS stubbed)

```python
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class Kms:  # stand-in: in production, AWS KMS GenerateDataKey/Decrypt, GCP KMS, Vault Transit
    def __init__(self):
        self._kek = AESGCM(AESGCM.generate_key(bit_length=256))
    def generate_data_key(self) -> tuple[bytes, bytes]:
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        return dek, nonce + self._kek.encrypt(nonce, dek, b"dek")
    def decrypt_data_key(self, wrapped: bytes) -> bytes:
        return self._kek.decrypt(wrapped[:12], wrapped[12:], b"dek")

def aad(tenant_id: str, row_id: str, column: str, version: int = 1) -> bytes:
    return f"v{version}|{tenant_id}|{row_id}|{column}".encode()

def encrypt_field(dek: bytes, plaintext: str, ad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(dek).encrypt(nonce, plaintext.encode(), ad)

def decrypt_field(dek: bytes, nonce: bytes, ct: bytes, ad: bytes) -> str:
    return AESGCM(dek).decrypt(nonce, ct, ad).decode()    # raises InvalidTag if swapped or tampered
```

### 13.4 Token vault with safe concurrent refresh

```python
import time

class TokenVault:
    """The only component allowed to unwrap provider tokens. Agents and the LLM never call it directly;
    tool runtimes do, with (tenant, user, provider, scope) taken from the authenticated run context."""

    def __init__(self, db, kms, deks, provider_clients, audit):
        self.db, self.kms, self.deks, self.providers, self.audit = db, kms, deks, provider_clients, audit

    def access_token(self, ctx, provider: str, needed_scopes: set[str]) -> str:
        acct = self.db.connected_accounts.get(tenant_id=ctx.tenant_id, user_id=ctx.user_id, provider=provider)
        if acct is None or acct.status != "active":
            raise PermissionError("account not connected or needs re-authorization")
        if not needed_scopes <= set(acct.scopes):
            raise PermissionError("insufficient scopes: user must re-consent")       # surface as step-up
        self.audit.log("vault.unwrap", run=ctx.run_id, agent=ctx.agent_id, acct=acct.id, scopes=sorted(needed_scopes))
        if acct.access_token_expires_at - time.time() > 60:
            return self._decrypt(acct, "access_token")
        return self._refresh(ctx, acct.id)

    def _refresh(self, ctx, acct_id) -> str:
        with self.db.transaction():
            acct = self.db.connected_accounts.get_for_update(id=acct_id)       # SELECT ... FOR UPDATE
            if acct.access_token_expires_at - time.time() > 60:                # another worker refreshed
                return self._decrypt(acct, "access_token")
            rt = self._decrypt(acct, "refresh_token")
            try:
                tok = self.providers[acct.provider].refresh(rt)               # POST /token grant_type=refresh_token
            except self.providers[acct.provider].InvalidGrant:
                self.db.connected_accounts.update(acct.id, status="needs_reauth")
                raise PermissionError("grant revoked or expired: user must reconnect")
            self._store(acct, tok)                                            # new AT, and new RT if rotated
            return tok["access_token"]

    def _decrypt(self, acct, column: str) -> str:
        dek = self.deks.get(acct.dek_id)                                       # cached unwrap via KMS
        nonce, ct = (acct.nonce_at, acct.access_token_ct) if column == "access_token" \
            else (acct.nonce_rt, acct.refresh_token_ct)
        return decrypt_field(dek, nonce, ct, aad(acct.tenant_id, acct.id, column))

    def _store(self, acct, tok: dict) -> None:
        dek = self.deks.get(acct.dek_id)
        n_at, at_ct = encrypt_field(dek, tok["access_token"], aad(acct.tenant_id, acct.id, "access_token"))
        fields = dict(nonce_at=n_at, access_token_ct=at_ct,
                      access_token_expires_at=time.time() + tok.get("expires_in", 3600),
                      version=acct.version + 1)
        if "refresh_token" in tok:                                            # provider rotated it
            n_rt, rt_ct = encrypt_field(dek, tok["refresh_token"], aad(acct.tenant_id, acct.id, "refresh_token"))
            fields.update(nonce_rt=n_rt, refresh_token_ct=rt_ct)
        self.db.connected_accounts.update(acct.id, **fields)
```

The `FOR UPDATE` lock serializes refreshes for one account; the re-check after acquiring it makes the second worker
reuse the first worker's result instead of presenting an already-rotated refresh token.

---

## 14. Best and worst practices, side by side

| Worst practice | Why it's bad | Best practice |
|---|---|---|
| Plaintext passwords, or reversible encryption of passwords | One leak = every password | Argon2id |
| `sha256(password)` or `md5(salt + password)` | GPU-crackable in hours | Argon2id/scrypt/bcrypt |
| One global salt | Identical passwords visible; one table attacks all | Per-user random salt (built into the encoded hash) |
| Hand-rolled "stretching" loops | Wrong in subtle ways | Library KDF |
| Plaintext API keys / session IDs / refresh tokens in DB | DB read = account takeover | SHA-256/HMAC of high-entropy tokens |
| Argon2 for session IDs | Wastes CPU, invites DoS | SHA-256 |
| Third-party tokens stored plaintext "because we need them" | DB read = access to every customer's Google/Salesforce | Envelope encryption + KMS + AAD |
| Encryption key in the same DB or in `.env` in the repo | Encryption becomes decoration | KMS/HSM; app identity to unwrap |
| AES-ECB, AES-CBC without MAC, reused GCM nonces | Leaks patterns / allows forgery / key recovery | AEAD with random nonces |
| No AAD | Ciphertext swapping between rows/tenants | AAD = tenant + row + column |
| TDE treated as "encrypted at rest, done" | Doesn't stop SQLi or app compromise | Field-level encryption for secrets |
| Tokens in URLs, logs, Sentry, traces, CDC, warehouse | Copies are the real leak path | Redaction, exclusion, no tokens in URLs |
| Showing API keys again in the dashboard | Requires storing plaintext | Show once; prefix only afterwards |
| Keys that never expire, no owner | Orphaned credentials after people leave | Expiry, owner, last-used, revocation |
| Tokens in LLM context, memory or checkpoints | Prompt injection exfiltration, durable copies | Broker injects at tool runtime; references only |
| Concurrent refresh of rotating tokens | Provider revokes the whole grant | Per-account lock + re-check |
| "Disconnect" that only deletes the row | Token still valid at provider | Revoke at provider, then delete |

---

## 15. Interview questions

**Q1. Why hash passwords with Argon2id but session tokens with SHA-256?**
Password hashes must resist guessing of low-entropy inputs, so cost and memory-hardness matter. Session tokens are
256-bit random values that can't be guessed; any preimage-resistant hash suffices, and a slow hash would only add
latency and a DoS vector.

**Q2. Your app stores users' Google refresh tokens to sync calendars. Design the storage.**
Envelope encryption: per-tenant (or per-user) DEKs wrapped by a KMS KEK; AES-256-GCM with AAD binding tenant, row and
column; only the sync service's identity can unwrap; refresh serialized per account with a row lock; tokens never
logged, excluded from CDC/warehouse/backups beyond ciphertext; disconnect calls Google's revoke endpoint; audit every
unwrap; incident plan for bulk revocation.

**Q3. How do you look up an API key without a timing side channel?**
Hash the presented key (HMAC with a server key) and query by the hash through a unique index. The comparison of secret
material happens inside the index lookup on a digest; there's no byte-by-byte compare of the secret in your code.

**Q4. An attacker has read-write SQL access but not KMS access. What can they do to encrypted OAuth tokens?**
They can't decrypt. Without AAD, they could swap ciphertexts between rows so their account uses a victim's token; with
AAD bound to row and tenant, decryption fails. They could delete or corrupt rows (availability), which is why integrity
monitoring and backups matter.

**Q5. What changes when the consumer of a stored token is an AI agent?**
The token must never enter model context, memory, checkpoints or traces; a broker injects it into tool execution; tool
outputs are scrubbed; scopes are minimal and per-user; refresh is serialized across concurrent runs; every unwrap is
audited with agent and run IDs; and disconnect/deprovisioning revokes at the provider.

---

## Sources

- OWASP Password Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- OWASP Secrets Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- OWASP Cryptographic Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html
- RFC 9106, Argon2 Memory-Hard Function: https://datatracker.ietf.org/doc/html/rfc9106
- NIST SP 800-38D (GCM) and nonce limits: https://csrc.nist.gov/pubs/sp/800/38/d/final
- AWS KMS, envelope encryption concepts: https://docs.aws.amazon.com/kms/latest/developerguide/kms-cryptography.html#enveloping
- GitHub, "Behind GitHub's new authentication token formats" (prefixes and checksums, 2021): https://github.blog/engineering/platform-security/behind-githubs-new-authentication-token-formats/
- GitHub secret scanning partner program: https://docs.github.com/en/code-security/secret-scanning/secret-scanning-partnership-program
- CircleCI, "CircleCI incident report for January 4, 2023 security incident": https://circleci.com/blog/jan-4-2023-incident-report/
- Google Threat Intelligence Group, "Widespread Data Theft Targets Salesforce Instances via Salesloft Drift" (August 2025): https://cloud.google.com/blog/topics/threat-intelligence/data-theft-salesforce-instances-via-salesloft-drift
- MCP 2026-07-28 Security Considerations (token theft, secure storage): https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
