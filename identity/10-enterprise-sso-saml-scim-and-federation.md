# 10 — Enterprise SSO: SAML, SCIM, B2B federation and continuous access

> **Prerequisites:** [`05-openid-connect.md`](05-openid-connect.md), [`00-mental-models.md`](00-mental-models.md) (§2.1
> IdP topologies, §8 lifecycle).
>
> **Feeds into:** [`18-designing-an-identity-platform.md`](18-designing-an-identity-platform.md) (B2B SaaS identity
> architecture), [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md) (ID-JAG for
> enterprise-managed MCP access).
>
> **THESIS:** selling to enterprises means accepting that **the customer's IdP, not yours, is the source of truth for who
> works there**. Your system becomes a relying party for hundreds of IdPs at once (SAML and OIDC), receives account
> lifecycle events from them (SCIM), and increasingly must react to their risk signals in real time (Shared Signals /
> CAEP). The hard engineering isn't the login redirect; it's **routing each user to the right IdP, trusting each IdP
> only for its own users, and making deprovisioning actually remove access everywhere, quickly.**

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Workforce identity: what enterprises expect](#1-workforce-identity-what-enterprises-expect)
2. [SAML 2.0 in depth, and why it keeps producing CVEs](#2-saml-20-in-depth-and-why-it-keeps-producing-cves)
3. [SAML versus OIDC](#3-saml-versus-oidc)
4. [Multi-tenant B2B SSO: routing, trust and linking](#4-multi-tenant-b2b-sso-routing-trust-and-linking)
5. [Provisioning and deprovisioning with SCIM 2.0](#5-provisioning-and-deprovisioning-with-scim-20)
6. [Continuous access: Shared Signals, CAEP and RISC](#6-continuous-access-shared-signals-caep-and-risc)
7. [Cross-app access: the Identity Assertion Authorization Grant (ID-JAG)](#7-cross-app-access-the-identity-assertion-authorization-grant-id-jag)
8. [Federation at scale: metadata aggregates and OpenID Federation](#8-federation-at-scale-metadata-aggregates-and-openid-federation)
9. [Inside the corporate network: Kerberos, AD and LDAP](#9-inside-the-corporate-network-kerberos-ad-and-ldap)
10. [Code: home realm discovery and a SCIM deprovisioning handler](#10-code-home-realm-discovery-and-a-scim-deprovisioning-handler)
11. [Anti-patterns](#11-anti-patterns)
12. [Interview questions](#12-interview-questions)
13. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Big companies run one identity provider (Okta, Microsoft Entra ID, Google Workspace, Ping, JumpCloud) that knows all
their employees. When they buy your SaaS product they want three things:

1. **SSO**: employees log in to your product through *their* IdP (with their MFA policies), not with a password you
   store. Protocols: SAML 2.0 (older, XML) or OIDC.
2. **Provisioning**: when HR hires someone, an account appears in your product; when someone leaves, it is disabled
   immediately. Protocol: SCIM.
3. **Control**: their security team can see and revoke what your product (and any AI tool connected to it) can do,
   and your product reacts when they flag a session as risky. Protocols: Shared Signals/CAEP, and for AI-tool access,
   ID-JAG.

The traps: sending a user to the wrong IdP, trusting one customer's IdP to log in users of another customer, and
leaving access alive after someone is fired because their session or API token outlived their IdP account.

---

## 1. Workforce identity: what enterprises expect

The enterprise-readiness checklist your sales team will be handed:

| Requirement | What it means technically |
|---|---|
| SAML SSO and OIDC SSO | Per-tenant IdP connections |
| Enforce SSO | Disable password login for the customer's verified domains |
| SCIM provisioning | Users and groups created/updated/deactivated by the IdP |
| Group → role mapping | IdP groups drive your RBAC (ch 11) |
| Session controls | Max session length, re-auth, IP restrictions per tenant |
| Audit log export | Who did what, streamed to their SIEM |
| MFA/conditional access honored | Respect `amr`/`acr`; re-auth when their IdP says so |
| Admin roles, delegated admin | Customer admins manage their own tenant |
| Third-party app and token governance | List and revoke API tokens, OAuth grants, AI/MCP integrations |
| Real-time revocation | React to CAEP session-revoked / RISC account-disabled events |

---

## 2. SAML 2.0 in depth, and why it keeps producing CVEs

> **In plain words.** SAML is the 2005 ancestor of OIDC: the IdP sends the browser back to the app with a signed XML
> document (the *assertion*) that says who the user is. The idea is the same as an ID token. The difference is XML,
> and XML signatures are extremely easy to verify incorrectly.

### 2.1 The SP-initiated flow (HTTP-Redirect request, HTTP-POST response)

```
Browser ──GET app.example.com──▶ SP (your app)
SP ──302 to IdP SSO URL?SAMLRequest=<deflated, base64 AuthnRequest>&RelayState=...──▶ Browser ──▶ IdP
IdP authenticates user
IdP ──HTML auto-submit form POST to SP's ACS URL: SAMLResponse=<base64 signed XML>, RelayState──▶ SP
SP validates the Response/Assertion, creates session
```

IdP-initiated SSO (the user clicks a tile in the Okta dashboard and the IdP POSTs an unsolicited response) has no
request to bind to, so the SP can't check `InResponseTo`; it's more exposed to replay and CSRF-style login injection.
Support it if customers demand it, with short validity, one-time assertion IDs, and no deep-link redirects from
`RelayState`.

### 2.2 The assertion

```xml
<samlp:Response ID="_r1" InResponseTo="_req42" Destination="https://app.example.com/saml/acs" ...>
  <saml:Issuer>http://www.okta.com/exk123</saml:Issuer>
  <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
  <saml:Assertion ID="_a1" IssueInstant="2026-10-29T09:00:00Z">
    <saml:Issuer>http://www.okta.com/exk123</saml:Issuer>
    <ds:Signature> ... Reference URI="#_a1" ... </ds:Signature>
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">00u1abc</saml:NameID>
      <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
        <saml:SubjectConfirmationData InResponseTo="_req42" Recipient="https://app.example.com/saml/acs"
                                      NotOnOrAfter="2026-10-29T09:05:00Z"/>
      </saml:SubjectConfirmation>
    </saml:Subject>
    <saml:Conditions NotBefore="2026-10-29T08:59:30Z" NotOnOrAfter="2026-10-29T09:05:00Z">
      <saml:AudienceRestriction><saml:Audience>https://app.example.com/saml/metadata</saml:Audience></saml:AudienceRestriction>
    </saml:Conditions>
    <saml:AuthnStatement AuthnInstant="2026-10-29T08:59:58Z" SessionIndex="_s9">
      <saml:AuthnContext><saml:AuthnContextClassRef>...PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext>
    </saml:AuthnStatement>
    <saml:AttributeStatement> email, groups, ... </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>
```

Mapping to OIDC: `Issuer` ≈ `iss`, `NameID` ≈ `sub`, `Audience` ≈ `aud`, `InResponseTo` ≈ `nonce`/`state`,
`NotOnOrAfter` ≈ `exp`, `AuthnInstant` ≈ `auth_time`, `AuthnContextClassRef` ≈ `acr`, `SessionIndex` ≈ `sid`.

### 2.3 Why XML signatures are fragile

A JWS signs exact bytes (ch 03 §1). An XML signature signs **a node selected by reference** (`URI="#_a1"`) after
**canonicalization** and **transforms**. Verification answers "is *some* element with ID `_a1` signed?", while the
application then reads "the" assertion via its own XPath/DOM navigation. If those two pick different nodes, an attacker
can keep the signed original somewhere harmless and add an unsigned forged assertion where the application looks.
That's **XML Signature Wrapping (XSW)**, documented in 2012 (Somorovsky et al., "On Breaking SAML") and rediscovered in
libraries ever since.

Bug classes and real examples:

| Class | Example |
|---|---|
| Signature wrapping (XSW) | Many SAML SPs in the 2012 study; recurring in later libraries |
| Comment / text-node truncation | 2018 (Duo Labs): `alice@acme.com<!---->.evil.com` parsed differently by canonicalizer and app, across several libraries (CVE-2017-11427 and siblings) |
| Parser differentials | ruby-saml **CVE-2024-45409** (signature bypass, GitLab affected) and **CVE-2025-25291 / 25292** (REXML vs Nokogiri parsing differently, found by GitHub Security Lab, March 2025) |
| Unsigned assertion accepted when Response is signed (or vice versa) | Checking the wrong element's signature |
| XXE / entity expansion | XML parser with external entities enabled |
| Accepting certificates from inside the message (`KeyInfo`) | Attacker signs with their own key |

Rules for SAML SPs:

1. **Use a maintained library** and track its advisories closely; this is the most CVE-prone code in most stacks.
2. Verify with the **IdP certificate from configured metadata**, never from `KeyInfo` in the message.
3. Require the **Assertion** to be signed (and optionally the Response); after verification, **use only the
   verified node** (the library should return it) for every subsequent read.
4. Check `Destination`, `Recipient`, `Audience`, `InResponseTo` (for SP-initiated), `NotBefore`/`NotOnOrAfter`
   with small skew, `Issuer` equals the configured IdP entity ID **for this tenant**.
5. Reject replays: cache assertion IDs until `NotOnOrAfter`.
6. Disable DTDs and external entities in the XML parser.
7. Prefer encrypted assertions when attributes include sensitive data; SAML encryption has its own pitfalls (use
   AES-GCM, not CBC, where the IdP supports it).

---

## 3. SAML versus OIDC

| | SAML 2.0 | OIDC |
|---|---|---|
| Format | XML, XML-DSig | JSON, JWS |
| Transport | Browser POST of assertion to ACS | Code via redirect, token via back channel |
| Mobile/SPA/API friendliness | Poor | Good |
| API access tokens | No (SAML bearer grant exists, rare) | Yes, natively |
| Discovery | XML metadata documents | `.well-known` (ch 06) |
| Logout | SAML SLO (front-channel, unreliable) | Back-channel logout |
| Enterprise prevalence | Universal | Universal for modern IdPs |
| Security track record | Many signature bugs | Fewer, mostly config bugs |

Recommendation: **support both** for enterprise customers (some regulated customers only offer SAML), implement OIDC
first, and put both behind one internal abstraction (or a broker like Keycloak/Auth0/WorkOS/Okta) so your app receives
one normalized identity.

---

## 4. Multi-tenant B2B SSO: routing, trust and linking

### 4.1 Home realm discovery (HRD)

The login page must decide **which IdP** to send a user to before authenticating them:

| Method | How | Notes |
|---|---|---|
| Email domain | User types `alice@acme.com` → tenant with verified domain `acme.com` → Acme's IdP | Most common; requires **domain verification** (DNS TXT) so tenant X can't claim `gmail.com` or tenant Y's domain |
| Tenant subdomain | `acme.app.example.com` → Acme's IdP | Simple and unambiguous; leaks customer list via DNS |
| Tenant slug / org picker | `app.example.com/login?org=acme` | Good for users in multiple orgs |
| IdP-initiated | Click tile at IdP | No discovery needed; see §2.1 caveats |

Ambiguity is real: a contractor with `alice@gmail.com` belongs to three customer tenants. Let the user pick the org after
authenticating, or use per-org invite links.

### 4.2 Trust each IdP only for its own tenant

The critical invariant: **an assertion from Acme's IdP may only log users into Acme's tenant.** Implement it by:

- Storing per connection: `tenant_id`, IdP entity ID / issuer, certificates or JWKS, allowed email domains.
- After validation, look up the **identity by `(connection_id, subject)`** and require the resulting user to belong to the
  connection's tenant.
- Never look up an existing user by email across tenants and log them in because Acme's IdP asserted that email. A
  malicious tenant admin controls their IdP and can assert any email, including `ceo@othercustomer.com`.
- Only allow an IdP to assert emails in **domains verified by that tenant**; otherwise treat the email as unverified.

### 4.3 Brokering

Instead of integrating every customer IdP into every service, run an **identity broker** (your own IdP: Keycloak,
Auth0, Okta CIC, WorkOS, Zitadel, Ory, Cognito) that federates upstream to customer IdPs and issues uniform OIDC tokens
downstream, with `tenant_id` and normalized roles as claims. Apps then trust one issuer. The broker owns HRD, SAML
parsing, SCIM, and connection management.

### 4.4 JIT provisioning versus SCIM

- **Just-in-time (JIT)**: create the account at first SSO login from assertion attributes. Simple, but you learn about a
  user only when they log in, and you **never learn they left** (they just stop logging in, while their API tokens and
  sessions keep working).
- **SCIM**: the IdP pushes create/update/deactivate. Required for real deprovisioning. Use both: JIT as fallback, SCIM as
  source of truth.

---

## 5. Provisioning and deprovisioning with SCIM 2.0

SCIM (RFC 7643 schema, RFC 7644 protocol) is REST + JSON for `/Users` and `/Groups`, with the IdP as the client and your
app as the server, authenticated with a per-tenant bearer token (store hashed, ch 08 §4) or OAuth.

```http
PATCH /scim/v2/Users/2819c223-7f76-453a-919d-413861904646
Content-Type: application/scim+json
Authorization: Bearer <tenant SCIM token>

{ "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
  "Operations": [ { "op": "replace", "path": "active", "value": false } ] }
```

**Deprovisioning must cascade.** "active: false" is meaningless unless it reaches everything that grants access:

```
SCIM deactivate ─┬─▶ users.status = disabled
                 ├─▶ delete all app sessions (ch 02 §3)
                 ├─▶ revoke refresh tokens and OAuth grants the user gave to third-party apps
                 ├─▶ bump users.tokens_valid_after (kills outstanding JWTs at verifiers that check it)
                 ├─▶ disable or reassign personal API keys / PATs
                 ├─▶ revoke connected accounts / agent grants made by the user (ch 08 §10)
                 ├─▶ stop scheduled jobs and autonomous agents running under the user's authority
                 ├─▶ emit CAEP session-revoked to downstream services (§6)
                 └─▶ audit event
```

Practical SCIM issues:

- IdP implementations vary (Okta and Entra send different PATCH shapes, filters like `userName eq "x"`, and group
  membership styles). Test against each major IdP; OpenID/SCIM conformance tooling and vendor test suites help.
- **Idempotency and ordering**: retries and out-of-order updates happen. Use `externalId` + version/`meta.lastModified`.
- **Hard delete vs deactivate**: most IdPs deactivate; keep the account (audit, ownership of objects) but remove access.
- **Group explosion**: map only the groups the tenant configures, not all 3,000 groups of the enterprise.

---

## 6. Continuous access: Shared Signals, CAEP and RISC

> **In plain words.** Tokens and sessions are decisions made at login time. Shared Signals lets the IdP (and other
> systems) tell your app *after* login that something changed: "this session was revoked", "this user's credentials
> changed", "this device is no longer compliant", so you can act within seconds instead of at token expiry.

The OpenID Foundation approved three final specifications in September 2025:

| Spec | What |
|---|---|
| **Shared Signals Framework (SSF) 1.0** | How a transmitter and receiver set up a stream (push via HTTP POST or poll), with events as **Security Event Tokens** (SET, RFC 8417: signed JWTs with an `events` claim) |
| **CAEP 1.0** (Continuous Access Evaluation Profile) | Session and access events: `session-revoked`, `credential-change`, `device-compliance-change`, `assurance-level-change`, `token-claims-change` |
| **RISC 1.0** (Risk Incident Sharing and Coordination) | Account-level events: `account-disabled`, `account-purged`, `credential-compromise`, `identifier-changed` |

Example SET:

```json
{
  "iss": "https://idp.acme.example", "aud": "https://app.example.com", "iat": 1761730000, "jti": "...",
  "sub_id": { "format": "iss_sub", "iss": "https://idp.acme.example", "sub": "00u1abc" },
  "events": {
    "https://schemas.openid.net/secevent/caep/event-type/session-revoked": {
      "event_timestamp": 1761729990, "reason_admin": {"en": "Account compromised"}
    }
  }
}
```

Receiver rules: validate like any JWT (issuer per tenant, `aud`, signature, `jti` dedupe), act idempotently, and treat
it as a hint to **re-evaluate** (kill sessions, force re-auth), not as a remote-control channel that can grant access.
Microsoft's Continuous Access Evaluation, Google's RISC support and Okta's/Apple's implementations are the production
precedents.

---

## 7. Cross-app access: the Identity Assertion Authorization Grant (ID-JAG)

> **In plain words.** Today, when an employee connects app A (say, an AI assistant) to app B (say, a wiki), they click
> through an OAuth consent screen at B, and the company's IT team can't see or control that connection. ID-JAG puts the
> company's IdP in the middle: app A asks the IdP "may I access B for Alice?", the IdP checks policy, and hands A a
> signed grant that B's authorization server accepts.

Mechanics (`draft-ietf-oauth-identity-assertion-authz-grant`, revision -04 May 2026, an OAuth WG draft; marketed by
Okta as **Cross App Access (XAA)**):

```
1. App A has Alice's ID token from the enterprise IdP (she logged into A via SSO).
2. A → IdP token endpoint: RFC 8693 token exchange
      subject_token = Alice's ID token, requested_token_type = urn:ietf:params:oauth:token-type:id-jag,
      audience = B's authorization server, resource = B's API, scope = ...
   IdP checks enterprise policy: is A allowed to access B for this user? → issues ID-JAG (short-lived signed JWT
   for one user, one requesting client, one target AS)
3. A → B's authorization server: RFC 7523 JWT bearer grant with assertion = ID-JAG
   B's AS trusts the enterprise IdP (it already does, for SSO) → issues B's own access token
4. A calls B's API with that token.
```

If the user signed in to A with SAML rather than OIDC, A first exchanges the SAML assertion at the IdP for a refresh
token and uses that as the subject token. A resource AS advertises support with
`urn:ietf:params:oauth:grant-profile:id-jag` in `authorization_grant_profiles_supported` in its metadata.

Why it matters: per-user consent screens disappear for IT-approved pairs, admins get a central list of which apps
(and which AI agents) can reach which other apps, and revoking at the IdP cuts the link. Note what the IdP does *not*
see: it takes part in issuing the token, not in the traffic that follows. The MCP project publishes it as the
**Enterprise-Managed Authorization** extension, marked *stable* and versioned with the 2026-07-28 protocol revision in the
`ext-auth` repository, so enterprise AI clients can connect to MCP servers under IdP policy (ch 13 §7). The underlying
IETF document is still a draft: wrap it behind your own interface.

---

## 8. Federation at scale: metadata aggregates and OpenID Federation

Bilateral trust (each SP configures each IdP) doesn't scale to national research networks with thousands of entities.

- **SAML metadata aggregates**: a federation operator (InCommon, eduGAIN) publishes a signed XML file with all member
  IdPs and SPs; members trust the operator's signing key and refresh the aggregate daily.
- **OpenID Federation 1.0** (final, February 2026): each entity publishes a signed **entity configuration** at
  `/.well-known/openid-federation`; intermediates and trust anchors publish signed statements about subordinates;
  a relying party resolves a **trust chain** from the OP up to a trust anchor it already trusts, applying metadata
  policies (e.g. "all OPs in this federation must support PKCE") along the way. Used in Italian and other public-sector
  deployments and in the EU digital wallet ecosystem's trust frameworks. Federation 1.1 splits protocol-neutral and
  OIDC-specific parts.

---

## 9. Inside the corporate network: Kerberos, AD and LDAP

You'll still meet these behind enterprise firewalls and in hybrid setups:

- **Kerberos** (Active Directory's protocol): a Key Distribution Center issues a ticket-granting ticket at login and
  service tickets per service; symmetric-key cryptography; SSO across Windows services. Attacks you'll hear about:
  pass-the-hash, pass-the-ticket, Kerberoasting (offline cracking of service tickets for accounts with weak passwords),
  golden tickets (forged TGTs after `krbtgt` key theft).
- **LDAP**: directory protocol; "LDAP bind" authentication sends the password to the app, which forwards it to the
  directory. Avoid building new apps on it; if you must, use LDAPS and never log bind DNs with passwords.
- **Hybrid**: Entra ID Connect / Okta AD agents sync AD to the cloud IdP, which then speaks OIDC/SAML to SaaS. Your SaaS
  should only ever see the cloud IdP.

---

## 10. Code: home realm discovery and a SCIM deprovisioning handler

```python
from dataclasses import dataclass

@dataclass
class Connection:
    id: str
    tenant_id: str
    protocol: str            # "oidc" | "saml"
    issuer: str              # OIDC issuer or SAML IdP entity ID
    verified_domains: set[str]
    enforce_sso: bool

def home_realm(email: str, connections_by_domain: dict[str, Connection]) -> Connection | None:
    domain = email.rsplit("@", 1)[-1].lower().strip()
    return connections_by_domain.get(domain)          # only DNS-verified domains are in this map

def resolve_user(conn: Connection, subject: str, email: str | None, email_verified: bool, db):
    ident = db.identities.get(connection_id=conn.id, subject=subject)
    if ident:
        user = db.users.get(ident.user_id)
        if user.tenant_id != conn.tenant_id:          # invariant: an IdP only logs into its own tenant
            raise PermissionError("cross-tenant assertion")
        return user
    trusted_email = (email if email_verified and email
                     and email.rsplit("@", 1)[-1].lower() in conn.verified_domains else None)
    # JIT: create in this tenant only; never attach to a user found by email in another tenant
    user = db.users.create(tenant_id=conn.tenant_id, email=trusted_email)
    db.identities.create(connection_id=conn.id, subject=subject, user_id=user.id)
    return user

def scim_deactivate(tenant_id: str, scim_user_id: str, svc) -> None:
    user = svc.db.users.get_by_scim_id(tenant_id, scim_user_id)
    if user is None or user.status == "disabled":
        return                                         # idempotent
    with svc.db.transaction():
        svc.db.users.update(user.id, status="disabled", tokens_valid_after=svc.now())
        svc.sessions.destroy_all(user.id)
        svc.oauth.revoke_all_grants(user.id)           # refresh tokens + third-party app grants
        svc.api_keys.disable_owned_by(user.id)
        svc.vault.revoke_connected_accounts(user.id)   # calls providers' revoke endpoints asynchronously
        svc.agents.suspend_runs_for(user.id)
        svc.audit.log("user.deprovisioned", tenant=tenant_id, user=user.id, source="scim")
    svc.ssf.emit("session-revoked", tenant_id, user.id)
```

---

## 11. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| Matching SSO users to existing accounts by email across tenants | Tenant A's admin logs in as tenant B's users | `(connection, subject)` identities scoped to the connection's tenant |
| Unverified domain claims | Tenant claims `gmail.com` or a competitor's domain | DNS TXT verification |
| Trusting `KeyInfo` certificate in SAML responses | Attacker-signed assertions accepted | Certificates from configured metadata |
| Reading attributes from the DOM after verifying the signature | XSW | Use the library's verified assertion object |
| JIT only, no SCIM | Leavers keep access via sessions/tokens | SCIM deactivate with full cascade |
| SCIM deactivate that only flips a flag | Sessions, API keys, agent grants survive | Cascade (§5) |
| Ignoring `amr`/`acr` from customer IdP | Customer's MFA policy bypassed by your password fallback | Enforce SSO for verified domains |
| SAML SLO as the only logout mechanism | Unreliable | Short sessions + SCIM + CAEP |
| Permanent customer IdP certificate pinning without rollover | Logins break on IdP cert rotation | Metadata URL refresh, multiple certs during rollover |

---

## 12. Interview questions

**Q1. What is XML signature wrapping and how do you prevent it?**
The verifier checks the signature of a referenced node while the application reads a different (forged, unsigned) node.
Prevent by using a maintained library, verifying with configured certificates, and consuming only the node the library
returns as verified; keep the library patched.

**Q2. Design SSO for a B2B SaaS with 3,000 enterprise customers.**
Broker IdP with per-tenant OIDC/SAML connections; HRD via verified domains or org slugs; identities keyed by
`(connection, subject)` and pinned to the connection's tenant; enforce-SSO for verified domains; SCIM with cascading
deprovisioning; group-to-role mapping; CAEP receiver for session revocation; audit export.

**Q3. An employee is terminated at 09:00. List everything that must happen for access to end by 09:05.**
IdP disables the account (new logins fail) → SCIM deactivate → app disables user, deletes sessions, revokes refresh
tokens and OAuth grants, disables PATs/API keys, revokes agent/connected-account grants, bumps `tokens_valid_after` →
access tokens ≤5 min or CAEP-driven revocation at resource servers → audit.

**Q4. What does ID-JAG change for AI assistants connecting to enterprise apps?**
Access is granted by the enterprise IdP under admin policy rather than by per-user consent at each app; the IdP issues a
short-lived grant per user/client/target that the target's AS redeems for its own token. Admins can see and revoke
connections centrally.

---

## Sources

- OASIS SAML 2.0 Core and Bindings: https://docs.oasis-open.org/security/saml/v2.0/
- J. Somorovsky et al., "On Breaking SAML: Be Whoever You Want to Be" (USENIX Security 2012): https://www.usenix.org/conference/usenixsecurity12/technical-sessions/presentation/somorovsky
- Duo Labs, "Duo finds SAML vulnerabilities affecting multiple implementations" (2018): https://duo.com/blog/duo-finds-saml-vulnerabilities-affecting-multiple-implementations
- GitHub Security Lab, "Sign in as anyone: Bypassing SAML SSO authentication with parser differentials" (ruby-saml, 2025): https://github.blog/security/sign-in-as-anyone-bypassing-saml-sso-authentication-with-parser-differentials/
- NVD, CVE-2024-45409 (ruby-saml): https://nvd.nist.gov/vuln/detail/CVE-2024-45409
- RFC 7643 / RFC 7644, SCIM 2.0: https://datatracker.ietf.org/doc/html/rfc7644
- OpenID Shared Signals Framework 1.0, CAEP 1.0, RISC 1.0 (final, 2025): https://openid.net/wg/sharedsignals/
- RFC 8417, Security Event Token (SET): https://datatracker.ietf.org/doc/html/rfc8417
- Identity Assertion JWT Authorization Grant, draft-ietf-oauth-identity-assertion-authz-grant-04: https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/
- MCP authorization extensions (enterprise-managed authorization): https://github.com/modelcontextprotocol/ext-auth
- OpenID Federation 1.0: https://openid.net/specs/openid-federation-1_0.html
