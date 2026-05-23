# Admission Control: Webhooks, CEL Policies, and the #1 Source of Cluster Outages

Admission control is the slice of the apiserver request pipeline that runs *after* a request has been authenticated and authorized but *before* the object is persisted to etcd. It is the single most powerful extension point in Kubernetes — every defaulter, every sidecar injector, every "no `:latest` tag" policy, every image-signature gate, every multi-tenancy guardrail lives here — and it is also, empirically, the most common cause of total cluster write outages. The pattern is always the same: a webhook with `failurePolicy: Fail` becomes unreachable (cert expired, pod evicted, network blip, controller crash), every write across the whole cluster blocks, and the apiserver cannot even delete its own coordination leases to elect new leaders.

This chapter is a staff-level tour of that surface. We walk the in-process plugin chain, the AdmissionReview wire protocol, the three webhook types (mutating, validating, conversion), the in-process CEL successors (ValidatingAdmissionPolicy GA in 1.30, MutatingAdmissionPolicy in beta), and the policy engines that build on top — Pod Security Admission, Kyverno, and OPA Gatekeeper. We end with operational metrics, alerting, and a long pitfalls section drawn from real outages.

Chapter 05 covered the full apiserver request pipeline; this chapter zooms into one stage of it. Chapter 07 covers what runs *before* admission (AuthN/AuthZ). Chapter 23 covers conversion webhooks from the CRD author's side. Chapter 28 covers runtime security, where policy engines extend their reach beyond admission-time gating.

---

## Table of Contents

1. [Where Admission Fits in the Apiserver Pipeline](#1-where-admission-fits-in-the-apiserver-pipeline)
2. [Built-in Admission Plugins (In-Tree Controllers)](#2-built-in-admission-plugins-in-tree-controllers)
3. [The AdmissionReview v1 Wire Protocol](#3-the-admissionreview-v1-wire-protocol)
4. [MutatingWebhookConfiguration: Every Field, Every Trap](#4-mutatingwebhookconfiguration-every-field-every-trap)
5. [ValidatingWebhookConfiguration: The Read-Only Sibling](#5-validatingwebhookconfiguration-the-read-only-sibling)
6. [Ordering, Reinvocation, and Why Webhooks Are Not Idempotent for Free](#6-ordering-reinvocation-and-why-webhooks-are-not-idempotent-for-free)
7. [JSON Patch vs JSON Merge Patch vs Strategic Merge Patch](#7-json-patch-vs-json-merge-patch-vs-strategic-merge-patch)
8. [Building a Webhook Server in Go](#8-building-a-webhook-server-in-go)
9. [Building a Webhook Server in Python (and Why Language Does Not Matter)](#9-building-a-webhook-server-in-python-and-why-language-does-not-matter)
10. [TLS, caBundle, and Certificate Rotation Without Outages](#10-tls-cabundle-and-certificate-rotation-without-outages)
11. [Failure Modes: The Classic Cluster-Wide Wedge](#11-failure-modes-the-classic-cluster-wide-wedge)
12. [Conversion Webhooks (Forward Reference to CRDs)](#12-conversion-webhooks-forward-reference-to-crds)
13. [ValidatingAdmissionPolicy: In-Process CEL, Zero RTT](#13-validatingadmissionpolicy-in-process-cel-zero-rtt)
14. [MutatingAdmissionPolicy: CEL-Driven Patches](#14-mutatingadmissionpolicy-cel-driven-patches)
15. [The CEL Cost Budget and Writing Cheap Expressions](#15-the-cel-cost-budget-and-writing-cheap-expressions)
16. [Kyverno: Declarative YAML Policy](#16-kyverno-declarative-yaml-policy)
17. [OPA Gatekeeper: Rego, ConstraintTemplates, and the Audit Controller](#17-opa-gatekeeper-rego-constrainttemplates-and-the-audit-controller)
18. [Pod Security Admission: The PSP Successor](#18-pod-security-admission-the-psp-successor)
19. [Real Policy Examples: Webhook, CEL, Kyverno, and Gatekeeper Side by Side](#19-real-policy-examples-webhook-cel-kyverno-and-gatekeeper-side-by-side)
20. [Operational Metrics and Alerts](#20-operational-metrics-and-alerts)
21. [Pitfalls: A Catalog From Real Outages](#21-pitfalls-a-catalog-from-real-outages)
22. [TL;DR](#22-tldr)

---

## 1. Where Admission Fits in the Apiserver Pipeline

Chapter 05 laid out the full apiserver request chain. Admission is a single stage of that pipeline, but it actually consists of *two* separate phases (mutating and validating) sitting on opposite sides of schema validation, plus a third pseudo-phase (conversion) that fires whenever a read/write crosses API versions. Diagrammatically:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  kube-apiserver request pipeline                                             │
│                                                                              │
│   HTTP/2 + TLS                                                               │
│      │                                                                       │
│      ▼                                                                       │
│   ┌──────────────┐                                                           │
│   │ AuthN        │   x509 / OIDC / SA token / webhook                  ch 07 │
│   └──────┬───────┘                                                           │
│          ▼                                                                   │
│   ┌──────────────┐                                                           │
│   │ AuthZ        │   RBAC / ABAC / Node / Webhook                      ch 07 │
│   └──────┬───────┘                                                           │
│          ▼                                                                   │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ Decoding + version conversion (external → internal)              │ ch 05 │
│   │   - protobuf or JSON decode                                      │       │
│   │   - convert to internal hub version                              │       │
│   │   - dry-run flag captured here                                   │       │
│   └──────────────────────────────────┬───────────────────────────────┘       │
│                                      ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ MUTATING ADMISSION                                          ch 06 │      │
│   │   1. In-tree mutators (defaulters, ServiceAccount, ...)          │       │
│   │   2. ValidatingAdmissionPolicy match-conditions evaluated first   │       │
│   │      for short-circuit (1.30+)                                   │       │
│   │   3. MutatingAdmissionWebhook plugin                              │       │
│   │        - iterate MutatingWebhookConfigurations                    │       │
│   │        - alphabetical name order, sequential                      │       │
│   │        - reinvoke if needed (one pass max)                        │       │
│   │   4. MutatingAdmissionPolicy (beta, 1.32+)                        │       │
│   └──────────────────────────────────┬───────────────────────────────┘       │
│                                      ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ SCHEMA + CEL VALIDATION                                          │       │
│   │   - OpenAPI v3 schema validation                                  │       │
│   │   - x-kubernetes-validations (CEL, embedded in CRD schema)        │       │
│   │   - structural schema checks                                      │       │
│   └──────────────────────────────────┬───────────────────────────────┘       │
│                                      ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ VALIDATING ADMISSION                                        ch 06 │      │
│   │   1. In-tree validators (ResourceQuota, PodSecurity, ...)         │       │
│   │   2. ValidatingAdmissionWebhook plugin (parallel calls)           │       │
│   │   3. ValidatingAdmissionPolicy (in-process CEL, parallel)         │       │
│   └──────────────────────────────────┬───────────────────────────────┘       │
│                                      ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ STORAGE CONVERSION (internal → storage version)            ch 05 │       │
│   │   - protobuf encode for etcd                                     │       │
│   │   - storage version is one specific version per resource         │       │
│   └──────────────────────────────────┬───────────────────────────────┘       │
│                                      ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ etcd transaction (compare-and-swap on resourceVersion)     ch 04 │       │
│   └──────────────────────────────────┬───────────────────────────────┘       │
│                                      ▼                                       │
│   ┌──────────────────────────────────────────────────────────────────┐       │
│   │ Watch fan-out                                              ch 05 │       │
│   └──────────────────────────────────────────────────────────────────┘       │
│                                                                              │
│  (On READ:)                                                                  │
│   etcd → decode storage version → convert internal → CONVERSION WEBHOOK      │
│         (if CRD with conversion: Webhook) → external version → respond       │
└──────────────────────────────────────────────────────────────────────────────┘
```

The shape that matters: **mutating runs first, sequentially; then schema/CEL validates the shape; then validating runs in parallel; then storage**. Mutating webhooks see the object *before* schema validation, which is why a buggy mutator can produce a payload that fails validation and rejects the entire request (a confusing class of bug — the user sees a validation error but the validator they wrote did not produce it).

Two further pieces of context the chapter will keep returning to:

- **Dry-run requests** (`?dryRun=All`) traverse the entire pipeline up to but not including the etcd write. Webhooks are passed `dryRun: true` in the AdmissionRequest; well-behaved webhooks must skip any external side effects (writing to a database, calling a cloud API) when `dryRun` is true, but still mutate/validate as if real. This is enforced by the `sideEffects` field on the webhook config (more in §4).
- **Subresources** (`status`, `scale`, `eviction`, `attach`, `exec`, etc.) are routed through admission independently. A common bug is matching on `pods` only and not realizing `pods/eviction` (used by `kubectl drain`) bypasses your check.

Source layout for the apiserver-side machinery (paths are in the `kubernetes/kubernetes` repo):

```
staging/src/k8s.io/apiserver/pkg/admission/                  ← framework
  ├── interfaces.go              ← Interface, MutationInterface, ValidationInterface
  ├── chain.go                   ← chainAdmissionHandler: iterate plugins
  ├── attributes.go              ← Attributes struct passed to each plugin
  ├── plugin.go                  ← Factory, Plugins registry
  ├── plugins.go                 ← well-known plugin names
  ├── config/                    ← AdmissionConfiguration file parsing
  ├── initializer/               ← inject ClientSet, Informers into plugins
  ├── metrics/                   ← Prometheus metrics emitted from chain
  └── plugin/                    ← in-tree plugin implementations
      └── webhook/               ← the mutating/validating webhook plugins
          ├── mutating/
          ├── validating/
          ├── config/            ← AdmissionWebhookConfiguration discovery
          ├── generic/           ← shared webhook dispatch code
          ├── request/           ← AdmissionReview marshalling
          └── rules/             ← rule matching (operations, GVR, scope)

plugin/pkg/admission/                                        ← in-tree controllers
  ├── namespace/                 ← NamespaceLifecycle, NamespaceExists, NamespaceAutoProvision
  ├── limitranger/               ← LimitRanger
  ├── serviceaccount/            ← ServiceAccount
  ├── storage/                   ← DefaultStorageClass, PersistentVolumeClaimResize, PersistentVolumeLabel
  ├── resourcequota/             ← ResourceQuota
  ├── priority/                  ← Priority
  ├── security/                  ← PodSecurity, PodNodeSelector, AlwaysPullImages, ...
  ├── noderestriction/           ← NodeRestriction
  ├── eventratelimit/            ← EventRateLimit
  └── ...
```

If you take one structural fact from this section: **admission is a chain, not a tree**. The chain is built once at apiserver startup from the `--enable-admission-plugins` / `--disable-admission-plugins` flags plus the webhook plugins (which are themselves chain entries that fan out internally). Every plugin sees the attributes object in order, and any non-nil error from any plugin aborts the request.

---

## 2. Built-in Admission Plugins (In-Tree Controllers)

Before any webhook configuration matters, the apiserver runs a stack of *in-tree* admission controllers. These are compiled into the binary and live in `plugin/pkg/admission/`. They are enabled by name via `--enable-admission-plugins` (and the special `--admission-control-config-file` for per-plugin configuration). Many are on-by-default; the canonical default set is `DefaultAdmissionControl` defined in the apiserver options.

A plugin can implement `MutationInterface` (`Admit()`), `ValidationInterface` (`Validate()`), or both. The framework will call whichever applies during each phase.

| Plugin | Phase | Default? | What it does |
|---|---|---|---|
| **NamespaceLifecycle** | V | yes | Forbids creation of objects in non-existent / terminating namespaces; protects `kube-system` and `default` from deletion |
| **NamespaceExists** | V | (subsumed by Lifecycle) | Older name; reject if namespace does not exist |
| **NamespaceAutoProvision** | M | no | Auto-create namespace on first object creation in it (dev convenience, off in prod) |
| **LimitRanger** | M+V | yes | Apply defaults from `LimitRange` (request/limit); reject violators |
| **ServiceAccount** | M+V | yes | If `pod.spec.serviceAccountName==""`, set to `default`; auto-mount projected SA token volume; validate referenced SA exists |
| **DefaultStorageClass** | M | yes | If `PVC.spec.storageClassName==nil`, assign the default `StorageClass` (the one with `storageclass.kubernetes.io/is-default-class: "true"`) |
| **DefaultIngressClass** | M | yes | Same idea for `Ingress` and `IngressClass` |
| **PersistentVolumeClaimResize** | V | yes | Allow / deny PVC `spec.resources.requests.storage` changes based on whether the `StorageClass` has `allowVolumeExpansion: true` |
| **PersistentVolumeLabel** | M | (deprecated, cloud-controller-manager owns this now) | Labels PVs with zone/region |
| **ResourceQuota** | V | yes | Enforce `ResourceQuota` objects; charges quota against the namespace's running totals; uses optimistic concurrency to handle races |
| **Priority** | M+V | yes | Resolve `pod.spec.priorityClassName` into `pod.spec.priority` (integer); reject if class missing or `system-*` used by non-system tenant |
| **PodSecurity** | V | yes (1.25+) | Enforce Pod Security Standards (privileged / baseline / restricted) per namespace labels; replaces deprecated `PodSecurityPolicy` |
| **NodeRestriction** | V | yes | Restrict what a kubelet can do: a node can only modify its own `Node` object, can only write `Pod` status for pods bound to it, cannot delete other nodes, etc. Critical with the Node authorizer |
| **TaintNodesByCondition** | M | yes | Auto-taint nodes based on `Node` conditions (`NotReady`, `MemoryPressure`, etc.) |
| **AlwaysPullImages** | M | no | Rewrite `imagePullPolicy` to `Always` for every container — useful in multi-tenant clusters to prevent image cache abuse |
| **AlwaysDeny** / **AlwaysAllow** | V | no | Test plugins; `AlwaysAllow` is the default-for-tests and a footgun in prod |
| **EventRateLimit** | V | no | Limit `Event` creation rate per source to protect etcd from event storms |
| **ExtendedResourceToleration** | M | no | Add tolerations for extended-resource taints (GPU nodes etc.) |
| **OwnerReferencesPermissionEnforcement** | V | no | Require that the user setting `metadata.ownerReferences` has delete permission on the owner — prevents privilege escalation via owner cascade |
| **PodTopologySpread** | M | yes | Apply cluster-level default topology-spread constraints from the scheduler config |
| **PodNodeSelector** | V | no | Enforce per-namespace allowed `nodeSelector` (annotated on the namespace) |
| **PodTolerationRestriction** | M+V | no | Enforce per-namespace allowed tolerations |
| **CertificateApproval / CertificateSigning / CertificateSubjectRestriction** | V | yes | Govern `CertificateSigningRequest` lifecycle and what subject fields are allowed |
| **RuntimeClass** | M+V | yes | Validate `pod.spec.runtimeClassName` references an existing `RuntimeClass`; apply its overhead and scheduling constraints |
| **TaintNodesByCondition** | M | yes | Translate node conditions into taints |
| **MutatingAdmissionWebhook** | M | yes | The plugin that dispatches to external webhook servers — covered in §4 |
| **ValidatingAdmissionWebhook** | V | yes | Same for validating |
| **ValidatingAdmissionPolicy** | V | yes (1.30+) | The in-process CEL plugin — §13 |

Two operational rules about this list:

1. **Order matters and is fixed.** The chain order is *not* the order of `--enable-admission-plugins`; it is hard-coded in `pkg/kubeapiserver/options/plugins.go`. Webhook plugins always run *after* most in-tree plugins, which is why a webhook will see the result of `ServiceAccount` injection and `LimitRanger` defaulting. The fixed order is part of the API contract.
2. **You almost never disable these.** The defaults are minimal and dropping one (e.g., `NamespaceLifecycle`) breaks invariants that other components assume (a `Pod` in a terminating namespace is a guaranteed lifecycle bug). Operators who think they want to disable an in-tree plugin almost always want a webhook *on top*, not a removal.

A concrete example of how `ServiceAccount` interacts with `MutatingAdmissionWebhook`: `ServiceAccount` runs first and injects the projected token volume, then any sidecar-injector webhook runs and sees a pod that already has volumes. If the webhook reorders or strips volumes, the in-tree behavior is silently undone — that is the source of many sidecar-injector bugs.

---

## 3. The AdmissionReview v1 Wire Protocol

External webhooks (mutating, validating, and conversion) all speak the same envelope: an `AdmissionReview` object with `apiVersion: admission.k8s.io/v1`, `kind: AdmissionReview`. The apiserver sends one with a `request` field; the webhook responds with the same object populated with a `response` field. Both ends echo the `uid` so the apiserver can correlate the response with the in-flight request (important because the apiserver may have many requests in flight to the same webhook).

The Go types live in `staging/src/k8s.io/api/admission/v1/types.go`.

### 3.1 AdmissionRequest

```json
{
  "apiVersion": "admission.k8s.io/v1",
  "kind": "AdmissionReview",
  "request": {
    "uid": "705ab4f5-6393-11e8-b7cc-42010a800002",
    "kind": {"group": "apps", "version": "v1", "kind": "Deployment"},
    "resource": {"group": "apps", "version": "v1", "resource": "deployments"},
    "subResource": "",
    "requestKind": {"group": "apps", "version": "v1", "kind": "Deployment"},
    "requestResource": {"group": "apps", "version": "v1", "resource": "deployments"},
    "name": "my-deployment",
    "namespace": "my-namespace",
    "operation": "UPDATE",
    "userInfo": {
      "username": "system:serviceaccount:argo-cd:argo-cd-application-controller",
      "uid": "014fbff9-a07c-4b70-9f55-3b3f4f43c6f6",
      "groups": ["system:serviceaccounts", "system:serviceaccounts:argo-cd", "system:authenticated"],
      "extra": {"authentication.kubernetes.io/pod-name": ["argo-cd-application-controller-0"]}
    },
    "object": { ... full new object ... },
    "oldObject": { ... previous object (UPDATE/DELETE only) ... },
    "dryRun": false,
    "options": {
      "apiVersion": "meta.k8s.io/v1",
      "kind": "UpdateOptions",
      "fieldManager": "argocd-controller"
    }
  }
}
```

Field meaning, beyond the obvious:

- **`uid`** — random per request, used to correlate the response. Webhooks MUST echo this.
- **`kind` vs `requestKind`** — `kind` is the kind the apiserver decoded the request into (which may differ from `requestKind` if `matchPolicy: Equivalent` is set and the request came in as a different GVK that the apiserver converted). `requestKind` is what the client actually sent. Webhooks that care about original intent (rare) consult `requestKind`.
- **`subResource`** — empty for the main resource; `"status"`, `"scale"`, `"eviction"`, etc. for subresources. *This is the single most-missed field in policy authoring.* A policy that matches on `pods` does not match on `pods/eviction`; if you want to control `kubectl drain` (which uses the `Eviction` API), you must add `pods/eviction` to your rules.
- **`operation`** — `CREATE`, `UPDATE`, `DELETE`, `CONNECT` (for `exec`, `attach`, `portforward`, `proxy`).
- **`userInfo`** — the authenticated identity, *as the apiserver saw it post-AuthN*. This is how a policy enforces "only the istio injector SA can mutate Pods to add the istio sidecar." Note `system:masters` (cluster admin via x509 group) bypasses RBAC but *not* admission; admission can still see who they are.
- **`object`** — the new object for CREATE/UPDATE; nil for DELETE.
- **`oldObject`** — the prior object for UPDATE/DELETE; nil for CREATE.
- **`dryRun`** — true when `?dryRun=All`. Webhooks must not side-effect when this is true. The `sideEffects` field in the webhook config declares the webhook's contract here.
- **`options`** — the `CreateOptions` / `UpdateOptions` / `DeleteOptions` object, including the `fieldManager` for server-side apply.

### 3.2 AdmissionResponse (allow)

```json
{
  "apiVersion": "admission.k8s.io/v1",
  "kind": "AdmissionReview",
  "response": {
    "uid": "705ab4f5-6393-11e8-b7cc-42010a800002",
    "allowed": true
  }
}
```

### 3.3 AdmissionResponse (allow + mutate)

A mutating webhook returns a base64-encoded JSON Patch (RFC 6902) in `patch`, with `patchType: JSONPatch`. (As of v1, only `JSONPatch` is supported as a patchType; this is a tightening from v1beta1, where `JSONPatchType` was technically configurable.)

```json
{
  "apiVersion": "admission.k8s.io/v1",
  "kind": "AdmissionReview",
  "response": {
    "uid": "705ab4f5-6393-11e8-b7cc-42010a800002",
    "allowed": true,
    "patchType": "JSONPatch",
    "patch": "W3sib3AiOiAiYWRkIiwgInBhdGgiOiAiL21ldGFkYXRhL2xhYmVscy9pbmplY3RlZCIsICJ2YWx1ZSI6ICJ0cnVlIn1d",
    "warnings": [
      "deprecated annotation 'foo' will be removed in v2"
    ]
  }
}
```

The decoded `patch` is `[{"op": "add", "path": "/metadata/labels/injected", "value": "true"}]`.

### 3.4 AdmissionResponse (deny)

```json
{
  "apiVersion": "admission.k8s.io/v1",
  "kind": "AdmissionReview",
  "response": {
    "uid": "705ab4f5-6393-11e8-b7cc-42010a800002",
    "allowed": false,
    "status": {
      "code": 403,
      "message": "container 'app' uses image tag ':latest', which is not allowed",
      "reason": "Forbidden",
      "details": {
        "group": "",
        "kind": "Pod",
        "causes": [{"field": "spec.containers[0].image", "message": "tag :latest forbidden"}]
      }
    }
  }
}
```

The `status` is a `metav1.Status` and is what the user sees in their `kubectl` error. Use it well — half the operational pain of webhook policy is messages that say `denied by policy` with no context. Include the field path, the actual offending value, and (ideally) a doc URL.

### 3.5 Warnings

Both mutating and validating responses may include a `warnings: []string` slice. These appear in the response headers (`Warning:` per RFC 7234) and `kubectl` prints them. Warnings are a critical UX tool for *graceful policy rollouts*: ship the policy as warnings for a week, watch the metrics, then flip to deny. Warnings are limited to 256 chars each and a total of 4KB across the response.

### 3.6 Conversion review

Conversion webhooks use the same envelope shape but with `kind: ConversionReview`. The request payload is an *array* of objects to convert (batched), and the response is the converted array. We cover this in §12.

---

## 4. MutatingWebhookConfiguration: Every Field, Every Trap

A webhook is registered by creating a `MutatingWebhookConfiguration` (cluster-scoped). The apiserver watches this resource and rebuilds its in-memory dispatch table when it changes. Here is the canonical fully-annotated YAML:

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: istio-sidecar-injector
webhooks:
- name: sidecar-injector.istio.io                 # MUST be FQDN-like, MUST be unique within this config
  admissionReviewVersions: ["v1"]                 # API versions this webhook understands; apiserver picks the latest both support
  sideEffects: None                               # None | NoneOnDryRun | Some | Unknown
                                                  #   None        = no side effects in any case (preferred)
                                                  #   NoneOnDryRun = side effects on real requests; respects dryRun
                                                  #   Some/Unknown = apiserver REFUSES dryRun requests
  failurePolicy: Fail                             # Fail | Ignore
                                                  #   Fail   = webhook unreachable / error => request denied
                                                  #   Ignore = webhook unreachable => request proceeds w/o webhook
  matchPolicy: Equivalent                         # Exact | Equivalent
                                                  #   Exact      = only match the GVR the client sent
                                                  #   Equivalent = also match if apiserver auto-converts to your GVR
  timeoutSeconds: 5                               # 1..30 (default 10). KEEP LOW. See §11.
  reinvocationPolicy: IfNeeded                    # Never | IfNeeded — see §6
  clientConfig:
    service:                                      # Either service or url, not both.
      namespace: istio-system
      name: istiod
      path: /inject
      port: 443
    caBundle: LS0tLS1CRUdJTi...                   # PEM cert chain that signs the webhook server cert
  rules:
  - operations: ["CREATE"]                        # CREATE | UPDATE | DELETE | CONNECT | *
    apiGroups:   [""]                             # core group is "". Use ["*"] for everything (rare, dangerous).
    apiVersions: ["v1"]
    resources:   ["pods"]                         # add "pods/eviction" if you also want eviction
    scope: "Namespaced"                           # Namespaced | Cluster | *
  namespaceSelector:                              # match the namespace's labels
    matchExpressions:
    - key: istio-injection
      operator: In
      values: ["enabled"]
    - key: kubernetes.io/metadata.name            # auto-set label since 1.22 - excludes kube-system
      operator: NotIn
      values: ["kube-system", "kube-public", "kube-node-lease"]
  objectSelector:                                 # match the OBJECT's labels (not namespace)
    matchExpressions:
    - key: sidecar.istio.io/inject
      operator: NotIn
      values: ["false"]
  matchConditions:                                # CEL early-filter, 1.27+, beta 1.28, stable 1.30
  - name: exclude-host-network
    expression: "!has(object.spec.hostNetwork) || object.spec.hostNetwork == false"
  - name: skip-system-sa
    expression: "!request.userInfo.username.startsWith('system:serviceaccount:kube-system:')"
```

Walking the surprising bits:

### 4.1 `name`

Two facts most teams miss:

- It must be FQDN-like (contain a dot). The apiserver validates this. A name like `sidecar-injector` (no dot) is rejected.
- The *order in which mutating webhooks run* is alphabetical by `name`. This is the simplest reproducible ordering rule the API designers could pick. If you have an order dependency between two of your webhooks, you express it by naming: `00-defaulter.acme.io`, `99-validator.acme.io`. This is fragile — prefer making each webhook order-independent (idempotent + commutative).

### 4.2 `clientConfig`: `url` vs `service`

There are exactly two ways to reach a webhook:

- **`url`** — an https URL accessible from the apiserver. Use this when the webhook runs *outside* the cluster (e.g., a hosted policy SaaS). The DNS resolution and routing happen via the apiserver's own resolv.conf — which on a typical cluster does *not* go through coredns or kube-proxy. Don't put a `Service` VIP here; use `service` instead.
- **`service`** — a Kubernetes `Service` reference (namespace + name + optional path/port). The apiserver uses an *internal* resolver that maps `(namespace, name)` to an endpoint IP, bypassing kube-dns. This is the production default. The cert returned by the service must validate against `caBundle` *for the Service DNS name*, i.e., the cert's SAN must include `<svc>.<ns>.svc`.

### 4.3 `caBundle`

The PEM-encoded CA(s) that sign the webhook server's TLS cert. The apiserver does *not* use the system trust store; it ignores `/etc/ssl/certs` for webhooks. You must provide the CA explicitly. This is the source of most webhook outages: cert rotation forgets to update the `caBundle`. See §10.

### 4.4 `rules`

Each rule is `(operations × apiGroups × apiVersions × resources × scope)`. The webhook fires if any rule matches. Two common mistakes:

- Forgetting to list subresources. `resources: ["pods"]` doesn't match `pods/eviction`, `pods/status`, `pods/exec`. If you want to also gate eviction, add `pods/eviction` explicitly.
- Using `apiGroups: ["*"]`. This matches *every* group including aggregated APIs (metrics.k8s.io, custom.metrics.k8s.io, extension APIs), which usually breaks them. Always be specific.

### 4.5 `namespaceSelector` and `objectSelector`

- **`namespaceSelector`** matches on the *containing namespace's labels*. Since 1.22 every namespace automatically has the label `kubernetes.io/metadata.name: <name>`, which is what you use to exclude `kube-system` reliably.
- **`objectSelector`** matches on the *object's own labels*. Useful for opt-in injection (`sidecar.istio.io/inject: "true"`).

Both selectors are evaluated *before* the webhook is called. They are the cheapest possible filter. Use them aggressively.

### 4.6 `failurePolicy`

`Fail` or `Ignore`. If `Fail`, an unreachable, timed-out, or erroring webhook denies the request. If `Ignore`, the apiserver logs the failure and proceeds as if the webhook returned allow.

**The decision tree, blunt edition:**

- Pod sidecar injection: `failurePolicy: Fail` — a pod that runs without its mandated sidecar is a security incident.
- Image policy / signature verification: `failurePolicy: Fail` — failing open here is a CVE.
- Audit-only / informational policy: `failurePolicy: Ignore` — and consider VAP (§13) with audit annotations instead.
- Policy that touches `kube-system`: `failurePolicy: Ignore` *or* exclude `kube-system` via `namespaceSelector`. Otherwise a wedged webhook makes the apiserver unable to renew its own leases. See §11.

### 4.7 `reinvocationPolicy`

`Never` (default) or `IfNeeded`. We explore the ordering implications in §6.

### 4.8 `sideEffects`

A contract you sign with the apiserver:

- **`None`** — webhook has no side effects. The apiserver may freely call it with `dryRun: true`. Strongly preferred.
- **`NoneOnDryRun`** — webhook *does* have side effects (writes to a database, calls a cloud API), but respects the `dryRun` flag in the request. Apiserver will pass dry-run requests; webhook must skip the side effect.
- **`Some`** / **`Unknown`** — webhook either has unrestricted side effects or doesn't know. The apiserver will *refuse* any dry-run request that would invoke this webhook. **This breaks `kubectl apply --dry-run=server` and ArgoCD's preview flows.** Avoid.

### 4.9 `timeoutSeconds`

1–30 seconds, default 10. Cluster-wide, mutating webhooks together cannot exceed `--mutating-admission-webhook-timeout` (default not set, falls through to per-webhook). Empirically: **keep this at 5 or below**. Anything beyond 5s exhausts APF (API Priority and Fairness) seats and produces cascading rejections. Webhooks that need more than 5s are misdesigned — fetch nothing synchronously, cache aggressively, or move the work out-of-band.

### 4.10 `matchPolicy`

- **`Exact`** — the webhook only sees requests whose GVR exactly matches a rule.
- **`Equivalent`** — the webhook also sees requests whose GVR is equivalent to a matched GVR (e.g., a v1beta1 request that is equivalent to your v1 rule after conversion). This is what you want for most policies; otherwise a client sending an old version sidesteps your check.

### 4.11 `admissionReviewVersions`

The webhook lists which `AdmissionReview` versions it understands. The apiserver picks the latest mutually supported. Today this should be `["v1"]`. `v1beta1` is removed as of 1.22; legacy webhooks declaring only `v1beta1` no longer work on supported releases.

### 4.12 `matchConditions` (1.27 beta, 1.30 stable)

CEL expressions evaluated *before* the webhook is called. If any expression is `false`, the webhook is skipped (request proceeds). This is huge: every early-filter you can express in CEL saves a network round trip. The CEL variables here mirror VAP (§13): `object`, `oldObject`, `request`, `authorizer`, `namespaceObject`.

```yaml
matchConditions:
- name: exclude-system-namespaces
  expression: |
    !(request.namespace in ['kube-system', 'kube-public', 'kube-node-lease'])
- name: ignore-controller-updates
  expression: |
    request.userInfo.username != 'system:serviceaccount:kube-system:replicaset-controller'
```

The classic case: a mutating webhook that injects a sidecar must not modify pods owned by replicaset/job controllers re-creating an already-injected pod. Express that as a `matchCondition` so the webhook isn't even called.

### 4.13 The full apiserver request to your webhook

When all of `rules`, `namespaceSelector`, `objectSelector`, and `matchConditions` pass, the apiserver POSTs the AdmissionReview JSON to `https://<service>.<ns>.svc:<port><path>`. The request is authenticated using a webhook authenticator config (`--admission-webhook-config-file`) — typically a kubeconfig with the apiserver's serving cert as the client. If you want your webhook to verify caller identity, you read it from that mTLS client cert (or trust the network).

---

## 5. ValidatingWebhookConfiguration: The Read-Only Sibling

`ValidatingWebhookConfiguration` is structurally identical to `MutatingWebhookConfiguration` *minus* `reinvocationPolicy`. The webhook receives the same `AdmissionRequest`, returns the same `AdmissionResponse`, but:

- The response MUST NOT include `patch` / `patchType`. The apiserver ignores these if present.
- Validating webhooks may return `warnings`.
- All validating webhooks run **in parallel** after the mutation phase is complete. Their `allowed` results are AND-ed: if any returns false, the request is denied.

Example: deny `:latest` tag.

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: deny-latest-tag
webhooks:
- name: no-latest-tag.policy.acme.io
  admissionReviewVersions: ["v1"]
  sideEffects: None
  failurePolicy: Fail
  timeoutSeconds: 3
  clientConfig:
    service: {namespace: policy-system, name: image-policy, path: /validate}
    caBundle: ...
  rules:
  - operations: ["CREATE", "UPDATE"]
    apiGroups: [""]
    apiVersions: ["v1"]
    resources: ["pods"]
  namespaceSelector:
    matchExpressions:
    - {key: kubernetes.io/metadata.name, operator: NotIn, values: [kube-system, kube-public]}
```

Because validating webhooks run in parallel, the total latency added by N validating webhooks is `max(latencies)` rather than `sum`. (Mutating, by contrast, is `sum`.)

---

## 6. Ordering, Reinvocation, and Why Webhooks Are Not Idempotent for Free

The single trickiest semantic in admission webhooks is ordering. The apiserver guarantees:

1. **In-tree mutating plugins run first**, in a fixed compiled order. (`ServiceAccount` injects SA tokens *before* any external mutator sees the pod.)
2. **External mutating webhooks run sequentially in alphabetical order of webhook `name`**. Each webhook sees the cumulative effect of all earlier mutations.
3. After all mutators have run once, the apiserver checks if any webhook's `reinvocationPolicy: IfNeeded` should fire. A webhook is re-invoked if a *later* mutator modified the object in a way that might cause the earlier webhook to make different decisions. **At most one re-invocation pass** is performed, even if that pass triggers more changes. This is the cycle-prevention mechanism.
4. Schema + CEL validation runs once on the final mutated object.
5. **Validating webhooks run in parallel.** Order is unobservable.

The reinvocation rule deserves a picture. Suppose three mutating webhooks `A`, `B`, `C` are registered, with `A` and `B` having `reinvocationPolicy: IfNeeded`:

```
Round 1 (sequential, alphabetical):

  Object v0 ──> A.Admit ──> Object v1
  Object v1 ──> B.Admit ──> Object v2
  Object v2 ──> C.Admit ──> Object v3

After round 1, the apiserver compares v3 to what each webhook saw:
  - A saw v0, output v1. Final is v3 — v3 differs from v1.
    A has IfNeeded → schedule A for round 2.
  - B saw v1, output v2. Final is v3 — v3 differs from v2.
    B has IfNeeded → schedule B for round 2.
  - C saw v2, output v3. Final is v3. No change → no re-invoke.

Round 2 (sequential, alphabetical, only those scheduled):

  Object v3 ──> A.Admit ──> Object v4
  Object v4 ──> B.Admit ──> Object v5

After round 2, NO further re-invocation, even if v5 still differs.
```

This caps the total work at `2 * N` calls to mutating webhooks (where `N` is the number of webhooks with `IfNeeded`). The cost: **your `IfNeeded` webhook may be called twice on the same object during a single request**. It MUST be idempotent — calling it on its own output must be a no-op. Otherwise you double-inject the sidecar.

Idempotency in practice:

```go
// BAD: appends every time. After round 2, you have two sidecars.
pod.Spec.Containers = append(pod.Spec.Containers, sidecar)

// GOOD: check first.
already := false
for _, c := range pod.Spec.Containers {
    if c.Name == "istio-proxy" { already = true; break }
}
if !already {
    pod.Spec.Containers = append(pod.Spec.Containers, sidecar)
}
```

A cleaner pattern: emit a marker label (`sidecar.istio.io/status: injected`) at the same time as the mutation, then check that label up front.

### 6.1 When to use `reinvocationPolicy: IfNeeded`

The canonical case is **defaulting in the presence of other mutators**. Webhook `A` injects a sidecar; webhook `B` (alphabetically later) adds a security context. After `B` runs, `A` may need to add the security context to the sidecar it injected. `IfNeeded` makes that work without forcing a particular ordering.

If your webhook only mutates fields no one else touches, leave `reinvocationPolicy` at the default (`Never`) — it's cheaper.

### 6.2 The hidden ordering bug: across configurations

There can be multiple `MutatingWebhookConfiguration` objects. Within a single configuration, webhooks run alphabetically by `name`. Across configurations, all webhooks from all configurations are flattened and sorted alphabetically — the *configuration* name does not affect ordering. So `kyverno.io/A` and `istio.io/B` interleave based on the webhook name, not the configuration name.

### 6.3 Why validating webhooks run in parallel

There is no semantic dependency between validators — they all return allow/deny on the *same* object. Running them sequentially would be slower without giving anything new. The apiserver dispatches them concurrently and collects results. This means a slow validator does not delay other validators; only the slowest determines the request's latency.

---

## 7. JSON Patch vs JSON Merge Patch vs Strategic Merge Patch

A mutating webhook returns mutations as **JSON Patch (RFC 6902)** only — the `patchType` field is `JSONPatch` and nothing else is supported by v1. This is despite the fact that the rest of the Kubernetes API uses *three* different patch formats for `kubectl patch` and server-side apply. Let's untangle that.

### 7.1 The three patch formats in Kubernetes

| Format | Spec | Used by |
|---|---|---|
| **JSON Patch** | RFC 6902 | Mutating webhooks (only) |
| **JSON Merge Patch** | RFC 7396 | `kubectl patch --type=merge`; some controllers |
| **Strategic Merge Patch** | Kubernetes-specific extension | `kubectl patch --type=strategic` (default); kubelet patching pod status |

**JSON Patch** is an array of operations:

```json
[
  {"op": "add",    "path": "/metadata/labels/injected", "value": "true"},
  {"op": "remove", "path": "/spec/containers/0/imagePullPolicy"},
  {"op": "replace","path": "/spec/replicas", "value": 5},
  {"op": "test",   "path": "/spec/replicas", "value": 5}
]
```

Operations: `add`, `remove`, `replace`, `move`, `copy`, `test`. Paths are JSON Pointer (RFC 6901): `/` separated, with `~0` for `~` and `~1` for `/`. Array indices are numeric; `-` means "append to the end".

**JSON Merge Patch** is just a partial object that overlays the original:

```json
{"metadata": {"labels": {"injected": "true"}}, "spec": {"replicas": 5}}
```

Simple but has a key problem: it cannot express list operations beyond *replace the whole list*. Setting `containers: [{name: foo}]` *replaces* the entire container list with one element — likely not what you want.

**Strategic Merge Patch** fixes JSON Merge Patch's list problem by attaching merge metadata to the schema. For each list field, the schema declares a *merge key* (usually `name`):

```go
type PodSpec struct {
    // +patchMergeKey=name
    // +patchStrategy=merge
    Containers []Container `json:"containers"`
    ...
}
```

That tag means a Strategic Merge Patch on `containers` merges by `name`: `[{name: foo, image: x}]` merges into an existing pod by finding the container named `foo` and updating its image, rather than replacing all containers. This is what makes `kubectl patch` "do the right thing" by default.

### 7.2 Why webhooks use JSON Patch only

Strategic Merge Patch requires the patch consumer to know the resource's *schema with merge tags*. The apiserver knows its own resources, but a webhook returning a patch in strategic-merge form against, say, a CRD whose schema the apiserver doesn't have annotated would be ambiguous. JSON Patch is schema-free: its meaning is fully determined by the patch document and the original object. Webhooks therefore stick to JSON Patch — explicit, unambiguous, and works against any resource.

### 7.3 Generating JSON Patches

Most webhook libraries do not have you write JSON Patches by hand. The common pattern: deep-copy the input object, mutate the copy as a normal Go struct, then diff:

```go
import "gomodules.xyz/jsonpatch/v3"

patch, err := jsonpatch.CreatePatch(originalBytes, mutatedBytes)
// patch is []jsonpatch.Operation, marshal to JSON, base64-encode, set as response.Patch.
```

There are two patch-generation pitfalls:

1. **Default values appear as changes.** If your Go deserialization fills in defaults (`imagePullPolicy: IfNotPresent`), and the original JSON didn't have that field, the diff will include a spurious `add` operation for it. Either keep the original JSON exactly or be careful about which fields you touch.
2. **Map ordering is non-deterministic.** Two semantically equal JSON objects can diff if you re-serialize. Use a canonical JSON serializer when diffing, or do the diff at the Go-struct level instead.

### 7.4 An end-to-end mutation example

Inject a sidecar into a Pod. JSON Patch returned by the webhook:

```json
[
  {
    "op": "add",
    "path": "/spec/containers/-",
    "value": {
      "name": "istio-proxy",
      "image": "docker.io/istio/proxyv2:1.21.0",
      "args": ["proxy", "sidecar"],
      "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}},
      "securityContext": {"runAsUser": 1337, "runAsGroup": 1337}
    }
  },
  {
    "op": "add",
    "path": "/spec/initContainers",
    "value": [
      {
        "name": "istio-init",
        "image": "docker.io/istio/proxyv2:1.21.0",
        "args": ["istio-iptables", "-p", "15001"],
        "securityContext": {"runAsUser": 0, "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]}}
      }
    ]
  },
  {
    "op": "add",
    "path": "/metadata/labels",
    "value": {"sidecar.istio.io/injected": "true"}
  }
]
```

Note `path: /spec/containers/-` (the `-` means append). And note that we use `op: add` for `initContainers` because the original pod likely has no `initContainers` field at all — `op: replace` against a missing field is an error in JSON Patch.

---

## 8. Building a Webhook Server in Go

Minimum viable webhook server, using only `net/http` and the official `admission/v1` types. This is roughly 80 lines of code, intentionally without `controller-runtime` so the wire shape is visible.

```go
package main

import (
    "encoding/json"
    "io"
    "log"
    "net/http"

    admissionv1 "k8s.io/api/admission/v1"
    corev1 "k8s.io/api/core/v1"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/apimachinery/pkg/runtime"
    "k8s.io/apimachinery/pkg/runtime/serializer"
)

var (
    scheme  = runtime.NewScheme()
    codecs  = serializer.NewCodecFactory(scheme)
    decoder runtime.Decoder
)

func init() {
    _ = admissionv1.AddToScheme(scheme)
    _ = corev1.AddToScheme(scheme)
    decoder = codecs.UniversalDeserializer()
}

func main() {
    http.HandleFunc("/mutate", handleMutate)
    log.Fatal(http.ListenAndServeTLS(":8443",
        "/etc/webhook/tls/tls.crt", "/etc/webhook/tls/tls.key", nil))
}

func handleMutate(w http.ResponseWriter, r *http.Request) {
    body, err := io.ReadAll(r.Body)
    if err != nil { http.Error(w, err.Error(), 400); return }

    var review admissionv1.AdmissionReview
    if _, _, err := decoder.Decode(body, nil, &review); err != nil {
        http.Error(w, err.Error(), 400); return
    }

    req := review.Request
    resp := &admissionv1.AdmissionResponse{UID: req.UID, Allowed: true}

    if req.Kind.Kind == "Pod" {
        var pod corev1.Pod
        if err := json.Unmarshal(req.Object.Raw, &pod); err != nil {
            resp.Allowed = false
            resp.Result = &metav1.Status{Message: err.Error()}
        } else {
            // already-injected? short-circuit (idempotency).
            if pod.Labels["sidecar.acme.io/injected"] != "true" {
                patch := []byte(`[
                  {"op":"add","path":"/metadata/labels/sidecar.acme.io~1injected","value":"true"},
                  {"op":"add","path":"/spec/containers/-","value":{"name":"sidecar","image":"acme/sidecar:1.0"}}
                ]`)
                resp.Patch = patch
                pt := admissionv1.PatchTypeJSONPatch
                resp.PatchType = &pt
            }
        }
    }

    review.Response = resp
    out, _ := json.Marshal(review)
    w.Header().Set("Content-Type", "application/json")
    w.Write(out)
}
```

A few production-grade details we elided:

- **Always echo the UID.** A missing UID is the apiserver's signal that the response is malformed; it logs `unexpected response` and returns 500 to the client.
- **Decode the AdmissionReview using the apiserver scheme.** Don't use `json.Unmarshal` directly into the typed struct; the `Raw` fields are byte slices and the decoder handles content-type negotiation (JSON vs protobuf — though protobuf is rare for webhooks).
- **Use `metav1.Status` for denial details.** Setting `Result.Code = 403` produces a clean `kubectl` error; `nil` makes the user see a generic "denied" message.
- **TLS is mandatory.** Webhooks must serve on HTTPS. The apiserver will not even attempt HTTP.
- **Health endpoints.** Add `/healthz` and `/readyz` separate from `/mutate` so the Service's readiness probe isn't routed through the policy code path.

### 8.1 Using controller-runtime

The community Go scaffolding is `sigs.k8s.io/controller-runtime/pkg/webhook`. It handles decoder boilerplate, exposes a clean `admission.Handler` interface, and supports an in-memory cert manager. The same code under that framework:

```go
import (
    "context"
    "encoding/json"
    corev1 "k8s.io/api/core/v1"
    "sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

type SidecarInjector struct{}

func (s *SidecarInjector) Handle(ctx context.Context, req admission.Request) admission.Response {
    var pod corev1.Pod
    if err := json.Unmarshal(req.Object.Raw, &pod); err != nil {
        return admission.Errored(400, err)
    }
    if pod.Labels["sidecar.acme.io/injected"] == "true" {
        return admission.Allowed("already injected")
    }
    if pod.Labels == nil { pod.Labels = map[string]string{} }
    pod.Labels["sidecar.acme.io/injected"] = "true"
    pod.Spec.Containers = append(pod.Spec.Containers, corev1.Container{
        Name: "sidecar", Image: "acme/sidecar:1.0",
    })
    out, _ := json.Marshal(pod)
    return admission.PatchResponseFromRaw(req.Object.Raw, out)  // computes JSONPatch diff for you
}
```

The `admission.PatchResponseFromRaw` helper does the diff-to-JSONPatch conversion. That's the production pattern.

### 8.2 Latency budget

Webhooks are on the apiserver's hot path. Concretely: every Pod creation triggers your webhook if you match `pods`. In a busy cluster, that is dozens to hundreds of QPS. Budget:

- p50 < 10 ms
- p95 < 50 ms
- p99 < 100 ms
- timeoutSeconds: 5

Anything more and you're paying APF taxes. To hit those numbers:

- Never call the apiserver synchronously from inside a webhook. Use a cached client (informer + lister).
- Never call external services synchronously (no Sigstore lookups inline — pre-pull signatures into a cache, or use a separate verification webhook async).
- Don't deserialize twice. The `Raw` JSON is enough for many checks.
- Run multiple replicas behind a Service; the apiserver load-balances across endpoints.

---

## 9. Building a Webhook Server in Python (and Why Language Does Not Matter)

A webhook is *just* an HTTPS endpoint that accepts a JSON object and returns one. Nothing about it requires Go. Python in 30 lines, using FastAPI:

```python
import base64, json
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/mutate")
async def mutate(req: Request):
    body = await req.json()
    req_obj = body["request"]
    uid = req_obj["uid"]
    pod = req_obj["object"]
    labels = pod.get("metadata", {}).get("labels", {}) or {}

    response = {"uid": uid, "allowed": True}
    if labels.get("sidecar.acme.io/injected") != "true":
        patch = [
            {"op": "add", "path": "/metadata/labels", "value": {"sidecar.acme.io/injected": "true"}}
                if not labels else
                {"op": "add", "path": "/metadata/labels/sidecar.acme.io~1injected", "value": "true"},
            {"op": "add", "path": "/spec/containers/-",
             "value": {"name": "sidecar", "image": "acme/sidecar:1.0"}},
        ]
        response["patch"] = base64.b64encode(json.dumps(patch).encode()).decode()
        response["patchType"] = "JSONPatch"

    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": response}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8443,
                ssl_keyfile="/etc/webhook/tls/tls.key",
                ssl_certfile="/etc/webhook/tls/tls.crt")
```

Functionally equivalent to the Go version. Where Python actually pays a tax:

- **Cold start**: a Python interpreter with FastAPI may take 1–3 seconds to boot. With `failurePolicy: Fail`, a deployment rollout that loses all replicas momentarily wedges every cluster write for those seconds. Mitigation: pin a `PodDisruptionBudget` of `minAvailable: 1`, set `terminationGracePeriodSeconds` generously, ensure `PreStop` lets in-flight requests drain.
- **Per-request latency**: a JSON-decode + dict-lookup + JSON-encode round trip in Python is ~1–2ms. Compared to Go's ~100µs, it's a 10–20× tax — but still well under 50ms p95 unless you're doing CPU-heavy work in Python.

The point: language choice is a team-productivity decision, not a Kubernetes-compatibility one. Teams without Go expertise should use Python (or Java, Node, Rust, whatever) freely.

---

## 10. TLS, caBundle, and Certificate Rotation Without Outages

TLS is the single most common source of webhook outages in the wild. The mechanism:

1. The webhook server presents a TLS cert. Its SAN must match the Service DNS (`<svc>.<ns>.svc`).
2. The apiserver validates that cert against the `caBundle` in the webhook configuration. The apiserver does *not* fall through to the system trust store.
3. Cert expires.
4. Apiserver TLS handshake fails.
5. `failurePolicy: Fail` means every matching request is rejected.
6. Cluster wedges.

The defensive patterns:

### 10.1 cert-manager + the CA injector

The canonical setup uses cert-manager:

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata: {name: webhook-ca, namespace: policy-system}
spec:
  ca: {secretName: webhook-ca-root}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: webhook-server-cert, namespace: policy-system}
spec:
  secretName: webhook-server-tls
  issuerRef: {name: webhook-ca}
  dnsNames:
  - image-policy.policy-system.svc
  - image-policy.policy-system.svc.cluster.local
  duration: 2160h     # 90 days
  renewBefore: 720h   # rotate 30 days before expiry
---
# CA injector annotation: cert-manager rewrites caBundle on every issuer change
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: image-policy
  annotations:
    cert-manager.io/inject-ca-from: policy-system/webhook-server-cert
webhooks:
- name: image-policy.acme.io
  clientConfig:
    service: {name: image-policy, namespace: policy-system, path: /validate}
    # caBundle intentionally OMITTED — cert-manager populates it
  ...
```

The `cert-manager.io/inject-ca-from` annotation is consumed by the cert-manager cainjector controller, which watches the Certificate and patches the webhook configuration's `caBundle` field whenever the CA rotates.

Rotation flow:

1. cert-manager's Certificate controller sees `renewBefore` is hit; renews the leaf cert.
2. The Secret gets new `tls.crt` / `tls.key`. The webhook Pod's volume is a *projected secret*, but Pods do NOT auto-reload secrets — you must either restart the Pod or have the server watch the file. (cert-manager's docs recommend rolling the Deployment after rotation; some webhook frameworks like `controller-runtime` watch the file.)
3. The cainjector updates `caBundle` in the webhook configuration if the CA itself rotated.

### 10.2 Self-signed-on-startup (anti-pattern)

Some webhooks generate their own self-signed cert at startup and patch the webhook configuration's `caBundle` themselves. This works in 90% of cases but breaks the moment you scale to multiple replicas (each replica has a different cert) or run a Pod restart at the wrong moment (the new cert isn't yet in the configuration). Don't do this.

### 10.3 The classic outage

Here is the post-mortem template, almost verbatim from multiple real incidents:

> A team installed Kyverno / Gatekeeper / a custom webhook. The webhook used a self-signed cert generated at install time with a 1-year expiry. The team forgot to set up rotation. 365 days later, on a Saturday, the cert expired. The webhook's failurePolicy was Fail. Every cluster write started failing, including kubelet's heartbeats, the apiserver's lease renewal, controller manager leader election. The apiserver could not even DELETE the broken webhook configuration because deleting `validatingwebhookconfigurations.admissionregistration.k8s.io` itself goes through admission. The on-call engineer had to bypass admission entirely by editing the apiserver manifest to add `--disable-admission-plugins=ValidatingAdmissionWebhook`, restart the apiserver, fix the cert, restart again.

How to avoid:

- Always use cert-manager (or equivalent) with auto-rotation.
- Set up an alert on cert expiry: `kubernetes_certmanager_certificate_expiration_timestamp_seconds - time() < 7*86400`.
- Exclude `kube-system` and policy-system namespaces from your own webhook (so a wedge doesn't cascade into your control plane).
- Document the bypass procedure: `--disable-admission-plugins=MutatingAdmissionWebhook,ValidatingAdmissionWebhook` on the apiserver as a break-glass.

---

## 11. Failure Modes: The Classic Cluster-Wide Wedge

Admission webhooks fail in distinctively bad ways because they sit synchronously on every write. The five canonical wedges:

### 11.1 The expired-cert wedge

Described in §10. The most common.

### 11.2 The self-fence wedge

A webhook deployed as a Deployment in a normal namespace. Its rules match `pods`. The Deployment's pods are restarted (rolling update). When the last pod is terminating and the new ones haven't started, the webhook is unreachable. With `failurePolicy: Fail`, the new pods cannot be created because their CREATE goes through the webhook — which is unreachable. Deadlock.

Defense: `namespaceSelector` MUST exclude the webhook's own namespace. Concretely:

```yaml
namespaceSelector:
  matchExpressions:
  - {key: kubernetes.io/metadata.name, operator: NotIn,
     values: [policy-system, kube-system, kube-public, kube-node-lease]}
```

### 11.3 The kube-system wedge

A webhook that matches `kube-system` namespace. The apiserver routinely creates and updates objects in kube-system (events, endpoints, leases, services). If the webhook is unreachable or slow, the apiserver's own internal writes block.

Defense: always exclude `kube-system` from policy webhooks unless you have a specific reason. The Pod Security Admission built-in is the only thing that should universally inspect kube-system, and it's in-tree (zero RTT).

### 11.4 The slow-webhook APF wedge

A webhook with `timeoutSeconds: 30` (the maximum). Each request the webhook is called on holds an APF seat for up to 30 seconds. The default APF `system-leader-election` priority level has, say, 100 seats. If 100 requests pile up on the slow webhook, lease renewal across the cluster stalls. New controllers can't elect leaders. The cluster degrades.

Defense: `timeoutSeconds: 5` max. Profile your webhook in production; if it's slow, fix the webhook.

### 11.5 The deletion-cascade wedge

`kubectl delete ns staging` cascades to every object in the namespace. Each deletion goes through DELETE admission. If a webhook with `operations: ["DELETE"]` is wedged, the namespace stays in Terminating forever. The finalizer `kubernetes` cannot be cleared.

Defense: think hard before matching `DELETE`. Most policies need only `CREATE` and `UPDATE`. If you must validate DELETE (e.g., to enforce backup retention), do it with `failurePolicy: Ignore` or with VAP (which is in-process and can't go down independently).

### 11.6 General defensive patterns

- `matchConditions` (CEL) to filter aggressively before the webhook is even called.
- `namespaceSelector` excluding system namespaces.
- `objectSelector` requiring opt-in labels.
- `failurePolicy: Ignore` for everything that isn't security-critical, paired with audit logging.
- Multiple replicas of the webhook server, with PodDisruptionBudget.
- An entry in your runbook: "if cluster writes are wedged, ssh into a control-plane node, edit `/etc/kubernetes/manifests/kube-apiserver.yaml` to add `--disable-admission-plugins=MutatingAdmissionWebhook,ValidatingAdmissionWebhook` to the args, the apiserver pod will restart automatically, then debug."

---

## 12. Conversion Webhooks (Forward Reference to CRDs)

Conversion webhooks are a third type, structurally similar to admission webhooks but for a different purpose: converting CRD objects between API versions. Detail belongs in chapter 23 (CRDs); this section gives the apiserver-side picture so you understand the request flow.

### 12.1 When conversion fires

A CRD can declare multiple versions. One of them is the storage version; all others are "served." Every time the apiserver reads a stored object, it must convert from storage version to the served version the client asked for. Every time it writes, it converts from the served version to storage.

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: foos.example.com}
spec:
  group: example.com
  names: {kind: Foo, plural: foos}
  scope: Namespaced
  conversion:
    strategy: Webhook
    webhook:
      clientConfig:
        service: {name: foo-conversion, namespace: foo-system, path: /convert}
        caBundle: ...
      conversionReviewVersions: ["v1"]
  versions:
  - name: v1alpha1
    served: true
    storage: false
    schema: {openAPIV3Schema: ...}
  - name: v1
    served: true
    storage: true
    schema: {openAPIV3Schema: ...}
```

Two `strategy` values:

- **`None`** — apiserver does the conversion automatically *if and only if* the versions are structurally identical. Almost never useful in practice — if your versions are identical there's no reason to have multiple.
- **`Webhook`** — apiserver calls your webhook for every conversion.

### 12.2 ConversionReview request shape

```json
{
  "apiVersion": "apiextensions.k8s.io/v1",
  "kind": "ConversionReview",
  "request": {
    "uid": "0000-1111-...",
    "desiredAPIVersion": "example.com/v1",
    "objects": [
      {"apiVersion": "example.com/v1alpha1", "kind": "Foo", "metadata": {"name": "a"}, "spec": {...}},
      {"apiVersion": "example.com/v1alpha1", "kind": "Foo", "metadata": {"name": "b"}, "spec": {...}}
    ]
  }
}
```

Note `objects` is an array — conversion is batched. A `LIST` of 1000 Foos may produce one ConversionReview with 1000 objects.

### 12.3 ConversionReview response

```json
{
  "apiVersion": "apiextensions.k8s.io/v1",
  "kind": "ConversionReview",
  "response": {
    "uid": "0000-1111-...",
    "result": {"status": "Success"},
    "convertedObjects": [
      {"apiVersion": "example.com/v1", "kind": "Foo", "metadata": {"name": "a"}, "spec": {...}},
      {"apiVersion": "example.com/v1", "kind": "Foo", "metadata": {"name": "b"}, "spec": {...}}
    ]
  }
}
```

The webhook MUST return the same number of objects, in the same order, with `metadata.name`, `namespace`, `uid`, `resourceVersion`, `creationTimestamp`, and the kind unchanged (only `apiVersion` differs). It MAY change spec/status; that is the whole point.

### 12.4 Performance pitfall

Every LIST or GET of the CRD calls your conversion webhook. A reconcile loop doing `client.List(Foos)` every 30 seconds against a CRD with 5000 objects yields ~170 conversions per second per controller. The conversion webhook becomes a hot path.

Defenses:

- Cache aggressively in the webhook. Conversions for a given (oldObject, targetVersion) are deterministic — memoize.
- Keep the webhook stateless and behind a Service with multiple replicas.
- Where possible, store the *latest* version and convert downward only on read of legacy clients.

Pitfall: a conversion webhook that returns a different `metadata.uid` than received — the apiserver detects this and the request fails with a confusing "object identity mismatch" error.

---

## 13. ValidatingAdmissionPolicy: In-Process CEL, Zero RTT

`ValidatingAdmissionPolicy` (VAP) is the in-process successor to validating webhooks. It went beta in 1.28 and GA in 1.30. It runs *inside the apiserver*, evaluates **CEL** (Common Expression Language) expressions, and has zero network round-trip cost.

It's structured as two objects:

- **`ValidatingAdmissionPolicy`** — declares *what* to check. Cluster-scoped, like webhook configurations.
- **`ValidatingAdmissionPolicyBinding`** — declares *where* to apply it (namespace selector, parameter reference). Cluster-scoped, multiple bindings per policy.

The separation lets a security team author one policy and let app teams bind it (perhaps with different `params`) into their namespaces.

### 13.1 A minimum example

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: deny-latest-tag.policy.acme.io
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE", "UPDATE"]
      resources: ["pods"]
  matchConditions:                  # early filter, same semantics as webhook matchConditions
  - name: not-system-namespace
    expression: |
      !(request.namespace in ['kube-system', 'kube-public', 'kube-node-lease'])
  validations:
  - expression: |
      object.spec.containers.all(c, !c.image.endsWith(':latest') && c.image.contains(':'))
    message: "container image must specify an explicit (non-:latest) tag"
    reason: Forbidden
  - expression: |
      object.spec.containers.all(c, has(c.resources.limits.memory))
    messageExpression: "'container ' + object.spec.containers.filter(c, !has(c.resources.limits.memory))[0].name + ' must declare memory limits'"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: deny-latest-tag-prod-binding
spec:
  policyName: deny-latest-tag.policy.acme.io
  validationActions: ["Deny"]                # Deny | Warn | Audit (any combination)
  matchResources:
    namespaceSelector:
      matchLabels: {environment: production}
```

### 13.2 CEL primer (just enough)

CEL is a typed expression language designed by Google for safe in-process policy. Its core promise: every expression has a cost bound determinable statically, so it can never be slower than `O(k * length)` for some small `k`.

Types: `bool`, `int`, `uint`, `double`, `string`, `bytes`, `list<T>`, `map<K,V>`, `null`, plus protobuf message types.

Selected built-in functions and operators (full reference: `https://github.com/google/cel-spec`):

| Construct | Example |
|---|---|
| Field access | `object.spec.replicas` |
| Index | `object.spec.containers[0]` |
| Map index | `object.metadata.labels['app']` |
| `has()` | `has(object.metadata.labels)` — true if field is present (use before access to nullable fields) |
| `size()` | `size(object.spec.containers)` |
| String functions | `s.startsWith('foo')`, `s.endsWith(...)`, `s.contains(...)`, `s.matches('^[a-z]+$')` (regex), `s.lowerAscii()` |
| List comprehensions | `list.all(x, pred)`, `list.exists(x, pred)`, `list.exists_one(x, pred)`, `list.filter(x, pred)`, `list.map(x, expr)` |
| Comparisons | `==`, `!=`, `<`, `>`, `<=`, `>=` (typed) |
| Logical | `&&`, `||`, `!`, ternary `a ? b : c` |
| Membership | `x in ['a','b']`, `key in map` |
| Duration | `duration('5m') > duration('1m')` |
| Timestamp | `timestamp('2025-01-01T00:00:00Z')`, `now()` (NOT available in admission CEL — non-deterministic) |
| Quantity | `quantity('100Mi').compareTo(quantity('1Gi')) < 0` (Kubernetes-specific extension) |

The variables available in admission CEL:

- **`object`** — the new object (`null` for DELETE).
- **`oldObject`** — the prior object (`null` for CREATE).
- **`request`** — the AdmissionRequest metadata (operation, namespace, name, userInfo, dryRun, kind, resource, ...).
- **`params`** — the parameter object referenced by the Binding's `paramRef`, if any.
- **`authorizer`** — a CEL object for running RBAC checks: `authorizer.path('/healthz').check('GET').allowed()` or `authorizer.group('').resource('pods').namespace('default').check('create').allowed()`.
- **`namespaceObject`** — the full `Namespace` object containing this resource (for namespaced resources). Useful for reading namespace labels/annotations.
- **`variables`** — names defined via `spec.variables` (see §13.5).

### 13.3 The shape of `validations`

Each validation has:

- **`expression`** — a CEL expression returning `bool`. If `true`, the validation passes.
- **`message`** — a static string shown on failure.
- **`messageExpression`** — a CEL expression returning a string, evaluated on failure. Useful for "container `<name>` must declare limits" messages.
- **`reason`** — a `metav1.StatusReason` (`Forbidden`, `Invalid`, `RequestEntityTooLarge`).

### 13.4 `validationActions` on the binding

The binding declares how a failed validation translates to behavior. **A single binding can declare any combination** of:

- **`Deny`** — reject the request with the validation's `reason` and message.
- **`Warn`** — let the request through, but add the validation's message to the `Warning:` response header (so `kubectl` prints it).
- **`Audit`** — let the request through, but record `validation.policy.admission.k8s.io/validation_failure` annotations in the apiserver's audit log.

This three-way split is *the* operational killer feature of VAP. The migration playbook:

1. Author the policy. Bind it with `validationActions: [Audit]`. Wait a week. Inspect audit logs for false positives.
2. Promote to `[Warn, Audit]`. Now users see the policy in their kubectl output. Wait a week.
3. Promote to `[Deny, Audit]`. Enforcement is on; audit still captures denials for forensics.

With webhooks, achieving the same migration required wrapping every check in a "report-only" mode in webhook code, then flipping a flag. With VAP it's a YAML edit and zero webhook deploy.

### 13.5 Parameters

A policy can be parameterized via a CRD (or any built-in resource) referenced by the binding:

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata: {name: max-replicas.policy.acme.io}
spec:
  paramKind: {apiVersion: acme.io/v1, kind: ReplicaPolicy}
  matchConstraints:
    resourceRules:
    - apiGroups: ["apps"]
      apiVersions: ["v1"]
      operations: ["CREATE", "UPDATE"]
      resources: ["deployments"]
  validations:
  - expression: "object.spec.replicas <= params.spec.maxReplicas"
    messageExpression: "'replicas (' + string(object.spec.replicas) + ') exceeds max (' + string(params.spec.maxReplicas) + ')'"
---
apiVersion: acme.io/v1
kind: ReplicaPolicy
metadata: {name: prod-limits}
spec: {maxReplicas: 100}
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata: {name: prod-binding}
spec:
  policyName: max-replicas.policy.acme.io
  paramRef: {name: prod-limits, parameterNotFoundAction: Deny}
  validationActions: [Deny]
  matchResources:
    namespaceSelector: {matchLabels: {tier: prod}}
```

This is how a single policy serves multiple environments (prod vs staging) with different limits, without duplicating logic.

### 13.6 `auditAnnotations`

In addition to `validations`, a policy can declare audit annotations — CEL expressions whose output is recorded in the audit log regardless of pass/fail. Useful for forensics:

```yaml
auditAnnotations:
- key: image-tags
  valueExpression: "object.spec.containers.map(c, c.image).join(',')"
```

This causes every Pod admission to attach the comma-joined list of images to the audit record. Combined with `validationActions: [Audit]`, you can build the world's cheapest "what images are deployed where" inventory.

### 13.7 `variables`

CEL supports named variables to reduce duplication. They are evaluated lazily and memoized within an evaluation:

```yaml
spec:
  variables:
  - name: appContainers
    expression: "object.spec.containers"
  - name: limitlessContainers
    expression: "variables.appContainers.filter(c, !has(c.resources.limits.memory))"
  validations:
  - expression: "size(variables.limitlessContainers) == 0"
    messageExpression: "'containers without memory limits: ' + variables.limitlessContainers.map(c, c.name).join(',')"
```

### 13.8 The CEL evaluation pipeline

```
apiserver decode → in-tree mutators → mutating webhooks → schema/CEL validate
                                                              │
                                                              ▼
                                                ┌──────────────────────────────┐
                                                │  Validating phase             │
                                                │                                │
                                                │  Compile-time (once):          │
                                                │   - parse VAP CEL              │
                                                │   - type-check                 │
                                                │   - cost estimate              │
                                                │   - reject if cost > budget    │
                                                │                                │
                                                │  Per-request (parallel):       │
                                                │   - in-tree validators         │
                                                │   - validating webhooks (RPC)  │
                                                │   - VAP evaluations (in-proc)  │
                                                │                                │
                                                │  Per VAP:                      │
                                                │   - eval matchConditions       │
                                                │     (short-circuit if false)   │
                                                │   - eval variables (lazy)      │
                                                │   - eval validations           │
                                                │   - if fail: apply action      │
                                                │     (Deny → reject;            │
                                                │      Warn → response header;   │
                                                │      Audit → annotation)       │
                                                │   - eval auditAnnotations      │
                                                └──────────────────────────────┘
```

The compile-time check is enforced when the `ValidatingAdmissionPolicy` is created/updated. The apiserver computes a worst-case cost (a synthetic number, roughly proportional to operation count and traversal depth) and rejects the policy if it exceeds `RuntimeCELCostBudget` (10 million units; this is also tunable via the `RuntimeCELCostBudget` feature configuration). This is what makes VAP safe to put on the hot path.

---

## 14. MutatingAdmissionPolicy: CEL-Driven Patches

`MutatingAdmissionPolicy` is the mutating counterpart, introduced as alpha in 1.32 and beta in subsequent releases. The shape is similar:

```yaml
apiVersion: admissionregistration.k8s.io/v1alpha1
kind: MutatingAdmissionPolicy
metadata: {name: default-resources.policy.acme.io}
spec:
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE"]
      resources: ["pods"]
  reinvocationPolicy: Never
  failurePolicy: Fail
  matchConditions:
  - name: not-system
    expression: "!(request.namespace in ['kube-system','kube-public'])"
  mutations:
  - patchType: ApplyConfiguration
    applyConfiguration:
      expression: |
        Object{
          spec: Object.spec{
            containers: object.spec.containers.map(c, c.with({
              resources: c.resources.with({
                limits: has(c.resources.limits) ? c.resources.limits : {'memory': '512Mi', 'cpu': '500m'}
              })
            }))
          }
        }
```

Two `patchType` options:

- **`ApplyConfiguration`** — CEL expression returns an `Object{...}` literal in *apply-configuration* form (the same shape used by server-side apply). The apiserver merges it into the original. This is the preferred form because it integrates with SSA field ownership.
- **`JSONPatch`** — CEL expression returns a list of JSON Patch operations.

`Object{ ... }` is a CEL extension specific to admission: it constructs typed objects matching the target's schema. `Object.spec{...}` constructs a partial spec. The `with()` method on an object returns a copy with the given fields merged.

The big wins:

- Zero RTT, like VAP.
- Compile-time type checking against the resource's schema.
- Field ownership integrates with server-side apply — the apiserver knows the policy is the owner of fields it set, so subsequent applies from the user don't fight the policy.

The limitations (as of beta):

- Less expressive than a Go webhook for complex mutations (e.g., constructing a sidecar with environment variables from external sources is awkward).
- Cannot make external calls (by design).

For most "default-this-field-if-absent" use cases — the bulk of mutating-webhook traffic in practice — MutatingAdmissionPolicy will replace the webhook within the next few minor releases.

---

## 15. The CEL Cost Budget and Writing Cheap Expressions

The CEL cost estimator runs at policy creation time. It computes a *worst-case* cost for the expression, assuming maximum input sizes (the apiserver knows OpenAPI schemas and reads `maxItems` / `maxLength` from them). If the estimate exceeds `RuntimeCELCostBudget` (default 10 million), the policy is rejected with an explicit error message naming the offending expression.

### 15.1 What costs what (approximate)

- Field access: O(1), cost 1.
- List iteration (`all`, `exists`, `filter`, `map`): O(N), cost N × per-element cost.
- Nested iteration: multiplies. `list1.all(x, list2.all(y, p))` is O(N×M).
- Regex match: O(length × pattern complexity), often >100.
- String concatenation: O(length).
- Quantity comparison: O(1) but non-trivial constant (~10).

### 15.2 Patterns to prefer

**Use `matchConditions` to short-circuit cheaply.** A `matchCondition` that filters out 99% of traffic is essentially free; the expensive `validations` then only run on 1%.

```yaml
matchConditions:
- name: only-prod
  expression: "namespaceObject.metadata.labels['env'] == 'prod'"
validations:
- expression: "..."  # only runs on prod
```

**Use `has()` before accessing potentially-missing fields.** CEL is null-strict; `object.spec.foo.bar` is an error if `foo` is missing. Wrap with `has(object.spec.foo) && object.spec.foo.bar == 'x'`.

**Hoist common subexpressions to `variables`.** Without variables, `object.spec.containers.filter(...).all(...)` would iterate twice. With a variable, once.

**Pre-filter lists rather than nesting `all` inside `all`.**

```cel
# Worse: O(N×M)
object.spec.containers.all(c,
  c.env.all(e, e.name != 'PROXY_URL'))

# Better: O(N+M) by hoisting
variables.banned.all(b,
  !object.spec.containers.exists(c, c.env.exists(e, e.name == b)))
```

**Avoid `matches()` (regex) unless necessary.** A `startsWith` + `endsWith` combination is faster.

### 15.3 Anti-patterns

**Nested quantifiers over unbounded lists**. If your CRD has a list field with no `maxItems`, the cost estimator assumes a giant size and rejects the policy. Fix the CRD schema to set `maxItems`.

**String building in tight loops**. `containers.map(c, c.name + ',' + c.image).join(',')` allocates a list of strings *and* concatenates each. Use `join` after `map` only on small inputs.

**Calling `authorizer.check(...)` inside a list comprehension**. The `authorizer` calls into RBAC and is *not* free (cost ~100 per call). Doing it 1000 times per request will blow the per-request CEL budget.

---

## 16. Kyverno: Declarative YAML Policy

Kyverno is one of the two dominant policy engines (the other being OPA Gatekeeper). Its differentiator: **policies are written in YAML**, not a separate language. This makes it accessible to operators who already think in Kubernetes manifests.

### 16.1 Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  apiserver                                                          │
│   ├── MutatingAdmissionWebhook  ──► kyverno-admission-controller   │
│   └── ValidatingAdmissionWebhook ──► kyverno-admission-controller   │
└─────────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│  kyverno-admission-controller (Deployment, 3 replicas typical)   │
│    - Receives AdmissionReview                                     │
│    - Looks up matching ClusterPolicy / Policy                     │
│    - Evaluates rules (mutate, validate, generate, verifyImages)   │
│    - Returns AdmissionResponse                                    │
└──────────────────────────────────────────────────────────────────┘
              │  enqueue PolicyReport entries
              ▼
┌──────────────────────────────────────────────────────────────────┐
│  kyverno-reports-controller (Deployment, 1 replica)              │
│    - Aggregates per-resource compliance into PolicyReport CRs    │
│    - Periodically audits existing resources against policies      │
│    - Used by kubectl-kyverno and dashboards                       │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  kyverno-cleanup-controller                                       │
│    - Executes CleanupPolicy rules (delete X older than Y)         │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  kyverno-background-controller                                    │
│    - Executes "generate" rules                                    │
│    - Watches sources, creates downstream resources                │
└──────────────────────────────────────────────────────────────────┘
```

The four controllers are separate Deployments. Admission lives on the critical path; the others are background.

### 16.2 Policy shape

Kyverno's primary CRDs:

- **`ClusterPolicy`** — cluster-scoped policy applying to all namespaces.
- **`Policy`** — namespaced policy applying only to that namespace.

A policy contains one or more *rules*, each of one *type*:

- **`validate`** — allow/deny based on a pattern or CEL expression.
- **`mutate`** — patch the object (strategic merge or JSON Patch).
- **`generate`** — create downstream resources (e.g., a NetworkPolicy in every new namespace).
- **`verifyImages`** — check image signatures (Sigstore).

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: deny-latest-tag
spec:
  validationFailureAction: Enforce            # Enforce | Audit
  background: true                            # also evaluate existing resources
  rules:
  - name: no-latest
    match:
      any:
      - resources: {kinds: [Pod]}
    exclude:
      any:
      - resources: {namespaces: [kube-system, kube-public]}
    validate:
      message: "image tag :latest is forbidden"
      pattern:
        spec:
          containers:
          - image: "!*:latest"             # Kyverno wildcard pattern: NOT ending in :latest
```

The `pattern` field is Kyverno's signature feature: a "matching template" written in the same shape as the resource itself. `image: "!*:latest"` means "the image field must NOT match the pattern `*:latest`." The pattern walks the resource and any non-matching position fails the validation.

For more complex checks, Kyverno supports CEL (since v1.11):

```yaml
validate:
  cel:
    expressions:
    - expression: "object.spec.containers.all(c, !c.image.endsWith(':latest'))"
      message: "image :latest forbidden"
```

### 16.3 Mutate rule example

Inject default labels:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: inject-labels}
spec:
  rules:
  - name: add-team-label
    match: {any: [{resources: {kinds: [Deployment]}}]}
    mutate:
      patchStrategicMerge:
        metadata:
          labels:
            team: "{{ request.namespace }}"        # JMESPath-like context substitution
```

Kyverno's mutation can use Strategic Merge Patch (because Kyverno has the schema) or JSON Patch.

### 16.4 Generate rule example

Auto-create a default-deny NetworkPolicy in every new namespace:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: default-deny-netpol}
spec:
  rules:
  - name: deny-all
    match: {any: [{resources: {kinds: [Namespace]}}]}
    generate:
      apiVersion: networking.k8s.io/v1
      kind: NetworkPolicy
      name: default-deny
      namespace: "{{ request.object.metadata.name }}"
      data:
        spec:
          podSelector: {}
          policyTypes: [Ingress, Egress]
```

This is *generate*, not *mutate*: it creates a *separate* object in response to the Namespace event. The generate controller watches the trigger and reconciles the dependent object on every change. If a user deletes the generated NetworkPolicy, the controller re-creates it.

### 16.5 verifyImages

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: require-image-signature}
spec:
  rules:
  - name: cosign-verify
    match: {any: [{resources: {kinds: [Pod]}}]}
    verifyImages:
    - imageReferences:
      - "ghcr.io/acme/*"
      attestors:
      - entries:
        - keys:
            publicKeys: |
              -----BEGIN PUBLIC KEY-----
              MFkwEwYH...
              -----END PUBLIC KEY-----
      mutateDigest: true        # rewrite tag → digest after verification
```

This calls Sigstore at admission time, verifies the signature, and (optionally) rewrites the image reference to the resolved digest. Caching matters here — see chapter 27 on supply-chain.

### 16.6 Autogen

A Kyverno policy that matches `Pod` is automatically *also* applied to `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, and `CronJob` by virtue of their embedded PodTemplate. Without autogen, a policy that denies `:latest` on Pods would let a Deployment with `:latest` through (because the controller-created pod would be denied, leaving the Deployment in a perpetual "Progressing" state). Autogen rewrites the rule to match the workload controller, so the check fires at the controller layer.

### 16.7 Strengths and limitations

**Strengths:** YAML-native; one engine for mutate/validate/generate/cleanup/verify; large policy library; PolicyReport CRs for compliance dashboards.

**Limitations:** the engine is heavyweight (four controllers, ~500 MB memory in busy clusters); CEL support is newer than the native pattern engine; some advanced patterns (referencing arbitrary other resources) require Kyverno's API calls feature, which adds RTT.

---

## 17. OPA Gatekeeper: Rego, ConstraintTemplates, and the Audit Controller

OPA Gatekeeper is the policy engine built on Open Policy Agent. Its differentiator: **policies are written in Rego**, a declarative datalog-derived language. This makes it more expressive than Kyverno for complex cross-resource policies but requires learning Rego.

### 17.1 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  apiserver                                                      │
│   └── ValidatingAdmissionWebhook ──► gatekeeper-controller       │
└─────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│  gatekeeper-controller (Deployment, 3 replicas typical)          │
│    - Receives AdmissionReview                                     │
│    - Evaluates Rego policies (ConstraintTemplate + Constraint)    │
│    - Returns AdmissionResponse                                    │
│  (Mutating admission added later, via AssignMetadata / Assign    │
│   CRDs — opt-in feature)                                          │
└──────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│  gatekeeper-audit (Deployment, 1 replica)                        │
│    - Periodically runs every constraint against every existing   │
│      resource and reports violations (auditFromCache)             │
│    - Audit results stored on the Constraint's .status            │
└──────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│  External Data feature                                            │
│    - Constraint can call ExternalDataProvider (gRPC)              │
│    - Used for image signature verification, asset inventory       │
└──────────────────────────────────────────────────────────────────┘
```

### 17.2 The ConstraintTemplate + Constraint pattern

This is the architecturally interesting bit. A ConstraintTemplate defines:

- A Rego policy.
- A CRD schema for parameters.

When you create a ConstraintTemplate, Gatekeeper auto-generates a CRD whose schema is the template's parameters. You then create *instances* of that CRD (the Constraints) to enable the policy with specific parameters.

```yaml
# Step 1: ConstraintTemplate — defines the policy
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata: {name: k8srequiredlabels}
spec:
  crd:
    spec:
      names: {kind: K8sRequiredLabels}     # this generates the K8sRequiredLabels CRD
      validation:
        openAPIV3Schema:
          type: object
          properties:
            labels:
              type: array
              items: {type: string}
  targets:
  - target: admission.k8s.gatekeeper.sh
    rego: |
      package k8srequiredlabels

      violation[{"msg": msg, "details": {"missing_labels": missing}}] {
        provided := {label | input.review.object.metadata.labels[label]}
        required := {label | label := input.parameters.labels[_]}
        missing := required - provided
        count(missing) > 0
        msg := sprintf("missing required labels: %v", [missing])
      }

# Step 2: Constraint — instance with parameters
---
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sRequiredLabels                    # the CRD auto-created by the template
metadata: {name: must-have-team-label}
spec:
  match:
    kinds:
    - apiGroups: [""]
      kinds: ["Namespace"]
  parameters:
    labels: ["team", "owner", "environment"]
```

The split: **the security team writes the ConstraintTemplate (Rego)**; **the app teams write Constraints (YAML parameters)**. This is the same separation of concerns as VAP's Policy + Binding, but with the Rego layer between them.

### 17.3 Rego primer (just enough to read examples)

Rego is rule-based: each `name { body }` defines that `name` is true if `body` is satisfied. The body is a conjunction of clauses, each of which is a unification or comparison.

```rego
package k8sbanlatest

violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]    # iterate
  endswith(container.image, ":latest")
  msg := sprintf("container %v uses :latest", [container.name])
}
```

The `[_]` underscore is "any element of the array" — Rego iterates implicitly. The block produces a `violation` for each container with `:latest`. Multiple violations across the same request are aggregated.

`input.review.object` is the AdmissionRequest's `object` — Gatekeeper wraps the AdmissionReview into a Rego-friendly shape.

### 17.4 The audit controller

Unlike Kyverno (whose reports controller is similar), Gatekeeper's audit controller is built-in to the core install. It re-evaluates every Constraint against every existing resource on a periodic interval (default 60 seconds). Results land on the Constraint's `.status.violations`:

```yaml
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sRequiredLabels
metadata: {name: must-have-team-label}
status:
  totalViolations: 3
  violations:
  - enforcementAction: deny
    kind: Namespace
    name: legacy-app
    message: "missing required labels: {\"team\", \"owner\"}"
  - ...
```

This is how you find pre-existing non-compliant objects after rolling out a new policy.

### 17.5 enforcementAction

A Constraint can declare:

- **`deny`** — reject the request on violation.
- **`dryrun`** — record the violation in audit, allow the request through.
- **`warn`** — return a warning (since Gatekeeper 3.10).

Same migration playbook as VAP: dryrun → warn → deny.

### 17.6 Match criteria

```yaml
match:
  kinds:
  - apiGroups: ["apps"]
    kinds: ["Deployment", "StatefulSet"]
  namespaces: ["prod-*"]                     # glob
  excludedNamespaces: ["kube-system"]
  scope: Namespaced                          # Cluster | Namespaced | *
  labelSelector: {matchLabels: {team: payments}}
  namespaceSelector: {matchLabels: {tier: prod}}
```

### 17.7 Kyverno vs Gatekeeper vs VAP

A staff-level decision table:

```
┌──────────────────────────┬────────────┬────────────┬────────────────┐
│ Capability               │ Kyverno    │ Gatekeeper │ VAP / MAP      │
├──────────────────────────┼────────────┼────────────┼────────────────┤
│ Validate                 │ YES        │ YES        │ YES            │
│ Mutate                   │ YES        │ YES (sep.) │ YES (MAP beta) │
│ Generate                 │ YES        │ NO         │ NO             │
│ Cleanup                  │ YES        │ NO         │ NO             │
│ Image verify             │ YES        │ via ext.   │ NO (use other) │
│ Audit existing resources │ YES        │ YES        │ via Audit act. │
│ Policy language          │ YAML+CEL   │ Rego       │ CEL            │
│ Network RTT per request  │ YES        │ YES        │ NO (in-proc)   │
│ Cluster outage if down   │ possible   │ possible   │ NO             │
│ Cost-bounded             │ NO         │ NO (Rego   │ YES (cost      │
│                          │            │  is Turing │  estimator)    │
│                          │            │  complete) │                │
│ Memory footprint         │ ~500MB     │ ~300MB     │ ~0 (in-proc)   │
│ Learning curve           │ Low        │ High       │ Medium         │
│ Cross-resource queries   │ Limited    │ Strong     │ Limited        │
│ Built-in install         │ NO         │ NO         │ YES (apiserver)│
│ Maturity                 │ Stable     │ Stable     │ GA 1.30 (V),   │
│                          │            │            │ Beta (M)       │
└──────────────────────────┴────────────┴────────────┴────────────────┘
```

The rough decision:

- **Need only validation, want zero RTT, want bounded blast radius**: VAP.
- **Need generation, cleanup, or image verification + YAML-native**: Kyverno.
- **Need cross-resource Rego or already standardized on OPA**: Gatekeeper.
- **In a mature cluster**: probably all three, used for different concerns. VAP for the cheap stuff, Kyverno for generation/verify, Gatekeeper for legacy / Rego-heavy.

---

## 18. Pod Security Admission: The PSP Successor

Pod Security Admission (PSA) is a *built-in* validating admission plugin that enforces the three Pod Security Standards. It replaces `PodSecurityPolicy` (PSP), which was deprecated in 1.21 and removed in 1.25.

### 18.1 The three profiles

Defined in the upstream Pod Security Standards document:

| Profile | Intent | Examples of what's banned |
|---|---|---|
| **`privileged`** | Unrestricted | nothing |
| **`baseline`** | Block known privilege escalations | hostNetwork, hostPID, hostIPC, hostPath, privileged containers, host ports, certain capabilities (NET_ADMIN, SYS_ADMIN, ...), unconfined seccomp, unsafe sysctls |
| **`restricted`** | Strict, follow current best practices | everything from baseline; *also* require: non-root user, runAsNonRoot, allowPrivilegeEscalation=false, drop ALL capabilities (add only NET_BIND_SERVICE), seccomp RuntimeDefault, no volumes other than configMap/secret/downwardAPI/emptyDir/PVC/projected/ephemeral, restricted volume types |

### 18.2 How you configure PSA

Per *namespace*, via labels:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: payments
  labels:
    # Enforce mode: deny non-compliant pods
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    # Warn mode: allow but warn (good for migration)
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: latest
    # Audit mode: allow but emit audit annotation
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/audit-version: latest
```

Three modes × three profiles × version pinning. The version pinning is important: each profile has accumulated tweaks over Kubernetes releases, and pinning to a specific version means a cluster upgrade doesn't silently retighten your policy.

### 18.3 What PSA does NOT do

- **No per-workload exceptions.** Either the whole namespace is restricted or it isn't. Need to run one elevated pod (e.g., a CNI agent) in an otherwise restricted namespace? Either move it to a different namespace or augment PSA with VAP / Kyverno / Gatekeeper, which can express finer-grained exceptions.
- **No mutation.** PSA only validates. It will not, e.g., set `runAsNonRoot: true` for you.
- **No image checks.** PSA is only about runtime privilege; image policy is separate.
- **No cluster-wide default with overrides.** You can set a cluster default via `AdmissionConfiguration`, but exceptions still go through namespace labels — not a more granular selector.

This is the gap that Kyverno, Gatekeeper, and VAP step into. The mature pattern: PSA `enforce: baseline` cluster-wide via default config, then VAP / Kyverno layered on top for everything more nuanced.

### 18.4 Cluster-wide default

Via `--admission-control-config-file`:

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: AdmissionConfiguration
plugins:
- name: PodSecurity
  configuration:
    apiVersion: pod-security.admission.config.k8s.io/v1
    kind: PodSecurityConfiguration
    defaults:
      enforce: baseline
      enforce-version: latest
      audit: restricted
      audit-version: latest
      warn: restricted
      warn-version: latest
    exemptions:
      usernames: []
      runtimeClasses: []
      namespaces: [kube-system]
```

This is the "out of the box, baseline; restricted in audit/warn; kube-system exempt" config that most production clusters end up with.

---

## 19. Real Policy Examples: Webhook, CEL, Kyverno, and Gatekeeper Side by Side

Six representative policies, each shown in webhook (Go pseudocode), VAP (CEL), Kyverno YAML, and Gatekeeper (Rego) form. This is the "decoder ring" — translate any policy idiom between engines.

### 19.1 Deny `:latest` tag

**Webhook (Go validating handler):**

```go
func (h *Handler) Handle(ctx context.Context, req admission.Request) admission.Response {
    var pod corev1.Pod
    if err := json.Unmarshal(req.Object.Raw, &pod); err != nil {
        return admission.Errored(400, err)
    }
    for _, c := range pod.Spec.Containers {
        if strings.HasSuffix(c.Image, ":latest") || !strings.Contains(c.Image, ":") {
            return admission.Denied(fmt.Sprintf("container %q uses :latest tag", c.Name))
        }
    }
    return admission.Allowed("")
}
```

**ValidatingAdmissionPolicy (CEL):**

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata: {name: deny-latest}
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE", "UPDATE"]
      resources: ["pods"]
  validations:
  - expression: |
      object.spec.containers.all(c, c.image.contains(':') && !c.image.endsWith(':latest'))
    messageExpression: |
      'containers with :latest tag: ' +
      object.spec.containers.filter(c, c.image.endsWith(':latest')).map(c, c.name).join(',')
```

**Kyverno:**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: deny-latest}
spec:
  validationFailureAction: Enforce
  rules:
  - name: no-latest
    match: {any: [{resources: {kinds: [Pod]}}]}
    validate:
      message: "image tag :latest forbidden"
      pattern:
        spec:
          containers:
          - image: "!*:latest"
```

**Gatekeeper:**

```yaml
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata: {name: k8sdenylatest}
spec:
  crd: {spec: {names: {kind: K8sDenyLatest}}}
  targets:
  - target: admission.k8s.gatekeeper.sh
    rego: |
      package k8sdenylatest
      violation[{"msg": msg}] {
        container := input.review.object.spec.containers[_]
        endswith(container.image, ":latest")
        msg := sprintf("container %v uses :latest tag", [container.name])
      }
---
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sDenyLatest
metadata: {name: deny-latest}
spec:
  match: {kinds: [{apiGroups: [""], kinds: ["Pod"]}]}
```

### 19.2 Require resource limits

**VAP:**

```yaml
validations:
- expression: |
    object.spec.containers.all(c,
      has(c.resources.limits) && has(c.resources.limits.memory) && has(c.resources.limits.cpu))
  message: "every container must declare cpu+memory limits"
```

**Kyverno:**

```yaml
rules:
- name: require-limits
  match: {any: [{resources: {kinds: [Pod]}}]}
  validate:
    message: "cpu and memory limits required"
    pattern:
      spec:
        containers:
        - resources:
            limits:
              memory: "?*"
              cpu: "?*"
```

`?*` is Kyverno's "any non-empty value" wildcard.

### 19.3 Deny hostPath volumes

**VAP:**

```yaml
validations:
- expression: |
    !has(object.spec.volumes) ||
    object.spec.volumes.all(v, !has(v.hostPath))
  message: "hostPath volumes are not allowed"
```

**Kyverno:**

```yaml
rules:
- name: no-hostpath
  match: {any: [{resources: {kinds: [Pod]}}]}
  validate:
    message: "hostPath volumes forbidden"
    pattern:
      spec:
        =(volumes):
        - X(hostPath): "null"
```

Kyverno's `=()` is "optional" and `X()` is "negation." The pattern says: if `volumes` is present, none of them may have a `hostPath` field.

This is also enforced by PSA `baseline`, so in most clusters you do not need a separate policy — just label namespaces with `pod-security.kubernetes.io/enforce: baseline`.

### 19.4 Require image signature

**Kyverno (the simplest):**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: require-signed-images}
spec:
  validationFailureAction: Enforce
  webhookTimeoutSeconds: 30                    # signature verification is slow
  rules:
  - name: verify
    match: {any: [{resources: {kinds: [Pod]}}]}
    verifyImages:
    - imageReferences: ["ghcr.io/acme/*"]
      attestors:
      - entries:
        - keyless:
            subject: "https://github.com/acme/*/.github/workflows/release.yml@refs/heads/main"
            issuer: "https://token.actions.githubusercontent.com"
            rekor: {url: "https://rekor.sigstore.dev"}
      mutateDigest: true
```

VAP cannot do this — it's CEL-only, no external calls. This is one of Kyverno's killer features. (Gatekeeper can via the ExternalData feature + a custom provider; more work.)

### 19.5 Inject Istio sidecar (mutation)

**Webhook (canonical Istio approach):** Istio runs a custom webhook (`istiod`). Pseudocode:

```go
func handle(req admission.Request) admission.Response {
    var pod corev1.Pod
    json.Unmarshal(req.Object.Raw, &pod)

    if pod.Labels["sidecar.istio.io/inject"] != "true" {
        return admission.Allowed("opt-out")
    }
    if alreadyInjected(&pod) {
        return admission.Allowed("already injected")
    }
    pod.Spec.InitContainers = append(pod.Spec.InitContainers, istioInitContainer())
    pod.Spec.Containers = append(pod.Spec.Containers, istioProxyContainer())
    pod.Annotations["sidecar.istio.io/status"] = "injected"

    out, _ := json.Marshal(pod)
    return admission.PatchResponseFromRaw(req.Object.Raw, out)
}
```

**MutatingAdmissionPolicy (alpha, simpler cases):**

```yaml
apiVersion: admissionregistration.k8s.io/v1alpha1
kind: MutatingAdmissionPolicy
metadata: {name: add-default-label}
spec:
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE"]
      resources: ["pods"]
  mutations:
  - patchType: ApplyConfiguration
    applyConfiguration:
      expression: |
        Object{
          metadata: Object.metadata{
            labels: {"injected-by": "mutating-admission-policy"}
          }
        }
```

For complex injection (containers with environment from external config), MAP is currently underpowered; Istio will keep using a Go webhook for the foreseeable future.

**Kyverno:**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: inject-sidecar}
spec:
  rules:
  - name: inject
    match: {any: [{resources: {kinds: [Pod]}, selector: {matchLabels: {sidecar: enabled}}}]}
    mutate:
      patchStrategicMerge:
        spec:
          containers:
          - name: my-sidecar
            image: acme/sidecar:1.0
            resources:
              limits: {cpu: 100m, memory: 64Mi}
```

Strategic merge with `name` as the merge key means this adds `my-sidecar` if absent, leaves other containers alone.

### 19.6 Enforce label propagation from namespace to pod

Goal: every Pod created in a namespace must inherit the `cost-center` label of that namespace.

**VAP (validation only — would reject pods without the right label):**

```yaml
matchConstraints:
  resourceRules:
  - apiGroups: [""]
    apiVersions: ["v1"]
    operations: ["CREATE"]
    resources: ["pods"]
validations:
- expression: |
    !has(namespaceObject.metadata.labels) ||
    !has(namespaceObject.metadata.labels['cost-center']) ||
    (has(object.metadata.labels) &&
     object.metadata.labels['cost-center'] == namespaceObject.metadata.labels['cost-center'])
  messageExpression: |
    'pod must inherit cost-center label from namespace: ' +
    namespaceObject.metadata.labels['cost-center']
```

**Kyverno (mutation, which is friendlier):**

```yaml
rules:
- name: propagate-cost-center
  match: {any: [{resources: {kinds: [Pod]}}]}
  context:
  - name: ns
    apiCall:
      urlPath: "/api/v1/namespaces/{{ request.namespace }}"
      jmesPath: "metadata.labels.\"cost-center\""
  mutate:
    patchStrategicMerge:
      metadata:
        labels:
          cost-center: "{{ ns }}"
```

This is one of the cases where Kyverno's `apiCall` context shines, but note: it adds an RTT to every Pod creation. In aggregate, that can dwarf the actual mutation cost. Consider caching the namespace labels in a sidecar or using a CRD as an intermediate cache.

---

## 20. Operational Metrics and Alerts

The apiserver exposes a rich set of metrics for the admission pipeline. The ones you must alert on:

### 20.1 Core metrics

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `apiserver_admission_controller_admission_duration_seconds` | histogram | `name, operation, rejected, type` | Per in-tree controller latency; `type` = admit/validate |
| `apiserver_admission_step_admission_duration_seconds` | histogram | `operation, type` | Aggregate per step (Mutating, Validating, etc.) |
| `apiserver_admission_step_admission_duration_seconds_summary` | summary | (deprecated path) | Same as above, summary form |
| `apiserver_admission_webhook_admission_duration_seconds` | histogram | `name, operation, rejected, type` | Per webhook latency |
| `apiserver_admission_webhook_rejection_count` | counter | `name, operation, type, error_type, rejection_code` | Per-webhook rejection counter |
| `apiserver_admission_webhook_request_total` | counter | `name, type, operation, code` | Per-webhook request counter |
| `apiserver_admission_webhook_fail_open_count` | counter | `name, type` | Counts of fail-open events (failurePolicy=Ignore) |
| `apiserver_admission_match_condition_evaluation_seconds` | histogram | `name, type` | matchConditions CEL latency |
| `apiserver_admission_match_condition_evaluation_errors_total` | counter | `name, type` | matchConditions CEL errors |
| `apiserver_admission_match_condition_exclusions_total` | counter | `name, type` | How many requests matchConditions filtered out |
| `apiserver_admission_webhook_request_filter_dropped_count` | counter | `name, type` | Apiserver-side filter drops |

### 20.2 Recommended alert rules

```yaml
# 1. Any webhook with p95 latency above 100ms — your latency budget violator
- alert: AdmissionWebhookSlow
  expr: |
    histogram_quantile(0.95,
      sum by (name, type, le) (
        rate(apiserver_admission_webhook_admission_duration_seconds_bucket[5m])
      )
    ) > 0.1
  for: 10m
  labels: {severity: warning}
  annotations:
    summary: "Webhook {{ $labels.name }} p95 > 100ms"

# 2. Any webhook timing out — failurePolicy=Fail means cluster is rejecting writes
- alert: AdmissionWebhookTimingOut
  expr: |
    sum by (name) (
      rate(apiserver_admission_webhook_rejection_count{error_type="calling_webhook_error"}[5m])
    ) > 0
  for: 2m
  labels: {severity: critical}
  annotations:
    summary: "Webhook {{ $labels.name }} is timing out / unreachable"

# 3. Fail-open events on webhooks that should be enforcing — silent policy failure
- alert: AdmissionWebhookFailingOpen
  expr: |
    sum by (name) (rate(apiserver_admission_webhook_fail_open_count[5m])) > 0
  for: 5m
  labels: {severity: warning}
  annotations:
    summary: "Webhook {{ $labels.name }} is failing open — policy not enforced"

# 4. Cert expiry — preempt the outage
- alert: WebhookCertExpiringSoon
  expr: |
    certmanager_certificate_expiration_timestamp_seconds - time() < 7*86400
  for: 1h
  labels: {severity: warning}

# 5. CEL match-condition errors — broken policy
- alert: MatchConditionErrors
  expr: |
    sum by (name) (rate(apiserver_admission_match_condition_evaluation_errors_total[5m])) > 0
  for: 5m
  labels: {severity: warning}
```

### 20.3 Useful dashboards

A single "admission health" dashboard should show, per webhook:

- Request rate (`apiserver_admission_webhook_request_total`).
- Latency p50/p95/p99 (`apiserver_admission_webhook_admission_duration_seconds`).
- Rejection rate (`apiserver_admission_webhook_rejection_count`).
- Fail-open count (`apiserver_admission_webhook_fail_open_count`).
- Cert expiry (cert-manager metrics).

Plus a per-policy panel for VAP:

- `apiserver_validating_admission_policy_check_duration_seconds`
- `apiserver_validating_admission_policy_definition_total`
- `apiserver_validating_admission_policy_check_total`

---

## 21. Pitfalls: A Catalog From Real Outages

A consolidated list, ordered by frequency in postmortems:

### 21.1 Cert expiry without rotation

Discussed in §10 and §11. The single most common admission outage.

**Fix:** cert-manager + cainjector + Certificate alerts.

### 21.2 `failurePolicy: Fail` on kube-system

A webhook that didn't intend to inspect kube-system, but didn't exclude it, gets called for apiserver lease renewals. When the webhook is even briefly unavailable (e.g., Pod restart), lease renewal fails, the apiserver loses its lock, and downstream controllers panic.

**Fix:**

```yaml
namespaceSelector:
  matchExpressions:
  - key: kubernetes.io/metadata.name
    operator: NotIn
    values: [kube-system, kube-public, kube-node-lease]
```

### 21.3 Webhook that watches its own targeted resources

Webhook pod label-selects `app: my-webhook`. Webhook rules match Pods. On rolling update, the new Pod can't be created because the webhook (currently shifting replicas) is unreachable.

**Fix:** `namespaceSelector` excludes the webhook's own namespace, or `objectSelector` excludes the webhook's own labels, or webhook is deployed in a dedicated namespace that's always excluded.

### 21.4 Slow webhook with high `timeoutSeconds`

`timeoutSeconds: 30` (max). A 5-second webhook latency × 1000 in-flight Pod creates = 5000 seat-seconds of APF burn. Lease renewals get queued behind, leader elections flap.

**Fix:** `timeoutSeconds: 5`. Profile and fix the webhook if it's actually that slow.

### 21.5 Mutating webhook not returning `patchType`

Returning `patch` without `patchType: JSONPatch` is treated as no-op by the apiserver (the patch is ignored, but the response is otherwise accepted). The bug is silent.

**Fix:** always set `patchType`. Most libraries do this automatically; raw-bytes implementations forget.

### 21.6 Conversion webhook returning wrong UID

A conversion webhook that re-encodes `metadata` and accidentally drops `uid` (or changes it) makes every read of that CRD fail with "object identity mismatch."

**Fix:** preserve all `metadata` fields except `apiVersion`. Most implementations only mutate `spec` / `status` and copy `metadata` verbatim.

### 21.7 Single-replica webhook

A Deployment with one replica. When that pod is evicted (node drain, scale-to-zero), all writes that the webhook applies to fail until a new pod is up.

**Fix:** minimum 3 replicas (anti-affinity across nodes), a PodDisruptionBudget with `minAvailable: 2`, and `topologySpreadConstraints` across zones.

### 21.8 `url` clientConfig pointing at a Service VIP

The apiserver's `url` lookup is *not* via kube-dns or kube-proxy; it uses the apiserver's own resolver. A Service VIP `10.96.0.42` works only because of kube-proxy on the same node as the apiserver — which is not guaranteed. The setup looks fine on a single-node cluster, breaks on multi-node.

**Fix:** use `service:` clientConfig, not `url:`, for in-cluster webhooks.

### 21.9 Auditing via webhook when VAP would do

A team writes a custom validating webhook in Go to *report* violations to a Slack channel. The webhook returns `allowed: true` always; it's purely audit. But every Pod create incurs an RTT and adds latency.

**Fix:** use VAP with `validationActions: [Audit]` and shape your log pipeline to consume audit annotations. Zero RTT, zero new code.

### 21.10 Missing subresource matching

Policy matches `pods`. User runs `kubectl drain`. The eviction API (`pods/eviction`) is not in the rule. Drain succeeds despite policy.

**Fix:** list subresources explicitly. For pods, common ones: `pods/eviction`, `pods/exec`, `pods/portforward`, `pods/proxy`. For deployments: `deployments/scale`, `deployments/status`.

### 21.11 Matching on `*` apiGroups

`apiGroups: ["*"]` matches CRDs the policy author never anticipated, including metrics-server's `metrics.k8s.io`, which causes weird latency on `kubectl top`.

**Fix:** be specific. List exact apiGroups.

### 21.12 Sidecar injection without idempotency

Mutating webhook with `reinvocationPolicy: IfNeeded` that appends a sidecar each call. Result: two sidecars on the second pass.

**Fix:** check for marker label/annotation before mutating. See §6.

### 21.13 ResourceQuota with restrictive admission webhook

ResourceQuota is an in-tree validating plugin. If your *validating* webhook runs in parallel and is slow, ResourceQuota timing is unaffected — fine. But if your *mutating* webhook adds containers to pods, the resource accounting changes — ResourceQuota sees the post-mutation pod and may reject. Users will see "exceeded quota" errors for pods they wrote at well under the limit.

**Fix:** account for webhook mutations when sizing ResourceQuota. Or: enforce limits via VAP at the same level the user wrote them.

### 21.14 CEL expressions with unbounded loops

A CEL expression iterating a CRD list with no `maxItems`. Cost estimator assumes 4 million items, rejects the policy at creation time. Worse: the policy creator's error message doesn't mention the schema — it just says "exceeded cost budget."

**Fix:** add `maxItems` to the CRD schema, or rewrite the expression to bound iteration explicitly.

### 21.15 Trusting `userInfo.username` for authorization

A policy that allows the `cluster-admin` group to bypass checks. But `userInfo.groups` is set by the *authenticator*, and a misconfigured ServiceAccount token reviewer could grant that group. Always check the username against an allowlist tied to specific identities, not group membership.

**Fix:** prefer exact `username` matches over group matches for policy bypasses. Better: don't have bypasses; expose them as separate policies bound to a privileged namespace.

### 21.16 Forgetting that `kubectl apply --server-side` uses different fieldManager

A policy that bypasses mutation for a specific fieldManager (e.g., the istio injector) and a user does `kubectl apply --server-side --field-manager=mine`. The user's update doesn't match the bypass, and a previously-injected sidecar gets touched (or, worse, removed if the user's manifest didn't declare it).

**Fix:** account for SSA semantics in mutation. Use server-side-apply-compatible mutation paths (MutatingAdmissionPolicy with `ApplyConfiguration` patch type does this correctly).

### 21.17 Operating under the assumption that admission catches everything

Admission only fires at the API layer. A pod that was admitted with a baseline-compliant config but then exec'd into and modified at runtime is not caught by admission. **Runtime security (Falco, Tetragon, ch 28) is the complement.**

### 21.18 Two policy engines fighting

Kyverno and Gatekeeper both installed. A pod create goes through both. Each is its own webhook. Both can mutate. Order between them depends on webhook names. The result is occasionally non-deterministic if both engines have policies that overlap.

**Fix:** if you install two policy engines, segregate their concerns (e.g., Kyverno for mutation/generation only; Gatekeeper for validation only). Document the boundary.

### 21.19 ConversionReview that takes >5 seconds

A controller does `List(MyCRD)` at startup; the apiserver calls the conversion webhook with a batch of 10,000 objects; the webhook iterates them synchronously and takes 30 seconds; the controller times out and crashloops.

**Fix:** make conversion webhooks streaming-fast (a single object should convert in <1ms). Batches arrive pre-sized; respond quickly.

### 21.20 Treating the AdmissionReview oldObject as authoritative

For an UPDATE, the apiserver provides both `object` (new) and `oldObject` (existing). A common bug: webhook checks only `object` and ignores `oldObject`. Consequence: a user can transition through a forbidden state with a single PATCH that ends up at an allowed state, defeating the policy. (Less of an issue for end-state checks; very important for transition checks like "spec.replicas cannot decrease.")

**Fix:** for transition policies, compare `object` and `oldObject` explicitly.

---

## 22. TL;DR

- **Admission is a chain inside the apiserver, sitting between AuthZ and storage**, in two phases: mutating (sequential, ordered alphabetically) then validating (parallel). Both phases combine in-tree plugins, external webhooks, and in-process CEL policies.
- **AdmissionReview v1** is the JSON envelope. The request includes the object, oldObject, userInfo, dryRun, options, and a UID the response must echo. The response returns `allowed`, plus (for mutating) a JSON Patch.
- **Mutating webhooks return JSON Patch only** (RFC 6902). Strategic merge patch is a Kubernetes extension used elsewhere, but not in webhook responses.
- **Reinvocation policy `IfNeeded`** lets a mutator be called a second time after later mutators ran, but only once. The webhook MUST be idempotent — appending a sidecar to its own output is a classic bug.
- **failurePolicy: Fail with a wedged webhook = cluster-wide write outage.** The single most common admission incident in production. Defenses: cert-manager, multi-replica deployments, `namespaceSelector` excluding kube-system, `timeoutSeconds: 5`, `matchConditions` filtering early, fail-Ignore for non-critical paths.
- **ValidatingAdmissionPolicy (GA 1.30)** moves CEL inline into the apiserver. Zero RTT, cost-bounded at policy creation, three actions per binding (`Deny` / `Warn` / `Audit`) that compose for safe migrations. Most simple validations belong here, not in webhooks.
- **MutatingAdmissionPolicy (beta)** does the same for mutations via `ApplyConfiguration` or `JSONPatch` CEL output. Will absorb most defaulter webhooks over time.
- **Pod Security Admission** is the in-tree PSP successor: per-namespace labels select one of three profiles (privileged/baseline/restricted) in one of three modes (enforce/warn/audit). Simpler than PSP, less flexible — exceptions need VAP/Kyverno/Gatekeeper layered on top.
- **Kyverno** is YAML-native, handles mutate/validate/generate/cleanup/verifyImages, and has the strongest image-signature story.
- **OPA Gatekeeper** uses Rego via ConstraintTemplate + Constraint, has a built-in audit controller, and is the right pick for cross-resource queries or Rego standardization.
- **CEL is the long-term winner** for validation and most defaulting because the apiserver can statically bound its cost. Reserve webhooks for the genuine cases that need external I/O, complex Go logic, or image-signature verification.
- **Conversion webhooks** (chapter 23 forward-reference) live in the same machinery: same TLS, same caBundle pitfalls, same operational story; but called on every read across versions of a CRD, so performance is in your hot path.
- **Operational alerts that matter**: webhook p95 > 100ms; webhook timeouts > 0; fail-open count > 0; cert-expiry < 7 days; CEL match-condition errors. These five are the difference between detecting an outage in the first minute and the first hour.
- **The mature mental model**: admission is a *write-time gate*, runtime security is a *runtime gate*, and policy engines are the layer that makes both expressible without forking Kubernetes. Most production clusters end up with PSA + one or two engines + a handful of bespoke webhooks for the hard cases. The future is more in-process CEL, fewer custom webhooks, and a shrinking blast radius from the policy layer.
