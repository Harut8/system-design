# Authentication and Authorization in Kubernetes

Identity is the first thing the apiserver does with a request and the prerequisite for everything else. Admission can't decide what to mutate without knowing *who* asked. RBAC can't grant a verb without a subject. Audit logs are useless without a name. Workload identity (IRSA, GKE Workload Identity, Azure AD Workload Identity, SPIFFE) is just "the cluster's identity system, federated outward". A misconfigured authenticator or a single over-broad role binding turns a sandboxed pod into a cluster-admin shell — and it is, in production, the single most exploited path to a cluster compromise.

This chapter is the staff-level companion to chapter 05 (the apiserver request chain, where AuthN and AuthZ are stages) and chapter 06 (admission, which runs *after* authorization). It also ties into chapter 27 (supply-chain identity — the cluster's OIDC issuer is what cosign verifies against in keyless flows) and chapter 37 (cloud provider integration — the cloud-side half of IRSA/Workload Identity). The mental model we will build here:

> Every API request carries credentials. Credentials → User (name + UID + groups + extras) via an **authenticator chain**. (User, verb, resource) → allow/deny via an **authorizer chain**. Both chains are pluggable, both can short-circuit, and the inputs/outputs are *just data* — which is why SPIFFE, OIDC, webhook AuthZ, and impersonation all compose.

If you internalize the chain model and the projected-token mechanics, every cloud workload identity story becomes the same story with the audience claim swapped out. If you internalize RBAC's evaluation algorithm, every "why can't this pod do X?" debugging session becomes a five-minute graph walk.

---

## Table of Contents

1.  [The Mental Model: Identity as the First Stage](#1-the-mental-model-identity-as-the-first-stage)
2.  [What a Request Looks Like at the Wire](#2-what-a-request-looks-like-at-the-wire)
3.  [The Authenticator Chain: Order and Semantics](#3-the-authenticator-chain-order-and-semantics)
4.  [x509 Client Certificates](#4-x509-client-certificates)
5.  [Static Token File (Deprecated)](#5-static-token-file-deprecated)
6.  [Bootstrap Tokens](#6-bootstrap-tokens)
7.  [ServiceAccount Tokens: Legacy, Projected, and Bound](#7-serviceaccount-tokens-legacy-projected-and-bound)
8.  [The TokenRequest API and Projected Token Mechanics](#8-the-tokenrequest-api-and-projected-token-mechanics)
9.  [The Legacy ServiceAccount Token Secret Is Dead](#9-the-legacy-serviceaccount-token-secret-is-dead)
10. [OpenID Connect (OIDC)](#10-openid-connect-oidc)
11. [Webhook Authentication (TokenReview)](#11-webhook-authentication-tokenreview)
12. [Anonymous Requests](#12-anonymous-requests)
13. [Audience-Bound Tokens: The Universal Federation Pattern](#13-audience-bound-tokens-the-universal-federation-pattern)
14. [Workload Identity in AWS: IRSA and Pod Identity](#14-workload-identity-in-aws-irsa-and-pod-identity)
15. [Workload Identity in GCP: GKE Workload Identity](#15-workload-identity-in-gcp-gke-workload-identity)
16. [Workload Identity in Azure: Azure AD Workload Identity](#16-workload-identity-in-azure-azure-ad-workload-identity)
17. [SPIFFE / SPIRE: The Federated Future](#17-spiffe--spire-the-federated-future)
18. [The Authorizer Chain](#18-the-authorizer-chain)
19. [RBAC: Objects, Verbs, Resources](#19-rbac-objects-verbs-resources)
20. [RBAC Evaluation Algorithm](#20-rbac-evaluation-algorithm)
21. [RBAC Modeling Patterns](#21-rbac-modeling-patterns)
22. [Aggregated ClusterRoles](#22-aggregated-clusterroles)
23. [The Node Authorizer](#23-the-node-authorizer)
24. [ABAC: Legacy](#24-abac-legacy)
25. [Webhook Authorization (SubjectAccessReview)](#25-webhook-authorization-subjectaccessreview)
26. [Impersonation](#26-impersonation)
27. [Audit and Identity](#27-audit-and-identity)
28. [Self-Inquiry: can-i, whoami, reconcile, SelfSubjectReview](#28-self-inquiry-can-i-whoami-reconcile-selfsubjectreview)
29. [Certificate-Based Identity: certificates.k8s.io and CSR Approval](#29-certificate-based-identity-certificatesk8sio-and-csr-approval)
30. [External Secrets and the Boundary of AuthN](#30-external-secrets-and-the-boundary-of-authn)
31. [At-Rest Secret Encryption: EncryptionConfiguration and KMS v2](#31-at-rest-secret-encryption-encryptionconfiguration-and-kms-v2)
32. [Common Attack Paths](#32-common-attack-paths)
33. [Defensive Patterns](#33-defensive-patterns)
34. [Pitfalls](#34-pitfalls)
35. [TL;DR](#35-tldr)

---

## 1. The Mental Model: Identity as the First Stage

The apiserver is, mechanically, a request pipeline. Chapter 05 traces the whole chain; this chapter zooms into the first two stages. Every request, regardless of verb or resource, goes through them.

```
   incoming request (TLS terminated)
            │
            ▼
   ┌────────────────────────┐
   │  AUTHENTICATION        │   stage 1
   │  credential → User     │
   └───────────┬────────────┘
               │ user.Info{Name, UID, Groups, Extra}
               ▼
   ┌────────────────────────┐
   │  AUTHORIZATION         │   stage 2
   │  (user, verb,          │
   │   resource, ns) → bool │
   └───────────┬────────────┘
               │  (allow)
               ▼
   ┌────────────────────────┐
   │  Mutating Admission    │   stage 3   (ch 06)
   └───────────┬────────────┘
               ▼
   ┌────────────────────────┐
   │  Schema + CEL validate │   stage 4
   └───────────┬────────────┘
               ▼
   ┌────────────────────────┐
   │  Validating Admission  │   stage 5
   └───────────┬────────────┘
               ▼
   ┌────────────────────────┐
   │  Storage (etcd txn)    │   stage 6
   └────────────────────────┘
```

A few invariants that anchor the rest of the chapter:

1. **AuthN is stateless per request.** There is no session, no cookie. Every request carries credentials. This is what makes Kubernetes scale: any apiserver replica in an HA control plane can authenticate any request without sticky sessions. The flip side is that *every* request must verify a credential, so authenticator cost matters at scale.
2. **AuthN produces a `User`.** The product of the AuthN stage is a Go struct, `user.Info`, with four fields: `Name` (string), `UID` (string), `Groups` ([]string), and `Extra` (map[string][]string). Everything downstream reads from those four fields. The credential type is forgotten the moment it's authenticated.
3. **AuthZ is "(user, verb, resource) → {allow, deny, no opinion}".** It does not see the request body. It does not run admission. It decides "may this subject do this action against this scope?" and nothing more. This is why creation policy ("only allow images from this registry") must be enforced at admission, not AuthZ.
4. **Both stages are pluggable, both are chains.** The apiserver runs configured authenticators in order, short-circuiting on first success. It runs configured authorizers in order, short-circuiting on first explicit allow or explicit deny. "No opinion" continues. RBAC's grant model is *additive* on top of this — see §20.
5. **Identity persists into the audit log.** What you authenticated as appears as `user` in every audit event. If you impersonated, both your real identity (`impersonatedUser`) and the assumed identity are recorded. Forensics depend on this; never strip it.

The `user.Info` interface is defined in `k8s.io/apiserver/pkg/authentication/user/user.go`:

```go
// k8s.io/apiserver/pkg/authentication/user/user.go
type Info interface {
    GetName() string
    GetUID() string
    GetGroups() []string
    GetExtra() map[string][]string
}
```

Four reserved group strings flow through the chain and matter at every later stage:

| Group                          | When applied |
|--------------------------------|--------------|
| `system:authenticated`         | Auto-added to *every* successfully authenticated user. |
| `system:unauthenticated`       | Auto-added to anonymous requests (if anonymous auth is enabled). |
| `system:masters`               | Bypass for the built-in RBAC authorizer's "super-user" path; mapped to `cluster-admin` in default RBAC. Treat as the *root* group. Never grant. |
| `system:serviceaccounts`       | Auto-added to every authenticated ServiceAccount. The per-namespace specialization is `system:serviceaccounts:<namespace>`. |

Two name conventions you will see everywhere:

- ServiceAccount usernames are always `system:serviceaccount:<ns>:<name>`. This is generated by the SA token authenticator — *the SA object itself doesn't carry a username field*.
- Node usernames are `system:node:<nodename>`, with the group `system:nodes` (this is what the Node authorizer keys on; see §23).

That is the entire mental model. Everything below is detail.

---

## 2. What a Request Looks Like at the Wire

Before diving into authenticators, look at the actual HTTP request the apiserver sees. There are five places a credential can appear:

```
GET /api/v1/namespaces/default/pods HTTP/2
Host: kube-apiserver.example.com:6443

Authorization: Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6Ii4uLiJ9...    ← (1) bearer token (SA / OIDC / static / bootstrap)
Impersonate-User: alice@example.com                              ← (2) impersonation header
Impersonate-Group: ops
Impersonate-Uid: 12345
Impersonate-Extra-scopes: prod

[client cert exchanged during TLS handshake]                      ← (3) x509 client cert
[mTLS SNI / connection peer info]                                 ← (4) connection-level identity
```

The apiserver's `genericapiserver` runs an authenticator chain over this request. Each authenticator is a Go function with the signature:

```go
// k8s.io/apiserver/pkg/authentication/authenticator/interfaces.go
type Request interface {
    AuthenticateRequest(req *http.Request) (*Response, bool, error)
}

type Response struct {
    Audiences   Audiences
    User        user.Info
    Annotations map[string]string
}
```

The contract is "look at the request, return (response, ok, err)":
- `(response, true, nil)` → success, stop the chain.
- `(nil, false, nil)` → "I have no opinion", continue to the next authenticator.
- `(nil, false, err)` → hard error, stop the chain and return 401 to the client.

The whole authenticator chain is itself wrapped as a single `authenticator.Request` via `union.New(authenticators...)`. The union short-circuits on the first `ok=true`. The construction sites are in `k8s.io/apiserver/pkg/server/options/authentication.go` and (for the apiserver) `cmd/kube-apiserver/app/options/authentication.go`.

```
TLS handshake
   │
   ▼
http.Request handed to apiserver
   │
   ▼
┌───────────────────────────────────────────────────────────────────┐
│ union.New(authenticators...) — runs each in order                  │
│                                                                    │
│   [1] x509       ──┐                                                │
│   [2] sa-token   ──┤  first ok=true wins                            │
│   [3] oidc       ──┤  ok=false → try next                           │
│   [4] webhook    ──┤  err     → 401, stop                           │
│   [5] bootstrap  ──┤                                                │
│   [6] anonymous  ──┘                                                │
└───────────────────────────────────────────────────────────────────┘
   │
   ▼
authenticated user.Info → next stage (AuthZ)
```

If *no* authenticator succeeds and anonymous auth is disabled, the request is rejected with 401. If anonymous auth is enabled (the default in many distros), the anonymous authenticator is the last entry in the chain and always succeeds with `user=system:anonymous, groups=[system:unauthenticated]`.

---

## 3. The Authenticator Chain: Order and Semantics

Order matters less than you might think — successful authenticators short-circuit, so as long as a given credential format is unambiguous, the order is mostly cosmetic. But there are three places where order has real consequences:

1. **x509 must run before bearer-token authenticators** so that a client presenting *both* a client cert and a token is identified by the cert (the strong credential).
2. **The SA token authenticator must run before generic webhook authenticators** unless your webhook explicitly excludes the `kubernetes/serviceaccount` audience — otherwise you can short-circuit SA tokens through an external webhook for no reason.
3. **Anonymous, if enabled, is always last.** It always succeeds. Anything after it would be dead code.

The default order, when all authenticators are configured, is:

```
1. RequestHeader               (apiserver-aggregator front-proxy headers)
2. x509 client cert            (--client-ca-file)
3. ServiceAccount tokens       (legacy + bound; built-in)
4. Bootstrap tokens            (--enable-bootstrap-token-auth)
5. OIDC                        (--oidc-issuer-url)
6. Webhook token authenticator (--authentication-token-webhook-config-file)
7. Static token file           (--token-auth-file) [deprecated]
8. Anonymous                   (--anonymous-auth=true)
```

`RequestHeader` is a special case — it's how an aggregated API server (chapter 24) inherits identity from the front-proxy. The aggregator presents a client cert (verified by `--requestheader-client-ca-file`) and the user identity is read from a header (`X-Remote-User`, `X-Remote-Group`, `X-Remote-Extra-*`). This is *internal*: end users don't talk to it.

```
┌──────────────────────────────────────────────────────────────────────┐
│                      AUTHENTICATOR CHAIN (default order)              │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  [01] RequestHeader  ────►  for aggregated apiservers (ch 24)         │
│           │                                                           │
│           ▼ no opinion                                                │
│  [02] x509 client cert                                                │
│           │   matches against --client-ca-file                        │
│           │   CN = username, O = groups                               │
│           ▼ no opinion                                                │
│  [03] ServiceAccount token                                            │
│           │   bearer token, JWT with iss=https://kubernetes.../...    │
│           │   verified against --service-account-key-file public keys │
│           ▼ no opinion                                                │
│  [04] Bootstrap token                                                 │
│           │   bearer token of form "abcdef.0123456789abcdef"          │
│           │   matched to a Secret of type bootstrap.kubernetes.io/    │
│           │   token in kube-system                                    │
│           ▼ no opinion                                                │
│  [05] OIDC                                                            │
│           │   bearer token, JWT with iss matching --oidc-issuer-url   │
│           │   verified via the OIDC provider's JWKS                   │
│           ▼ no opinion                                                │
│  [06] Webhook token                                                   │
│           │   apiserver POSTs TokenReview to your webhook             │
│           │   webhook returns user.Info                               │
│           ▼ no opinion                                                │
│  [07] Static token file (deprecated)                                  │
│           │   plaintext CSV: token,user,uid,"group1,group2"           │
│           ▼ no opinion                                                │
│  [08] Anonymous                                                       │
│           │   always succeeds (if --anonymous-auth=true)              │
│           ▼  user=system:anonymous                                    │
│              groups=[system:unauthenticated]                          │
└──────────────────────────────────────────────────────────────────────┘
```

Two semantic edge cases worth knowing:

- **Two authenticators might accept the same credential.** A bearer token could theoretically match both the OIDC and webhook authenticators if both are configured. In practice the chain stops at the first success, so order resolves it. If a webhook is intended only for a particular audience, configure the webhook to *not* match SA / OIDC tokens (e.g., by checking issuer in its own logic), and place it after them. There is no `requiredMatchPolicy` like for admission webhooks.
- **An authenticator can return an error.** A failing webhook authenticator (network timeout) returns `err != nil` and *stops* the chain. This is by design — you don't want a wedged webhook to silently fall back to anonymous. Implication: webhook authenticators must be highly available and fast, or you've made the entire apiserver depend on them.

The fact that the chain stops on success means it's safe to leave deprecated authenticators (e.g., `--token-auth-file`) enabled for migration, *as long as* the credentials in those authenticators are not also valid against an earlier authenticator. But "safe" is doing a lot of work in that sentence; in practice you should remove deprecated authenticators because they expand attack surface (see §32).

Source: `k8s.io/apiserver/pkg/authentication/request/union/union.go` for the chain, and `pkg/kubeapiserver/authenticator/config.go` (in `kubernetes/kubernetes`) for the assembly.

---

## 4. x509 Client Certificates

The oldest authenticator and still the foundation of intra-cluster trust. Every kubelet, every controller manager, every scheduler, every static admin `kubeconfig` shipped by `kubeadm` uses x509 against a CA configured by `--client-ca-file`.

### 4.1 How identity is encoded in the certificate

```
Certificate:
    Subject: O = system:masters, CN = kubernetes-admin
                     │                       │
                     ▼                       ▼
                  group                   username

Extensions:
    X509v3 Extended Key Usage: TLS Web Client Authentication
```

The mapping is:

- **`CN` (Common Name) → `user.Name`.**
- **`O` (Organization), one or many → `user.Groups`.** Multiple `O` values map to multiple groups.

The certificate must:

- Chain to a CA listed in `--client-ca-file` (one file, possibly with multiple PEM CA certs concatenated).
- Have `Extended Key Usage` including `clientAuth`.
- Be presented during TLS handshake — that is, the client must use mTLS. The apiserver's TLS config sets `tls.Config.ClientAuth = tls.RequestClientCert` so cert presentation is optional at the TLS layer; the x509 authenticator just returns "no opinion" if no cert is present, letting the next authenticator try.

### 4.2 The default control-plane PKI (kubeadm)

`kubeadm init` creates several CAs and certs:

```
/etc/kubernetes/pki/
├── ca.crt / ca.key                 ← cluster CA (signs apiserver server cert + client certs)
├── apiserver.crt / .key            ← apiserver TLS server cert
├── apiserver-kubelet-client.crt    ← apiserver → kubelet client cert
│                                     CN=kube-apiserver-kubelet-client, O=system:masters
├── front-proxy-ca.crt / .key       ← separate CA for aggregator (RequestHeader auth)
├── front-proxy-client.crt          ← apiserver → aggregated APIs client cert
├── sa.pub / sa.key                 ← ServiceAccount signing keys (see §7-8)
└── etcd/
    ├── ca.crt                      ← etcd CA (separate trust domain)
    ├── server.crt                  ← etcd server cert
    ├── peer.crt                    ← etcd peer-to-peer mTLS
    └── healthcheck-client.crt
```

The apiserver's flags reference these:

```
--client-ca-file=/etc/kubernetes/pki/ca.crt
--requestheader-client-ca-file=/etc/kubernetes/pki/front-proxy-ca.crt
--requestheader-allowed-names=front-proxy-client
--kubelet-client-certificate=/etc/kubernetes/pki/apiserver-kubelet-client.crt
--kubelet-client-key=/etc/kubernetes/pki/apiserver-kubelet-client.key
```

The default `admin.conf` produced by kubeadm contains a client cert with `CN=kubernetes-admin, O=system:masters`. That `system:masters` group hits a hard-coded super-user path in the RBAC authorizer (see §18); it is the literal definition of cluster-admin. This is why losing `admin.conf` to an attacker = full cluster compromise: no Role, no RoleBinding required, the cert is the keys.

### 4.3 Why x509 is great and why it isn't

What's great:

- No round-trip. Verification is local: parse the cert, validate the chain, check revocation (if `--client-ca-file` is rotated, though revocation lists are not enforced; see below).
- The credential is bound to the connection, not just a header — harder to replay if you compromise a log.
- Works without an external dependency: even if your OIDC provider is down, kubelet and admin can still talk to the apiserver.

What isn't:

- **Revocation is essentially absent.** The apiserver doesn't honor CRLs or OCSP for client certs. If a cert is leaked, your options are (a) rotate the CA (everybody's certs need re-issuing), (b) rotate the cert and wait for natural expiry (the leaked cert remains valid until then), or (c) add the user to a deny group via webhook AuthZ. None of these are good.
- **Groups are stuffed into `O`.** You can't have arbitrary key/value claims like OIDC. You can have multiple `O`s, but parsing nested structure is on you.
- **Hard to rotate at scale.** Every kubelet has its own client cert; kubelet's certificate manager handles this (chapter 10), but for *human* clusters this is why nobody scales x509 beyond the control plane and a few admins. For users, OIDC.
- **The CN convention is a leaky abstraction.** RFC 5280 declared CN as a "common name" without a single defined meaning; modern TLS uses SANs for identity, not CN. Kubernetes still reads CN for *user* identity, which means certs minted by general PKI tools may be unintentionally mapped to a Kubernetes user. This is a feature in `kubeadm`, a footgun everywhere else.

### 4.4 A working kubeconfig with x509

```yaml
apiVersion: v1
kind: Config
clusters:
- name: prod
  cluster:
    server: https://api.prod.example.com:6443
    certificate-authority-data: LS0tLS1CRUdJTi...   # base64 PEM
users:
- name: alice
  user:
    client-certificate-data: LS0tLS1CRUdJTi...      # base64 PEM
    client-key-data: LS0tLS1CRUdJTi...
contexts:
- name: alice@prod
  context:
    cluster: prod
    user: alice
current-context: alice@prod
```

Source: `k8s.io/apiserver/pkg/authentication/request/x509/x509.go`.

---

## 5. Static Token File (Deprecated)

The simplest authenticator ever shipped. It exists for tiny single-node setups and as the bootstrap credential for some installers. **Do not use in production.**

Format: a CSV file pointed at by `--token-auth-file`:

```
# token,user,uid,"group1,group2,group3"
1bda5f1c-...-7e7f3a2b,alice,12345,"developers,ops"
abcdef-1234-...-987654,prometheus,67890,"system:monitoring"
```

The client sends `Authorization: Bearer 1bda5f1c-...-7e7f3a2b`. The apiserver looks up the token in an in-memory map and returns the user.

Why this is bad in 2025:

- Tokens are **long-lived** and **plain text on disk**. There is no rotation, no audience binding, no expiration. A leaked token is valid until somebody edits the file and restarts the apiserver.
- Restarting the apiserver to add a user does not scale and is not safe in HA control planes.
- Anyone who can read the file is cluster-admin if any token in it is cluster-admin.

You will still find `--token-auth-file` referenced in some old installer docs. Treat it as a smell and migrate to projected SA tokens (for in-cluster) or OIDC (for humans).

Source: `k8s.io/apiserver/plugin/pkg/authenticator/token/tokenfile/tokenfile.go`.

---

## 6. Bootstrap Tokens

A more constrained, dynamic version of static tokens. Designed for one specific flow: a new kubelet joining a cluster needs *some* credential to talk to the apiserver to submit a CSR (chapter 29). It can't be a full cluster-admin token. It can't be a long-lived static token. It needs to be temporary, scoped, and revocable.

Bootstrap tokens are:

- Stored as Secrets of type `bootstrap.kubernetes.io/token` in `kube-system`. The Secret name is `bootstrap-token-<id>` where `<id>` is the first half of the token.
- The format on the wire is `<id>.<secret>`, e.g., `07401b.f395accd246ae52d` — 6 hex chars dot 16 hex chars.
- Backed by a built-in authenticator enabled with `--enable-bootstrap-token-auth`.
- Carry an optional TTL (`expiration` field in the Secret) and a usage list (`usage-bootstrap-authentication`, `usage-bootstrap-signing`).
- Authenticate as `user=system:bootstrap:<id>, groups=[system:bootstrappers, ...optional extra groups...]`.

Here's the YAML for one (created by `kubeadm token create` under the hood):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bootstrap-token-07401b
  namespace: kube-system
type: bootstrap.kubernetes.io/token
stringData:
  # ID and secret halves
  token-id: "07401b"
  token-secret: "f395accd246ae52d"
  # Optional expiration (RFC 3339)
  expiration: "2026-06-01T12:00:00Z"
  # What the token may be used for
  usage-bootstrap-authentication: "true"
  usage-bootstrap-signing: "true"
  # Extra groups (in addition to system:bootstrappers)
  auth-extra-groups: "system:bootstrappers:kubeadm:default-node-token"
```

Together with RBAC, the `system:bootstrappers:kubeadm:default-node-token` group is bound to a ClusterRole that allows submitting CSRs but not much else (see §29). This is the wiring that makes "join a node by typing a single command on it" safe-ish.

```
┌────────────────────────────────────────────────────────────────────┐
│  Kubelet bootstrap flow (high level — full version in §29)         │
│                                                                    │
│  1. Operator runs `kubeadm token create` on control plane          │
│     → creates a Secret of type bootstrap.kubernetes.io/token       │
│                                                                    │
│  2. Operator distributes "kubeadm join <ip>:6443 --token X.Y       │
│     --discovery-token-ca-cert-hash sha256:..." to the new node     │
│                                                                    │
│  3. New kubelet sends Authorization: Bearer X.Y                    │
│     → bootstrap authenticator matches, user becomes                │
│       system:bootstrap:X with groups [system:bootstrappers,...]    │
│                                                                    │
│  4. RBAC allows this group to POST CertificateSigningRequest       │
│     → kubelet submits its own CSR for a long-lived client cert     │
│                                                                    │
│  5. CSR signer (in-cluster) signs, kubelet downloads the cert      │
│                                                                    │
│  6. Kubelet switches kubeconfig to use the new x509 cert           │
│     → bootstrap token can now be deleted / expires naturally       │
└────────────────────────────────────────────────────────────────────┘
```

Bootstrap tokens are great because they're explicit, scoped, and time-bound. They're still bearer tokens, so they leak the same way every bearer token leaks. The mitigation is short TTL (kubeadm defaults to 24h) and aggressive deletion once the bootstrap completes.

Source: `k8s.io/cluster-bootstrap/token/api`, `plugin/pkg/auth/authenticator/token/bootstrap`.

---

## 7. ServiceAccount Tokens: Legacy, Projected, and Bound

The most-used credential in any cluster. Every Pod that talks to the apiserver authenticates with one. Understanding their evolution from "long-lived Secret-backed bearer tokens" to "audience-bound projected JWTs minted by the TokenRequest API" is the most important security upgrade in modern Kubernetes.

### 7.1 The ServiceAccount object

A ServiceAccount is a namespace-scoped identity:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: api-client
  namespace: payments
automountServiceAccountToken: true     # default
```

Its API username is generated, not stored on the object: `system:serviceaccount:<namespace>:<name>`. The groups are `[system:serviceaccounts, system:serviceaccounts:<namespace>]`.

When a Pod is created with `spec.serviceAccountName: api-client`, the apiserver's `ServiceAccount` admission plugin (built-in, chapter 06) injects a *token* into the pod. The mechanism for injecting that token has evolved through three generations:

### 7.2 Generation 1 — Legacy Secret-backed tokens (≤ 1.23 default)

Historically, when you created a ServiceAccount, the `tokens-controller` (part of kube-controller-manager) automatically created a companion Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: api-client-token-7f9x2
  namespace: payments
  annotations:
    kubernetes.io/service-account.name: api-client
    kubernetes.io/service-account.uid: 4d8b...
type: kubernetes.io/service-account-token
data:
  token: ZXlKaGJHY2lPaUpTVXpJMU5pSjkuLi4=    # base64 of the JWT
  ca.crt: ...
  namespace: cGF5bWVudHM=
```

The token was a **never-expiring** JWT signed by the apiserver's SA signing key. The Pod's container got it mounted at `/var/run/secrets/kubernetes.io/serviceaccount/token`.

The properties of this token:

- No `exp` claim. Valid forever.
- No `aud` claim. Valid against any audience (which means *any* receiver that trusts the cluster's issuer would accept it).
- Persisted unencrypted in etcd as a Secret (encrypted-at-rest only if you configured `EncryptionConfiguration`, §31).
- The Secret could be exfiltrated by anyone with read access to it, including any pod with `secrets/get` on the namespace.

This was the #1 vector of cluster compromise from ~2016 to ~2022. Exfiltrate a token → use it from anywhere on the internet → reach the apiserver as the SA → privilege escalate via whatever RBAC the SA has → end up at cluster-admin.

### 7.3 Generation 2 — Projected (bound) tokens (introduced 1.12, default since 1.21)

`BoundServiceAccountTokenVolume`, the feature gate that flipped the default, replaces the legacy mount with a *projected volume* whose token is minted by the `TokenRequest` API at pod creation, refreshed by kubelet, and bound to:

- An **audience** (typically `https://kubernetes.default.svc`, but configurable).
- An **expiration** (typically 1 hour, configurable; kubelet refreshes at 80% of TTL).
- The **specific Pod** (`kubernetes.io/pod/uid`) and **ServiceAccount** (`kubernetes.io/serviceaccount/uid`).
- The **specific Node** (in newer versions; `kubernetes.io/node/uid`).

This is the file at `/var/run/secrets/kubernetes.io/serviceaccount/token` in modern clusters: not a Secret reference, but a kubelet-managed, periodically-refreshed JWT.

When the apiserver receives such a token, it validates:

- The signature, using the public keys in `--service-account-key-file` (which can be a list of PEM keys for rotation).
- The `iss` claim, matching `--service-account-issuer`.
- The `exp` claim, against current time.
- The `aud` claim, against the cluster's expected audiences (`--api-audiences`). The apiserver itself is one such audience; arbitrary receivers can be added.
- That the bound Pod still exists and the bound ServiceAccount still exists (this is the "bound" part — if the Pod is deleted, the token stops working *before* its expiration).

We'll dive into the JWT structure and mint/verify flow in §8.

### 7.4 Generation 3 — `LegacyServiceAccountTokenNoAutoGeneration` (1.24+)

In 1.24, the auto-generation of `kubernetes.io/service-account-token` Secrets was *removed*. Creating a ServiceAccount no longer creates a companion Secret. You can still *manually* create one if you really need a long-lived token (e.g., for a CI system that can't use projected tokens), but it's now an explicit, audited action.

You'd write it explicitly:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ci-token
  namespace: ci
  annotations:
    kubernetes.io/service-account.name: ci-runner
type: kubernetes.io/service-account-token
```

The `LegacyServiceAccountTokenCleaner` controller (1.28+) also marks unused legacy tokens with a `kubernetes.io/legacy-token-last-used` annotation, and a separate cleaner can purge ones that haven't been used in 365 days. The trend is unambiguous: long-lived SA tokens are a deprecated pattern.

### 7.5 The pod-side picture

Even without you doing anything, a Pod ends up with this projected volume spec injected by the SA admission plugin:

```yaml
spec:
  volumes:
  - name: kube-api-access-xkz4f
    projected:
      defaultMode: 0644
      sources:
      - serviceAccountToken:
          audience: https://kubernetes.default.svc   # default cluster audience
          expirationSeconds: 3607                    # ~1 hour
          path: token
      - configMap:
          name: kube-root-ca.crt
          items:
          - key: ca.crt
            path: ca.crt
      - downwardAPI:
          items:
          - path: namespace
            fieldRef:
              fieldPath: metadata.namespace
  containers:
  - name: app
    volumeMounts:
    - name: kube-api-access-xkz4f
      mountPath: /var/run/secrets/kubernetes.io/serviceaccount
      readOnly: true
```

That's three sources projected into one directory: a fresh JWT, the cluster CA, and the namespace name. The in-cluster Go client (`rest.InClusterConfig()`) reads all three to build a working client.

You can ask for additional projected tokens with different audiences in your own pod spec — that's how IRSA, Vault, and cross-cluster auth work (see §13).

```yaml
spec:
  volumes:
  - name: vault-token
    projected:
      sources:
      - serviceAccountToken:
          audience: vault                              # different audience
          expirationSeconds: 600
          path: vault-token
```

The kubelet refreshes each token independently. The application reads the file when it needs to authenticate; the file is rotated atomically (kubelet writes to a tmp file and renames).

---

## 8. The TokenRequest API and Projected Token Mechanics

This is where the magic of "no secrets at rest, no round-trip on validation" gets concrete.

### 8.1 The mint flow

When a Pod with a projected SA token is scheduled, kubelet does *not* read a Secret. Instead, kubelet calls the apiserver's `TokenRequest` subresource:

```
POST /api/v1/namespaces/payments/serviceaccounts/api-client/token
{
  "kind": "TokenRequest",
  "apiVersion": "authentication.k8s.io/v1",
  "spec": {
    "audiences": ["https://kubernetes.default.svc"],
    "expirationSeconds": 3607,
    "boundObjectRef": {
      "kind": "Pod",
      "apiVersion": "v1",
      "name": "payments-7d9c-xyz",
      "uid": "ab12-..."
    }
  }
}
```

The apiserver's `TokenRequest` handler:

1. Verifies the caller (kubelet) has RBAC `create` on `serviceaccounts/token` for the target SA. The Node authorizer (§23) restricts kubelets to only their own pods' SAs.
2. Constructs a JWT payload from `spec.boundObjectRef`, the SA, the audiences, expiration.
3. Signs it with the key from `--service-account-signing-key-file`.
4. Returns `{status: {token: "ey...", expirationTimestamp: ...}}`.

The kubelet writes the token into the pod's projected volume. ~80% of the way to expiration, it does it again.

### 8.2 The JWT contents

The payload of a modern projected SA token looks like this (decoded; not a real signed token):

```json
{
  "aud": ["https://kubernetes.default.svc"],
  "exp": 1716489600,
  "iat": 1716486000,
  "iss": "https://kubernetes.default.svc.cluster.local",
  "jti": "abcd-1234-...",
  "kubernetes.io": {
    "namespace": "payments",
    "node": {
      "name": "ip-10-0-1-42.ec2.internal",
      "uid": "9e7c..."
    },
    "pod": {
      "name": "payments-7d9c-xyz",
      "uid": "ab12-..."
    },
    "serviceaccount": {
      "name": "api-client",
      "uid": "4d8b-..."
    },
    "warnafter": 1716487800
  },
  "nbf": 1716486000,
  "sub": "system:serviceaccount:payments:api-client"
}
```

Several claims are worth singling out:

| Claim                                          | Meaning |
|------------------------------------------------|---------|
| `iss`                                          | The cluster's issuer URL (set by `--service-account-issuer`). |
| `aud`                                          | List of audiences the token is valid for. The apiserver's expected audiences are set by `--api-audiences` (defaults to the issuer). |
| `sub`                                          | The SA username. |
| `exp` / `iat` / `nbf`                          | Standard time claims. |
| `kubernetes.io.pod`                            | Bound pod — token is invalid if pod is deleted. |
| `kubernetes.io.serviceaccount`                 | Bound SA — token is invalid if SA is deleted or recreated (UID changes). |
| `kubernetes.io.node`                           | Bound node, introduced for finer-grained validation. |
| `kubernetes.io.warnafter`                      | Soft-warning timestamp; kubelet uses this to log token-not-rotated warnings if it hasn't refreshed by then. |

A *legacy* (Secret-backed) SA token, for contrast, looks like this — note the missing `exp`, `aud`, and bound-object claims:

```json
{
  "iss": "kubernetes/serviceaccount",
  "kubernetes.io/serviceaccount/namespace": "payments",
  "kubernetes.io/serviceaccount/secret.name": "api-client-token-7f9x2",
  "kubernetes.io/serviceaccount/service-account.name": "api-client",
  "kubernetes.io/serviceaccount/service-account.uid": "4d8b-...",
  "sub": "system:serviceaccount:payments:api-client"
}
```

That token, signed and base64'd, was usable forever, from anywhere, for any audience.

### 8.3 The verify flow

When a request arrives at the apiserver bearing such a token:

```
                ┌────────────────────────────────────────────────┐
                │ apiserver SA token authenticator (in-process)   │
                ├────────────────────────────────────────────────┤
                │ 1. parse JWT, look up `kid` header              │
                │ 2. find matching public key from                │
                │    --service-account-key-file                   │
                │ 3. verify signature                             │
                │ 4. check iss == --service-account-issuer        │
                │ 5. check exp > now                              │
                │ 6. check aud contains an --api-audiences value  │
                │ 7. resolve sub → ServiceAccount object          │
                │ 8. check bound Pod / SA UIDs still match etcd   │
                │    (this is the network call)                   │
                │ 9. emit user.Info{                              │
                │      Name: "system:serviceaccount:ns:name",     │
                │      UID: SA.uid,                               │
                │      Groups: [                                  │
                │        "system:authenticated",                  │
                │        "system:serviceaccounts",                │
                │        "system:serviceaccounts:ns"              │
                │      ],                                         │
                │      Extra: {                                   │
                │        "authentication.kubernetes.io/pod-name": │
                │           ["payments-7d9c-xyz"],                │
                │        "authentication.kubernetes.io/pod-uid":  │
                │           ["ab12-..."]                          │
                │      }                                          │
                │    }                                            │
                └────────────────────────────────────────────────┘
```

Steps 1-6 are local: the apiserver has the public key in memory; verification is fast. Step 8 (bound-object existence) requires reading the Pod and ServiceAccount; in a busy cluster this means hitting the watch cache, which is cheap. The signature work dominates cost, and it's a few microseconds per request on modern hardware.

### 8.4 Key management and rotation

The apiserver flags:

```
--service-account-issuer=https://kubernetes.default.svc.cluster.local
--service-account-signing-key-file=/etc/kubernetes/pki/sa.key
--service-account-key-file=/etc/kubernetes/pki/sa.pub
--api-audiences=https://kubernetes.default.svc.cluster.local
```

- `--service-account-signing-key-file` is the *private* key used to mint tokens. Exactly one is active for signing.
- `--service-account-key-file` is the *public* key (or list of keys) used to *verify* tokens. It can be supplied multiple times to support key rotation: a new key is added, the signing flag is flipped to the new key, and the old verification key remains until all old tokens have expired.

Rotation cadence depends on threat model; quarterly is common on hardened clusters.

### 8.5 The OIDC discovery endpoints

This is the trick that powers IRSA/GKE/Azure Workload Identity (§14-16). The apiserver exposes two OIDC-compatible endpoints, *unauthenticated* (if you grant `system:service-account-issuer-discovery` to `system:unauthenticated`):

```
GET https://kubernetes.default.svc/.well-known/openid-configuration
```

returns:

```json
{
  "issuer": "https://kubernetes.default.svc.cluster.local",
  "jwks_uri": "https://kubernetes.default.svc/openid/v1/jwks",
  "response_types_supported": ["id_token"],
  "subject_types_supported": ["public"],
  "id_token_signing_alg_values_supported": ["RS256"]
}
```

```
GET https://kubernetes.default.svc/openid/v1/jwks
```

returns the cluster's public keys in JWK format:

```json
{
  "keys": [
    {
      "kty": "RSA",
      "kid": "A1B2...",
      "use": "sig",
      "alg": "RS256",
      "n": "0vx7agoebGcQSuuPiLJX...",
      "e": "AQAB"
    },
    { "kty": "RSA", "kid": "C3D4...", "use": "sig", "alg": "RS256", ... }
  ]
}
```

That second endpoint, served publicly (for cloud workload identity) or via the cluster's OIDC issuer URL (for managed clusters; EKS/GKE/AKS publish theirs at a stable URL), means **any** external system that knows the issuer URL can verify a cluster-minted JWT *without ever talking to the cluster*. It just fetches the JWKS, caches it, and verifies signatures locally. That property is what lets AWS IAM trust a Kubernetes-minted JWT (§14).

```
External relying party (e.g., AWS STS, Vault):
  1. Fetch https://issuer/.well-known/openid-configuration
  2. Extract jwks_uri
  3. Fetch JWKS, cache
  4. For each incoming token:
        verify(token, jwks)
        check iss == expected issuer
        check aud == "sts.amazonaws.com" (or "vault", or ...)
        check exp > now
        sub claim → identity ("system:serviceaccount:ns:sa")
  5. Map sub to whatever local identity it wants
```

No cross-cluster network calls. No shared secrets. No PKI handshake. Just JWTs and JWKS, exactly like OIDC works for human SSO.

### 8.6 Setting `--service-account-issuer` to an external URL

Managed clusters (EKS, GKE, AKS) set `--service-account-issuer` to a publicly reachable URL (e.g., `https://oidc.eks.us-east-1.amazonaws.com/id/AB12CD34`). The cloud control plane copies the cluster's JWKS into the public OIDC document. AWS IAM then trusts that issuer for `AssumeRoleWithWebIdentity` (§14).

If you're running your own cluster and want to do workload-identity-style federation, you set `--service-account-issuer=https://your-host/your-cluster`, publish the JWKS there (you can copy it from `/openid/v1/jwks` and serve statically), and now any external system that trusts that URL can verify your tokens.

The complete projected-token flow, end-to-end:

```
┌────────────────────────────────────────────────────────────────────────┐
│ PROJECTED SA TOKEN — MINT, MOUNT, REFRESH, VERIFY                      │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  Pod scheduled to node-2                                               │
│   │                                                                    │
│   ▼                                                                    │
│  kubelet (node-2):                                                     │
│   │  POST /api/v1/namespaces/ns/serviceaccounts/sa/token               │
│   │       audience=https://kubernetes.default.svc                      │
│   │       expirationSeconds=3607                                       │
│   │       boundObjectRef={Pod}                                         │
│   ▼                                                                    │
│  apiserver TokenRequest handler                                        │
│   │  authz check (Node authorizer: kubelet may mint for its own pod)   │
│   │  sign JWT with sa.key                                              │
│   │  return token + expirationTimestamp                                │
│   ▼                                                                    │
│  kubelet writes /var/lib/kubelet/pods/<uid>/volumes/.../token          │
│   │  bind-mounted into container at                                    │
│   │  /var/run/secrets/kubernetes.io/serviceaccount/token               │
│   ▼                                                                    │
│  workload reads file (e.g., client-go InClusterConfig)                 │
│   │  sets Authorization: Bearer <token>                                │
│   ▼                                                                    │
│  apiserver authenticator chain                                         │
│   │  SA token authenticator:                                           │
│   │    verify sig vs sa.pub                                            │
│   │    check iss / aud / exp / bound-pod existence                     │
│   ▼                                                                    │
│  user.Info{Name="system:serviceaccount:ns:sa", ...}                    │
│                                                                        │
│  Meanwhile, every ~50 minutes:                                         │
│   kubelet sees warnafter < now-margin → POST token again →             │
│   rewrites the file atomically. Container reads the new bytes          │
│   next time it opens the file. (client-go re-reads on 401.)            │
└────────────────────────────────────────────────────────────────────────┘
```

Source: `k8s.io/kubernetes/pkg/serviceaccount/jwt.go`, `pkg/registry/core/serviceaccount/storage/token.go`, `pkg/kubelet/token/token_manager.go`.

---

## 9. The Legacy ServiceAccount Token Secret Is Dead

Worth its own short section because the change is recent and the mindset hasn't caught up everywhere.

Pre-1.24:

```
$ kubectl create sa my-sa
$ kubectl get secret -o name | grep my-sa-token
secret/my-sa-token-7f9x2     ← auto-created, never-expiring JWT inside
```

Post-1.24:

```
$ kubectl create sa my-sa
$ kubectl get secret -o name | grep my-sa-token
# (nothing)
```

No more auto-generation. If you want a token for the SA, you either:

1. Use `kubectl create token my-sa` — calls the TokenRequest API, returns a short-lived (default 1h) bound token. The right answer for CI: fetch a fresh token per job.
2. Explicitly create a `kubernetes.io/service-account-token` Secret as in §7.4. The controller fills in the `data.token` field with a *non-expiring* legacy-shaped JWT. Use only when you absolutely need a long-lived token (e.g., a CI runner that polls the cluster and you can't yet integrate with TokenRequest).

The `LegacyServiceAccountTokenNoAutoGeneration` feature gate has been GA since 1.24. The `LegacyServiceAccountTokenTracking` and `LegacyServiceAccountTokenCleanUp` features (1.28+) add observability and a janitor for the long-lived tokens that still exist.

**Why this matters in plain terms:** when an auditor finds `Secret/foo-token-abcde` in your cluster, that Secret contains a forever-valid credential for an SA. If your RBAC for that SA is anything more than read-only on innocuous resources, anyone who can `get secret foo-token-abcde` is effectively that SA. Pre-1.24 this was the default state for *every* SA in every namespace. The number of clusters where some pod had `get secret` on a namespace that happened to host a powerful SA's token is, empirically, large.

Audit query to find legacy tokens still in your cluster:

```
kubectl get secrets --all-namespaces \
  -o jsonpath='{range .items[?(@.type=="kubernetes.io/service-account-token")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}'
```

Anything that returns shouldn't be there unless you put it there explicitly.

---

## 10. OpenID Connect (OIDC)

The standard way humans authenticate to production clusters. (And, when configured via `--service-account-issuer`, also the underlying mechanism for workload identity.)

### 10.1 What OIDC does in one paragraph

OIDC is OAuth 2.0 with a standardized identity layer. A user authenticates to an identity provider (Okta, Google, GitHub, Keycloak, Azure AD, Auth0). The IdP issues an *ID token* — a JWT containing claims about the user (`sub`, `email`, `groups`, etc.). The user presents the ID token to a relying party (the apiserver). The relying party fetches the IdP's public keys (the JWKS), verifies the token's signature, and trusts the claims.

```
┌──────────┐                ┌────────────┐                ┌───────────┐
│   User   │                │    IdP     │                │ apiserver │
│ (kubectl)│                │  (Okta..)  │                │           │
└────┬─────┘                └─────┬──────┘                └─────┬─────┘
     │                             │                             │
     │  1. browser login flow      │                             │
     │  (authorization code,       │                             │
     │   device flow, etc.)        │                             │
     │ ──────────────────────────► │                             │
     │                             │                             │
     │  2. id_token (JWT)          │                             │
     │ ◄────────────────────────── │                             │
     │                             │                             │
     │   3. GET /api/v1/...                                      │
     │      Authorization: Bearer <id_token>                     │
     │ ────────────────────────────────────────────────────────► │
     │                                                           │
     │                            4. fetch JWKS once (cached)    │
     │                            ◄─────────────────────────────►│
     │                                                           │
     │                            5. verify token locally,       │
     │                               extract claims              │
     │                                                           │
     │   6. response                                             │
     │ ◄──────────────────────────────────────────────────────── │
```

### 10.2 The apiserver flags

```
--oidc-issuer-url=https://login.example.com         # MUST match `iss` in JWTs
--oidc-client-id=kubernetes                          # MUST match `aud`
--oidc-username-claim=email                          # which claim becomes user.Name
--oidc-username-prefix=oidc:                         # prepended to username (avoid collisions)
--oidc-groups-claim=groups                           # which claim becomes user.Groups
--oidc-groups-prefix=oidc:                           # prepended to each group
--oidc-required-claim=hd=example.com                 # additional claim assertions
--oidc-required-claim=email_verified=true            # repeatable
--oidc-ca-file=/etc/.../idp-ca.crt                   # if the IdP uses an internal CA
--oidc-signing-algs=RS256,ES256                      # whitelist
```

Several of these flags are *security critical* and operators get them wrong all the time. We'll cover the footguns in §10.5.

You can also use a `StructuredAuthenticationConfiguration` file (newer, since 1.29 alpha → 1.30 beta, GA in 1.32 in some distributions) instead of these flags:

```yaml
apiVersion: apiserver.config.k8s.io/v1beta1
kind: AuthenticationConfiguration
jwt:
- issuer:
    url: https://login.example.com
    audiences:
    - kubernetes
    audienceMatchPolicy: MatchAny
  claimMappings:
    username:
      claim: email
      prefix: "oidc:"
    groups:
      claim: groups
      prefix: "oidc:"
    uid:
      claim: sub
  claimValidationRules:
  - claim: email_verified
    requiredValue: "true"
  - claim: hd
    requiredValue: example.com
  - expression: 'claims.exp - claims.iat < 3600'
    message: "token TTL too long, IdP misconfigured"
  userValidationRules:
  - expression: '!user.username.startsWith("system:")'
    message: "OIDC users may not impersonate system:* identities"
```

Multiple OIDC issuers can be configured at once via this file (one of the main reasons it exists; the CLI flags only allow one).

### 10.3 What a kubectl OIDC kubeconfig looks like

```yaml
users:
- name: alice@example.com
  user:
    auth-provider:
      name: oidc
      config:
        idp-issuer-url: https://login.example.com
        client-id: kubernetes
        client-secret: ...                              # only if confidential client
        refresh-token: 1//04dX...                       # cached after first login
        id-token: eyJhbGciOiJSUzI1NiIs...               # short-lived, refreshed
```

Modern setups use the `exec` plugin pattern instead (e.g., `kubectl-oidc-login`, `kubelogin`), which delegates the OIDC flow to a separate binary that handles browser launch, device-code flow, caching, refresh, and PKCE properly:

```yaml
users:
- name: alice@example.com
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1
      command: kubectl
      args:
      - oidc-login
      - get-token
      - --oidc-issuer-url=https://login.example.com
      - --oidc-client-id=kubernetes
      - --oidc-extra-scope=email
      - --oidc-extra-scope=groups
```

`kubectl-oidc-login` returns `{kind: ExecCredential, status: {token: "ey..."}}`, kubectl plugs that into the Authorization header. Refresh is handled by the plugin via the IdP's refresh token; the apiserver never sees that — it only ever sees ID tokens.

### 10.4 An ID token payload

```json
{
  "iss": "https://login.example.com",
  "sub": "alice@example.com",
  "aud": "kubernetes",
  "exp": 1716489600,
  "iat": 1716486000,
  "auth_time": 1716485900,
  "nonce": "abcd1234",
  "email": "alice@example.com",
  "email_verified": true,
  "hd": "example.com",
  "name": "Alice Operator",
  "groups": [
    "platform-ops",
    "sre-managers"
  ]
}
```

With the flags above, this becomes:

```
user.Name   = "oidc:alice@example.com"
user.Groups = ["oidc:platform-ops", "oidc:sre-managers",
               "system:authenticated"]
```

The `oidc:` prefix is crucial: without it, a malicious IdP (or a user able to influence their own claims) could end up as `system:masters` or `system:serviceaccount:default:cluster-admin-sa`. **Always set `--oidc-username-prefix` and `--oidc-groups-prefix`** unless you have an extremely thorough understanding of your IdP's claim-issuance policy.

### 10.5 OIDC pitfalls

OIDC is the most flexible authenticator and therefore the most footgun-laden.

1. **Trusting `groups` blindly.** If your IdP allows users to self-edit group membership (some directories do, intentionally or by misconfig), or if the IdP issues `groups` claims that flat-namespace collide across tenants, your RBAC is compromised. Use `--oidc-groups-prefix` to namespace them, and prefer `--oidc-required-claim` to gate sensitive groups behind other claims (e.g., `--oidc-required-claim=mfa=true`).
2. **Audience claim mismatches.** The `aud` claim must match `--oidc-client-id` exactly. If your IdP issues ID tokens with `aud` set to the client_id of a *web app* (not the cluster), they're not for you. Don't accept them.
3. **`auth_time` and re-authentication.** Some IdPs issue long-lived ID tokens (24h) that don't require recent authentication. For high-stakes clusters, enforce a short token lifetime via the IdP and a required-claim expression on `auth_time`.
4. **Username conflict with SA names.** If a user named `system` ends up in your IdP, and you don't prefix, you get `system` as a Kubernetes user — which prefixes group lookups, sometimes inadvertently matching `system:masters`. Always prefix.
5. **IdP availability.** If the IdP is down, ID tokens still verify locally (apiserver caches JWKS), but token refresh on the client side fails — new sessions can't start. Plan for IdP outages; have a break-glass x509 admin cert in a sealed envelope.
6. **No revocation.** ID tokens are valid for their `exp`. Revoking a user in the IdP doesn't invalidate already-issued tokens. Mitigate with short token lifetimes and IdP-side session controls.

Source: `k8s.io/apiserver/plugin/pkg/authenticator/token/oidc/oidc.go`.

---

## 11. Webhook Authentication (TokenReview)

When neither x509, SA, nor OIDC fits — for example, you want to integrate with a corporate identity system that doesn't speak OIDC — webhook authentication lets the apiserver delegate token verification to an external HTTPS endpoint.

### 11.1 The flow

```
[client]                  [apiserver]                       [webhook]
   │ Authorization: Bearer ...                                  │
   │ ─────────► AuthN chain                                     │
   │             webhook authn:                                 │
   │             POST /authenticate                             │
   │             { kind: TokenReview, spec: { token: ... } }    │
   │             ───────────────────────────────────────────►   │
   │                                                            │
   │             ◄───────────────────────────────────────────   │
   │             { status: { authenticated: true,               │
   │                          user: { username, uid, groups }}} │
   │ ◄───────── proceed to AuthZ                                │
```

The apiserver speaks the `authentication.k8s.io/v1 TokenReview` resource:

```yaml
apiVersion: authentication.k8s.io/v1
kind: TokenReview
spec:
  token: "abc.def.ghi"
  audiences:
  - https://kubernetes.default.svc
```

Webhook response:

```yaml
apiVersion: authentication.k8s.io/v1
kind: TokenReview
status:
  authenticated: true
  user:
    username: alice@example.com
    uid: "12345"
    groups: [ "platform-ops" ]
    extra:
      "authentication.kubernetes.io/sso-id": ["AB12CD"]
  audiences:
  - https://kubernetes.default.svc
```

### 11.2 Configuration

The apiserver needs a kubeconfig-format file pointing to the webhook:

```
--authentication-token-webhook-config-file=/etc/k8s/authn-webhook.kubeconfig
--authentication-token-webhook-cache-ttl=2m
--authentication-token-webhook-version=v1
```

`-cache-ttl` is critical: every request would otherwise round-trip to the webhook. Cache TTL of 2m means a revoked user keeps working for up to 2m after revocation. Set it short enough that revocation is meaningful, long enough that the webhook isn't on the request-rate critical path.

### 11.3 When to use it

Webhook AuthN makes sense when:
- You have a centralized PDP that already speaks an internal token format.
- You want server-side token introspection (e.g., revoke a token by deleting a database row).
- Your IdP isn't OIDC-compatible.

It's a poor choice when:
- OIDC works — webhook AuthN adds a network hop per uncached request and adds a service to your dependency graph.
- The webhook is implemented carelessly and becomes the apiserver's tightest SLO dependency.

The webhook is invoked once per token (cached); not once per request. Even so, a wedged or slow webhook stalls every new credential's first authentication.

Source: `k8s.io/apiserver/plugin/pkg/authenticator/token/webhook/webhook.go`.

---

## 12. Anonymous Requests

If no authenticator succeeds and `--anonymous-auth=true` (the default in upstream Kubernetes), the apiserver assigns:

```
user.Name   = "system:anonymous"
user.Groups = [ "system:unauthenticated" ]
```

What's anonymous useful for?
- The unauthenticated health endpoints (`/healthz`, `/livez`, `/readyz`) — these are rebound to allow anonymous via RBAC default `system:public-info-viewer`.
- The OIDC discovery endpoints (`/.well-known/openid-configuration`, `/openid/v1/jwks`) when you want cloud workload identity to work without bootstrap credentials.

What's it dangerous for?
- Everything else. If RBAC ever grants `system:unauthenticated` a non-trivial verb on a non-trivial resource, you have an unauthenticated path to that capability. CVEs have been issued for installers that did this.

Recommended posture:

- Leave `--anonymous-auth=true` (the default).
- Audit every RoleBinding/ClusterRoleBinding that binds `system:unauthenticated` or `system:anonymous`. The only legitimate bindings should be the default `system:public-info-viewer` and (if you publish OIDC discovery) `system:service-account-issuer-discovery`.

Quick audit:

```
kubectl get clusterrolebindings -o json | \
  jq '.items[] | select(.subjects[]? | .name == "system:unauthenticated" or .name == "system:anonymous") | .metadata.name'
```

Source: `k8s.io/apiserver/pkg/authentication/request/anonymous/anonymous.go`.

---

## 13. Audience-Bound Tokens: The Universal Federation Pattern

Everything from §14 onward is variations on a single pattern. Internalize this and the cloud integrations stop being magic.

The pattern in one diagram:

```
┌──────────────────────────────────────────────────────────────────────┐
│  AUDIENCE-BOUND PROJECTED TOKEN — THE UNIVERSAL FEDERATION SHAPE      │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  Cluster issues JWTs signed by its SA signing key.                    │
│  Cluster publishes OIDC discovery + JWKS at a stable URL.             │
│                                                                       │
│  External system R (= "relying party"):                               │
│   • Pre-configured to trust cluster's issuer URL.                     │
│   • Pre-configured to require a specific audience claim (e.g.,        │
│     "sts.amazonaws.com" or "vault" or "https://cluster-b.api/...").   │
│                                                                       │
│  Workload W in the cluster:                                           │
│   1. Pod spec includes a projected volume with                        │
│        serviceAccountToken:                                           │
│          audience: <R's expected audience>                            │
│          expirationSeconds: <short>                                   │
│   2. Kubelet calls TokenRequest API for SA, gets JWT bound to:        │
│        sub = system:serviceaccount:ns:sa                              │
│        aud = <R's expected audience>                                  │
│        exp = now + N                                                  │
│   3. Workload sends the JWT to R.                                     │
│                                                                       │
│  R verifies:                                                          │
│   - sig vs cached cluster JWKS                                        │
│   - iss == expected                                                   │
│   - aud == expected                                                   │
│   - exp > now                                                         │
│   → trusts sub claim as a federated identity                          │
│   → maps sub to local capability                                      │
│      (an IAM role, a Vault policy, a foreign cluster's user, ...)     │
└──────────────────────────────────────────────────────────────────────┘
```

This is the *whole shape* of IRSA, GKE Workload Identity, Azure AD Workload Identity, Vault Kubernetes auth, cross-cluster auth, and any custom federation you build. The next three sections instantiate it for the three big cloud providers.

The receiving-side rule that *cannot* be skipped: **always validate `aud`.** Many implementations of token verification (especially home-grown ones) verify signature + `iss` + `exp` and forget `aud`. A token minted for audience X then accepted by relying-party Y is a *confused-deputy* vulnerability waiting to happen. This is the single most important review item when you're writing code that accepts cluster-minted JWTs.

---

## 14. Workload Identity in AWS: IRSA and Pod Identity

AWS has two related but distinct mechanisms. **IRSA** (IAM Roles for Service Accounts) is the original; **EKS Pod Identity** (announced late 2023) is the simpler successor. Both end up at the same place — a workload getting a short-lived AWS access key without any cluster-side AWS secret — but they get there differently.

### 14.1 IRSA — the OIDC federation flow

```
                                ┌────────────────────────────────────┐
                                │  AWS IAM (per account)             │
                                │                                    │
                                │  OIDC provider:                    │
                                │    URL: https://oidc.eks.us-east-1│
                                │         .amazonaws.com/id/CLUSTER  │
                                │    JWKS thumbprint: ...            │
                                │                                    │
                                │  IAM Role: arn:aws:iam:::role/...  │
                                │    AssumeRolePolicy:               │
                                │      Principal:                    │
                                │        Federated: <OIDC provider>  │
                                │      Action: sts:AssumeRoleWith-   │
                                │              WebIdentity           │
                                │      Condition:                    │
                                │        StringEquals:               │
                                │          <oidc>:sub:               │
                                │            system:serviceaccount:  │
                                │            ns:sa                   │
                                │          <oidc>:aud:               │
                                │            sts.amazonaws.com       │
                                └─────────────────┬──────────────────┘
                                                  │ trusts
┌─────────────────────────────────────────────────┴─────────────────┐
│  EKS cluster (apiserver)                                          │
│  --service-account-issuer = https://oidc.eks.us-east-1...../CLU   │
│  publishes JWKS at that URL                                       │
│                                                                   │
│  Mutating admission webhook (eks-pod-identity-webhook,            │
│  reused from IRSA era):                                           │
│    triggered when ServiceAccount has annotation                   │
│      eks.amazonaws.com/role-arn: arn:aws:iam:::role/MyRole        │
│    injects into the Pod:                                          │
│      env AWS_ROLE_ARN=<that role>                                 │
│      env AWS_WEB_IDENTITY_TOKEN_FILE=/var/run/secrets/eks.amaz... │
│      volume serviceAccountToken{audience=sts.amazonaws.com,       │
│                                 expirationSeconds=86400}          │
│                                                                   │
│  Pod runs                                                         │
│   ▼                                                               │
│  aws-sdk reads AWS_WEB_IDENTITY_TOKEN_FILE                        │
│   ▼                                                               │
│  aws-sdk calls sts:AssumeRoleWithWebIdentity                      │
│    sends the JWT                                                  │
│   ▼                                                               │
│  STS verifies JWT vs cluster's JWKS, checks aud/sub/exp            │
│   ▼                                                               │
│  STS returns short-lived AWS access key (1h default, configurable)│
│   ▼                                                               │
│  aws-sdk caches the key, uses it for S3/DynamoDB/etc              │
└───────────────────────────────────────────────────────────────────┘
```

The annotation that wires it up:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: s3-reader
  namespace: data
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/S3ReaderRole
    # Optional:
    eks.amazonaws.com/audience: sts.amazonaws.com               # default
    eks.amazonaws.com/sts-regional-endpoints: "true"
    eks.amazonaws.com/token-expiration: "86400"                 # seconds
```

The IAM role's trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/AB12CD34"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "oidc.eks.us-east-1.amazonaws.com/id/AB12CD34:aud": "sts.amazonaws.com",
        "oidc.eks.us-east-1.amazonaws.com/id/AB12CD34:sub": "system:serviceaccount:data:s3-reader"
      }
    }
  }]
}
```

Read that condition carefully:
- `:aud` must be `sts.amazonaws.com` — that's the audience the webhook injected.
- `:sub` must be exactly that SA. Any other SA in the cluster cannot assume this role.

The mutating webhook (`eks-pod-identity-webhook`, runs as a Deployment in the cluster) is what tediously rewrites the Pod spec. Without it you'd add the volume and env vars yourself, which is exactly what some pre-IRSA tooling did.

### 14.2 EKS Pod Identity — the simpler model

EKS Pod Identity (2023) replaces the OIDC-trust dance with a node-local agent. The wiring:

- Cluster has the **EKS Pod Identity Agent** DaemonSet running on every node.
- A `PodIdentityAssociation` CRD-like AWS resource maps `(cluster, namespace, sa) → IAM role`. (It's an AWS API resource, not a Kubernetes one — that's by design, so cluster admins can't grant themselves arbitrary IAM roles.)
- The agent listens on a link-local address (`169.254.170.23:80`) and implements the SDK's container credentials provider protocol.
- A mutating webhook injects `AWS_CONTAINER_CREDENTIALS_FULL_URI` env var into pods whose SA is associated.
- The pod's AWS SDK fetches creds from the agent.
- The agent calls `eks:AssumeRoleForPodIdentity` to AWS (using its node identity) and returns the result.

Pro: no OIDC provider per cluster, no JWT in the pod, slightly less surface area.
Con: a node-local agent, and a tighter coupling to AWS. Most workloads can use either.

### 14.3 Why IRSA still matters

Even with Pod Identity, IRSA is the prototype that taught the industry the "cluster issuer → cloud IAM federation" pattern. Every other cloud's workload identity solution is a variation on it. Spend the time to understand IRSA and the others fall into place.

---

## 15. Workload Identity in GCP: GKE Workload Identity

GCP's variant uses a cluster-scoped *workload identity pool* and a binding between Kubernetes ServiceAccounts (KSAs) and Google ServiceAccounts (GSAs).

### 15.1 The components

- **Cluster-level**: when you enable Workload Identity on a GKE cluster, GKE configures the cluster's `--service-account-issuer` to point at the cluster's identity pool URL: `https://container.googleapis.com/v1/projects/PROJECT/locations/LOCATION/clusters/CLUSTER`. The cluster's JWKS is published at the OIDC discovery URL.
- **gke-metadata-server**: a DaemonSet that intercepts requests to `169.254.169.254` (the GCE metadata IP). When a pod's SDK asks for an access token from the metadata server, the DaemonSet redirects the request through the workload identity flow rather than serving the node's own service account credentials. This is the part that makes "the same SDK code that works on a GCE VM works in a pod" — except now it's using KSA identity, not node identity.
- **IAM binding**: a GSA grants `roles/iam.workloadIdentityUser` to a KSA via a member string `serviceAccount:PROJECT.svc.id.goog[NS/KSA]`. This is the trust statement.

### 15.2 The flow

```
KSA: payments/api-client
  annotation:
    iam.gke.io/gcp-service-account: api-client@PROJECT.iam.gserviceaccount.com

GSA: api-client@PROJECT.iam.gserviceaccount.com
  IAM policy:
    member: serviceAccount:PROJECT.svc.id.goog[payments/api-client]
    role:   roles/iam.workloadIdentityUser

Pod with serviceAccountName: api-client
  ▼
SDK calls http://metadata.google.internal/computeMetadata/v1/instance/...
  ▼
gke-metadata-server intercepts
  ▼
gke-metadata-server requests a projected SA token
  audience = the cluster's identity-pool audience
  ▼
gke-metadata-server calls STS API on Google
  exchange JWT for federated access token
  ▼
gke-metadata-server calls IAM Credentials API
  generateAccessToken on GSA, authorized because of the
  workloadIdentityUser binding
  ▼
GCP access token returned to the pod's SDK
```

The pod's *code* sees a Google access token, identical to what it would see on a GCE VM. All the federation happens in the DaemonSet — the pod knows nothing about JWTs.

### 15.3 Direct Workload Identity (newer)

GKE introduced "Workload Identity Federation for GKE" (sometimes called *direct* workload identity) that drops the GSA hop. Workloads bind directly to GCP IAM principals via `principalSet://iam.googleapis.com/projects/.../locations/global/workloadIdentityPools/POOL/subject/system:serviceaccount:NS:KSA`. Same underlying federation, fewer hops.

---

## 16. Workload Identity in Azure: Azure AD Workload Identity

Azure's path is the cleanest of the three because it adopted the OIDC federation model after the others had proven it out.

### 16.1 Components

- **Cluster**: AKS (or self-managed) cluster with `--service-account-issuer` set to a publicly reachable OIDC URL. JWKS published.
- **App registration (Entra ID)**: an application with a *federated credential* that says "trust JWTs from \<cluster issuer\> with subject = `system:serviceaccount:<ns>:<sa>` and audience = `api://AzureADTokenExchange`."
- **Mutating admission webhook** (`azure-workload-identity`): injects env vars and a projected token volume into pods.

### 16.2 The Pod-side

A ServiceAccount annotated for workload identity:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: storage-reader
  namespace: data
  annotations:
    azure.workload.identity/client-id: "11111111-2222-3333-4444-555555555555"
    # optional:
    azure.workload.identity/tenant-id: "..."
```

A Pod that uses it must be labeled to be picked up by the webhook:

```yaml
apiVersion: v1
kind: Pod
metadata:
  labels:
    azure.workload.identity/use: "true"
```

The webhook then injects:

```yaml
env:
- name: AZURE_CLIENT_ID
  value: "11111111-2222-3333-4444-555555555555"
- name: AZURE_TENANT_ID
  value: "..."
- name: AZURE_FEDERATED_TOKEN_FILE
  value: /var/run/secrets/azure/tokens/azure-identity-token
- name: AZURE_AUTHORITY_HOST
  value: https://login.microsoftonline.com/
volumes:
- name: azure-identity-token
  projected:
    sources:
    - serviceAccountToken:
        audience: api://AzureADTokenExchange
        expirationSeconds: 3600
        path: azure-identity-token
volumeMounts:
- name: azure-identity-token
  mountPath: /var/run/secrets/azure/tokens
  readOnly: true
```

The Azure SDK reads `AZURE_FEDERATED_TOKEN_FILE`, exchanges it at the Entra token endpoint for an Azure access token, uses that.

### 16.3 The federated credential on the Entra side

```json
{
  "name": "k8s-storage-reader",
  "issuer": "https://oidc.aks.westus2.azmk8s.io/abc...",
  "subject": "system:serviceaccount:data:storage-reader",
  "audiences": ["api://AzureADTokenExchange"]
}
```

That triple — `(issuer, subject, audience)` — is the trust. Anyone presenting a JWT matching that triple, verifiable against the issuer's JWKS, gets a token for the app.

### 16.4 What makes Azure's version different in practice

- The audience is a fixed magic string (`api://AzureADTokenExchange`) — the same for all Azure Workload Identity. AWS uses `sts.amazonaws.com`; GCP uses the cluster pool URL. The audience constant doesn't carry information, it's just a discriminator for the Azure STS.
- The webhook is opt-in per pod via label (versus AWS's webhook that activates on annotated SAs). Slightly different ergonomics.
- The federation crosses tenants without any further configuration — useful for multi-tenant SaaS.

---

## 17. SPIFFE / SPIRE: The Federated Future

The cloud workload identity solutions all do roughly the same thing in incompatible ways. **SPIFFE** (Secure Production Identity Framework for Everyone) is the open standard that tries to unify them.

### 17.1 The concepts

- **SPIFFE ID**: a URI of the form `spiffe://trust-domain/path`, e.g., `spiffe://prod.example.com/ns/payments/sa/api-client`. This is the workload's identity, period.
- **Trust domain**: the scope within which IDs are unique. Conventionally a DNS-style name. Each cluster might be its own trust domain, or many clusters share one.
- **SVIDs (SPIFFE Verifiable Identity Documents)**: the credentials that prove a workload's SPIFFE ID. Two shapes:
  - **X509-SVID**: an X.509 certificate with the SPIFFE ID encoded in a URI SAN. Used for mTLS.
  - **JWT-SVID**: a JWT with the SPIFFE ID in `sub`. Used when mTLS isn't viable.

### 17.2 SPIRE — the reference implementation

SPIRE has a server (per trust domain) and an agent (per node). The agent attests workloads (via Unix process attributes, K8s pod attributes, etc.), then mints SVIDs for them on demand via the **Workload API** — a Unix socket the workload talks to.

```
┌──────────────────────────────────────────────────────────────────────┐
│  SPIRE topology                                                       │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─────────────────────────┐                                          │
│  │  SPIRE Server (HA)      │ ── issues SVIDs, holds trust bundle      │
│  │   • DB-backed           │                                          │
│  │   • Federation bundles  │                                          │
│  └────────────┬────────────┘                                          │
│               │ node attestation (k8s_psat / aws_iid / ...)           │
│               ▼                                                       │
│  ┌─────────────────────────┐ (one per node)                           │
│  │  SPIRE Agent            │ ── attests workloads, caches SVIDs       │
│  │   • Unix socket         │                                          │
│  │   • /run/spire/sockets/ │                                          │
│  └────────────┬────────────┘                                          │
│               │ Workload API (gRPC over UDS)                          │
│               ▼                                                       │
│  ┌─────────────────────────┐                                          │
│  │  Workload                │ ── fetches X509/JWT SVIDs                │
│  │  (any pod)              │                                          │
│  └─────────────────────────┘                                          │
└──────────────────────────────────────────────────────────────────────┘
```

The agent attests workloads using **selectors**: things like "the pod's SA is payments/api-client, its container image hash is X, its uid is 1000". The server has registration entries mapping selectors → SPIFFE IDs.

### 17.3 Federation across trust domains

Two clusters each run SPIRE. Each has a trust domain (`prod.east.example.com`, `prod.west.example.com`). To allow a workload in east to mTLS to a workload in west:

1. Each SPIRE server exposes a *federation bundle* (essentially the cluster's CA + JWKS).
2. The other server is configured to trust this bundle.
3. Workloads in east, when establishing mTLS to a peer presenting a `spiffe://prod.west.../...` ID, can validate it against the federated bundle.

This is exactly the audience-bound-token pattern (§13) generalized to mTLS with full trust-domain semantics.

### 17.4 K8s relevance

SPIRE has a K8s integration that:
- Runs the agent as a DaemonSet.
- Uses `k8s_psat` (Projected Service Account Token) attestation: the agent reads a projected SA token from the workload, posts it to the server, server validates via the cluster's JWKS, and uses the SA name as a selector.

So the SPIRE story bootstraps off the same projected-token mechanism as IRSA et al — it just uses it to issue *its own* identity documents instead of a cloud-specific one.

The "what comes next" claim: SPIFFE may be the convergence point. mTLS service meshes (Istio, Linkerd) already issue SPIFFE-shaped identities. Workload-identity SDKs increasingly accept SPIFFE SVIDs. If you're building greenfield, designing your services to receive SPIFFE IDs and validate them via SPIRE means you can run anywhere — on-prem, in any cloud, across clouds — without per-environment auth code.

---

## 18. The Authorizer Chain

Now the request has a `user.Info`. The next stage is authorization. Like AuthN, AuthZ is a configurable chain of authorizers, but the chain semantics differ slightly.

### 18.1 The flag

```
--authorization-mode=Node,RBAC,Webhook
```

Order matters; the apiserver runs the listed authorizers in order. Built-in modes:

| Mode          | What it does |
|---------------|--------------|
| `Node`        | Special-cased authorizer for kubelet (see §23). Only authorizes users in `system:nodes`. |
| `RBAC`        | The standard Role/RoleBinding/ClusterRole/ClusterRoleBinding evaluator. |
| `ABAC`        | Legacy policy-file-based evaluator. Don't use (see §24). |
| `Webhook`     | External SubjectAccessReview to an HTTPS endpoint. |
| `AlwaysAllow` | Returns "allow" for everything. **Test/lab only.** |
| `AlwaysDeny`  | Returns "deny" for everything. Useful for negative testing. |

### 18.2 Decision semantics

Each authorizer returns one of three values:
- **allow**: short-circuit, authorize the request.
- **deny**: short-circuit, deny the request, return 403.
- **no opinion**: continue to the next authorizer.

If the entire chain finishes with "no opinion", the request is denied (closed-world default).

```
┌──────────────────────────────────────────────────────────────────────┐
│                 AUTHORIZER CHAIN (default: Node,RBAC)                 │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  request (user, verb, resource, ns, name)                             │
│       │                                                               │
│       ▼                                                               │
│  ┌─────────────┐                                                      │
│  │ Node        │  if user is kubelet, apply Node rules                │
│  │             │   → allow / deny / no opinion                        │
│  └──────┬──────┘                                                      │
│         │ no opinion                                                  │
│         ▼                                                             │
│  ┌─────────────┐                                                      │
│  │ RBAC        │  walk bindings, find a rule that matches             │
│  │             │   → allow / no opinion                               │
│  │             │   (RBAC never returns deny)                          │
│  └──────┬──────┘                                                      │
│         │ no opinion                                                  │
│         ▼                                                             │
│  ┌─────────────┐                                                      │
│  │ Webhook     │  POST SubjectAccessReview                            │
│  │             │   → allow / deny / no opinion                        │
│  └──────┬──────┘                                                      │
│         │ all returned no opinion                                     │
│         ▼                                                             │
│      403 Forbidden                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

A critical property: **RBAC never returns `deny`.** It's purely additive — bindings grant capabilities, and the absence of any matching binding is "no opinion". This is why you can't write an RBAC "deny" rule; you need webhook AuthZ for that.

A second critical property: **the FIRST allow wins.** If Node says allow, RBAC is never consulted. If RBAC says allow, Webhook is never consulted. This means a webhook AuthZ designed to deny something needs to come *before* any authorizer that might allow it.

### 18.3 The Go interface

```go
// k8s.io/apiserver/pkg/authorization/authorizer/interfaces.go
type Authorizer interface {
    Authorize(ctx context.Context, a Attributes) (authorized Decision, reason string, err error)
}

type Decision int
const (
    DecisionDeny Decision = iota
    DecisionAllow
    DecisionNoOpinion
)
```

The `Attributes` struct contains the user, verb, resource, namespace, name, subresource, API group/version — everything an authorizer needs.

### 18.4 Default modes

- `kube-up`-style clusters and many distributions default to `--authorization-mode=Node,RBAC`.
- Some installers add `Webhook` if you've enabled a policy webhook.
- Never include `AlwaysAllow` in a production cluster's mode list. There is no warning if you do; it just makes everything you've configured downstream pointless.

Source: `k8s.io/apiserver/pkg/authorization/union/union.go`, `pkg/kubeapiserver/authorizer/`.

---

## 19. RBAC: Objects, Verbs, Resources

RBAC is *the* authorization system you actually use day to day. Four objects, evaluated by one algorithm (next section).

### 19.1 The four objects

```
   Cluster scope                           Namespace scope
   ───────────                             ───────────────
   ClusterRole          binds via          Role
        ▲                  │                   ▲
        │                  ▼                   │
   ClusterRoleBinding ─────────────────► RoleBinding
        │                                      │
        │ subjects: User, Group, SA            │ subjects: User, Group, SA
        ▼                                      ▼
   Affects ALL namespaces +                Affects only one namespace
   cluster-scoped resources
```

| Object               | Scope                   | What it does                                                     |
|----------------------|-------------------------|------------------------------------------------------------------|
| `Role`               | namespace               | A set of rules (verb × resource × name). Lives in a namespace.   |
| `ClusterRole`        | cluster                 | A set of rules. Can be referenced by RoleBinding (scoped to a single namespace) or ClusterRoleBinding (cluster-wide). |
| `RoleBinding`        | namespace               | Binds a (Cluster)Role to subjects, within one namespace.         |
| `ClusterRoleBinding` | cluster                 | Binds a ClusterRole to subjects, cluster-wide.                   |

Two non-obvious facts:

1. **A RoleBinding can reference a ClusterRole.** This is the canonical "give Alice `view` in namespace foo" pattern — `view` is a default ClusterRole, but the binding is namespace-scoped, so Alice only has view rights in `foo`.
2. **A Role *cannot* be referenced by a ClusterRoleBinding.** ClusterRoleBindings only reference ClusterRoles. This is a strict type rule.

### 19.2 The rule structure

Every (Cluster)Role contains a `rules` field, each rule is a tuple:

```yaml
rules:
- apiGroups: [""]                  # "" = core API group
  resources: ["pods", "pods/log"]  # resource (and subresource as resource/subresource)
  resourceNames: []                # optional: restrict to specific names
  verbs: ["get", "list", "watch"]  # verbs (see below)
```

Multiple rules in one Role are OR'd together. A request matches if *any* rule matches all of its fields.

### 19.3 Verbs

Resource verbs (what you do to objects):

- `get`, `list`, `watch` — read
- `create` — POST
- `update`, `patch` — PUT, PATCH
- `delete` — DELETE
- `deletecollection` — DELETE on a collection (with a label selector or so)

Non-resource verbs (for URLs that aren't resources, like `/healthz` or `/metrics`):

```yaml
- nonResourceURLs: ["/healthz", "/livez", "/readyz"]
  verbs: ["get"]
```

These show up only in ClusterRoles (non-resource URLs are cluster-scoped).

Subresources are resource paths like `pods/exec`, `pods/log`, `pods/portforward`, `pods/binding`, `deployments/scale`, `nodes/proxy`. They're *first-class* in RBAC; granting `get` on `pods` does **not** grant `get` on `pods/log`. You must list `pods/log` explicitly.

This is intentional: `pods/exec` is a remote shell, so granting it is a much bigger deal than granting `get pods`. The granular split is the security feature.

### 19.4 The `apiGroups` field

- `""` — the core API group (Pods, Services, ConfigMaps, Secrets, Nodes, Namespaces, ServiceAccounts, etc.)
- `"apps"` — Deployments, StatefulSets, DaemonSets, ReplicaSets
- `"batch"` — Jobs, CronJobs
- `"networking.k8s.io"` — NetworkPolicy, Ingress
- `"rbac.authorization.k8s.io"` — RBAC objects themselves
- ...

Always specify the group. Forgetting and writing `apiGroups: [""]` for a Deployment rule is the most common mistake; it silently fails to grant anything (Deployments are in `apps`, not core).

### 19.5 `resourceNames`

Restricts a rule to specific resource names. Often used to grant `update` on exactly one ConfigMap or Secret:

```yaml
- apiGroups: [""]
  resources: ["secrets"]
  resourceNames: ["my-app-secrets"]
  verbs: ["get", "update"]
```

Subtle limitation: `resourceNames` does not work with `create`, `list`, `deletecollection`, or `watch`. It only works with verbs that target a *specific* named object (`get`, `update`, `patch`, `delete`). The reason: you can't know the name of an object before you create it; you can't list with a name filter through RBAC.

### 19.6 Full examples

A namespace-scoped Role allowing read of pods + their logs:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pod-reader
  namespace: prod
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log"]
  verbs: ["get", "list", "watch"]
```

A RoleBinding granting that Role to a group:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: prod-pod-readers
  namespace: prod
subjects:
- kind: Group
  name: oidc:platform-ops      # from OIDC claim
  apiGroup: rbac.authorization.k8s.io
- kind: ServiceAccount
  name: status-checker
  namespace: monitoring
- kind: User
  name: alice@example.com
  apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io
```

A ClusterRole that allows reading all custom resources of a particular CRD:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: orders-reader
rules:
- apiGroups: ["shop.example.com"]
  resources: ["orders"]
  verbs: ["get", "list", "watch"]
```

The default ClusterRoles every cluster ships with:

| ClusterRole       | What it grants                                                                                            |
|-------------------|-----------------------------------------------------------------------------------------------------------|
| `view`            | Read-only on most resources in a namespace. Does NOT include Secrets (because secrets are credentials).   |
| `edit`            | View + write on most resources. Does NOT allow editing RoleBindings/Roles (that would be escalation).     |
| `admin`           | Edit + manage Roles/RoleBindings inside a namespace. Bind via RoleBinding for per-namespace admin.        |
| `cluster-admin`   | Wildcard everything. The "root" role.                                                                     |
| `system:*`        | Many built-ins for control-plane components. Don't grant to humans.                                        |

The "admin via RoleBinding" pattern (binding `cluster-admin`'s namespace-scoped sibling, `admin`, to a Group via a RoleBinding) is how you delegate a namespace to a team without giving cluster-wide power.

### 19.7 The escalation guard

Without protection, a user with `create role` could write themselves a Role with arbitrary rules and bind it. RBAC has a built-in guard: **you cannot create or update a Role or ClusterRole whose rules exceed the rules you currently have.** This is enforced by the RBAC authorizer at create-time (the `RoleAdmission`-style logic is actually in the RBAC authorizer itself, not in admission). The technical name is the *escalation check*.

There's an explicit `escalate` verb you can grant to bypass this — used by trusted operators that need to manage roles dynamically. Grant it carefully; an `escalate`-holder is effectively cluster-admin.

Similarly, `bind`: to create a RoleBinding referencing a (Cluster)Role, you must either have all the rules in that role, or have the `bind` verb on the role.

These two guards (`escalate` for writing roles, `bind` for binding them) are why granting a user the `admin` role in their own namespace doesn't escalate them to cluster-admin: they can only bind roles that don't exceed `admin`.

Source: `k8s.io/kubernetes/plugin/pkg/auth/authorizer/rbac/`, `pkg/registry/rbac/validation/`.

---

## 20. RBAC Evaluation Algorithm

For each authorization request `(user, verb, resource, namespace, name)`, the RBAC authorizer does roughly the following:

```
function rbac_authorize(req):
    # Step 1: SystemMastersGroup shortcut (hardcoded)
    if "system:masters" in req.user.groups:
        return ALLOW

    # Step 2: Collect all bindings that mention this subject
    subjects_to_match = [
        ("User", req.user.name),
        *(("Group", g) for g in req.user.groups),
        # ServiceAccounts (if the user is one):
        ("ServiceAccount", parsed_ns, parsed_name) if SA,
    ]

    candidate_roles = []
    for rb in all ClusterRoleBindings:
        if any subject in rb.subjects matches subjects_to_match:
            candidate_roles.append(("ClusterRole", rb.roleRef.name, cluster_scope))
    for rb in all RoleBindings in (req.namespace OR cluster-scoped resource):
        if any subject in rb.subjects matches subjects_to_match:
            candidate_roles.append((rb.roleRef.kind, rb.roleRef.name, req.namespace))

    # Step 3: Walk each candidate role's rules; check if any rule matches
    for (kind, name, scope) in candidate_roles:
        role = fetch(kind, name)
        for rule in role.rules:
            if rule_matches(rule, req):
                return ALLOW

    return NO_OPINION   # not denied, just no match
```

The `rule_matches` function:

```
function rule_matches(rule, req):
    return (
        match_any(rule.apiGroups, req.apiGroup, "*") and
        match_any(rule.resources, req.resource, "*") and
        match_subresource(rule.resources, req.subresource) and
        match_any(rule.verbs, req.verb, "*") and
        match_resource_names(rule.resourceNames, req.name)
    )
```

A few subtle points:

- **All this happens in-memory.** The RBAC authorizer uses informers to cache all `Roles`, `RoleBindings`, `ClusterRoles`, `ClusterRoleBindings` locally. Authorization is a graph walk in RAM. No etcd round-trip per request.
- **Cost is roughly O(bindings-mentioning-user × rules-per-role).** For a normal cluster, this is < 1 ms. For a cluster with thousands of bindings per user (which happens in multi-tenant SaaS where each tenant has multiple bindings), it can become measurable. The mitigation is to use group-based bindings rather than per-user bindings.
- **`subjects` matching has special cases.** `ServiceAccount` subjects can be referenced by `kind: ServiceAccount, name: foo, namespace: bar`, but you can also bind the entire SA group: `kind: Group, name: system:serviceaccounts:bar` matches all SAs in `bar`.

### 20.1 A worked example

Setup:

```yaml
# A user authenticates with groups: ["oidc:platform-ops", "system:authenticated"]
# Namespace: payments
# Request: get pods/log

# Bindings:
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: platform-ops-view
subjects:
- kind: Group
  name: oidc:platform-ops
roleRef:
  kind: ClusterRole
  name: view
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: payments-debug
  namespace: payments
subjects:
- kind: Group
  name: oidc:platform-ops
roleRef:
  kind: ClusterRole
  name: pod-debugger          # custom: includes pods/log + pods/exec
```

Evaluation:

1. Collect subjects: User=alice@..., Group=oidc:platform-ops, Group=system:authenticated.
2. Find bindings mentioning these subjects:
   - `platform-ops-view` → ClusterRole `view`.
   - `payments-debug` → ClusterRole `pod-debugger` (RoleBinding lives in `payments`, request is in `payments`, so it applies).
3. Walk rules in `view`: no rule matches `pods/log` (because `view` excludes it).
4. Walk rules in `pod-debugger`: a rule grants `get pods/log` → **ALLOW**.

The same `get pods/log` request in a *different* namespace (`billing`, say) would only have `view` to evaluate, would not find a match, and would return NO_OPINION → 403.

### 20.2 The auditor's checklist

When investigating "who can do X?", the answer is the set of subjects mentioned in bindings whose roles include a rule matching X. The tooling for this:

- `kubectl auth can-i --as` (see §28) — answers for a specific subject.
- `kubectl-who-can` (krew plugin) — answers the reverse: who has a verb on a resource.
- `rbac-lookup` (krew plugin) — answers "what can this subject do?"

Internally, all these tools do the same graph walk.

---

## 21. RBAC Modeling Patterns

### 21.1 Principle of least privilege, operationalized

The phrase "least privilege" is meaningless without a model. In Kubernetes, "least privilege" means:

1. **One ServiceAccount per workload.** Never share. The default `ServiceAccount` in any namespace exists for compatibility; don't use it.
2. **One Role/ClusterRole per *capability*.** A capability is a coherent set of verbs over a coherent set of resources. "Read pods + logs + events for debugging" is one capability. "List nodes + get node metrics" is another. Don't make god-roles.
3. **Bind by group, not by user.** Bind `oidc:platform-ops` to a role; let your IdP control who's in the group. Per-user bindings are unmanageable.
4. **Per-namespace admin via RoleBinding to a Group.** Use the default `admin` ClusterRole, bind it via a RoleBinding to the team's group. They get full namespace control with no cluster-wide power.

### 21.2 The "view / edit / admin / cluster-admin" defaults

| Role            | Use case                                                                                             |
|-----------------|------------------------------------------------------------------------------------------------------|
| `view`          | Read-only observability access for SREs in a namespace. No Secrets.                                  |
| `edit`          | "I deploy here." Read+write on workloads, ConfigMaps, Services. No RBAC objects.                     |
| `admin`         | "I run this namespace." Edit + manage RBAC inside it (via the bind/escalate guards described in §19.7). |
| `cluster-admin` | The platform team. Often the *only* group bound to this should be break-glass.                       |

### 21.3 Separating human identity from CI identity

A common anti-pattern: developers run `kubectl apply` from their laptops using their personal OIDC kubeconfig, and CI deploys the same code using *their* token via a leaked file. Now you can't tell from the audit log whether a deploy was human-initiated or CI.

Correct pattern:

- Humans authenticate via OIDC. Their groups grant `edit` in dev namespaces, `view` in prod.
- CI authenticates via a per-pipeline ServiceAccount in a `ci` namespace. The SA's role lets it apply to specific target namespaces (via a RoleBinding in each target namespace that references the CI SA in the `ci` namespace as a subject — yes, RoleBindings can reference SAs in other namespaces).
- The CI SA in dev gets `edit` in dev; in prod it gets specific verbs, possibly through a webhook-mediated approval gate.

Audit logs now clearly distinguish `system:serviceaccount:ci:prod-deployer` from `oidc:alice@example.com`.

### 21.4 Aggregated ClusterRoles for extension

When you ship a CRD, you also want to extend `view`/`edit`/`admin` to include your CRD. The aggregation pattern lets you do this declaratively:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: my-crd-view
  labels:
    rbac.authorization.k8s.io/aggregate-to-view: "true"
rules:
- apiGroups: ["shop.example.com"]
  resources: ["orders", "orders/status"]
  verbs: ["get", "list", "watch"]
```

The label causes the `ClusterRoleAggregator` controller to merge your rules into the `view` ClusterRole. Anyone bound to `view` now also gets view on your orders. The aggregation labels are: `aggregate-to-view`, `aggregate-to-edit`, `aggregate-to-admin`. (`cluster-admin` is `*/*/*` and doesn't need aggregation.)

The receiver of aggregation:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: view
aggregationRule:
  clusterRoleSelectors:
  - matchLabels:
      rbac.authorization.k8s.io/aggregate-to-view: "true"
rules: []   # filled in by the controller, do not edit
```

When you write an operator, ship three aggregated ClusterRoles (view/edit/admin). It's the polite way to extend the defaults.

### 21.5 Dangerous wildcards

The patterns to flag in any review:

```yaml
# Cluster-admin in disguise:
- apiGroups: ["*"]
  resources: ["*"]
  verbs: ["*"]

# Almost as bad:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["*"]                   # includes update/patch on secrets → read by re-read

# The escalation footgun:
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["*"]
  verbs: ["*"]                   # can write any role, bind anything → cluster-admin via escalation

# The exec footgun:
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create"]              # remote shell into any pod = run as that pod's SA
```

A simple lint rule for any review: any verb of `*`, any resource of `*`, or any apiGroup of `*` is justified or rejected. No middle ground.

Pods with `pods/exec` (or `pods/portforward`, or `pods/attach`) are particularly dangerous because they let the holder execute as another pod's identity — implicit privilege escalation through the pod's mounted SA token.

---

## 22. Aggregated ClusterRoles

Covered above in §21.4. Briefly, the implementation:

The `clusterroleaggregation` controller (in `kube-controller-manager`) watches all ClusterRoles. For any ClusterRole that has an `aggregationRule`, it:

1. Lists all ClusterRoles matching the selectors.
2. Concatenates their `rules`.
3. Writes the result into the target's `rules` field (and only `rules`; the `aggregationRule` itself is untouched).

If you edit the `rules` of an aggregating ClusterRole, the controller will overwrite your edit on the next sync. The contract is: write aggregating ClusterRoles, leave the aggregated ones alone.

Source: `k8s.io/kubernetes/pkg/controller/clusterroleaggregation/`.

---

## 23. The Node Authorizer

A targeted authorizer specifically for the kubelet's identity. Without it, you'd have to write a complex RBAC policy that grants each kubelet read on its own Node, write on its own Node's status, read on its own pods' Secrets/ConfigMaps, etc. — and the policy would be the same for every node, parameterized by the node's name. That's awkward in RBAC, so the Node authorizer special-cases it.

### 23.1 What it gates

It only applies to users in the `system:nodes` group with usernames of the form `system:node:<nodename>`. For any other user it returns "no opinion" (falls through to RBAC).

For matching users, it allows:

- **Read** on `Nodes/<nodename>` (its own Node), `Pods` bound to this Node, `Services`, `Endpoints`, `EndpointSlices`, `PersistentVolumes` (to discover what's mounted), `PersistentVolumeClaims` referenced by its pods, `Secrets`/`ConfigMaps`/`ServiceAccountTokens` referenced by its pods (and only those).
- **Write** on `Nodes/<nodename>/status`, `Pods/<podname>/status` for its own pods, `Events`, `Leases` in `kube-node-lease`, `CertificateSigningRequests` (for cert rotation).
- **Create** TokenReviews / SubjectAccessReviews (for delegated auth).

What it forbids:
- Reading Secrets that aren't mounted by any of this kubelet's pods.
- Writing to Pods on other nodes.
- Writing to other Nodes.

### 23.2 The graph

The Node authorizer maintains an in-memory graph (`pkg/registry/authorization/node/node_authorizer.go`):

```
   Node ── runs ──► Pod ── uses ──► Secret
                      │              │
                      ├── uses ──► ConfigMap
                      │
                      ├── uses ──► PVC ── binds ──► PV
                      │
                      └── uses ──► SA  ── owns ──► TokenRequest
```

A query "may kubelet on node-2 read Secret X?" becomes "is there a path Node(node-2) → Pod → Secret(X)?" If yes, allow. If no, no opinion (which the chain will turn into deny).

### 23.3 Combined with NodeRestriction admission

The Node authorizer says what a kubelet *can read*. The `NodeRestriction` admission plugin (chapter 06) says what a kubelet *can write* — specifically, it forbids a kubelet from labeling its Node with labels in the `node-role.kubernetes.io/*` or `*.kubernetes.io/*` namespaces (except a small set), and forbids modifying other Nodes' status. This is the "kubelet can't claim it's a master node and steal scheduling" defense.

Both must be enabled together:
```
--authorization-mode=Node,RBAC
--enable-admission-plugins=...,NodeRestriction,...
```

Skipping NodeRestriction in particular is a real CVE-grade misconfiguration: a compromised kubelet on node-X can taint other nodes, lie about its own labels to attract sensitive workloads, etc.

Source: `k8s.io/kubernetes/plugin/pkg/auth/authorizer/node/`.

---

## 24. ABAC: Legacy

Attribute-Based Access Control. A flat JSON-lines policy file:

```
{"apiVersion":"abac.authorization.kubernetes.io/v1beta1","kind":"Policy","spec":{"user":"alice","namespace":"*","resource":"*","apiGroup":"*","readonly":true}}
{"apiVersion":"abac.authorization.kubernetes.io/v1beta1","kind":"Policy","spec":{"user":"bob","namespace":"prod","resource":"pods","apiGroup":""}}
```

Enabled via `--authorization-mode=ABAC --authorization-policy-file=/etc/.../policy.jsonl`. Documented for completeness; **don't use it.** It has no live management API, no escalation guards, no integration with bindings — any change requires editing a file and restarting the apiserver, in HA you have to do this on every replica. RBAC supersedes it in every dimension.

The only reason to know it exists: some very old custom installers may still use it, and you should migrate.

Source: `k8s.io/apiserver/pkg/authorization/abac/`.

---

## 25. Webhook Authorization (SubjectAccessReview)

When RBAC isn't expressive enough — for example, you want to enforce "deny any write to namespace `prod` between 22:00 and 06:00 UTC" — you can delegate the decision to an external endpoint.

### 25.1 Configuration

```
--authorization-mode=Node,RBAC,Webhook
--authorization-webhook-config-file=/etc/k8s/authz-webhook.kubeconfig
--authorization-webhook-cache-authorized-ttl=5m
--authorization-webhook-cache-unauthorized-ttl=30s
```

Caching is essential. Unauthorized decisions are cached for a shorter time so a newly-granted permission becomes effective quickly.

### 25.2 The protocol

The apiserver POSTs a `SubjectAccessReview` to the webhook:

```yaml
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: alice@example.com
  groups: [ "oidc:platform-ops", "system:authenticated" ]
  resourceAttributes:
    namespace: prod
    verb: update
    group: apps
    resource: deployments
    name: payments
```

The webhook responds:

```yaml
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
status:
  allowed: false
  denied: true
  reason: "writes to prod outside business hours"
```

Three response shapes:
- `allowed: true` → ALLOW.
- `denied: true` → DENY.
- both false → NO_OPINION.

### 25.3 Where it makes sense

- **Time-based access policies** (after-hours change freezes).
- **Centralized PDP** (your security team owns a single service that decides; webhook AuthZ asks it).
- **Integrating with non-RBAC policy engines** (some Kyverno/OPA setups expose authz this way, though most run as admission webhooks instead).

### 25.4 Where it doesn't

- **Anything you can express in RBAC.** Don't move RBAC decisions out of the cluster — every API request now waits for your webhook.
- **As a global "deny" mechanism if you trust your authenticators.** A webhook AuthZ misconfiguration can lock out the entire cluster, including the controllers that would let you fix it.

The latency impact: every authorization request now potentially round-trips. The cache helps; a well-tuned webhook authzer can run with sub-millisecond p99 because most decisions hit cache.

Source: `k8s.io/apiserver/plugin/pkg/authorizer/webhook/`.

---

## 26. Impersonation

A privileged user can ask the apiserver "do this as if you were a different user." This is what enables `kubectl --as=alice@example.com`, what platform tools do to act on behalf of users, and what dashboard apps use to evaluate "what could the user see?"

### 26.1 The headers

```
Impersonate-User: alice@example.com
Impersonate-Group: oidc:platform-ops
Impersonate-Group: system:authenticated
Impersonate-Uid: 12345
Impersonate-Extra-scope: prod
```

When the apiserver sees these on an authenticated request, it:

1. Authenticates the *real* request via the chain (the bearer token / cert) → real user.
2. Checks the real user has the `impersonate` verb on the requested impersonation:
   - For `Impersonate-User`: `impersonate` on `users` (with `resourceName` = the target).
   - For `Impersonate-Group`: `impersonate` on `groups`.
   - For `Impersonate-Uid`: `impersonate` on `uids`.
   - For `Impersonate-Extra-<key>`: `impersonate` on `userextras/<key>`.
3. Replaces `user.Info` with the impersonated identity, but records the real user in audit as `impersonatedUser`.

So a user who can impersonate alice can do *anything alice can do*. Granting `impersonate` is granting "be them".

### 26.2 An impersonation Role

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: support-impersonator
rules:
# Allow impersonating any developer:
- apiGroups: [""]
  resources: ["users"]
  resourceNames: ["alice@example.com", "bob@example.com"]
  verbs: ["impersonate"]
- apiGroups: [""]
  resources: ["groups"]
  resourceNames: ["oidc:developers"]
  verbs: ["impersonate"]
# Forbid impersonating system: groups (no resourceNames wildcards here).
```

Two important traps:

- **`impersonate` with `resourceNames: []` (i.e., unconstrained) = cluster-admin.** Because the impersonator can impersonate `system:masters`. Always restrict `resourceNames`.
- **`impersonate` on `groups` is independent of `impersonate` on `users`.** Granting impersonate on a user means you can pretend to be them, but *only with the groups they actually have*. To impersonate-with-arbitrary-groups requires impersonate on `groups` too.

### 26.3 Where impersonation is used

- **`kubectl --as=...`** for testing "can user X do Y?".
- **Dashboard backends** that present a per-user view by impersonating the user via the dashboard's own SA.
- **GitOps engines** like Argo CD (optionally) that apply manifests *as* a user identified in a Git commit (so audit logs name the human, not Argo).
- **Audit / debug tools** that want to observe through a specific user's permission set.

### 26.4 Audit trail of an impersonated request

```json
{
  "level": "RequestResponse",
  "user": {
    "username": "argocd-server@cluster.local",
    "groups": ["system:authenticated"]
  },
  "impersonatedUser": {
    "username": "alice@example.com",
    "groups": ["oidc:developers", "system:authenticated"]
  },
  "verb": "create",
  "objectRef": { "resource": "deployments", "namespace": "prod", "name": "payments" }
}
```

Both identities are preserved. Forensics rule: if you only log the impersonated user, you can't catch a rogue impersonator. If you only log the real user, you can't pin actions to a human.

---

## 27. Audit and Identity

Identity is what makes audit useful. The audit subsystem (full coverage: chapter 30) records every API request with the `user`, `verb`, `objectRef`, response code, and request/response bodies (depending on the configured `Level`).

### 27.1 The audit policy

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
# Don't log readonly system noise:
- level: None
  users: ["system:kube-proxy", "system:kube-scheduler"]
  verbs: ["get","list","watch"]
# Log all SA actions at Metadata:
- level: Metadata
  userGroups: ["system:serviceaccounts"]
# Full body on writes to secrets:
- level: RequestResponse
  resources:
  - group: ""
    resources: ["secrets"]
  verbs: ["create","update","patch","delete"]
# Default: log every other request at Metadata level
- level: Metadata
```

What "Metadata" means: user, verb, resource, namespace, name, response code, no body. "Request" includes the request body. "RequestResponse" includes both. "None" suppresses.

### 27.2 Identity-related queries

Common audit queries (e.g., via the audit log backend or a SIEM):

- "Show me every action by `oidc:platform-ops` in `prod` namespace in the last 24h."
- "Show me every impersonation of `system:masters` ever."
- "Show me every Secret read by SAs outside their own namespace."
- "Show me every action by a `system:anonymous` request that succeeded."

If anonymous succeeded on anything non-trivial, that's an incident.

### 27.3 What forensics depends on

- **Preserve `user`, `impersonatedUser`, and `sourceIPs`.** If you collapse to just "actor", you lose the chain.
- **Preserve `objectRef` including `uid`.** Names get reused; UIDs don't.
- **Track `userAgent`.** `kubectl/v1.29 ...` vs `argocd/2.10 ...` distinguishes humans from automation.

---

## 28. Self-Inquiry: can-i, whoami, reconcile, SelfSubjectReview

These are the operator-facing tools. They wrap several APIs:

### 28.1 `kubectl auth can-i`

```
$ kubectl auth can-i delete pods --namespace prod
yes

$ kubectl auth can-i delete pods --namespace prod --as alice@example.com
no

$ kubectl auth can-i '*' '*' --all-namespaces       # am I cluster-admin?
yes
```

Backed by `SelfSubjectAccessReview` (asks "can I?") or `SubjectAccessReview` with `--as` (asks "can subject X?").

### 28.2 `kubectl auth whoami`

```
$ kubectl auth whoami
ATTRIBUTE   VALUE
Username    oidc:alice@example.com
Groups      [oidc:platform-ops oidc:developers system:authenticated]
```

Backed by `SelfSubjectReview` — the apiserver echoes back what it sees as your identity. Invaluable for debugging "why isn't my OIDC working?" — if `whoami` shows `system:anonymous`, your token wasn't authenticated.

### 28.3 `kubectl auth reconcile`

```
$ kubectl auth reconcile -f roles.yaml
clusterrole.rbac.authorization.k8s.io/my-role reconciled
  reconciliation required create
  missing rules added:
        {Verbs:[get list] APIGroups:[] Resources:[pods] ...}
```

Reconciles RBAC objects in a smart way that handles the bind/escalate guards: it removes extra rules safely, adds missing rules, doesn't delete bindings unless `--remove-extra-permissions` is set. Designed for "apply this RBAC declaratively without locking yourself out."

### 28.4 SelfSubjectRulesReview

```
$ kubectl auth list -n prod    # 1.29+
```

Lists all rules that apply to *you* in a namespace. Backed by `SelfSubjectRulesReview` — the apiserver walks every binding that applies to you and returns the merged rule set. Great for "show me everything I can do here."

---

## 29. Certificate-Based Identity: certificates.k8s.io and CSR Approval

The bootstrap-token story (§6) leaves an obvious gap: how does the kubelet end up with a long-lived x509 cert? Answer: it generates a key, posts a `CertificateSigningRequest`, somebody (a controller or human) approves it, and a signer signs it.

### 29.1 The CSR object

```yaml
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: node-csr-abc123
spec:
  signerName: kubernetes.io/kube-apiserver-client-kubelet
  request: LS0tLS1CRUdJTi...        # base64 of PEM-encoded PKCS#10 CSR
  usages: ["client auth"]
  expirationSeconds: 31536000        # 1 year
  username: system:bootstrap:07401b  # who submitted it
  groups: ["system:bootstrappers:kubeadm:default-node-token"]
status:
  conditions: []                     # populated by approver
  certificate: ""                    # populated by signer
```

### 29.2 The built-in signers

| Signer name                                           | Purpose                                                              |
|-------------------------------------------------------|----------------------------------------------------------------------|
| `kubernetes.io/kube-apiserver-client`                 | Client certs for users that authenticate to the apiserver.            |
| `kubernetes.io/kube-apiserver-client-kubelet`         | Kubelet client certs (kubelet → apiserver).                          |
| `kubernetes.io/kubelet-serving`                       | Kubelet *serving* certs (apiserver → kubelet, e.g., for logs/exec).  |
| `kubernetes.io/legacy-unknown`                        | Catch-all for non-standard signers; you can add your own controller. |

Each signer corresponds to a CA configured on the cluster. The `csrsigning` controller in `kube-controller-manager` watches Approved CSRs and signs them using:

```
--cluster-signing-cert-file=/etc/kubernetes/pki/ca.crt
--cluster-signing-key-file=/etc/kubernetes/pki/ca.key
--cluster-signing-duration=8760h
```

You can also configure separate signing CAs per signer (`--cluster-signing-kubelet-client-cert-file=...`), which is the right pattern: don't use the same CA that signs admin certs to sign kubelet certs.

### 29.3 The approval flow

```
┌──────────────────────────────────────────────────────────────────────┐
│ KUBELET BOOTSTRAP → CERT APPROVAL → IDENTITY UPGRADE                 │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  1. Kubelet starts with bootstrap token in /etc/kubernetes/           │
│     bootstrap-kubelet.conf                                            │
│                                                                       │
│  2. Kubelet generates a private key, builds a CSR with                │
│     CN = system:node:<nodename>, O = system:nodes                     │
│     (these names are required; the CSR approver enforces them)        │
│                                                                       │
│  3. Kubelet authenticates as system:bootstrap:<id>                    │
│     POSTs CertificateSigningRequest                                   │
│                                                                       │
│  4. Approver (either auto-approver controller, or human via           │
│     `kubectl certificate approve <name>`) sets a condition:           │
│        type: Approved                                                 │
│        status: True                                                   │
│                                                                       │
│     The auto-approver (kubelet-csr-approver, runs in controller       │
│     manager) approves if:                                             │
│       - signerName == kube-apiserver-client-kubelet                   │
│       - submitter has group system:bootstrappers:kubeadm:...          │
│       - CN matches system:node:<nodename>                             │
│       - (optionally) the requested expiration is reasonable           │
│                                                                       │
│  5. csrsigning controller sees Approved, signs the CSR, writes        │
│     the cert into status.certificate.                                 │
│                                                                       │
│  6. Kubelet sees its CSR has been signed, reads the cert,             │
│     writes /var/lib/kubelet/pki/kubelet-client-current.pem,           │
│     switches kubeconfig to use it, deletes bootstrap-kubelet.conf.    │
│                                                                       │
│  7. Future authentication: x509 client cert, user=system:node:<n>,    │
│     group=system:nodes → matched by Node authorizer (§23).            │
│                                                                       │
│  8. Cert rotation: kubelet's cert manager submits a new CSR before    │
│     expiry, gets it signed, atomically rotates.                       │
└──────────────────────────────────────────────────────────────────────┘
```

The certificates.k8s.io API is also how *user* certs can be programmatically issued (via the `kube-apiserver-client` signer), how some service meshes issue their mTLS certs (via a custom signer), and how the cert-manager project (an unrelated project but with similar name) integrates with K8s' CSR API for ACME and other issuance.

### 29.4 Rotation

Kubelet's cert manager (chapter 10) tracks the current cert's expiry and submits a renewal CSR at ~80% TTL. The renewal CSR is auto-approved by the kubelet-cert-approver if the requesting kubelet's existing cert is still valid (chicken-and-egg solved: you authenticate the renewal with the current cert). This is how kubelet certs roll without human intervention.

Source: `k8s.io/kubernetes/pkg/controller/certificates/`, `pkg/registry/certificates/`.

---

## 30. External Secrets and the Boundary of AuthN

Authentication produces identity. *Authorization* decides what that identity can do. But identity is also what you use to *fetch secrets* — and at some point you cross from "Kubernetes verified your identity" to "Vault / AWS Secrets Manager / Azure Key Vault verified your federated identity and gave you a secret."

That boundary is where this chapter (mostly) ends and chapter 27/28 begins. Brief overview of what lives on the other side:

### 30.1 External Secrets Operator (ESO)

A controller that watches `ExternalSecret` resources and syncs values from an external store into Kubernetes Secrets:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: db-creds
  namespace: payments
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: db-creds          # the K8s Secret to write
  data:
  - secretKey: password
    remoteRef:
      key: secret/data/payments/db
      property: password
```

ESO authenticates to the external store using the cluster's projected SA token (audience `vault`), or IRSA, or Workload Identity, or a static credential. The Vault / cloud secret store then issues secrets back. ESO writes them into a normal K8s Secret object.

### 30.2 Secrets Store CSI Driver

Mounts external secrets *directly* into a pod's filesystem without first becoming a K8s Secret. The driver runs as a DaemonSet, the pod mounts a `csi` volume of type `secrets-store.csi.x-k8s.io`, the driver fetches secrets at mount time from the provider (Vault, AWS, Azure, GCP) using the pod's identity.

Pro: nothing in etcd, including the Secret.
Con: secrets are only available at pod start; tougher rotation story (though the driver supports refresh).

### 30.3 Why this isn't "AuthN" per se

Both patterns *use* AuthN (the pod's projected token authenticates to the external store), but they sit on top of it. The K8s side is unchanged. The differentiation:

- **AuthN gives the pod an identity.**
- **Cloud workload identity (§14-16) federates that identity outside the cluster.**
- **External secrets use that federated identity to fetch material.**

The next chapter where this gets full treatment is 28 (runtime security and policy) — including how admission policies enforce that pods *can only mount secrets they're entitled to*.

---

## 31. At-Rest Secret Encryption: EncryptionConfiguration and KMS v2

Kubernetes stores Secrets in etcd. By default they are stored **unencrypted** — `etcdctl get` returns the raw value. This is the single most common surprise to people new to the security model. Encryption at rest is a separate, opt-in feature.

### 31.1 EncryptionConfiguration

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
- resources:
  - secrets
  - configmaps                 # optional; configmaps may contain secrets too
  providers:
  - kms:                       # KMS v2 provider
      name: aws-kms
      endpoint: unix:///var/run/kmsplugin/socket
      timeout: 3s
  - aescbc:                    # local AES-CBC fallback (or primary)
      keys:
      - name: key1
        secret: <base64 32-byte key>
  - identity: {}               # last entry MUST be identity for ability to read old data
```

The apiserver runs through the providers in order on writes (first one wins) and on reads (each provider tries to decrypt, identity returns the raw value). The flag:

```
--encryption-provider-config=/etc/k8s/encryption-config.yaml
```

### 31.2 KMS v2

The original KMS v1 provider used a "per-secret data encryption key" approach but stored the wrapped DEK alongside the secret, requiring a KMS round-trip per read. KMS v2 (1.27+, GA in 1.29) changes this:

- KEK is fetched once and cached.
- DEKs are generated locally, each used for many encryptions before rotation.
- Reads are local (decrypt DEK once, then decrypt secrets locally).
- KEK rotation is tracked via a key ID; the apiserver re-encrypts data when the key ID changes.

A KMS v2 plugin is a Unix-socket gRPC server that the apiserver calls. AWS, GCP, Azure, and HashiCorp Vault all ship plugins.

### 31.3 Why kubelet still decrypts at runtime

This is the subtle part: the *secret* is encrypted at rest in etcd, but the apiserver decrypts it on every read. Kubelet, when it mounts a Secret into a pod, fetches the *plaintext* value from the apiserver. The pod sees the plaintext.

So encryption at rest defends against:
- Stolen etcd backups.
- Direct etcd disk reads.
- A compromised etcd node (less than fully — the KMS plugin is still on the apiserver, so etcd alone can't decrypt).

It does *not* defend against:
- A compromised apiserver (has access to the KMS).
- A compromised pod (sees its own plaintext).
- An attacker with RBAC `get secrets` (the API serves plaintext).

This is the correct model — encryption at rest closes the etcd attack surface, but it's not a substitute for RBAC, network policy, and pod-level secret hygiene.

### 31.4 Rotation

To rotate keys:

1. Add the new key to the providers list with a higher priority. New writes use it. Old reads still work because old keys are still listed.
2. Run a controller (or a kubectl loop) that `kubectl get && kubectl replace` every Secret, forcing re-encryption with the new key.
3. Remove the old key.

For KMS v2 the controller part is less manual; key version changes drive re-encryption.

Source: `k8s.io/apiserver/pkg/storage/value/encrypt/`, `staging/src/k8s.io/apiserver/plugin/pkg/admission/`.

---

## 32. Common Attack Paths

A staff-level chapter has to enumerate the threat model, not just the mechanism. Here are the paths attackers actually take.

### 32.1 Pod escape → cluster-admin via SA token

```
1. Attacker exploits a vulnerability in a pod (web app, dep with RCE, ...).
2. Reads /var/run/secrets/kubernetes.io/serviceaccount/token.
3. Calls apiserver as the SA.
4. The pod's SA has been granted (typically by carelessness) get/list/watch
   on Secrets across all namespaces.
5. Reads the kube-system secrets, including a SA token for a controller
   with cluster-admin rights.
6. Re-authenticates with that token. Game over.
```

Mitigation: the SA mounted in the pod has *minimal* RBAC. The default SA has none. `automountServiceAccountToken: false` for pods that don't need API access. PodSecurity restricted profile.

### 32.2 Wildcard impersonate

```
1. An operator was granted impersonate on users (without resourceNames).
2. Attacker compromises the operator's SA token.
3. Sends Impersonate-User: system:admin / Impersonate-Group: system:masters.
4. Apiserver allows (impersonate verb without resourceNames is unconstrained).
5. Game over.
```

Mitigation: every `impersonate` rule must have `resourceNames`. Period.

### 32.3 OIDC group spoofing

```
1. OIDC IdP allows users to set arbitrary "groups" claims (misconfig).
2. User adds "system:masters" to their groups.
3. apiserver doesn't have --oidc-groups-prefix set.
4. RBAC sees user with group system:masters.
5. RBAC's special-case grants everything.
```

Mitigation: `--oidc-groups-prefix`, `--oidc-required-claim` to gate sensitive groups, and lock down the IdP's claim issuance.

### 32.4 Static token in CI

```
1. CI uses a long-lived static token (file, secret, env).
2. Token leaks: CI logs, shared screen, a slack message.
3. Attacker hits apiserver from anywhere. No location pinning.
4. RBAC on CI is broad (it deploys to everything).
5. Game over.
```

Mitigation: short-lived projected tokens minted per CI run, audience pinned to the apiserver, rotated automatically. No long-lived bearer tokens in CI.

### 32.5 SA token usable from any node

This is true even today; a SA token is bearer-only. If you can read the file from inside the pod, you can take the token outside the cluster and use it from the internet. The bound-pod claim (§8) helps in that the token *expires* in 1h, but during that hour it's portable.

Mitigation:
- IP-restrict the apiserver to known networks (often impractical).
- Use the bound-pod claim explicitly in webhook AuthN/AuthZ: verify that requests claiming to be a pod SA come from inside the cluster network. Some service meshes encode this.
- Short TTL projected tokens (1h instead of 24h).

### 32.6 Default SA used everywhere

In every namespace, there's an SA named `default`. If you don't set `spec.serviceAccountName`, your pod runs as `default`. Most clusters have careless RBAC that grants `default` more than it should. Combined with all your workloads using `default`, an attacker who lands in *any* pod gets *all* of those capabilities.

Mitigation: every Deployment specifies `spec.serviceAccountName: <specific-sa>`. `default` SA in every namespace has `automountServiceAccountToken: false`.

### 32.7 Audience claim ignored

Receiver gets a JWT, verifies signature + issuer + exp, forgets to check audience. An attacker who has a token minted for a different audience (say, the cluster's internal one) presents it to the receiver. Receiver accepts. Cross-system confused deputy.

Mitigation: always verify `aud`. Code review for every JWT verification path.

### 32.8 NodeRestriction not enabled

A kubelet on a compromised node can label its node `prod=true`, taint other nodes to drive workloads to itself, modify pod tolerations, etc. NodeRestriction admission is the firewall.

Mitigation: `--enable-admission-plugins=...,NodeRestriction,...` always. Verify in `kubectl get --raw /metrics | grep apiserver_admission`.

### 32.9 Long-lived legacy SA Secret

Same as §32.1, but with a token that never expires. Even worse — once leaked, can't be revoked except by destroying the SA. Older clusters and any cluster that uses the manual Secret pattern for service-account-tokens still has this risk.

Mitigation: audit, replace with projected tokens or `kubectl create token` per use.

---

## 33. Defensive Patterns

The mirror image of the attack paths.

### 33.1 Per-workload SAs and `automountServiceAccountToken: false`

Default to no API access. Opt in per workload.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: default
  namespace: payments
automountServiceAccountToken: false   # disable default-SA mount in this namespace
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
spec:
  template:
    spec:
      serviceAccountName: payments-api-sa
      automountServiceAccountToken: true    # explicit
```

### 33.2 PodSecurity restricted profile

Namespace-level enforcement (chapter 28):

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: payments
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
```

Restricted forbids privileged pods, restricts host mounts, requires non-root, drops capabilities. None of these are AuthN directly but every one of them shrinks the blast radius of an SA token leak.

### 33.3 Short-TTL projected tokens

```yaml
projected:
  sources:
  - serviceAccountToken:
      audience: vault
      expirationSeconds: 600     # 10 minutes
      path: vault-token
```

The shorter the TTL, the smaller the window of a leak. Below ~10 minutes you start fighting kubelet's refresh cadence; that's a reasonable floor.

### 33.4 OIDC required claims for prod groups

```
--oidc-required-claim=mfa=true
--oidc-required-claim=email_verified=true
```

A user without MFA can't get an ID token with `mfa: true`, so they can't talk to the cluster, regardless of group membership.

For finer-grained control, use the `AuthenticationConfiguration` file (§10.2) to write CEL expressions.

### 33.5 Deny-by-default policy for `impersonate`

Almost no one should be able to impersonate. If you grant it, grant it with `resourceNames` to the exact subjects, never wildcards.

Audit query:

```
kubectl get clusterroles -o json | \
  jq '.items[] | select(.rules[]? | .verbs[]? == "impersonate") | .metadata.name'
```

Every result must be justified.

### 33.6 Disable anonymous in production

Despite the default, you almost certainly want:

```
--anonymous-auth=false
```

unless you have a clear reason to publish OIDC discovery or unauthenticated health endpoints publicly. The trade-off: bootstrap tooling that scrapes `/openid-configuration` from outside the cluster may break; you'll need to allow it explicitly via webhook AuthN or a known credential.

### 33.7 Audit log everything that touches identity

Audit policy should log at *RequestResponse* level:
- All RBAC writes (Role, ClusterRole, RoleBinding, ClusterRoleBinding).
- All ServiceAccount creates/deletes.
- All Secret writes.
- All TokenRequest calls.
- All CertificateSigningRequest writes.

The cost is bytes; the value is forensics.

### 33.8 Periodic RBAC reviews

Automate `rbac-lookup` or equivalent in CI:

- Every binding to `system:masters` (should be zero non-default).
- Every binding to `cluster-admin` (should be tiny, named, justified).
- Every binding to `system:unauthenticated` / `system:anonymous` (should be exactly the default discovery bindings).
- Every wildcard rule (`*` in verbs, resources, or apiGroups).
- Every `impersonate` rule.

---

## 34. Pitfalls

The grab-bag of "actually happened in production" failures. Some repeat themes from earlier sections.

1. **Long-lived SA Secrets.** Audit and migrate. They keep being valid; even after you stop creating them, the old ones persist.
2. **Default SA used everywhere.** Pods don't specify `serviceAccountName`; the `default` SA in every namespace ends up with too much access; pod compromise becomes namespace compromise.
3. **RBAC verb wildcards.** `verbs: ["*"]` looks innocuous until you realize it includes `escalate` and `bind` (if the resource is RBAC) or `exec` (if the resource is pods).
4. **OIDC group claim flat-namespace collisions.** Without `--oidc-groups-prefix`, an IdP group named `cluster-admin` does what you'd fear.
5. **Audience claim ignored on the receiving side.** Implementers forget; tokens minted for one audience are accepted by another. Lint your JWT verification code.
6. **Projected token TTL too long.** Default is ~1 hour. Many operators leave it; some bump it to a day for convenience. Leaks become long-lived.
7. **`--anonymous-auth=true` in production.** The default. Disable it.
8. **`--insecure-port` left enabled.** Removed entirely in 1.20+, but cluster restores from old backups can resurrect it via the old static manifest. Verify your apiserver flags after every upgrade.
9. **Apiserver client CA not rotated.** The `--client-ca-file` is forever; if a client cert's CA private key leaks, every cert is suspect. Have a rotation plan.
10. **SubjectAccessReview cache poisoning.** A webhook AuthZ that returns `allowed: true` based on outdated state (e.g., cached IdP groups) keeps allowing a revoked user until the cache expires. Set sane `--authorization-webhook-cache-authorized-ttl`.
11. **NodeRestriction not enabled.** Easy to miss; verify via the apiserver's `--enable-admission-plugins`.
12. **Service mesh mTLS confused with apiserver AuthN.** A service mesh issuing certs to pods does NOT replace SA token AuthN to the apiserver. The mesh handles east-west; SA tokens handle north (pod → apiserver).
13. **Confidential workload uses a shared SA.** One per workload; share never. Two pods sharing an SA share an identity, share blame, share blast radius.
14. **`system:masters` granted to a human.** This bypasses RBAC entirely. Even if a tool wants this for "break glass", lock it behind very specific access controls (HSM-protected cert, time-bound issuance).
15. **CSR auto-approver too lax.** A custom CSR approver that approves any CSR matching `system:bootstrappers` is a path to forging node identity. Verify the CN/SAN against expected forms.
16. **Token rotation not tested.** Workload SDKs assume they can re-read the token file. Some legacy libraries cache the token on first read and never re-read; rotation silently fails. Test by killing a workload mid-rotation and verifying it recovers.
17. **External secret provider trust without scoping.** Vault role bound to "any pod with audience=vault" → any pod in the cluster can ask Vault for anything that role allows. Always scope Vault roles to specific SAs.
18. **`escalate` granted too liberally.** Operators that manage Roles often need `escalate`, but it bypasses the escalation guard and lets the holder write any rule. Treat as cluster-admin equivalent.
19. **`bind` granted too liberally.** Same as escalate but for RoleBindings.
20. **Audit log retention too short.** Forensics need 90 days+ minimum. Shipping audit to a SIEM with proper retention.
21. **Audit policy that filters by `users:` and forgets impersonation.** A rule "don't log argocd-server" hides every action that argocd-server takes while impersonating users. Audit on `impersonatedUser` too.
22. **The cluster's OIDC issuer URL not stable.** If you rotate it without re-establishing cloud federation, every workload identity breaks at once.
23. **Multiple authentication methods accepted for the same identity.** A user has both x509 cert and OIDC; the cert grants cluster-admin (`system:masters`), the OIDC is least-privilege. They forget about the cert. Audit and remove unused credentials.
24. **`kubectl exec` without separate RBAC.** `pods/exec` is a remote shell as the pod's SA. Don't grant it to `edit`. Make a separate `debugger` role and bind sparingly.

---

## 35. TL;DR

Identity is the **first** thing the apiserver decides about a request and the **last** thing every other component cares about. The system has three layers:

1. **Authenticator chain** — credential to `user.Info{Name, UID, Groups, Extra}`. x509 first, then SA token, then OIDC, then webhook, then bootstrap, then static (deprecated), then anonymous (last). First success wins. The credential type is forgotten after this stage.
2. **Authorizer chain** — `(user, verb, resource, ns)` to allow/deny/no-opinion. Node (kubelet special case), RBAC (additive, never denies), then optional Webhook, then default deny. First allow wins; explicit deny stops.
3. **Federated identity outside the cluster** — projected SA tokens with custom audiences, verified by external relying parties (AWS STS, GCP STS, Azure AD, Vault, SPIFFE peers) against the cluster's public JWKS. The audience claim is the discriminator; the issuer URL is the trust root.

**Service account tokens are the universal currency.** Pre-1.24 they were long-lived Secret-backed JWTs that did not expire and were the #1 cluster compromise vector. Post-1.24 they are projected (mint via TokenRequest API), bound (to pod + SA + node UIDs), audience-restricted, and short-lived (default 1h, kubelet refresh at 80% TTL). The legacy pattern is dead; auto-generation of Secret tokens is removed; you can still create them manually but auditors should ask why.

**RBAC is four objects (Role, ClusterRole, RoleBinding, ClusterRoleBinding) evaluated as a graph walk over (user × groups × SAs) → bindings → roles → rules.** A rule matches `(verb × resource × name × apiGroup)`. RBAC never returns deny — it grants or shrugs. Wildcards (`*`) are the single largest source of unintended escalation. `escalate` and `bind` are the guards that prevent self-promotion; treat them as cluster-admin. The `system:masters` group bypasses RBAC entirely; reserved for break-glass only.

**The Node authorizer is RBAC's special-case sibling for kubelets**, granting "read what's mounted by my pods, write my own status, no more." NodeRestriction admission completes the picture, preventing kubelets from labeling other nodes or stealing scheduling. Both must be enabled together.

**Workload identity in clouds is the same pattern, different audience:**
- **AWS IRSA**: audience `sts.amazonaws.com`, exchanged at STS for an IAM role via `AssumeRoleWithWebIdentity`. The IAM role trusts the cluster's OIDC issuer; the trust policy pins `sub` to a specific SA.
- **GKE Workload Identity**: gke-metadata-server DaemonSet intercepts metadata requests, exchanges the projected SA token at GCP STS for an access token, optionally via a GSA bound by `roles/iam.workloadIdentityUser`.
- **Azure AD Workload Identity**: audience `api://AzureADTokenExchange`, federated to an Entra app via the app's `federatedCredentials` trust statement. Webhook injects env vars and the projected volume.
- **SPIFFE/SPIRE**: the open-standard generalization; X509-SVIDs and JWT-SVIDs with a `spiffe://trust-domain/path` identity. Federates across clusters and clouds without per-vendor code.

**The receiving side must always validate the audience.** Sig + iss + exp are not enough; missing `aud` check is a confused-deputy bug that has shipped in real systems. Lint it.

**Impersonation is "be them"**, gated by the `impersonate` verb on `users`, `groups`, `uids`, `userextras/<key>`. Always restrict via `resourceNames`. Audit records both real and impersonated users. Used legitimately by `kubectl --as`, dashboards, and GitOps engines acting on behalf of humans.

**Certificate-based identity** flows through `certificates.k8s.io` (CSR) signed by an in-cluster signer. The bootstrap token → CSR → x509 cert → `system:node:<name>` flow is how kubelets transition from temporary to long-lived credentials. Kubelet's cert manager handles rotation transparently.

**Encryption at rest** (EncryptionConfiguration + KMS v2) closes the etcd attack surface but does not substitute for RBAC or runtime hygiene. Pods see plaintext secrets regardless.

**The threat model** is small and well-known: long-lived SA tokens leaking from pods; default SA used everywhere with too much RBAC; wildcard impersonate; OIDC group spoofing; static tokens in CI; audience claim ignored; NodeRestriction off. The defenses are equally well-known: per-workload SAs, restricted PodSecurity, short-TTL projected tokens, OIDC required claims, no wildcard rules, NodeRestriction always on, audit-log everything that touches identity.

If you remember one sentence from this chapter: **Identity in Kubernetes is a JWT (or x509 cert) whose claims fall into a `user.Info` struct, whose groups drive RBAC via a graph walk, whose audience claim drives federation outward to clouds and meshes — and every place that audience is unchecked, every place that token is long-lived, every place that role is wildcard, is where your cluster gets breached.**

The next chapter (08) is controllers and client-go — once you have an authenticated, authorized request, the controller pattern is what *acts* on it. Beyond that, chapter 27 picks up the supply-chain side of identity (image signing, attestations) and chapter 28 picks up the runtime side (policy engines, PSA, eBPF detection). Identity is the lens; the rest of the stack is the apparatus.
