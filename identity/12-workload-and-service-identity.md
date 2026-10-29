# 12 — Workload identity and service-to-service trust

> **Prerequisites:** [`07-oauth-hardening-and-advanced-specs.md`](07-oauth-hardening-and-advanced-specs.md) (§5
> mTLS-bound tokens, §7 token exchange), [`09-where-clients-keep-tokens.md`](09-where-clients-keep-tokens.md) (§7).
> Kubernetes specifics: [`kubernetes/07-authentication-authorization.md`](../kubernetes/07-authentication-authorization.md),
> [`kubernetes/44-secrets-and-configmaps-deep-dive.md`](../kubernetes/44-secrets-and-configmaps-deep-dive.md).
>
> **Feeds into:** [`13-mcp-and-agent-protocol-authorization.md`](13-mcp-and-agent-protocol-authorization.md) (remote
> MCP servers and agent runtimes are workloads), [`15-agent-tool-permissions-hitl-and-long-running-loops.md`](15-agent-tool-permissions-hitl-and-long-running-loops.md).
>
> **THESIS:** inside a system, "the request came from our network" is not an identity. Every service needs its own
> cryptographic identity, **issued by the platform after attesting where the code runs, short-lived, rotated
> automatically, and never typed into a config file**. On top of that identity, a call between services must carry two
> answers: *which service is calling* (workload identity, usually mTLS) and *on whose behalf* (end-user context,
> usually a token). Most internal breaches exploit the absence of one of the two: flat networks where any compromised pod
> can call anything, or services that call downstream with their own god-mode credential for every user.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Non-human identities and why static secrets fail](#1-non-human-identities-and-why-static-secrets-fail)
2. [API keys and shared secrets: when they are acceptable](#2-api-keys-and-shared-secrets-when-they-are-acceptable)
3. [Service-to-service trust models compared](#3-service-to-service-trust-models-compared)
4. [mTLS, SPIFFE/SPIRE and service meshes](#4-mtls-spiffespire-and-service-meshes)
5. [Workload identity federation: CI and cross-cloud without keys](#5-workload-identity-federation-ci-and-cross-cloud-without-keys)
6. [Kubernetes service account tokens](#6-kubernetes-service-account-tokens)
7. [Propagating end-user context across services](#7-propagating-end-user-context-across-services)
8. [Request signing and webhooks](#8-request-signing-and-webhooks)
9. [Secrets that must exist: managers and dynamic secrets](#9-secrets-that-must-exist-managers-and-dynamic-secrets)
10. [Zero trust, east-west authorization and blast radius](#10-zero-trust-east-west-authorization-and-blast-radius)
11. [Code: CI OIDC trust check, webhook verification, user-context propagation](#11-code-ci-oidc-trust-check-webhook-verification-user-context-propagation)
12. [Anti-patterns](#12-anti-patterns)
13. [Interview questions](#13-interview-questions)
14. [Sources](#sources)

---

## Start here — the whole chapter in plain words

Services call other services all day. Two questions for every call:

1. **Which service is this?** The old answer was "it's from inside our network, so it's fine", which means one hacked
   server can call everything. The modern answer: each service gets a certificate saying "I am `billing-api` in
   `prod`", handed out automatically by the platform (Kubernetes, the cloud, or SPIFFE) after checking where it runs,
   valid for an hour and replaced before it expires. Services prove themselves to each other with these certificates
   (**mutual TLS**).
2. **For which user?** If `orders` calls `payments` because Alice clicked "buy", `payments` should know it's for Alice,
   so it can check Alice's limits and log it. That's carried in a token, ideally a narrowed one obtained by **token
   exchange** rather than Alice's original login token.

For things outside your platform, like a GitHub Actions job deploying to AWS, the same idea works across systems: GitHub
gives the job a signed token saying "I'm the `main` branch of `acme/api`", and AWS is configured to trust exactly that,
so no AWS keys are stored in GitHub.

---

## 1. Non-human identities and why static secrets fail

Service accounts, CI jobs, cron jobs, Lambdas, Kubernetes pods, bots, AI agents: in most organizations these outnumber
people many times over. Historically they authenticated with **static secrets**: API keys, passwords in config, cloud
access keys. Why that fails:

| Property of a static secret | Consequence |
|---|---|
| Long-lived | Leaks are exploitable for months; the Salesloft Drift (2025) and CircleCI (2023) incidents both turned stolen long-lived tokens and secrets into multi-customer data theft |
| Copyable | Ends up in repos, CI logs, images, laptops, Slack (ch 08 §9) |
| Not bound to where it runs | Works identically from an attacker's machine |
| Shared | Many services use one key; can't attribute or revoke one |
| Rotated by humans | Rarely rotated; rotation breaks things, so people stop |
| No owner after people leave | Orphaned credentials |

The replacement is **attestation-based identity**: the platform verifies facts it can observe (this pod runs in namespace
`billing` with service account `api`, on a node with a valid kubelet identity; this VM has instance ID X; this job is
GitHub workflow Y on branch `main`) and issues a short-lived credential encoding those facts.

---

## 2. API keys and shared secrets: when they are acceptable

API keys remain fine for: developer onboarding to a public API, low-risk read access, and third-party services that
support nothing else. Rules (detail in ch 08 §6): vendor prefix + checksum for leak scanning, hashed at rest, scoped,
expiring, owned by a service identity, rotated with two-key overlap, never shared between services.

Within your own platform there's rarely a reason for them: use mTLS identities (§4), OAuth client credentials with
`private_key_jwt`, or workload identity federation (§5).

---

## 3. Service-to-service trust models compared

| Model | Who is the caller? | Strength | Weakness |
|---|---|---|---|
| Network perimeter (VPC, security groups, firewall allowlists) | "Something at this IP" | Easy; coarse blast-radius limit | IPs aren't identities; one foothold reaches everything allowed; breaks with autoscaling, NAT |
| Shared secret / API key per service pair | "Something with the key" | Simple | Static, copyable (§1) |
| HMAC request signing (AWS SigV4 style, RFC 9421) | "Holder of key K signed this exact request at time T" | Integrity, replay window, no bearer token on the wire | Still a shared secret unless keys are derived/short-lived |
| Bearer service tokens (OAuth client credentials JWT) | Client ID in a signed token | Standard; audience/scope; works through proxies | Bearer: stolen token replayable until `exp` |
| **mTLS with platform-issued certs (SPIFFE, mesh)** | Workload identity in the certificate | Mutual, automatic rotation, bound to the TLS connection | Needs PKI/mesh; L7 proxies must forward identity |
| mTLS + user-context token | Workload **and** user | Both questions answered | Most moving parts |

Typical modern answer: **mTLS between every service** (identity = SPIFFE ID or mesh identity) for *which service*, plus
**a short-lived, audience-restricted token** for *which user* when a user is involved, plus **authorization policy** that
uses both (§10).

---

## 4. mTLS, SPIFFE/SPIRE and service meshes

### 4.1 mTLS in one paragraph

Normal TLS: the client verifies the server's certificate. **Mutual** TLS: the server also requests and verifies a
client certificate. Each side then knows the other's identity from the certificate (in SPIFFE, the URI SAN
`spiffe://prod.acme/ns/billing/sa/api`). Hard parts are all operational: issuing certificates to thousands of workloads,
rotating them before expiry, distributing trust bundles, and making L7 proxies pass identity to the application.

### 4.2 SPIFFE and SPIRE

- **SPIFFE ID**: `spiffe://<trust-domain>/<path>`, a URI naming a workload.
- **SVID** (SPIFFE Verifiable Identity Document): an **X.509-SVID** (certificate with the SPIFFE ID as URI SAN) or a
  **JWT-SVID** (for L7 or when TLS terminates early).
- **Trust bundle**: CA certificates for a trust domain; **federation** exchanges bundles between trust domains (e.g.
  two clusters, two companies).
- **Workload API**: a local Unix socket; the workload asks "who am I?" and receives SVIDs + bundles, rotated
  automatically. No secret is provisioned to the workload.
- **SPIRE**: the reference implementation. A server (CA, registration entries) and per-node agents. **Node attestation**
  (the agent proves the node: AWS instance identity document, GCP/Azure equivalents, Kubernetes PSAT, TPM). **Workload
  attestation** (the agent inspects the calling process: Kubernetes pod/namespace/service account via the kubelet, Unix
  UID, Docker labels) and matches registration entries' **selectors** to decide which SPIFFE ID it gets.

```
SPIRE server (CA) ◀──node attestation── SPIRE agent (on each node)
                                             ▲  Workload API (unix socket)
                                             │  "who am I?" → kernel tells agent the caller's PID
                                             │  agent asks kubelet: pod ns=billing sa=api → selectors match entry
                                     pod: billing/api  ◀── X.509-SVID spiffe://prod.acme/ns/billing/sa/api (TTL 1h)
```

### 4.3 Service meshes

Istio, Linkerd, Consul Connect and Cilium give every pod an identity (Istio's is SPIFFE-format, derived from the
Kubernetes service account) and do mTLS transparently in sidecars or node proxies (Istio ambient's ztunnel, Cilium's
mutual authentication). Then **authorization policies** reference identities:

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata: {name: payments-allow, namespace: payments}
spec:
  selector: {matchLabels: {app: payments}}
  action: ALLOW
  rules:
  - from: [{source: {principals: ["cluster.local/ns/orders/sa/orders-api"]}}]
    to:   [{operation: {methods: ["POST"], paths: ["/v1/charges"]}}]
```

Rules: run mTLS in **STRICT** mode (permissive mode accepts plaintext and is a migration step, not a destination);
start with a default-deny policy per namespace; pass the verified peer identity to the app (e.g. `X-Forwarded-Client-Cert`
set by the sidecar, and **stripped** from incoming external requests so callers can't forge it).

---

## 5. Workload identity federation: CI and cross-cloud without keys

> **In plain words.** Your CI system already knows exactly which repository, branch and workflow a job is. It signs
> that as an OIDC token. Your cloud account is configured to accept tokens from that CI issuer **only** for specific
> repo/branch combinations and to hand back temporary credentials. No cloud key is ever stored in CI.

GitHub Actions → AWS:

```
job (permissions: id-token: write) ──request──▶ GitHub OIDC provider
   ◀── JWT: iss=https://token.actions.githubusercontent.com
            sub=repo:acme/api:environment:production
            aud=sts.amazonaws.com, repository=acme/api, ref=refs/heads/main, workflow_ref=..., job_workflow_ref=...
job ──AssumeRoleWithWebIdentity(JWT, role arn)──▶ AWS STS
   STS validates signature via GitHub's JWKS, checks role trust policy conditions
   ◀── temporary credentials (≤1 h)
```

The role trust policy is the entire security boundary:

```json
{
  "Effect": "Allow",
  "Principal": {"Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "repo:acme/api:environment:production"
    }
  }
}
```

**The classic misconfiguration**: a `sub` condition with a wildcard like `repo:acme/*` or, worse, no `sub` condition at
all, so **any** GitHub repository in the world (or any repo in the org, including a fork-triggered workflow or an
intern's test repo) can assume the role. Security researchers have repeatedly found such roles in the wild. Pin `sub` to
repo **and** environment or branch; prefer `environment:` (with GitHub environment protection rules requiring
approval) for production roles; consider `job_workflow_ref` to pin a reusable workflow.

The same pattern: GCP Workload Identity Federation (pools + providers + attribute conditions), Azure federated identity
credentials, GitLab CI ID tokens, Kubernetes service account tokens federated to cloud IAM (EKS IRSA / Pod Identity, GKE
Workload Identity, AKS Workload Identity), SPIFFE JWT-SVIDs federated to clouds. It is RFC 8693/RFC 7523 under the hood.

---

## 6. Kubernetes service account tokens

(Full internals: [kubernetes ch 07](../kubernetes/07-authentication-authorization.md).)

- **Legacy secret-based tokens**: non-expiring JWTs stored as Secrets, valid until the Secret is deleted. Don't create
  them (Kubernetes stopped auto-creating them in 1.24).
- **Bound, projected tokens** (TokenRequest API): audience-bound, time-bound (default ~1 h, kubelet refreshes at 80%),
  bound to the pod object (invalid once the pod is deleted). Projected into the pod via a volume.
- Use a **custom audience** when the token is for something other than the API server (Vault, a cloud STS, your own
  service): `serviceAccountToken: {audience: vault, expirationSeconds: 3600}`. Never send API-server-audience tokens
  elsewhere.
- `automountServiceAccountToken: false` for pods that don't call the API server.
- The cluster's service account issuer publishes OIDC discovery (`/.well-known/openid-configuration` and JWKS) so
  external systems can validate these tokens: that's how IRSA and Vault's Kubernetes/JWT auth work (ch 06).

---

## 7. Propagating end-user context across services

> **In plain words.** The gateway knows the user. Five services later, the ledger service needs to know too, and it
> should get proof it can check, not a header saying `X-User: alice` that any service could forge.

| Option | How | Pros | Cons |
|---|---|---|---|
| Trusted headers (`X-User-Id`) set by gateway | Gateway authenticates, services trust headers | Trivial | Any compromised service or misrouted request forges users; must strip at edge |
| Forward the user's access token | Each hop sends the original token | Simple, verifiable | Audience must include every downstream (overbroad token); replayable everywhere; external token leaks inward |
| Gateway-minted internal JWT ("phantom token") | Gateway validates external token, mints short internal JWT (`aud` = internal mesh) | Clean separation; internal claims normalized | One internal audience for all services; still bearer |
| **Token exchange per hop** (RFC 8693) | Service exchanges incoming token for one with `aud` = next service, down-scoped, `act` = itself | Least privilege, full chain auditable | AS on the hot path (cache exchanged tokens per user+audience for their lifetime) |
| **Transaction tokens** (`draft-ietf-oauth-transaction-tokens`, OAuth WG draft) | A token service mints a short-lived, signed token describing the whole transaction (user, purpose, request context) at the edge; services pass it unchanged and can't expand it | Designed for exactly this; immutable context | Draft; adopt behind an interface |

Recommendation: mTLS for the service identity at every hop, plus either a gateway-minted internal token (simplest
correct option) or token exchange for sensitive downstreams (payments, PII stores). Each service authorizes on
**both** the peer identity and the user context (§10).

---

## 8. Request signing and webhooks

When you send webhooks to customers (or receive them), the receiver must know the request is authentic and fresh:

```
signature = HMAC-SHA256(secret, timestamp + "." + raw_body)
headers:   Webhook-Timestamp: 1761730000
           Webhook-Signature: v1=5257a869...
```

Receiver: recompute over the **raw bytes** (not re-serialized JSON), compare in constant time, reject timestamps older
than ~5 minutes (replay), dedupe by event ID, support **two active secrets** during rotation (`v1=...,v1=...`). Standard
Webhooks (standardwebhooks.com) codifies this shape; Stripe and GitHub use variants. For asymmetric signatures (receiver
can't forge), use **HTTP Message Signatures (RFC 9421)** with Ed25519.

Also: SSRF protection for outbound webhook URLs customers configure (they can point them at your internal network; same
guard as ch 06 §8.2).

---

## 9. Secrets that must exist: managers and dynamic secrets

Some credentials can't be federated (third-party APIs, legacy databases). Then:

- **Secrets manager** as the source of truth (Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault),
  accessed with workload identity (so the secret-zero problem is solved by the platform).
- **Dynamic secrets**: Vault's database secrets engine creates a database user per lease (e.g. 1 h) and drops it at
  expiry; each service instance gets distinct credentials; leaks expire on their own.
- **Delivery**: CSI Secrets Store driver or External Secrets Operator into files; app reads on demand and reloads on
  rotation. Avoid env vars (ch 09 §7).
- **Rotation**: automated, with dual-validity windows; alert on secrets older than policy.

---

## 10. Zero trust, east-west authorization and blast radius

Zero trust (NIST SP 800-207, Google BeyondCorp) for services means: no implicit trust from network location; every
request authenticated and authorized; policies on identity and context; continuous verification.

Practical east-west checklist:

1. Every workload has a platform-issued identity (SPIFFE/mesh/cloud).
2. mTLS STRICT everywhere internal.
3. Default-deny authorization policies; explicit allow per (caller identity → callee operation).
4. User context propagated as verifiable tokens; services authorize on both caller and user.
5. Egress control: workloads can reach only the external hosts they need (crucial for agents and code-executing
   sandboxes, ch 15).
6. Network segmentation still exists as defense in depth (Kubernetes NetworkPolicy/Cilium policies), not as the identity.
7. Identity-aware audit: every call logged with caller identity, user, decision.

---

## 11. Code: CI OIDC trust check, webhook verification, user-context propagation

### 11.1 Validating a GitHub Actions OIDC token in your own deploy service

```python
import jwt
from jwt import PyJWKClient

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
gh_jwks = PyJWKClient(f"{GITHUB_ISSUER}/.well-known/jwks", lifespan=3600)

ALLOWED = {   # exact matches only: repo + environment, and the reusable workflow that may deploy
    ("acme/api", "production"): "acme/deploy-workflows/.github/workflows/deploy.yml@refs/heads/main",
}

def authorize_ci(token: str, audience: str = "https://deploy.acme.internal") -> dict:
    key = gh_jwks.get_signing_key_from_jwt(token)
    c = jwt.decode(token, key.key, algorithms=["RS256"], audience=audience, issuer=GITHUB_ISSUER,
                   options={"require": ["sub", "repository", "exp", "iat"]})
    env = c.get("environment")
    expected_sub = f"repo:{c['repository']}:environment:{env}"
    if c["sub"] != expected_sub:
        raise PermissionError("unexpected subject format")
    wf = ALLOWED.get((c["repository"], env))
    if wf is None or c.get("job_workflow_ref") != wf:
        raise PermissionError("repository/environment/workflow not allowed to deploy")
    return c
```

(The JWKS URL comes from GitHub's discovery document at `{issuer}/.well-known/openid-configuration`; per ch 06, fetch
it from there rather than hardcoding in production.)

### 11.2 Webhook verification with replay window and rotation

```python
import hashlib, hmac, time

def verify_webhook(raw_body: bytes, timestamp: str, signature_header: str,
                   secrets_: list[bytes], tolerance_s: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False                                              # replay / stale
    signed = timestamp.encode() + b"." + raw_body
    presented = [p.split("=", 1)[1] for p in signature_header.split(",") if p.startswith("v1=")]
    for secret in secrets_:                                       # current + previous during rotation
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, p) for p in presented):
            return True
    return False
```

### 11.3 Calling a downstream service with an exchanged, audience-specific token

```python
import httpx

class Downstream:
    """mTLS for *which service* (client cert from the SPIFFE Workload API or mesh),
    exchanged token for *which user* (sub=user, act=this service, aud=the downstream)."""

    def __init__(self, token_endpoint: str, my_client_assertion, cache, mtls_context):
        self.token_endpoint, self.assertion, self.cache = token_endpoint, my_client_assertion, cache
        self.http = httpx.Client(verify=mtls_context, timeout=5)   # ssl.SSLContext with our SVID + trust bundle

    def token_for(self, user_token: str, user_sub: str, audience: str, scope: str) -> str:
        key = (user_sub, audience, scope)
        if (hit := self.cache.get(key)) is not None:
            return hit
        r = self.http.post(self.token_endpoint, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": user_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience, "scope": scope,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self.assertion(),                 # private_key_jwt as this service
        })
        r.raise_for_status()
        body = r.json()
        self.cache.set(key, body["access_token"], ttl=max(body.get("expires_in", 300) - 30, 0))
        return body["access_token"]

    def charge(self, user_token: str, user_sub: str, payload: dict) -> dict:
        tok = self.token_for(user_token, user_sub, "https://payments.internal", "charges.create")
        r = self.http.post("https://payments.internal/v1/charges", json=payload,
                           headers={"Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        return r.json()
```

---

## 12. Anti-patterns

| Anti-pattern | Consequence | Instead |
|---|---|---|
| "Internal network = trusted" | Lateral movement from any foothold | mTLS + default-deny policies |
| Shared service account key for many services | No attribution, no per-service revocation | Per-workload identity |
| Cloud access keys stored in CI secrets | Long-lived keys leak via CI | OIDC workload identity federation |
| Trust policy with `sub` wildcard or no `sub` | Any repo can assume your role | Pin repo + environment/branch (+ workflow) |
| Mesh mTLS in PERMISSIVE forever | Plaintext still accepted | STRICT after migration |
| Trusting `X-User-Id`/`X-Forwarded-Client-Cert` from anywhere | Header forgery | Strip at edge; set only by trusted proxy; prefer signed tokens |
| Forwarding the external user token to every internal service | Over-broad audience; leak amplification | Internal token or token exchange |
| Service calls downstream with its own admin identity for user actions | Confused deputy | Token exchange with `act` |
| Long-lived legacy Kubernetes SA tokens | Non-expiring cluster credentials | Projected, audience-bound tokens |
| Webhook verification over re-serialized JSON | Signature mismatches → people disable verification | Verify raw bytes |

---

## 13. Interview questions

**Q1. How does a pod get an identity without any secret being deployed to it?**
Node attestation proves the node to the identity issuer (SPIRE server, cloud, or kubelet credentials); workload attestation
on the node identifies the pod (namespace, service account) via the kubelet; the issuer returns a short-lived SVID or
projected token through a local API/volume, rotated automatically.

**Q2. Design CI deployment to AWS without stored keys. What's the critical misconfiguration?**
GitHub OIDC → `AssumeRoleWithWebIdentity` with a trust policy pinning `aud` and `sub` (repo + environment), environment
protection rules for production. The critical misconfiguration is a missing or wildcard `sub` condition, letting other
repositories assume the role.

**Q3. Service A calls B on behalf of a user. What should B know and how?**
B should know A's identity (mTLS peer identity) and the user (a token with `aud`=B, `sub`=user, ideally `act`=A, obtained
by token exchange or a gateway-minted internal token), and authorize using both.

**Q4. Compare mTLS and bearer JWTs for service-to-service authentication.**
mTLS binds identity to the connection, is mutual, and its credentials are rotated by the platform; it needs PKI and
identity forwarding through L7 proxies. Bearer JWTs work through any proxy and carry claims/audience but are replayable if
stolen; combine with mTLS-binding (RFC 8705) or DPoP to fix that.

---

## Sources

- SPIFFE specifications: https://github.com/spiffe/spiffe/tree/main/standards ; SPIRE: https://spiffe.io/docs/latest/spire-about/
- NIST SP 800-207, Zero Trust Architecture: https://csrc.nist.gov/pubs/sp/800/207/final
- Google BeyondCorp papers: https://research.google/pubs/beyondcorp-a-new-approach-to-enterprise-security/
- GitHub Actions, OpenID Connect hardening: https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect
- AWS IAM, OIDC federation for GitHub: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html
- Kubernetes, service account token volume projection: https://kubernetes.io/docs/concepts/security/service-accounts/
- Istio security (identity, mTLS, AuthorizationPolicy): https://istio.io/latest/docs/concepts/security/
- RFC 9421, HTTP Message Signatures: https://datatracker.ietf.org/doc/html/rfc9421
- Standard Webhooks: https://www.standardwebhooks.com/
- OAuth Transaction Tokens (IETF draft): https://datatracker.ietf.org/doc/draft-ietf-oauth-transaction-tokens/
- HashiCorp Vault, database secrets engine: https://developer.hashicorp.com/vault/docs/secrets/databases
