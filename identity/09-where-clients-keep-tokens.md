# 09 — Where clients keep tokens: backend, browser, mobile, CLI, workloads and AI agents

> **Prerequisites:** [`02-sessions-cookies-and-browser-security.md`](02-sessions-cookies-and-browser-security.md) (XSS,
> cookies, BFF), [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) (client types, refresh
> tokens), [`08-storing-credentials-and-tokens-at-rest.md`](08-storing-credentials-and-tokens-at-rest.md) (server-side
> storage).
>
> **Feeds into:** [`12-workload-and-service-identity.md`](12-workload-and-service-identity.md),
> [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md),
> [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md).
>
> **THESIS:** "where do I store the token?" is really "**who else can read that place?**". A server's memory is read by
> your process; `localStorage` is read by every script on your origin; a CLI's config file is read by every process
> running as the user; an AI agent's context window is read by whoever can inject text into it. Pick the storage whose
> set of readers is smallest, prefer platform key stores that bind secrets to hardware and to the app, prefer tokens
> that are short-lived or bound to a key so that a copy is worth little, and best of all, **don't hold a long-lived
> token at all** when the platform can issue you a fresh one.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The question behind the question: who can read it?](#1-the-question-behind-the-question-who-can-read-it)
2. [Classic server-side web apps](#2-classic-server-side-web-apps)
3. [Backend-for-frontend](#3-backend-for-frontend)
4. [Browser-only SPAs](#4-browser-only-spas)
5. [Native mobile apps](#5-native-mobile-apps)
6. [Desktop apps, CLIs and local MCP servers](#6-desktop-apps-clis-and-local-mcp-servers)
7. [Backend services and workloads](#7-backend-services-and-workloads)
8. [AI agents: four deployment shapes, four answers](#8-ai-agents-four-deployment-shapes-four-answers)
9. [The master comparison table](#9-the-master-comparison-table)
10. [Edge cases that break token storage](#10-edge-cases-that-break-token-storage)
11. [Code: keychain storage, in-memory token cache, non-extractable browser keys](#11-code-keychain-storage-in-memory-token-cache-non-extractable-browser-keys)
12. [Anti-patterns](#12-anti-patterns)
13. [Interview questions](#13-interview-questions)
14. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Every program that calls an API needs to keep its key somewhere between calls. The right place depends on what kind of
program it is:

- A **website's server** keeps tokens in its own database (encrypted) or memory. The browser only gets a session cookie.
- A **single-page app with no server** has to keep tokens in the browser. The least bad place is JavaScript memory,
  with a short lifetime, and ideally the token is bound to a key the browser can't export.
- A **phone app** uses the phone's secure key store (iOS Keychain, Android Keystore), which only that app can read.
- A **command-line tool** uses the computer's password manager (macOS Keychain, Windows Credential Manager, Linux
  Secret Service), not a plain text file in your home folder.
- A **server workload** shouldn't keep a stored key at all: the cloud or Kubernetes hands it a fresh short-lived
  identity on request.
- An **AI agent** must never have the token in its prompt. A trusted piece of code next to the agent holds tokens and
  attaches them when a tool runs.

---

## 1. The question behind the question: who can read it?

| Storage | Readers | Survives restart | Hardware-bound possible |
|---|---|---|---|
| Server process memory | Your process (and root, and core dumps) | No | — |
| Server DB, encrypted (ch 08) | Anyone with DB **and** KMS access | Yes | KEK in HSM |
| `HttpOnly` cookie | Browser; sent to your server only; not page JS | Configurable | DBSC (ch 02 §8) |
| JS memory | All JS on the origin (XSS) | No (tab) | — |
| `sessionStorage` / `localStorage` | All JS on the origin; disk; extensions with host permissions | Tab / yes | No |
| IndexedDB + non-extractable WebCrypto key | JS on origin can **use** but not **export** the key | Yes | Platform-dependent |
| iOS Keychain / Android Keystore | The app (and its app group / shared user ID) | Yes | Secure Enclave / StrongBox / TEE |
| macOS Keychain / Windows Credential Manager (DPAPI) / Linux Secret Service | Processes running as the user (with prompts/ACLs on macOS) | Yes | TPM / Secure Enclave for keys |
| Plain file `~/.tool/credentials` (0600) | Every process running as the user, backups, sync tools, dotfile repos | Yes | No |
| Environment variables | The process, its children, `/proc/<pid>/environ` (same user), crash reports, debug endpoints | No | No |
| LLM context window / agent memory | The model provider, tracing tools, **anyone who can inject text** | Often persisted | No |

Three properties beat any choice of location:

1. **Short lifetime** (minutes): a copy expires before it's useful.
2. **Sender-constraining** (DPoP/mTLS, ch 07 §5): a copy is useless without the key, and the key is hardware-bound.
3. **No long-lived secret at all**: workload identity, OS-attested identities, token exchange on demand.

---

## 2. Classic server-side web apps

Rails/Django/Spring/Express app that renders pages and calls APIs:

- **User sign-in**: OIDC code flow as a **confidential client** (`private_key_jwt` preferred). The client private key or
  secret comes from a **secrets manager** at startup or via workload identity, never from the repo.
- **Browser**: gets only an `__Host-` session cookie (ch 02).
- **ID token**: validate at login, extract claims into the session; don't keep it (except as `id_token_hint` for logout,
  in the server session).
- **User's access/refresh tokens for downstream APIs**: in the **server-side session store** (encrypted if the store is
  shared or persisted) or in a `connected_accounts` table with envelope encryption (ch 08 §5) when they must outlive the
  session (background sync).
- **The app's own tokens** (client credentials for calling internal APIs): **in-memory cache** per process, refreshed
  shortly before expiry (§11.2). No need to persist; fetch on start.

---

## 3. Backend-for-frontend

The BFF is a classic server-side app whose UI happens to be a SPA (ch 02 §6). Specifics:

- Tokens are keyed by session ID in Redis or a DB, encrypted at rest (they're third-party-usable bearer credentials).
- Session cookie: `__Host-`, `HttpOnly`, `Secure`, `SameSite=Lax` or `Strict`; CSRF protection on the BFF's API routes.
- Refresh happens server-side, serialized per session (concurrent tab requests).
- On logout or back-channel logout: delete session, revoke refresh token at the AS.
- Horizontal scale: the session store is shared; tokens never go to the browser so sticky sessions aren't needed.

---

## 4. Browser-only SPAs

When there's genuinely no server (static hosting, a demo, some embedded widgets), the IETF browser-apps guidance and
OAuth 2.1 apply: public client, code + PKCE, refresh-token rotation, short access tokens.

| Option | XSS can steal? | Survives reload | Verdict |
|---|---|---|---|
| `localStorage` | Yes, trivially, and it persists for days | Yes | Avoid for tokens |
| `sessionStorage` | Yes | Per tab | Slightly better lifetime, same XSS exposure |
| JS memory (closure, module scope) | Usually, with effort (hooking `fetch`, reading framework state) | No → re-auth or refresh on reload | **Best browser-only option** |
| Web Worker / Service Worker holding tokens, main thread never sees them | Harder; worker can be asked to make requests | Worker lifetime | Good hardening; adds complexity |
| Refresh token in `HttpOnly` cookie set by the AS for the AS's token endpoint | No (cookie), but needs AS support and same-site setup | Yes | Niche |
| **DPoP with non-extractable key in IndexedDB** | XSS can use the key while page is open, **can't export it** | Yes | Pair with any of the above; stolen tokens alone are useless |

Practical recipe: access token in memory (5–10 min), refresh token in memory or `sessionStorage` with rotation, DPoP
key as a non-extractable `CryptoKey` persisted in IndexedDB (the key object can be stored and reused without being
exportable), strict CSP. Accept that XSS is game over for the session while the page is open, and keep it from being
game over for longer.

Do **not** store tokens in cookies that JavaScript can read (`document.cookie`): that combines XSS exposure with CSRF
exposure.

---

## 5. Native mobile apps

**iOS:** Keychain Services. Choose the accessibility class deliberately:

| Class | Meaning | Use |
|---|---|---|
| `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` | Readable only when unlocked; **not** in backups/migrations | Refresh tokens: a restored backup on another phone must re-login |
| `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` | Readable after first unlock since boot | Background refresh (push-triggered sync) |
| `...` without `ThisDeviceOnly` | Migrates in encrypted backups | Avoid for tokens |

Add `SecAccessControl` with `.biometryCurrentSet` or `.userPresence` to require Face ID / Touch ID before reading
high-value secrets (e.g. step-up for payments), and generate DPoP/device keys **in the Secure Enclave**
(`kSecAttrTokenIDSecureEnclave`), where they're non-exportable.

**Android:** Android Keystore for keys (prefer StrongBox-backed where available; `setUserAuthenticationRequired` for
biometric gating), and encrypt token blobs with a Keystore key before writing them to DataStore/SharedPreferences
(Jetpack Security's `EncryptedSharedPreferences` was the common wrapper; it has been deprecated, and the usual
replacement is Tink with a Keystore-backed master key). Set `android:allowBackup="false"` or exclude token files from
backup rules.

**Both:**
- Login through the system browser (ASWebAuthenticationSession / Custom Tabs), code + PKCE, claimed HTTPS redirects
  (ch 04 §11, ch 06 §5).
- **App attestation** (App Attest / DeviceCheck on iOS, Play Integrity on Android) lets your backend gain confidence
  the request comes from your genuine app on a genuine device; useful for rate-limiting abuse and for dynamic client
  registration of mobile instances. It's a signal, not authentication of the user.
- Rooted/jailbroken devices weaken all of this; decide per risk whether to degrade features.

---

## 6. Desktop apps, CLIs and local MCP servers

Use the **OS credential store** via a library (`keyring` in Python, `keytar`-style libraries or `@napi-rs/keyring` in
Node, `zalando/go-keyring` in Go, `keyring` crate in Rust):

| OS | Store | Notes |
|---|---|---|
| macOS | Keychain | Per-item ACLs (which apps may read without prompting); code-signing identity matters |
| Windows | Credential Manager (DPAPI-protected) | Readable by any process of the same user; DPAPI binds to user's logon credentials |
| Linux desktop | Secret Service (GNOME Keyring, KWallet) via D-Bus | Unavailable on headless servers/containers |
| Headless Linux / CI / containers | No keychain | Fallback: file with 0600 in `$XDG_CONFIG_HOME`, ideally encrypted with a key from env/TPM; or don't persist (device flow each time) |

How widely used tools behave (worth knowing because you'll inherit their choices):

- `gh` stores its OAuth token in the system keychain by default and falls back to a plaintext
  `~/.config/gh/hosts.yml` when none is available.
- `aws` CLI static access keys live in plaintext `~/.aws/credentials`; **IAM Identity Center (SSO)** logins cache
  short-lived credentials in `~/.aws/sso/cache` and `~/.aws/cli/cache`. Prefer SSO: the plaintext file then holds only
  hours-long credentials.
- `gcloud` keeps credentials in its config directory (`~/.config/gcloud`), and application-default credentials in
  `application_default_credentials.json` (contains a refresh token, plaintext).
- `kubectl` kubeconfigs can embed static tokens or client keys (plaintext) or, better, use `exec` credential plugins that
  fetch short-lived tokens from an IdP on demand.
- `docker` uses credential helpers (`docker-credential-osxkeychain`, `-wincred`, `-secretservice`, `-pass`); without one,
  registry credentials sit base64-encoded in `~/.docker/config.json`.

**Local MCP servers and agent tools.** A local (stdio) MCP server is a process the AI app launches as the user. Its
credentials are typically passed via an `env` block in a JSON config file, so:

- The config file is a plaintext secret store that may be synced, backed up, or committed (`.cursor/mcp.json`,
  `.vscode/mcp.json` inside repos are common accidental commits).
- Every other tool the agent runs as the same user can read it.

Better: MCP servers that implement OAuth over HTTP (tokens obtained per the MCP authorization spec and stored by the MCP
client in the OS keychain, keyed by issuer as MCP 2026-07-28 requires), or local servers that read their secret from
the OS keychain / a secrets manager CLI at startup instead of from inline config. Treat any repo-committed MCP config
like a `.env` file: add it to secret scanning.

**File locking for shared token caches.** Several CLI processes may refresh the same token concurrently. Use an
advisory file lock around read-refresh-write, write atomically (temp file + rename), and re-read after acquiring the
lock (same pattern as ch 08 §13.4).

---

## 7. Backend services and workloads

The goal is **no stored secret**:

| Platform | Identity source | Token location |
|---|---|---|
| Kubernetes | Projected service account token (audience-bound, auto-rotated, ~1 h) at `/var/run/secrets/...` on tmpfs | Read from file each time it's needed (kubelet rotates it) |
| AWS (EC2/ECS/EKS/Lambda) | Instance/task roles, EKS Pod Identity or IRSA | SDK fetches from metadata endpoint, caches in memory |
| GCP | Metadata server, Workload Identity | SDK, memory |
| Azure | Managed identity (IMDS) | SDK, memory |
| Anywhere | SPIFFE/SPIRE Workload API (Unix socket) | X.509-SVID/JWT-SVID in memory, rotated |
| CI (GitHub Actions, GitLab) | OIDC ID token from the CI | Exchanged for cloud creds per job (ch 12 §5) |

Rules:
- Cache access tokens **in memory**, refresh at ~80% of lifetime with jitter, single-flight per audience.
- Don't put tokens or secrets in **environment variables** when you can avoid it: they leak through `/proc`, crash
  reporters, debug pages (`phpinfo`, Spring actuator `/env`), child processes and `docker inspect`. A file on tmpfs,
  read on demand, is better; a fetch from a local agent is better still.
- Secrets that must exist (third-party API keys with no federation): secrets manager → mounted at runtime (CSI driver,
  External Secrets) → read by the app; rotated by the manager. See [kubernetes ch 44](../kubernetes/44-secrets-and-configmaps-deep-dive.md).

---

## 8. AI agents: four deployment shapes, four answers

> **In plain words.** An agent is a loop: the model decides on a tool call, code runs the tool, the result goes back
> into the model's context. Credentials belong to the **code that runs the tool**, never to the **model** and never to
> the **context**.

### 8.1 Local agent in the user's app or terminal (coding agents, desktop assistants)

- Runs as the user; it already has the user's ambient authority (files, SSH keys, cloud CLIs). That is the threat: a
  prompt-injected local agent can read `~/.aws/credentials`, `~/.config/gh/hosts.yml`, `.env` files.
- Token storage: the agent host's own OAuth tokens (for remote MCP servers, the model API) in the OS keychain, keyed by
  issuer and server.
- Defenses are mostly about **reach**, not storage: sandbox the agent's tool execution (filesystem allowlists, network
  egress allowlists, no access to credential directories), require approval for commands that read secrets or touch the
  network (ch 15 §5), and keep long-lived credentials out of the workspace.

### 8.2 Hosted agent platform acting for many users (SaaS assistant with "connect your Google/Slack/Jira")

- Per-user delegated tokens in an encrypted **token vault** (ch 08 §10, §13.4).
- A **credential broker** in the tool runtime fetches the right token for `(tenant, user, provider, scope)` at
  execution time. The orchestrator passes only identifiers.
- Short-lived tokens via token exchange where providers support it; refresh serialized per account.

### 8.3 Autonomous background agent (scheduled, no user present)

- Uses **its own workload identity** (ch 12) and client credentials, scoped to what the job needs, not a human's
  refresh token "borrowed" from whoever set it up.
- If it acts for a user, it holds a **grant** from that user (an offline refresh token with narrow scopes, or a
  delegated grant recorded at the AS), and every action is attributed as `act=agent, sub=user` (ch 14 §2).

### 8.4 Sandboxed code execution (agent writes and runs code)

- **Never inject long-lived credentials into the sandbox**: anything inside can be printed, written to a file, sent over
  the network, or end up in the transcript.
- Use an **egress proxy that injects credentials** outside the sandbox: code inside calls
  `https://api.github.com/...` with no token, the proxy (which the sandbox can't read) adds `Authorization` for allowed
  hosts and methods only. The sandbox never holds a secret, and the proxy enforces scope.
- If something must be inside, use minutes-long, single-purpose, audience-bound tokens.

| Agent shape | Where tokens live | Never |
|---|---|---|
| Local agent | OS keychain, owned by the host app | In the workspace, in prompts, in MCP config files committed to git |
| Hosted multi-user | Encrypted vault + broker in tool runtime | In model context, memory, checkpoints, traces |
| Autonomous | Workload identity, in-memory short tokens | A human's personal token |
| Code sandbox | Outside: credential-injecting proxy | Inside the sandbox |

---

## 9. The master comparison table

| Client | Long-term credential | Access token | Refresh token | Best upgrade |
|---|---|---|---|---|
| Server web app | Client key in KMS / workload identity | Server memory / session store | Encrypted server store | `private_key_jwt`, PAR |
| BFF | Same | Server session store | Encrypted server store | DPoP/mTLS to APIs |
| SPA (no server) | None (public client) | JS memory | Memory/sessionStorage + rotation | Move to BFF; else DPoP non-extractable key |
| iOS / Android | None (public client) + device key | Memory | Keychain/Keystore, this-device-only | Secure Enclave/StrongBox DPoP key, app attestation |
| Desktop / CLI | None (public client) | Memory | OS keychain | Loopback + PKCE; device flow when headless |
| Microservice | Workload identity (no secret) | Memory | Usually none | SPIFFE, mTLS-bound tokens |
| CI job | CI OIDC token (no secret) | Memory | None | Workload identity federation |
| Local agent / MCP client | OS keychain | Memory | OS keychain, keyed by issuer | OAuth MCP servers instead of inline API keys |
| Hosted agent | Platform workload identity | Broker memory | Encrypted vault | Token exchange, per-user grants, DPoP |
| Sandboxed agent code | None | None inside sandbox | None | Credential-injecting egress proxy |

---

## 10. Edge cases that break token storage

1. **Backup and restore.** iOS non-`ThisDeviceOnly` items and Android backups move refresh tokens to a new device.
   Exclude them; force re-login after restore.
2. **Multiple accounts.** Key stored tokens by `(issuer, subject, client)` (and, for MCP, per authorization server), not
   by a single "current token" slot. MCP 2026-07-28 requires credentials keyed by issuer and never reused with another AS.
3. **Logout must clear every store.** Memory, keychain, cookie, IndexedDB DPoP key (or keep the key and drop tokens),
   server session, and revoke at the AS.
4. **Concurrent refresh** across tabs, threads, processes (§6 file locks, ch 04 §9).
5. **Crash dumps and swap.** Process memory with tokens goes into core dumps and swap. Disable core dumps on auth
   services; encrypted swap.
6. **Clock skew** on devices: tokens "expired" on arrival. Use server-provided `expires_in` relative to receipt time,
   not absolute `exp` against a wrong device clock.
7. **Token size limits.** Cookies max ~4 KB; headers 8–16 KB. Large JWTs in cookies silently fail. Keep tokens server-side
   in a BFF.
8. **Shared machines / kiosks.** Never persist refresh tokens; session-only storage; short timeouts.
9. **Browser extensions** with broad host permissions can read DOM and storage on your origin. Nothing in page context
   is safe from them; another argument for the BFF.
10. **Agent runs that outlive the token.** Covered in ch 15 §6: refresh mid-loop, retry semantics, and what to do when
    the grant is gone.

---

## 11. Code: keychain storage, in-memory token cache, non-extractable browser keys

### 11.1 CLI: store refresh tokens in the OS keychain, keyed by issuer

```python
import json, keyring           # pip install keyring

SERVICE = "acme-cli"

def _account(issuer: str, subject: str) -> str:
    return f"{issuer}|{subject}"

def save_tokens(issuer: str, subject: str, tokens: dict) -> None:
    blob = json.dumps({"refresh_token": tokens["refresh_token"], "scope": tokens.get("scope")})
    keyring.set_password(SERVICE, _account(issuer, subject), blob)

def load_refresh_token(issuer: str, subject: str) -> str | None:
    blob = keyring.get_password(SERVICE, _account(issuer, subject))
    return json.loads(blob)["refresh_token"] if blob else None

def logout(issuer: str, subject: str, revoke) -> None:
    rt = load_refresh_token(issuer, subject)
    if rt:
        revoke(rt)                                   # POST /revoke at the AS first
    try:
        keyring.delete_password(SERVICE, _account(issuer, subject))
    except keyring.errors.PasswordDeleteError:
        pass
```

### 11.2 Service: in-memory token cache with early refresh and single-flight

```python
import asyncio, random, time

class TokenCache:
    """One cached token per audience; refreshed at ~80% of lifetime; concurrent callers share one fetch."""

    def __init__(self, fetch):                      # fetch(audience) -> (token, expires_in_seconds)
        self._fetch = fetch
        self._tokens: dict[str, tuple[str, float, float]] = {}   # aud -> (token, refresh_at, expires_at)
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, audience: str) -> str:
        now = time.monotonic()
        hit = self._tokens.get(audience)
        if hit and now < hit[1]:
            return hit[0]
        lock = self._locks.setdefault(audience, asyncio.Lock())
        async with lock:                             # single-flight
            hit = self._tokens.get(audience)
            if hit and time.monotonic() < hit[1]:
                return hit[0]
            try:
                token, ttl = await self._fetch(audience)
            except Exception:
                if hit and time.monotonic() < hit[2]:    # AS hiccup: use the still-valid token
                    return hit[0]
                raise
            now = time.monotonic()
            refresh_at = now + ttl * random.uniform(0.75, 0.85)   # jitter avoids thundering herds
            self._tokens[audience] = (token, refresh_at, now + ttl)
            return token
```

### 11.3 Browser: a non-extractable DPoP key persisted in IndexedDB

```javascript
// The CryptoKey object can be stored in IndexedDB (structured clone) and reused after reload,
// but extractable:false means exportKey() fails: script can sign with it, not steal it.
async function getDpopKey(db) {
  const existing = await idbGet(db, "dpop-key");        // your IndexedDB helper
  if (existing) return existing;
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,                                               // non-extractable private key
    ["sign", "verify"]
  );
  await idbPut(db, "dpop-key", pair);
  return pair;
}
```

---

## 12. Anti-patterns

| Anti-pattern | Who can read it | Instead |
|---|---|---|
| Refresh token in `localStorage` | Any XSS, for days | BFF; or memory + rotation + DPoP |
| Tokens in JS-readable cookies | XSS + CSRF | `HttpOnly` cookie (BFF) |
| Mobile tokens in `UserDefaults` / plain `SharedPreferences` | Backups, rooted devices, other tools | Keychain / Keystore-encrypted |
| CLI tokens in plaintext dotfiles by default | Every process as the user, dotfile repos | OS keychain; plaintext only as an explicit fallback |
| Static cloud keys in `~/.aws/credentials` | Malware, agents, backups | SSO / short-lived credentials |
| API keys inline in committed MCP configs | Anyone with the repo | OAuth MCP servers; keychain references; secret scanning |
| Long-lived secrets in env vars of services | `/proc`, crash reports, debug endpoints | Workload identity; files on tmpfs; secrets manager |
| Credentials inside agent sandboxes | Generated code, transcript, network | Credential-injecting egress proxy |
| Tokens in agent context or memory | Model provider logs, traces, prompt injection | Broker in tool runtime |
| One global "current token" for multi-account / multi-AS clients | Mix-ups, wrong-account actions | Key by issuer + subject (+ client) |

---

## 13. Interview questions

**Q1. A product team insists their SPA must keep tokens in `localStorage` "so users stay logged in". Respond.**
`localStorage` makes any XSS a persistent account takeover. Offer a BFF with an `HttpOnly` session cookie (same UX,
tokens server-side). If no server is possible, keep the access token in memory, refresh with rotation on reload, and bind
tokens with DPoP using a non-extractable key so stolen tokens don't work elsewhere.

**Q2. How should a CLI store credentials on macOS, Windows, and a headless Linux CI box?**
Keychain / Credential Manager / and on CI no persistent user credentials at all: use the CI's OIDC token and workload
identity federation, or device flow per session. If a file is unavoidable, 0600, short-lived contents, atomic writes and
locking.

**Q3. Where should credentials live for an agent that runs generated Python in a sandbox and must call GitHub?**
Outside the sandbox, in an egress proxy that injects a short-lived, repo-scoped token for allowed GitHub endpoints. The
sandbox code makes unauthenticated requests through the proxy; it never sees the token.

**Q4. Why are environment variables a weak place for secrets?**
They're inherited by children, readable via `/proc/<pid>/environ` by the same user, dumped by crash reporters and debug
endpoints, visible in `docker inspect` and orchestration UIs, and often logged at startup.

---

## Sources

- OAuth 2.0 for Browser-Based Applications (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
- RFC 8252, OAuth 2.0 for Native Apps: https://datatracker.ietf.org/doc/html/rfc8252
- RFC 9449 (DPoP), browser key handling guidance in §2 and §11: https://datatracker.ietf.org/doc/html/rfc9449
- Apple, Keychain item accessibility: https://developer.apple.com/documentation/security/keychain-services
- Android Keystore system: https://developer.android.com/privacy-and-security/keystore
- OWASP Mobile Application Security (MASVS-STORAGE): https://mas.owasp.org/MASVS/
- W3C Web Cryptography API (non-extractable keys): https://www.w3.org/TR/WebCryptoAPI/
- Python `keyring`: https://pypi.org/project/keyring/
- GitHub CLI `gh auth login` (keyring storage): https://cli.github.com/manual/gh_auth_login
- AWS CLI, IAM Identity Center credential caching: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html
- Kubernetes client-go credential plugins: https://kubernetes.io/docs/reference/access-authn-authz/authentication/#client-go-credential-plugins
- MCP 2026-07-28, Client Registration (authorization server binding): https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
