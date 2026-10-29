# 02 — Sessions, cookies and browser security

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (§6: stateful versus self-contained),
> [`01-passwords-mfa-and-passkeys.md`](01-passwords-mfa-and-passkeys.md) (what produced the session).
>
> **Feeds into:** [`05-openid-connect.md`](05-openid-connect.md) (the app session created after OIDC login, and
> logout), [`09-where-clients-keep-tokens.md`](09-where-clients-keep-tokens.md) (browser storage options compared
> with mobile, CLI and agents), [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md)
> (session store at scale).
>
> **THESIS:** after login, the session is the credential. Attackers steal sessions far more often than passwords,
> because a session skips MFA. A browser session should therefore be an **opaque, high-entropy ID in an
> `HttpOnly; Secure; SameSite` cookie, scoped to the narrowest host, backed by server-side state you can revoke,
> rotated on every privilege change, and with both idle and absolute timeouts.** Putting JWTs in `localStorage`
> trades every one of those properties for a convenience you rarely need.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Why sessions exist at all](#1-why-sessions-exist-at-all)
2. [Cookie mechanics that matter for security](#2-cookie-mechanics-that-matter-for-security)
3. [Server-side sessions: design and lifecycle](#3-server-side-sessions-design-and-lifecycle)
4. [CSRF: what it is and why SameSite only mostly fixes it](#4-csrf-what-it-is-and-why-samesite-only-mostly-fixes-it)
5. [XSS: why it beats every token storage choice](#5-xss-why-it-beats-every-token-storage-choice)
6. [SPAs: backend-for-frontend versus tokens in the browser](#6-spas-backend-for-frontend-versus-tokens-in-the-browser)
7. [CORS is not an authorization mechanism](#7-cors-is-not-an-authorization-mechanism)
8. [Session theft and binding sessions to devices](#8-session-theft-and-binding-sessions-to-devices)
9. [Timeouts, re-authentication and "remember me"](#9-timeouts-re-authentication-and-remember-me)
10. [Code: a minimal secure session layer](#10-code-a-minimal-secure-session-layer)
11. [Anti-patterns](#11-anti-patterns)
12. [Interview questions](#12-interview-questions)
13. [Sources](#sources)

---

## Start here — the whole chapter in plain words

HTTP forgets you between requests. After you log in, the server gives your browser a **session ID** in a
**cookie**; the browser sends it back automatically with every request to that site, and the server looks it up
to know it's you.

Because the browser sends cookies *automatically*, two classic attacks follow:

- **CSRF** (cross-site request forgery): a different website makes your browser send a request to your bank, and
  the cookie rides along. Defense: `SameSite` cookies plus a check that the request really came from your site.
- **XSS** (cross-site scripting): attacker JavaScript runs *inside* your site. It can do anything you can do in
  that tab. Defense: output encoding and a Content Security Policy; `HttpOnly` cookies at least stop the
  script from *copying* the session out.

For single-page apps, the safest pattern is a small server, the **backend-for-frontend (BFF)**, that does the
OAuth dance, keeps the tokens on the server, and gives the browser only an `HttpOnly` session cookie.

---

## 1. Why sessions exist at all

> **In plain words.** Logging in is expensive (password hash, MFA, maybe a redirect to an IdP). A session
> remembers the result so you do that once, not on every click.

Three different "sessions" exist in a typical SSO deployment, and each needs its own lifetime and logout story:

| Session | Lives at | Created by | Ended by |
|---|---|---|---|
| IdP session | `login.example.com` cookie + IdP store | User authenticating at the IdP | IdP logout, IdP timeout |
| App session | `app.example.com` cookie + app store | App after validating the ID token | App logout, app timeout, back-channel logout from IdP (ch 05 §7) |
| Token "session" | Refresh token at the authorization server | OAuth grant | Revocation, refresh-token expiry, IdP policy |

Confusing these is the root of "I logged out but I'm still logged in" bugs (the app session ended, the IdP
session didn't, so the next click silently logs you back in).

---

## 2. Cookie mechanics that matter for security

`Set-Cookie: __Host-sid=Yx3...; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=28800`

| Attribute | Effect | Why you want it |
|---|---|---|
| `Secure` | Only sent over HTTPS | Network attacker can't read it |
| `HttpOnly` | Invisible to `document.cookie` | XSS can't exfiltrate it (it can still *use* it in-page) |
| `SameSite=Lax` | Not sent on cross-site subrequests (fetch, iframe, POST forms); sent on top-level GET navigation | Blocks most CSRF while keeping links into the site working |
| `SameSite=Strict` | Never sent cross-site, even on link clicks | Strongest; but arriving from an email link looks logged-out |
| `SameSite=None` | Always sent (requires `Secure`) | Needed only for genuine cross-site embedding; re-opens CSRF |
| `Domain=` omitted | Host-only cookie | `Domain=example.com` sends it to *every* subdomain, including a compromised `blog.example.com` |
| `Path=/` | Scope by path | Paths are **not** a security boundary; same-origin scripts can read other paths |
| `__Host-` prefix | Browser enforces `Secure`, `Path=/`, no `Domain` | Stops a subdomain from setting/overwriting your session cookie (cookie tossing) |
| `__Secure-` prefix | Browser enforces `Secure` | Weaker than `__Host-` |
| `Max-Age` / `Expires` | Persistent cookie; omit for session cookie | Persistent only for deliberate "remember me" |

**Site versus origin.** SameSite uses **site** = scheme + registrable domain (eTLD+1). `a.example.com` and
`b.example.com` are the **same site**, so SameSite does nothing between them. That is why a takeover of any
subdomain (dangling DNS to a deleted cloud bucket is the usual way) is so damaging, and why `__Host-` matters.

**Third-party cookies.** Browsers have restricted third-party cookies (Safari and Firefox block them by default;
Chrome's plans have shifted several times, so do not design around their availability either way). Anything
that relied on an IdP cookie inside a hidden iframe, such as legacy "silent renew" in SPAs or OIDC session
management iframes, is unreliable. This is one more reason to prefer the BFF (§6).

---

## 3. Server-side sessions: design and lifecycle

**Session ID generation.** ≥128 bits from a CSPRNG (`secrets.token_urlsafe(32)` gives 256). Never derive from
user ID, time, or a counter.

**Store the hash, not the ID.** Store `SHA-256(session_id)` as the key. A read of the session table (backup,
replica, SQL injection) then does not yield usable cookies. A fast hash is correct here because the input already
has 256 bits of entropy; there is nothing to brute-force. (Detail and the general rule in ch 08 §4.)

**What a session row holds:**

```
session_hash (PK) | user_id | tenant_id | created_at | last_seen_at | absolute_expiry
auth_time | amr (["pwd","otp"] / ["hwk"]) | acr | ip_first | ua_first | device_id
csrf_secret | idp_sid (for back-channel logout) | refresh_token_ref (BFF) | revoked_at
```

**Lifecycle rules:**

1. **Rotate the ID on login and on every privilege change** (MFA completed, role elevated, user switched
   tenant). Otherwise **session fixation**: an attacker plants a known session ID before login and inherits the
   authenticated session afterward.
2. **Idle timeout** (e.g. 30 minutes for admin, hours to days for consumer) **and absolute timeout** (e.g. 8–24
   hours workforce, 30 days consumer with re-auth for sensitive actions). Idle alone lets a stolen cookie live
   forever if used regularly.
3. **Logout deletes server state**, then clears the cookie. Clearing only the cookie leaves a valid session for
   anyone who copied it.
4. **"Log out everywhere"** = delete all rows for the user. Do this on password change, recovery, and MFA reset.
5. **Index by user** so you can list a user's sessions ("active devices" page) and revoke one.

**Store choice:** Redis (TTL native, ~sub-millisecond) for hot data, with the session table in Postgres if you
need durability and audit. At scale, the session store is on the request path for *every* request; cache-aside
in process with a few seconds TTL is common, which sets your revocation lag to those few seconds.

---

## 4. CSRF: what it is and why SameSite only mostly fixes it

> **In plain words.** Your browser attaches cookies to requests no matter which website *caused* the request.
> CSRF abuses that: evil.com makes your browser POST to bank.com, and your bank cookie goes along.
>
> **Real-world example.** `<form action="https://bank.example/transfer" method="POST">` auto-submitted by
> JavaScript on a malicious page, back when cookies defaulted to being sent on cross-site POSTs.

`SameSite=Lax` blocks cross-site POSTs, fetches and iframes, which covers the classic attack. Remaining gaps:

- **Same-site attackers**: a compromised or user-content subdomain is same-site.
- **GET requests that change state**: Lax still sends cookies on top-level GET navigation. Never mutate on GET.
- **Lax-by-default edge**: Chrome's default-Lax (when no SameSite is set) historically allowed cross-site POST
  for 2 minutes after cookie creation. Set SameSite explicitly.
- **Older or embedded browsers** that ignore SameSite.

Defense in depth, in order of simplicity:

1. `SameSite=Lax` (or `Strict`) set explicitly.
2. **Fetch Metadata**: reject state-changing requests whose `Sec-Fetch-Site` is `cross-site` (and decide on
   `same-site`). All modern browsers send it.
3. **Origin check**: for non-GET requests, require `Origin` to be in your allowlist.
4. **Synchronizer token** or **signed double-submit cookie** for forms, if you must support old clients.

APIs authenticated by an `Authorization: Bearer` header are not CSRF-able (the browser doesn't attach headers
automatically); APIs authenticated by cookies are. A BFF uses cookies, so it needs the above.

---

## 5. XSS: why it beats every token storage choice

If attacker script runs on your origin, it can call your API *as the user from inside the page*, regardless of
where the token lives:

| Token location | Can XSS **steal** it? | Can XSS **use** it in-page? |
|---|---|---|
| `localStorage` / `sessionStorage` | Yes, trivially | Yes |
| JS memory (closure) | Usually (hook `fetch`, read from app state) | Yes |
| `HttpOnly` cookie | **No** | Yes (requests from the page carry it) |
| Web Worker holding token | Harder, not impossible | Yes, via the worker's messages |

So `HttpOnly` doesn't stop XSS from acting, but it stops **exfiltration**: the attacker must act while the victim's
tab is open, cannot replay from their own machine for days, and cannot sell the token. That is a big reduction in
blast radius, and it is the main argument for the BFF.

Primary XSS defenses: framework auto-escaping, no `innerHTML`/`dangerouslySetInnerHTML` with untrusted data, a
strict **Content Security Policy** (nonce- or hash-based `script-src`, no `unsafe-inline`), **Trusted Types**,
and sanitizing any user HTML with a maintained sanitizer (DOMPurify).

---

## 6. SPAs: backend-for-frontend versus tokens in the browser

> **In plain words.** A single-page app is JavaScript in a browser. Anything it holds, an XSS bug can take. The
> BFF pattern moves OAuth tokens to a small server on the same site, and the browser only gets an `HttpOnly`
> cookie.
>
> **Real-world example.** `app.example.com` serves the React bundle and a `/bff/*` route. The BFF runs the OIDC
> code flow as a *confidential* client, stores the access and refresh tokens server-side, and proxies
> `/bff/api/*` to the real APIs, attaching the access token.

The IETF's *OAuth 2.0 for Browser-Based Applications* (draft-ietf-oauth-browser-based-apps) describes three
architectures, from most to least secure:

| Pattern | Tokens live | Client type | Trade-off |
|---|---|---|---|
| **BFF** | Server only | Confidential | Most secure; needs a server, cookie + CSRF handling, proxies API traffic |
| **Token-mediating backend** | Access token in browser, refresh token on server | Confidential | Less infra on the request path; access token exposed to XSS |
| **Browser-only (public client)** | Browser | Public | No server; code + PKCE, refresh-token rotation, DPoP if possible; tokens exposed to XSS |

```
BFF:
Browser ──cookie──▶ app.example.com/bff ──Bearer AT──▶ api.example.com
   ▲                    │  holds AT + RT (encrypted, server-side)
   └── HttpOnly; Secure; SameSite=Lax; __Host-
```

Decision: **BFF by default** for anything with real data. Browser-only is defensible for low-risk apps or where
no server can exist, and then use **refresh-token rotation** (ch 07) and keep the access token in memory, not
`localStorage`. Full comparison with mobile, CLI and agents in [ch 09](09-where-clients-keep-tokens.md).

---

## 7. CORS is not an authorization mechanism

CORS relaxes the browser's same-origin policy for **reading responses**. It does not stop requests from being
sent, and it does nothing for non-browser clients (`curl` ignores it).

Mistakes that create vulnerabilities:

- Reflecting any `Origin` into `Access-Control-Allow-Origin` together with `Access-Control-Allow-Credentials:
  true`: any website can read authenticated responses.
- Allowing `null` origin (sandboxed iframes and `file://` send it).
- Regex allowlists like `^https://.*example\.com$` that match `https://evilexample.com`.

Rule: exact-match an allowlist; send credentials-enabled CORS only to origins you own; and never treat "CORS
blocked it" as the reason an endpoint is safe.

---

## 8. Session theft and binding sessions to devices

Infostealer malware (RedLine, Lumma and successors) copies browser cookie databases and ships them to
marketplaces; attackers import the cookies and skip both password and MFA. AitM phishing (ch 01 §4) does the same
in real time. Defenses:

- **Short absolute lifetimes** for privileged sessions; re-auth for sensitive actions (§9).
- **Anomaly detection**: the same session from a new ASN/country/UA within minutes → step up or kill.
- **Device-bound sessions.** Chrome's **Device Bound Session Credentials (DBSC)** has the browser generate a
  TPM-backed key pair per session and prove possession periodically to refresh a short-lived cookie, so a
  copied cookie expires quickly and cannot be refreshed elsewhere. Treat availability as browser-dependent and
  keep server-side anomaly detection regardless.
- **Token binding for APIs** is DPoP or mTLS (ch 07 §5), the API-side analogue.

---

## 9. Timeouts, re-authentication and "remember me"

| Account / action | Idle | Absolute | Re-auth for |
|---|---|---|---|
| Consumer app | days | 30–90 days | Password/email change, payment method, deleting account |
| Workforce SaaS | 1–8 h | 8–24 h | Admin console, exporting data, changing MFA |
| Admin / production access | 15–30 min | 1–4 h | Every destructive action, or just-in-time elevation |
| Banking (PSD2 SCA) | 5 min | — | Payments (strong customer authentication with dynamic linking) |

Re-authentication should check **freshness** (`auth_time` within N minutes) and **strength** (`amr`/`acr`
includes a phishing-resistant factor), and update the session's `auth_time` and `amr` after success. "Remember
me" should extend only the *low-privilege* session; sensitive actions still re-auth.

---

## 10. Code: a minimal secure session layer

```python
import hashlib, secrets, time
from dataclasses import dataclass

IDLE, ABSOLUTE = 30 * 60, 12 * 3600

@dataclass
class Session:
    user_id: str
    created_at: float
    last_seen_at: float
    auth_time: float
    amr: list[str]
    csrf_secret: str

def _key(sid: str) -> str:
    return "sess:" + hashlib.sha256(sid.encode()).hexdigest()   # store hash, never the raw ID

class SessionStore:
    def __init__(self, redis):
        self.r = redis

    def create(self, user_id: str, amr: list[str]) -> str:
        sid = secrets.token_urlsafe(32)                          # 256-bit
        now = time.time()
        s = Session(user_id, now, now, now, amr, secrets.token_urlsafe(32))
        self.r.set(_key(sid), serialize(s), ex=IDLE)
        self.r.sadd(f"user_sess:{user_id}", _key(sid))           # for "log out everywhere"
        return sid

    def load(self, sid: str) -> Session | None:
        raw = self.r.get(_key(sid))
        if not raw:
            return None
        s = deserialize(raw)
        now = time.time()
        if now - s.created_at > ABSOLUTE:
            self.destroy(sid, s.user_id)
            return None
        s.last_seen_at = now
        self.r.set(_key(sid), serialize(s), ex=IDLE)             # sliding idle timeout
        return s

    def rotate(self, old_sid: str, s: Session) -> str:
        """Call after login, MFA step-up, role change: defeats session fixation."""
        self.destroy(old_sid, s.user_id)
        new_sid = secrets.token_urlsafe(32)
        self.r.set(_key(new_sid), serialize(s), ex=IDLE)
        self.r.sadd(f"user_sess:{s.user_id}", _key(new_sid))
        return new_sid

    def destroy(self, sid: str, user_id: str) -> None:
        self.r.delete(_key(sid))
        self.r.srem(f"user_sess:{user_id}", _key(sid))

    def destroy_all(self, user_id: str) -> None:
        keys = self.r.smembers(f"user_sess:{user_id}")
        if keys:
            self.r.delete(*keys)
        self.r.delete(f"user_sess:{user_id}")

COOKIE = "__Host-sid={sid}; Path=/; Secure; HttpOnly; SameSite=Lax"

def check_csrf(method: str, headers: dict, allowed_origins: set[str]) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = headers.get("origin")
    return origin is None or origin in allowed_origins   # None: very old clients; tighten if you can
```

(`serialize`/`deserialize` are your JSON helpers. In production prefer your framework's session middleware
configured with these properties over hand-rolled code; the point here is the list of properties.)

---

## 11. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| JWT in `localStorage` as the session | XSS exfiltrates; no revocation | `HttpOnly` cookie + server session (BFF) |
| `Domain=.example.com` on the session cookie | Every subdomain receives it | Host-only, `__Host-` prefix |
| No ID rotation after login | Session fixation | Rotate on login and privilege change |
| Logout only clears the cookie | Copied cookie still valid | Delete server state |
| Idle timeout only | Stolen session lives forever with use | Idle **and** absolute |
| State change via GET | CSRF via links and images | POST/PUT/DELETE + CSRF checks |
| `Access-Control-Allow-Origin: <reflected>` + credentials | Any site reads user data | Exact allowlist |
| Storing raw session IDs in the DB | DB read = session hijack | Store SHA-256 of the ID |
| "Silent renew" via hidden IdP iframe | Breaks under third-party cookie blocking | BFF, or refresh-token rotation |

---

## 12. Interview questions

**Q1. Why `HttpOnly` if XSS can still make requests as the user?**
It prevents exfiltration. The attacker must act inside the live tab, cannot replay the session from their own
infrastructure for days, and cannot sell it. Combined with short sessions, that sharply limits blast radius.

**Q2. Does `SameSite=Lax` fully prevent CSRF?**
No. It doesn't apply between same-site subdomains, still sends cookies on top-level GETs, and depends on browser
support. Add Fetch Metadata / Origin checks and never mutate on GET.

**Q3. Design auth for a React SPA calling three internal APIs.**
BFF on the same site as the SPA: confidential OIDC client with PKCE, tokens stored server-side (encrypted),
`__Host-` `HttpOnly` `SameSite=Lax` session cookie, CSRF via Fetch Metadata/Origin check, BFF proxies API calls
with audience-specific access tokens obtained by refresh or token exchange, back-channel logout handled to kill
the BFF session.

**Q4. What is session fixation and how do you prevent it?**
The attacker gets the victim to use a session ID the attacker knows (via a URL parameter, or a cookie set from a
sibling subdomain), the victim logs in, and that ID becomes authenticated. Prevent by issuing a new ID at login
and on privilege changes, never accepting IDs from URLs, and using `__Host-` cookies.

---

## Sources

- RFC 6265, HTTP State Management Mechanism; and the update draft-ietf-httpbis-rfc6265bis: https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/
- OAuth 2.0 for Browser-Based Applications (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
- OWASP Session Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- OWASP CSRF Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- W3C Fetch Metadata Request Headers: https://www.w3.org/TR/fetch-metadata/
- Device Bound Session Credentials (W3C WebAppSec explainer): https://github.com/w3c/webappsec-dbsc
- MDN, Set-Cookie (prefixes and SameSite): https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie
