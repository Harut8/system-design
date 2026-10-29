# 06 — `.well-known` discovery and metadata: what it is and why identity depends on it

> **Prerequisites:** [`04-oauth2-foundations-and-flows.md`](04-oauth2-foundations-and-flows.md) (endpoints the
> metadata points to), [`05-openid-connect.md`](05-openid-connect.md), [`03-jwt-jose-and-signing-keys.md`](03-jwt-jose-and-signing-keys.md)
> (JWKS).
>
> **Feeds into:** [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md) (MCP
> discovery is RFC 9728 → RFC 8414), [`16-decentralized-identity-and-verifiable-credentials.md`](16-decentralized-identity-and-verifiable-credentials.md)
> (issuer metadata, `did:web`), [`01-passwords-mfa-and-passkeys.md`](01-passwords-mfa-and-passkeys.md) (passkey
> well-knowns).
>
> **THESIS:** a well-known URI is a promise that **whoever controls a domain name also controls what this fixed path
> says about it**. That turns DNS + TLS, which every client already trusts, into a configuration channel: a client
> that knows only `https://login.example.com` can find the endpoints, supported features and current signing keys
> with no manual setup, and pick up key rotations automatically. In open ecosystems (any MCP client talking to any
> MCP server, any wallet talking to any issuer) discovery is not a convenience, it is the only way interoperation can
> work. The security rules are equally simple and frequently skipped: **the document must describe the identifier
> you fetched it for, you must not follow URLs from it into your private network, and you must cache it.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [RFC 8615: what a well-known URI is and why the prefix is reserved](#1-rfc-8615-what-a-well-known-uri-is-and-why-the-prefix-is-reserved)
2. [Why identity protocols need discovery](#2-why-identity-protocols-need-discovery)
3. [Authorization server metadata: OIDC Discovery and RFC 8414](#3-authorization-server-metadata-oidc-discovery-and-rfc-8414)
4. [Protected resource metadata: RFC 9728](#4-protected-resource-metadata-rfc-9728)
5. [The identity-related well-known catalogue](#5-the-identity-related-well-known-catalogue)
6. [Path insertion versus path appending: the multi-tenant trap](#6-path-insertion-versus-path-appending-the-multi-tenant-trap)
7. [The complete discovery chain, end to end](#7-the-complete-discovery-chain-end-to-end)
8. [Security: what can go wrong with discovery](#8-security-what-can-go-wrong-with-discovery)
9. [Operating metadata: caching, availability, versioning](#9-operating-metadata-caching-availability-versioning)
10. [Code: a defensive discovery client](#10-code-a-defensive-discovery-client)
11. [Anti-patterns](#11-anti-patterns)
12. [Interview questions](#12-interview-questions)
13. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Imagine you have to connect your app to a new identity provider. Without discovery, someone types into a config
file: the login URL, the token URL, the logout URL, which algorithms it uses, and pastes its public signing key.
When the provider rotates its key next month, your logins break until someone pastes the new one.

With discovery, you configure one thing, the **issuer** `https://login.example.com`. Your app fetches
`https://login.example.com/.well-known/openid-configuration` and gets a JSON document listing every endpoint and
a `jwks_uri` where the current public keys live. Key rotation is automatic because you re-fetch keys from that URL.

Why can you trust that document? Because only whoever controls `login.example.com` (proven by its TLS certificate)
can put a file at that path. The `/.well-known/` prefix is reserved by an internet standard so that a website's own
pages can't accidentally collide with these special files.

The same trick is used for many things: telling clients which authorization server protects an API (crucial for
MCP), letting a phone know which app may open a domain's links, telling password managers where your
"change password" page is, publishing a security contact, and publishing an AI agent's capabilities card.

---

## 1. RFC 8615: what a well-known URI is and why the prefix is reserved

> **In plain words.** `/.well-known/` is a folder name that every website agrees to reserve for machine-readable
> facts about the site. Standards register names inside it with IANA so they don't clash.

Before RFC 5785 (2010, updated by RFC 8615 in 2019), protocols claimed root paths: `/robots.txt`, `/favicon.ico`,
`/crossdomain.xml`. Every new protocol that did this risked colliding with a site's existing URL (a site might have a
page called `/openid` already) and claimed more of everyone's URL space.

RFC 8615 rules:

- One reserved prefix: `/.well-known/` at the **root of an origin** (scheme + host + port). Never under a path.
- Names are registered in the **IANA Well-Known URIs registry**, with a spec reference, so `openid-configuration`
  means the same thing on every site.
- The origin is the authority: the content is "what this origin says about itself". This is the security
  property everything else leans on.

Why an origin is a good trust anchor for identity:

| Property | Provided by |
|---|---|
| Only the domain owner can serve content at `https://name/...` | DNS control + WebPKI TLS certificate for `name` |
| The client already knows the name | It is the issuer, the resource URL, the client ID, the email domain |
| Rotation without coordination | Owner updates the file; clients re-fetch on their cache schedule |
| Universal tooling | Every HTTP client, CDN and load balancer handles it |

The flip side: **trust in a well-known document is exactly as strong as control of the domain and its TLS**. A
dangling DNS record or a compromised CDN configuration lets an attacker publish your metadata.

---

## 2. Why identity protocols need discovery

Four problems discovery solves:

1. **Configuration explosion.** A client needs ~10 values per provider (endpoints, algorithms, PKCE methods, client
   auth methods, supported scopes, response types). An enterprise SaaS with 2,000 customer IdPs can't maintain that by
   hand; it stores 2,000 issuer URLs.
2. **Key rotation.** Signing keys must rotate (ch 03 §7). Clients that pin keys break on rotation; clients that fetch
   `jwks_uri` keep working.
3. **Capability negotiation.** "Does this AS support PKCE S256? PAR? DPoP? Client ID Metadata Documents?" is
   answered from metadata fields, letting clients pick the most secure option both sides support (and refuse to
   proceed if a required one is missing).
4. **Open ecosystems with no prior relationship.** An MCP client (an AI app) connecting to an MCP server it has never
   seen knows only the server's URL. It must discover: which authorization server protects this server, where that AS's
   endpoints are, how to register, what scopes to ask for. Without discovery, every pairing of client and server would
   need a human to configure it. With RFC 9728 → RFC 8414 → CIMD, the user pastes a URL and the client does the rest
   (ch 13 §3).

What discovery **does not** do: it doesn't establish *whether you should trust* this issuer. That decision (an
allowlist of issuers, a federation trust anchor, the user typing the URL) is yours. Discovery only tells you how to
talk to an issuer you have decided to trust.

---

## 3. Authorization server metadata: OIDC Discovery and RFC 8414

Two specs, nearly the same document:

| | OIDC Discovery 1.0 (2014) | RFC 8414 OAuth AS Metadata (2018) |
|---|---|---|
| Path | `/.well-known/openid-configuration` | `/.well-known/oauth-authorization-server` |
| For | OpenID Providers | Any OAuth AS (OIDC or not) |
| Path handling for issuers with a path | **Append**: `{issuer}/.well-known/openid-configuration` | **Insert**: `{origin}/.well-known/oauth-authorization-server{path}` |

A typical document (fields abridged):

```json
{
  "issuer": "https://login.example.com",
  "authorization_endpoint": "https://login.example.com/oauth2/authorize",
  "token_endpoint": "https://login.example.com/oauth2/token",
  "jwks_uri": "https://login.example.com/oauth2/jwks",
  "userinfo_endpoint": "https://login.example.com/oauth2/userinfo",
  "end_session_endpoint": "https://login.example.com/oauth2/logout",
  "revocation_endpoint": "https://login.example.com/oauth2/revoke",
  "introspection_endpoint": "https://login.example.com/oauth2/introspect",
  "pushed_authorization_request_endpoint": "https://login.example.com/oauth2/par",
  "registration_endpoint": "https://login.example.com/oauth2/register",
  "device_authorization_endpoint": "https://login.example.com/oauth2/device",
  "scopes_supported": ["openid", "profile", "email", "offline_access"],
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials",
                            "urn:ietf:params:oauth:grant-type:token-exchange"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["private_key_jwt", "tls_client_auth", "none"],
  "id_token_signing_alg_values_supported": ["RS256", "ES256"],
  "dpop_signing_alg_values_supported": ["ES256"],
  "authorization_response_iss_parameter_supported": true,
  "require_pushed_authorization_requests": false,
  "client_id_metadata_document_supported": true,
  "backchannel_logout_supported": true,
  "subject_types_supported": ["public", "pairwise"]
}
```

The fields that drive security decisions:

| Field | Client should |
|---|---|
| `issuer` | **Equal exactly** the issuer you derived the URL from; otherwise reject (§8.1) |
| `code_challenge_methods_supported` | Contain `S256`; MCP clients MUST refuse otherwise |
| `authorization_response_iss_parameter_supported` | If `true`, require `iss` in responses (RFC 9207) |
| `token_endpoint_auth_methods_supported` | Prefer `private_key_jwt` / mTLS over secrets |
| `dpop_signing_alg_values_supported` | If present, use DPoP (ch 07 §5) |
| `require_pushed_authorization_requests` | If `true`, use PAR (ch 07 §2) |
| `client_id_metadata_document_supported` | If `true`, MCP-style clients can use a URL as `client_id` (ch 13 §4) |
| `jwks_uri` | Fetch keys; host should normally be the issuer's own |

---

## 4. Protected resource metadata: RFC 9728

> **In plain words.** RFC 8414 lets you find out about an *authorization server* once you know it. RFC 9728 answers
> the question before that: "I have an API URL; which authorization server do I need a token from?"
>
> **Real-world example.** A user adds `https://mcp.tracker.example/mcp` to their AI app. The app calls it without a
> token, gets `401` with a `WWW-Authenticate` header pointing at
> `https://mcp.tracker.example/.well-known/oauth-protected-resource/mcp`. That document says
> `"authorization_servers": ["https://auth.tracker.example"]`. Now the client knows where to go.

RFC 9728 (April 2025) defines:

1. A metadata document at `/.well-known/oauth-protected-resource` (with **path insertion** for resources that have a
   path).
2. A `resource_metadata` parameter in the `WWW-Authenticate: Bearer` challenge, so a 401 can point to it directly.

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://mcp.tracker.example/.well-known/oauth-protected-resource/mcp",
                         scope="issues.read"
```

```json
{
  "resource": "https://mcp.tracker.example/mcp",
  "authorization_servers": ["https://auth.tracker.example"],
  "scopes_supported": ["issues.read", "issues.write"],
  "bearer_methods_supported": ["header"],
  "resource_signing_alg_values_supported": ["ES256"],
  "dpop_bound_access_tokens_required": false,
  "resource_name": "Tracker MCP"
}
```

Rules from the RFC that matter:

- The `resource` value in the document **must equal** the resource identifier the client used to build the
  well-known URL; otherwise reject. This stops a server from claiming to speak for another resource.
- The `resource` value is what the client then sends as the RFC 8707 `resource` parameter, so the AS mints a token
  with that audience.
- Metadata can be **signed** (`signed_metadata` JWT) when a client trusts a third party that vouches for it.

The MCP authorization spec (2025-06-18 onward, unchanged in 2026-07-28) made RFC 9728 mandatory for MCP servers and
for clients' AS discovery, which is why RFC 9728 moved from obscure to everywhere in a year.

---

## 5. The identity-related well-known catalogue

| Path | Spec | Published by | Consumed by | Purpose |
|---|---|---|---|---|
| `openid-configuration` | OIDC Discovery 1.0 | OpenID Provider | RPs | Endpoints, algorithms, capabilities |
| `oauth-authorization-server` | RFC 8414 | OAuth AS | OAuth clients, MCP clients | Same for non-OIDC ASes |
| `oauth-protected-resource` | RFC 9728 | Resource server / MCP server | Clients | Which AS protects me, which scopes |
| `jwks.json` (convention only) | — | AS | Verifiers | Many ASes put keys here, but **the real location is whatever `jwks_uri` says**; don't hardcode |
| `webfinger` | RFC 7033 | Domain | OIDC clients | Map `acct:alice@acme.com` to an issuer (OIDC issuer discovery); rarely used outside fediverse |
| `openid-federation` | OpenID Federation 1.0 | Every federation entity | Federation peers | Signed entity configuration for trust chains (ch 10 §8) |
| `webauthn` | WebAuthn Level 3 (Related Origin Requests) | RP | Browsers | List of origins allowed to use this RP ID's passkeys |
| `passkey-endpoints` | W3C passkey endpoints | RP | Password managers | Where to enroll/manage passkeys |
| `change-password` | W3C | Site | Password managers, browsers | Redirect to the change-password page (used after breach alerts) |
| `apple-app-site-association` | Apple | Domain | iOS/macOS | Universal Links, passkey and password autofill association (`webcredentials`) |
| `assetlinks.json` | Google Digital Asset Links | Domain | Android | App Links, credential sharing between app and site |
| `security.txt` | RFC 9116 | Site | Researchers | Security contact for vulnerability reports |
| `openid-credential-issuer` | OpenID4VCI 1.0 | Credential issuer | Wallets | Credential types, endpoints (ch 16) |
| `jwt-vc-issuer` | SD-JWT VC draft | Issuer | Verifiers | Issuer keys for SD-JWT VCs |
| `did.json` | did:web method | Domain | DID resolvers | DID document for `did:web:example.com` |
| `agent-card.json` | A2A protocol | A2A agent | Other agents | Agent capabilities and declared security schemes (ch 13 §8) |
| `acme-challenge/...` | RFC 8555 | Domain (temporarily) | ACME CA | Proves domain control to get the TLS certificate the rest depends on |

Two in the list deserve a closer look, because they quietly secure mobile auth:

- **`apple-app-site-association` and `assetlinks.json`** are how a domain declares "my app with bundle/package ID X
  and signing certificate fingerprint Y may handle my `https` links". This turns `https://app.example.com/cb` into a
  **claimed** redirect URI that a malicious app can't intercept (unlike a custom scheme, ch 04 §11), and it's how the
  OS decides that a passkey for `example.com` may be used inside the native app.
- **`webauthn`** solves the "we have `example.com`, `example.co.uk` and `example.de`" problem: passkeys are bound to
  one RP ID, and this file on the RP ID's domain lists the other origins allowed to use them.

---

## 6. Path insertion versus path appending: the multi-tenant trap

Multi-tenant identity platforms give each tenant an issuer **with a path**: `https://auth.example.com/tenants/acme`.
Where does its metadata live?

```
OIDC Discovery (append):     https://auth.example.com/tenants/acme/.well-known/openid-configuration
RFC 8414 (insert):           https://auth.example.com/.well-known/oauth-authorization-server/tenants/acme
RFC 9728 (insert):           https://res.example.com/.well-known/oauth-protected-resource/v1/api
```

Why RFC 8414 inserts: RFC 8615 says well-known URIs live at the origin root, and appending violates that. OIDC
predates the rule. The result is that robust clients must try several locations. The MCP authorization spec spells out
the order for an issuer with a path component:

1. `https://{host}/.well-known/oauth-authorization-server/{path}`
2. `https://{host}/.well-known/openid-configuration/{path}`
3. `https://{host}/{path}/.well-known/openid-configuration`

and for an issuer without a path, `oauth-authorization-server` then `openid-configuration` at the root.

Whichever URL succeeds, the `issuer` field inside must equal the issuer you started from, byte for byte. That check is
what makes trying several URLs safe.

---

## 7. The complete discovery chain, end to end

What a modern client (an MCP client is the most complete example) does from "I have a URL" to "I have a token":

```
1. Call resource without token
   GET https://mcp.tracker.example/mcp                                  → 401
   WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp", scope="issues.read"

2. Fetch protected resource metadata (RFC 9728)
   GET https://mcp.tracker.example/.well-known/oauth-protected-resource/mcp
   check: doc.resource == "https://mcp.tracker.example/mcp"
   pick:  authorization_servers[0] = "https://auth.tracker.example"   (if several, client chooses)

3. Fetch AS metadata (RFC 8414 / OIDC Discovery, trying the URLs in §6)
   GET https://auth.tracker.example/.well-known/oauth-authorization-server
   check: doc.issuer == "https://auth.tracker.example"
   check: "S256" in code_challenge_methods_supported
   note:  client_id_metadata_document_supported, registration_endpoint, authorization_response_iss_parameter_supported

4. Get a client_id
   pre-registered? use it. Else CIMD supported? use client_id = "https://client.example/oauth/client.json".
   Else DCR (deprecated in MCP 2026-07-28): POST registration_endpoint. Else ask the user.

5. Authorization code + PKCE with resource=https://mcp.tracker.example/mcp and scope=issues.read
   check iss in the response against step 3's issuer (RFC 9207)

6. Token request, then call the resource with the token (audience = the resource).

7. Fetch jwks_uri only on the RS side (the MCP server validates tokens); clients treat access tokens as opaque.
```

Every arrow is a fetch of a URL that, at least indirectly, came from a party the client doesn't fully trust. §8 is
about that.

---

## 8. Security: what can go wrong with discovery

### 8.1 Issuer mismatch (impersonation and mix-up)

If a client fetches `https://good.example/.well-known/...` and the document says `"issuer":
"https://evil.example"` (or the reverse via redirect), accepting it lets one AS impersonate another. Rule: `issuer`
must equal the expected value exactly; do not follow redirects across hosts for metadata; record the validated issuer
for the RFC 9207 check (MCP 2026-07-28 makes the client record it alongside the PKCE verifier).

### 8.2 SSRF through discovery

URLs in `WWW-Authenticate` headers, PRM documents (`authorization_servers`), AS metadata (`jwks_uri`, endpoints), CIMD
`client_id`s and `logo_uri`s are attacker-influenced. A server-side component that fetches them (an AS fetching a
CIMD URL, a hosted MCP client or gateway fetching PRM) can be pointed at `http://169.254.169.254/latest/meta-data/` or
`http://localhost:6379/`. Rules for any server-side fetch of discovered URLs:

- `https` only; resolve DNS once and **block private, loopback, link-local and metadata IP ranges** on the resolved
  address (and on every redirect hop, or disable redirects).
- Tight timeouts and response size limits (metadata is a few KB).
- Don't return fetched content or errors verbatim to the requester (blind SSRF oracle).
- Fetch from an egress proxy that enforces the above.

### 8.3 Untrusted authorization servers

A malicious MCP server can list an attacker-run AS in its PRM. Discovery works perfectly; the user is then shown a
consent screen at the attacker's AS. Defense is a trust decision, not a protocol check: show the user which AS they're
being sent to, maintain allowlists in managed environments, and for enterprises use IdP-mediated authorization
(ID-JAG, ch 13 §7) so the corporate IdP decides which servers are allowed.

### 8.4 Endpoint hijack via metadata

Client trusts `token_endpoint` from metadata and sends client credentials there. If metadata is compromised (or
the client accepted a mismatched issuer), credentials go to the attacker. Defense: validated issuer, TLS,
`private_key_jwt` with `aud` = issuer (ch 04 §6) so a stolen assertion is useless elsewhere.

### 8.5 Domain control is the root

Dangling CNAMEs to deleted cloud resources, expired domains, and CDN misconfigurations let attackers serve your
well-known files. Monitor DNS for dangling records, use CAA records to restrict which CAs can issue your
certificates, and watch Certificate Transparency logs for unexpected certificates on identity domains.

### 8.6 Information disclosure

Metadata is public by design. Don't put internal hostnames, debugging endpoints or tenant lists in it.

---

## 9. Operating metadata: caching, availability, versioning

- **Clients cache** metadata and JWKS, honoring `Cache-Control`, with floors and ceilings (e.g. metadata 1–24 h,
  JWKS 5 min–24 h) and **serve stale on error**. An IdP metadata outage should not log out the world.
- **Publishers** serve these files from the CDN edge with explicit `Cache-Control`, and treat them as tier-0: a broken
  `openid-configuration` breaks every login.
- **Change management**: adding a field is safe; removing an endpoint or algorithm is a breaking change for cached
  clients. Treat JWKS changes with the rotation protocol in ch 03 §7.
- **Monitoring**: synthetic checks that fetch metadata and JWKS from several regions and validate them (issuer
  matches, keys parse, `kid`s unique).
- **CORS**: SPAs and browser-based MCP clients fetch metadata from JavaScript; serve `Access-Control-Allow-Origin: *`
  on these public documents (they carry no credentials).

---

## 10. Code: a defensive discovery client

```python
import ipaddress, socket, time
from urllib.parse import urlsplit
import httpx

BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10", "0.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")]

def _assert_public_https(url: str) -> None:
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("https URL required")
    for info in socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in n for n in BLOCKED_NETS):
            raise ValueError(f"blocked address for {u.hostname}")
    # NB: to defeat DNS rebinding, connect to the IP you checked (custom transport) or use an egress proxy.

def _get_json(url: str, max_bytes: int = 64_000) -> dict:
    _assert_public_https(url)
    with httpx.Client(follow_redirects=False, timeout=5) as c:
        r = c.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        if len(r.content) > max_bytes:
            raise ValueError("metadata too large")
        return r.json()

def as_metadata_urls(issuer: str) -> list[str]:
    u = urlsplit(issuer)
    base, path = f"{u.scheme}://{u.netloc}", u.path.rstrip("/")
    if path:
        return [f"{base}/.well-known/oauth-authorization-server{path}",
                f"{base}/.well-known/openid-configuration{path}",
                f"{base}{path}/.well-known/openid-configuration"]
    return [f"{base}/.well-known/oauth-authorization-server",
            f"{base}/.well-known/openid-configuration"]

_cache: dict[str, tuple[float, dict]] = {}

def discover_as(issuer: str, ttl: int = 3600) -> dict:
    hit = _cache.get(issuer)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    last_err = None
    for url in as_metadata_urls(issuer):
        try:
            doc = _get_json(url)
        except Exception as e:
            last_err = e
            continue
        if doc.get("issuer") != issuer:                       # exact match, no normalization
            raise ValueError("issuer mismatch in metadata")
        if "S256" not in doc.get("code_challenge_methods_supported", []):
            raise ValueError("AS does not support PKCE S256")
        _cache[issuer] = (time.time(), doc)
        return doc
    if hit:                                                   # serve stale on outage
        return hit[1]
    raise RuntimeError(f"discovery failed for {issuer}: {last_err}")

def prm_urls(resource: str) -> list[str]:
    u = urlsplit(resource)
    origin, path = f"{u.scheme}://{u.netloc}", u.path.rstrip("/")
    # MCP order: path-inserted first, then root.
    return ([f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []) + \
           [f"{origin}/.well-known/oauth-protected-resource"]

def discover_resource(resource: str, www_authenticate_url: str | None = None) -> dict:
    u = urlsplit(resource)
    origin = f"{u.scheme}://{u.netloc}"
    for url in ([www_authenticate_url] if www_authenticate_url else prm_urls(resource)):
        try:
            doc = _get_json(url)
        except Exception:
            continue
        # The document must speak for this resource (a root document may name the origin).
        if doc.get("resource") not in (resource, origin):
            raise ValueError("protected resource metadata is for a different resource")
        if not doc.get("authorization_servers"):
            raise ValueError("no authorization server advertised")
        return doc
    raise RuntimeError("no protected resource metadata found")
```

Note that `www_authenticate_url` comes from a server response, so it passes through the same SSRF guard, and the
`resource` check still applies to the document it returns. The MCP spec (2026-07-28) requires clients to use the
`WWW-Authenticate` URL when present and otherwise probe the path-inserted URL, then the root, exactly as above.

---

## 11. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Hardcoding endpoints and public keys | Breaks on rotation; config drift | Configure the issuer; discover the rest |
| Not checking `issuer` in fetched metadata | AS impersonation, mix-up | Exact match |
| Hardcoding `/.well-known/jwks.json` | Breaks for ASes with a different `jwks_uri` | Use `jwks_uri` |
| Server-side fetch of discovered URLs without SSRF guards | Cloud metadata theft, internal port scans | https-only, IP blocklist, no redirects, egress proxy |
| Fetching metadata on every request | Latency; IdP rate limits | Cache with TTL; serve stale on error |
| Only trying path-appended OIDC URL for path issuers | Fails on RFC 8414-only servers | Try insertion and append orders |
| Treating "discoverable" as "trusted" | Users sent to attacker ASes | Allowlists / user confirmation / IdP-mediated trust |
| Serving metadata from the app servers that also serve login | Metadata outage = login outage for all RPs | CDN, static, monitored |

---

## 12. Interview questions

**Q1. Why does `/.well-known/` exist instead of each protocol choosing its own root path?**
To avoid collisions with sites' existing URLs and to give protocols a registered, predictable location under the origin's
authority (RFC 8615). Registration with IANA makes each name's meaning global.

**Q2. Why do identity protocols rely on well-known metadata rather than configuration?**
Configuration doesn't scale to many providers, breaks on key rotation and can't negotiate capabilities; in open
ecosystems like MCP there's no prior relationship to configure. Discovery uses domain control, already proven by TLS, as
the trust anchor for the metadata.

**Q3. What checks must a client perform on discovered metadata?**
`issuer` exactly equals the expected issuer; for RFC 9728, `resource` equals the resource URL; required capabilities
are present (S256); no cross-host redirects; and, for server-side fetchers, SSRF protections on every discovered URL.

**Q4. Walk through how an MCP client finds where to get a token for a server it has never seen.**
401 with `resource_metadata` → fetch PRM, check `resource` → pick an `authorization_servers` entry → fetch AS metadata
via RFC 8414/OIDC (insertion then append), check `issuer` and S256 → get a client ID (pre-registered, CIMD, DCR) → code +
PKCE with `resource` → check `iss` → token → call the server.

---

## Sources

- RFC 8615, Well-Known Uniform Resource Identifiers: https://datatracker.ietf.org/doc/html/rfc8615
- IANA Well-Known URIs registry: https://www.iana.org/assignments/well-known-uris/well-known-uris.xhtml
- OpenID Connect Discovery 1.0: https://openid.net/specs/openid-connect-discovery-1_0.html
- RFC 8414, OAuth 2.0 Authorization Server Metadata: https://datatracker.ietf.org/doc/html/rfc8414
- RFC 9728, OAuth 2.0 Protected Resource Metadata: https://datatracker.ietf.org/doc/html/rfc9728
- RFC 7033, WebFinger: https://datatracker.ietf.org/doc/html/rfc7033
- RFC 9116, security.txt: https://datatracker.ietf.org/doc/html/rfc9116
- W3C, A Well-Known URL for Changing Passwords: https://w3c.github.io/webappsec-change-password-url/
- WebAuthn Level 3, Related Origin Requests: https://www.w3.org/TR/webauthn-3/#sctn-related-origins
- MCP specification 2026-07-28, Authorization (authorization server discovery): https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- OpenID Federation 1.0: https://openid.net/specs/openid-federation-1_0.html
- Apple, Supporting associated domains: https://developer.apple.com/documentation/xcode/supporting-associated-domains
- Google, Digital Asset Links: https://developers.google.com/digital-asset-links
