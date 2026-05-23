# CRDs, Operators, and controller-runtime: A Staff-Level Deep Dive

A staff-engineer reference for the mechanism that lets anyone bolt a typed resource onto the Kubernetes API server without forking it, and the programming model — the Operator pattern, implemented on top of controller-runtime — that turns those resources into actual behaviour. CRDs are the door. Operators are what walks through.

This chapter sits squarely on top of chapter 08 (the controller pattern and client-go), reuses the admission machinery from chapter 06, foreshadows the API aggregation alternative in chapter 24, the GitOps interaction in chapter 31, and the finalizer/GC details in chapter 36. If chapter 08 taught you the *loop*, this chapter teaches you the *object* the loop reconciles when the object is yours.

We walk the CRD spec field by field, the OpenAPI v3 schema and the structural-schema requirement, the four `x-kubernetes-*` extensions that change Kubernetes' semantics, the CEL `x-kubernetes-validations` block (stable in 1.25) versus admission webhooks, the `status` and `scale` subresources (why HPA on a CRD needs the second one), multi-version CRDs and the conversion webhook machinery, the kubebuilder/operator-sdk project layout, controller-runtime's `Manager`/`Reconciler`/`Cache`/`Client`/`builder` stack, finalizer-driven cleanup, the Operator Capability Levels (L1–L5), OLM (`ClusterServiceVersion` + `Subscription` + `CatalogSource` + `OperatorGroup`), OperatorHub, the universe of real-world operators (databases, infra, networking, cloud), Crossplane, multi-cluster operators, envtest/chainsaw/kuttl, and a long list of pitfalls the next operator you write is statistically going to hit.

---

## Table of Contents

1. [Why CRDs Exist](#1-why-crds-exist)
2. [The CRD Object, Field by Field](#2-the-crd-object-field-by-field)
3. [OpenAPI v3 Schema and Structural Schemas](#3-openapi-v3-schema-and-structural-schemas)
4. [CEL Validation: `x-kubernetes-validations`](#4-cel-validation-x-kubernetes-validations)
5. [The `x-kubernetes-*` Extensions](#5-the-x-kubernetes--extensions)
6. [Subresources: `status` and `scale`](#6-subresources-status-and-scale)
7. [`additionalPrinterColumns`](#7-additionalprintercolumns)
8. [Multiple Versions and the Storage Version](#8-multiple-versions-and-the-storage-version)
9. [Conversion: None vs Webhook](#9-conversion-none-vs-webhook)
10. [Conversion Webhook Implementation](#10-conversion-webhook-implementation)
11. [Versioning Strategy: v1alpha1 → v1beta1 → v1](#11-versioning-strategy-v1alpha1--v1beta1--v1)
12. [CRD Storage in etcd](#12-crd-storage-in-etcd)
13. [RBAC for CRDs](#13-rbac-for-crds)
14. [The Operator Pattern](#14-the-operator-pattern)
15. [Operator Capability Levels (L1–L5)](#15-operator-capability-levels-l1l5)
16. [kubebuilder and operator-sdk Scaffolding](#16-kubebuilder-and-operator-sdk-scaffolding)
17. [controller-runtime Layers](#17-controller-runtime-layers)
18. [A Full Reconciler: Spec, Status, Finalizer, Conditions](#18-a-full-reconciler-spec-status-finalizer-conditions)
19. [Watching Dependent Resources: `Owns` and `Watches`](#19-watching-dependent-resources-owns-and-watches)
20. [Webhook-Based Defaulting and Validation](#20-webhook-based-defaulting-and-validation)
21. [OLM: ClusterServiceVersion, Subscription, CatalogSource, OperatorGroup](#21-olm-clusterserviceversion-subscription-catalogsource-operatorgroup)
22. [OperatorHub.io](#22-operatorhubio)
23. [Real-World Operators](#23-real-world-operators)
24. [Crossplane: Compositions and XRDs](#24-crossplane-compositions-and-xrds)
25. [Multi-Cluster Operators](#25-multi-cluster-operators)
26. [Testing Operators](#26-testing-operators)
27. [Performance: Informer Memory, Cache Scoping, Backpressure](#27-performance-informer-memory-cache-scoping-backpressure)
28. [Operator vs Helm](#28-operator-vs-helm)
29. [Pitfalls: The Long List](#29-pitfalls-the-long-list)
30. [TL;DR](#30-tldr)

---

## 1. Why CRDs Exist

Kubernetes won not because of Pods. It won because of CRDs. Pods you can build on any orchestrator; what Kubernetes did that nobody else did at scale is make *extension* a first-class operation. You can add a brand-new typed resource — `PostgresCluster`, `Certificate`, `VirtualService`, `Redis`, `Function`, `Workspace`, `Cluster` — to a running cluster, with full RBAC, full kubectl integration, full watch semantics, full validation, full GC, full versioning, full conversion — without recompiling the apiserver, without restarting it, without anyone's permission except cluster-admin.

The trick is that the apiserver from chapter 03 has, since 1.7, *two* paths from URL to storage:

```
   ┌──────────────────────────────────────────────────────────────────────────┐
   │              How the apiserver answers /apis/<group>/<version>/...        │
   ├──────────────────────────────────────────────────────────────────────────┤
   │                                                                          │
   │     incoming request                                                     │
   │           │                                                              │
   │           ▼                                                              │
   │   ┌──────────────┐                                                       │
   │   │  AuthN/AuthZ │                                                       │
   │   └──────┬───────┘                                                       │
   │          ▼                                                               │
   │   ┌──────────────┐    is the GroupVersion a built-in?                    │
   │   │  Discovery   │────────────────┬───────────────┬─────────────────┐    │
   │   └──────────────┘                │               │                 │    │
   │                              YES (Pod,           │ NO            (ch 24) │
   │                              Deployment,         │ but registered as     │
   │                              Service, ...)       │ a CRD                 │
   │                                   │              │                       │
   │                                   ▼              ▼                       │
   │                          ┌────────────────┐  ┌──────────────────────┐    │
   │                          │ built-in REST  │  │ CRD generic handler  │    │
   │                          │ storage        │  │ (apiextensions)      │    │
   │                          │ (typed,        │  │ Unstructured JSON    │    │
   │                          │  protobuf)     │  │ in etcd              │    │
   │                          └────────┬───────┘  └──────────┬───────────┘    │
   │                                   │                     │                │
   │                                   └─────────┬───────────┘                │
   │                                             ▼                            │
   │                                       admission chain (ch 06)            │
   │                                             │                            │
   │                                             ▼                            │
   │                                          etcd (ch 04)                    │
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘
```

The third path — *aggregation* — is chapter 24. CRDs and aggregation are the two extension surfaces; CRDs are 95% of what people actually need, aggregation is what you reach for when you need a different storage backend or you are implementing something that does not fit the JSON-in-etcd model (metrics-server, a Git-backed inventory, an external policy engine). For everything else: CRDs.

**The CRD social contract.**

1. *Anyone can define a new type.* No PR to kubernetes/kubernetes, no Kubernetes release cycle, no API review meeting. `kubectl apply -f crd.yaml` and the type exists.
2. *The new type works with every existing client.* `kubectl get`, `kubectl describe`, `kubectl patch`, `kubectl explain`, server-side apply, RBAC, namespacing, labels, annotations, finalizers, ownerReferences, Garbage Collection — all of it. Free.
3. *The new type has a schema.* OpenAPI v3, structural, enforced server-side. Optionally CEL for cross-field invariants.
4. *The new type has a controller.* The CRD does nothing by itself — it is data. A controller, written in Go (or anything that speaks the API), watches it and reconciles it to real-world side effects. That controller is the *operator*.
5. *The user pays for the schema.* If you ship a bad schema, you cannot break it later without a conversion webhook. There is no `ALTER TABLE`.

This is the loop from chapter 08 applied recursively: the platform team adds a new object type, and now everyone in the cluster can author that object, and a controller drives it. The kernel of Kubernetes (apiserver + etcd + watch) didn't have to learn anything new.

### 1.1 Extend without forking

Before CRDs (and before their predecessor, ThirdPartyResources, which were removed in 1.8), the only way to add behavior to Kubernetes was to either run a sidecar daemon that watched ConfigMaps (a pattern still seen in some legacy software) or fork the apiserver. Forking was a dead end: every upstream release had to be rebased, and your fork could never participate in the wider ecosystem because nobody else was running your apiserver.

CRDs make extension *additive*. The upstream apiserver does not need to know that your CRD exists; it discovers it from etcd at startup and registers the generic handler at the appropriate path. Two operators from two different vendors can coexist as long as their CRDs do not collide on group+name.

The downside, foreshadowed in the OLM section, is that *uninstall* is hard, multiple operators can compete to own the same CRD, and CRD upgrades across versions are subtle. Most of this chapter is about handling that.

---

## 2. The CRD Object, Field by Field

The CRD itself is a Kubernetes object, in the group `apiextensions.k8s.io/v1`. The apiserver ships with a built-in controller (the *apiextensions* controller) that watches CRDs and, for each one, registers a generic REST handler at the right URL.

Here is a complete, production-shaped CRD. We will dissect every field.

```yaml
# crd-postgrescluster.yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: postgresclusters.db.example.com  # MUST be <plural>.<group>
spec:
  group: db.example.com
  scope: Namespaced                      # or Cluster
  names:
    plural: postgresclusters
    singular: postgrescluster
    kind: PostgresCluster
    listKind: PostgresClusterList
    shortNames: [pgc, pgcluster]
    categories: [databases, all]
  versions:
    - name: v1beta1
      served: true
      storage: false                     # served but not the storage version
      deprecated: true
      deprecationWarning: "db.example.com/v1beta1 is deprecated; use v1"
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [version, replicas]
              properties:
                version:
                  type: string
                  enum: ["13","14","15","16"]
                replicas:
                  type: integer
                  minimum: 1
                  maximum: 9
                  default: 1
                storage:
                  type: object
                  required: [size]
                  properties:
                    size:
                      type: string
                      pattern: '^[0-9]+(Mi|Gi|Ti)$'
                    storageClassName:
                      type: string
            status:
              type: object
              properties:
                phase:
                  type: string
                readyReplicas:
                  type: integer
                observedGeneration:
                  type: integer
                conditions:
                  type: array
                  items:
                    type: object
                    required: [type, status, lastTransitionTime, reason]
                    properties:
                      type:           { type: string }
                      status:         { type: string, enum: ["True","False","Unknown"] }
                      lastTransitionTime: { type: string, format: date-time }
                      reason:         { type: string }
                      message:        { type: string }
                      observedGeneration: { type: integer }
      subresources:
        status: {}
        scale:
          specReplicasPath:     .spec.replicas
          statusReplicasPath:   .status.readyReplicas
          labelSelectorPath:    .status.selector
      additionalPrinterColumns:
        - name: Version
          jsonPath: .spec.version
          type: string
        - name: Replicas
          jsonPath: .spec.replicas
          type: integer
        - name: Ready
          jsonPath: .status.readyReplicas
          type: integer
        - name: Phase
          jsonPath: .status.phase
          type: string
        - name: Age
          jsonPath: .metadata.creationTimestamp
          type: date

    - name: v1
      served: true
      storage: true                      # exactly one across versions
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [version, instances]
              properties:
                version:
                  type: string
                  enum: ["14","15","16","17"]
                instances:
                  type: array
                  minItems: 1
                  maxItems: 9
                  items:
                    type: object
                    required: [name]
                    properties:
                      name:    { type: string, pattern: '^[a-z0-9-]{1,32}$' }
                      replicas:{ type: integer, minimum: 1, default: 1 }
                      storage:
                        type: object
                        required: [size]
                        properties:
                          size:               { type: string, pattern: '^[0-9]+(Mi|Gi|Ti)$' }
                          storageClassName:   { type: string }
                  x-kubernetes-list-type: map
                  x-kubernetes-list-map-keys: [name]
                backup:
                  type: object
                  properties:
                    schedule: { type: string }
                    s3:
                      type: object
                      required: [bucket]
                      properties:
                        bucket: { type: string }
                        region: { type: string, default: us-east-1 }
              x-kubernetes-validations:
                - rule: "self.instances.size() <= 9"
                  message: "at most 9 instances per cluster"
                - rule: "!has(self.backup) || has(self.backup.s3)"
                  message: "backup requires s3 destination"
                - rule: "self.version in ['14','15','16','17']"
                  messageExpression: "'unsupported version: ' + self.version"
            status:
              type: object
              properties:
                phase:              { type: string }
                readyInstances:     { type: integer }
                observedGeneration: { type: integer }
                selector:           { type: string }
                conditions:
                  type: array
                  x-kubernetes-list-type: map
                  x-kubernetes-list-map-keys: [type]
                  items:
                    type: object
                    required: [type, status, lastTransitionTime]
                    properties:
                      type:           { type: string }
                      status:         { type: string, enum: ["True","False","Unknown"] }
                      lastTransitionTime: { type: string, format: date-time }
                      reason:         { type: string }
                      message:        { type: string }
                      observedGeneration: { type: integer }
      subresources:
        status: {}
        scale:
          specReplicasPath:     .spec.instances[0].replicas
          statusReplicasPath:   .status.readyInstances
          labelSelectorPath:    .status.selector
      additionalPrinterColumns:
        - name: Version
          jsonPath: .spec.version
          type: string
        - name: Instances
          jsonPath: .spec.instances[*].name
          type: string
        - name: Ready
          jsonPath: .status.readyInstances
          type: integer
        - name: Phase
          jsonPath: .status.phase
          type: string
        - name: Age
          jsonPath: .metadata.creationTimestamp
          type: date

  conversion:
    strategy: Webhook
    webhook:
      conversionReviewVersions: [v1]
      clientConfig:
        service:
          namespace: db-system
          name: pg-operator-webhook
          path: /convert
          port: 443
        caBundle: <base64 PEM>
```

That object alone, applied with `kubectl apply`, gives you a brand-new typed resource at `/apis/db.example.com/v1/namespaces/<ns>/postgresclusters` (and the same for v1beta1), with full kubectl behaviour, RBAC, validation, two subresources, six printer columns, conversion between two versions, and a default value on `replicas`. No controller yet — that comes later — but the *shape* of the resource exists and the apiserver enforces it.

### 2.1 `metadata.name`

Must be `<plural>.<group>`. The apiextensions controller checks this. The reason: this is the only piece of metadata the apiserver uses to *route* the URL `/apis/<group>/<version>/<plural>` to this CRD, and it wants a canonical name to look up.

### 2.2 `spec.group`

The API group. Conventional choices:

- A reverse-DNS domain you own: `db.example.com`, `monitoring.coreos.com`, `cert-manager.io`. **Always pick a domain you own.** A future ecosystem you cannot foresee may collide with you on a generic-looking group like `app.com`.
- *Never* use `*.k8s.io` or `*.kubernetes.io` unless you are part of the Kubernetes project. Those are reserved.

### 2.3 `spec.scope`

`Namespaced` or `Cluster`. Cluster-scoped CRDs (`ClusterIssuer`, `StorageClass`, `ClusterRole`) live outside any namespace; namespaced ones (the vast majority — `Certificate`, `PostgresCluster`, `VirtualService`) live inside one and inherit namespace-based RBAC and quotas.

Pick `Namespaced` by default. Only go `Cluster` when the resource genuinely has no owning tenant (cluster-wide configuration, infrastructure that crosses namespaces).

### 2.4 `spec.names`

```
plural:    URL segment           /apis/g/v/postgresclusters
singular:  kubectl singular form kubectl get postgrescluster pgc-1
kind:      Go type / Kind field  kind: PostgresCluster
listKind:  Go list type          kind: PostgresClusterList
shortNames: kubectl shortcuts    kubectl get pgc
categories: kubectl get groups   kubectl get all  (if 'all' is listed)
```

Conventions: `plural` is lowercase, kebab-case; `kind` is UpperCamelCase; `listKind` is `<Kind>List`. The plural is the canonical URL segment and *cannot* be safely changed after a CRD has been used in production — old clients will 404 against the new URL.

### 2.5 `spec.versions[]`

A list of versions of the resource. Each one is independently *served* and *one* of them is the *storage* version. Detailed in §8.

### 2.6 `spec.conversion`

How to convert between versions. `strategy: None` works only when the versions are structurally identical except for trivial differences (which in practice means "you're not really versioning"). `strategy: Webhook` invokes a conversion webhook. Detailed in §9.

### 2.7 What the apiextensions controller does

When this CRD lands in etcd, the in-process apiextensions controller in kube-apiserver:

1. Validates the CRD itself (no, you cannot define a CRD with no schema in v1).
2. Registers the OpenAPI schema(s) into the global OpenAPI document the apiserver serves at `/openapi/v2` and `/openapi/v3`.
3. Wires up a generic REST handler in the per-CRD strategy that stores Unstructured JSON in etcd at `/registry/<group>/<plural>/<namespace>/<name>`.
4. Updates the Discovery document so `kubectl api-resources` knows the new type.
5. Sets `Established: True` on the CRD's status once the URLs are answering. Until then, `kubectl get postgrescluster` returns 404.

This is the *exact* same controller pattern from chapter 08, applied to CRDs themselves. The CRD is data; the apiextensions controller is the controller.

---

## 3. OpenAPI v3 Schema and Structural Schemas

The CRD's `spec.versions[].schema.openAPIV3Schema` is what makes a CRD more than "anything goes JSON". It is an OpenAPI v3 schema document — a recursive description of object/array/string/integer/boolean shapes — that the apiserver evaluates *on every write* before the object reaches etcd.

### 3.1 What you can express

```yaml
type: object                  # object | array | string | integer | number | boolean
required: [name, replicas]
properties:
  name:
    type: string
    minLength: 1
    maxLength: 253
    pattern: '^[a-z0-9-]+$'
  replicas:
    type: integer
    minimum: 1
    maximum: 100
    default: 1                # populated server-side if missing
  ratio:
    type: number
    format: double            # informational; openapi formats
    exclusiveMaximum: true
    maximum: 1.0
  mode:
    type: string
    enum: [primary, replica]
    default: primary
  tags:
    type: array
    items: { type: string }
    minItems: 0
    maxItems: 64
    uniqueItems: true
  config:
    type: object
    additionalProperties:
      type: string            # arbitrary string→string map (Like a label map)
```

### 3.2 The structural schema requirement

Since 1.16, every served CRD version *must* have a **structural schema**. A structural schema is one that:

1. For every object node, *all* its allowed fields are explicitly listed under `properties` (or `additionalProperties` is used for "any extra fields are these"). You cannot have implicit fields.
2. The same property names cannot appear under `properties`, `patternProperties`, etc. inside `allOf`/`anyOf`/`oneOf`/`not`.
3. `type` is set at every level (or `x-kubernetes-preserve-unknown-fields: true` is set to escape it, see §5.3).
4. `additionalProperties` and `properties` are not used together except in narrow ways.

The reason is that the apiserver needs a deterministic schema to do *pruning* (drop fields that aren't in the schema), *defaulting* (populate fields with `default:`), validation, and conversion. Non-structural schemas make those operations ambiguous.

The error you get when you violate the rule is usually:

```
The CustomResourceDefinition "..." is invalid: spec.validation.openAPIV3Schema: NotSupported:
  must only have "properties", "required" or "description" at the root of the schema
```

or

```
spec.versions[0].schema.openAPIV3Schema.properties[spec].properties[foo]:
  Required value: must have a type
```

Once the schema is structural, the apiserver also does **pruning**: any field in the user's submission that is not in the schema is silently dropped before being persisted. This is the opposite of what most users expect ("I sent it, I assumed it was kept"), but it's essential — without pruning, every typo would land in etcd and stay there forever.

```
   User POST:                                  Stored in etcd:
   {                                           {
     spec: {                                     spec: {
       version: "15",                              version: "15",
       relicas: 3,            ◄── typo!            // dropped: not in schema
       replicas: 1                                 replicas: 1
     }                                           }
   }                                           }
```

If you want to opt out of pruning for a subtree — say, a field that holds a free-form JSON blob — you use `x-kubernetes-preserve-unknown-fields: true` (§5.3).

### 3.3 Defaults

`default:` populates a field that the user omitted. Defaults run *after* validation of the user's input but *before* admission webhooks see the object, so admission webhooks see the defaulted form. They run on writes; they do not retroactively populate existing stored objects (a stored object that was created before the default was added stays without the field).

```yaml
properties:
  replicas:
    type: integer
    minimum: 1
    default: 1
```

This means a user who omits `spec.replicas` gets `spec.replicas: 1` written to etcd. Without `default:`, the field would be absent in storage (and a controller reading it would have to handle the missing-field case).

### 3.4 OpenAPI v3 served at /openapi/v3

The apiserver merges every CRD's schema into the OpenAPI v3 document it serves. This is what enables `kubectl explain postgrescluster.spec.instances` to print field descriptions, what kubectl uses for client-side validation, and what generators like `client-go`'s applyconfiguration use to build typed clients.

A common pitfall: if your `description:` strings on each field are missing or stale, `kubectl explain` is useless. Treat schema descriptions like docstrings.

---

## 4. CEL Validation: `x-kubernetes-validations`

OpenAPI says "this field is an integer between 1 and 9." That works for one field at a time. The moment you want a *cross-field* invariant — "if `mode` is `replica`, then `primary` must be set" — OpenAPI can't help you. Before 1.25 you had to write an admission webhook for that. Since 1.25 (stable), CEL — the Common Expression Language — is available directly inside the CRD via `x-kubernetes-validations`.

### 4.1 What CEL looks like

```yaml
openAPIV3Schema:
  type: object
  properties:
    spec:
      type: object
      required: [mode, replicas]
      properties:
        mode:     { type: string, enum: [primary, replica] }
        primary:  { type: string }
        replicas: { type: integer, minimum: 1, maximum: 9 }
        instances:
          type: array
          items: { type: object, properties: { name: { type: string } } }
          x-kubernetes-list-type: map
          x-kubernetes-list-map-keys: [name]
      x-kubernetes-validations:
        - rule: "self.mode == 'primary' || has(self.primary)"
          message: "replica mode requires .spec.primary to be set"
        - rule: "self.instances.size() <= self.replicas"
          message: "instances cannot exceed replicas"
          messageExpression: |
            'have ' + string(self.instances.size())
            + ' instances but only ' + string(self.replicas) + ' replicas allowed'
        - rule: "self.instances.all(i, i.name.matches('^[a-z0-9-]+$'))"
          message: "instance names must be DNS-safe"
        - rule: "oldSelf == null || self.mode == oldSelf.mode"
          message: ".spec.mode is immutable"
```

Key features:

- `self` refers to the value at the schema position where the rule is attached (here, `spec`).
- `oldSelf` is the *previous* value on Update — enabling immutability constraints without webhooks.
- `messageExpression` is a CEL expression that computes the error message dynamically (used to show what's wrong).
- Rules can be attached at *any* level of the schema, not just the root. `self` re-roots accordingly.
- The full CEL spec is allowed: `has()`, `.matches()`, `.size()`, list/map operations, set membership, arithmetic.

### 4.2 Per-field CEL

You can attach CEL rules to specific fields, where `self` is the field value:

```yaml
properties:
  retentionDays:
    type: integer
    x-kubernetes-validations:
      - rule: "self >= 1 && self <= 365"
        message: "retentionDays must be 1..365"
      - rule: "oldSelf == null || self >= oldSelf"
        message: "retentionDays may only increase"
```

### 4.3 CEL vs admission webhooks

| Concern                  | CEL (`x-kubernetes-validations`)              | Validating admission webhook                          |
|--------------------------|-----------------------------------------------|--------------------------------------------------------|
| Latency                  | In-process. Sub-millisecond.                  | Network call. 1–50ms typical.                          |
| Failure modes            | None except cluster-admin-broken config.      | Webhook down → policy.failurePolicy decides.           |
| Side effects             | Pure. Cannot read other objects.              | Can call out to any service, but slow and risky.       |
| Logic                    | Expression-only.                              | Arbitrary Go (or whatever).                            |
| Cross-object             | No — only this object and its old form.       | Yes — the webhook can read the apiserver.              |
| Immutability             | Yes (oldSelf).                                 | Yes.                                                   |
| Cross-namespace          | No.                                            | Yes (with care).                                       |
| Upgrade story            | Bundled with CRD.                             | Webhook server lifecycle, certs, plumbing.             |
| Skippable                | No — runs always.                              | Can have failurePolicy: Ignore (often a footgun).      |

**Rule of thumb:** every invariant you can express in CEL, express in CEL. Reach for a webhook only when you need to consult other objects, do complex defaulting beyond what `default:` can do, or run business logic that can't fit in CEL. The chapter on admission webhooks (ch 06) covers the latter case.

CEL is also the same language used by `ValidatingAdmissionPolicy` (chapter 06), so reusing it inside CRDs keeps the cognitive load down: one language for two extension points.

### 4.4 CEL cost limits

CEL execution is bounded — the apiserver imposes a runtime cost limit (configurable per rule with `x-kubernetes-validations[].reason` and `messageExpression`, and capped globally) so that no validation can spend more than a few ms. If you write a rule that loops over a huge array, you may hit the budget and the rule will be rejected at validation time. Keep rules linear in input size or smaller.

---

## 5. The `x-kubernetes-*` Extensions

OpenAPI v3 is a generic schema language. To make it work for Kubernetes' semantics — Server-Side Apply, list merging, defaulting, escape hatches — the schema is annotated with a small set of `x-kubernetes-*` extensions. Three of them change behaviour you must understand.

### 5.1 `x-kubernetes-list-type`: how lists merge

This is the most consequential extension you will ever set. It tells the apiserver — and SSA in particular — *how to interpret* a list.

| Value     | Semantics                                                                                         |
|-----------|----------------------------------------------------------------------------------------------------|
| `atomic`  | The list is a single value. Any apply replaces the entire list. Order is preserved.                |
| `set`     | The list is a set of scalars. Items have no order. Apply unions the values.                        |
| `map`     | The list is a map keyed by `x-kubernetes-list-map-keys`. Items are merged by key.                  |

The default *if you don't set it* is `atomic`, which is almost always wrong for any list of objects you expect controllers and users to share authorship of.

```yaml
# Wrong: two field managers fight over the entire array.
ports:
  type: array
  items:
    type: object
    properties:
      port: { type: integer }
      name: { type: string }

# Right: ports merge by name.
ports:
  type: array
  x-kubernetes-list-type: map
  x-kubernetes-list-map-keys: [name]
  items:
    type: object
    required: [name]
    properties:
      name: { type: string }
      port: { type: integer }
```

With `map`, two clients applying different port entries (one by an Ingress controller, one by the user) will both be preserved. With `atomic`, the second apply silently overwrites the first.

The `conditions` slice in status is *always* `map` keyed by `type`. Forgetting this on a custom condition list will guarantee that two reconcilers stomp each other.

For a list of scalars where order does not matter (`finalizers`, label values), `set` is the right answer. For a list of scalars where order *does* matter (`args` to a container), `atomic` is correct.

### 5.2 SSA semantics

Server-Side Apply (SSA, chapter on api basics + ch 08) tracks ownership by field manager. The CRD schema's `x-kubernetes-list-type` is what tells SSA how to compute the *granularity* of ownership inside a list.

```
   atomic:   one ownership token for the whole list
   set:      one ownership token per scalar value
   map:      one ownership token per map-key
```

A controller doing `client.Apply(...)` against a `map` list with `x-kubernetes-list-map-keys: [name]` will own only the entries it submits, and any other field manager (the user, GitOps, another controller) can own other entries. With `atomic`, the controller owns the whole list and writing to it strips out anything anyone else owned.

This is exactly why operator authors who never set `x-kubernetes-list-type` end up writing controllers that fight with GitOps engines (chapter 31): every apply by either side wipes the other side's contribution. Setting `map` everywhere a list of objects appears is the single highest-impact thing you can do for SSA compatibility.

### 5.3 `x-kubernetes-preserve-unknown-fields`

The structural schema requirement forces you to enumerate every field. Sometimes you genuinely want a sub-object to be a free-form JSON blob — e.g., a `helmValues` field, a `customConfig` that maps to a YAML config file someone else owns. The escape hatch:

```yaml
properties:
  helmValues:
    type: object
    x-kubernetes-preserve-unknown-fields: true
```

Now any keys under `helmValues` will be kept verbatim — pruning is disabled for that subtree. The cost: there is no structural schema for that subtree, so you cannot use `default:` or `x-kubernetes-validations` inside it, and SSA falls back to atomic semantics. Use it sparingly and only where you really do mean "anything goes."

### 5.4 `x-kubernetes-int-or-string`

The `targetPort` pattern — a field that accepts either an integer (a port number) or a string (a named port) — exists in many built-in types. CRDs can express it:

```yaml
properties:
  targetPort:
    x-kubernetes-int-or-string: true
```

The apiserver will accept either `targetPort: 8080` or `targetPort: "http"` and store whichever the user sent. Your Go client will see the field as an `intstr.IntOrString` (from `k8s.io/apimachinery/pkg/util/intstr`), and your reconciler unpacks it via `IntValue()` or `StrVal`.

### 5.5 `x-kubernetes-embedded-resource`

Tells the apiserver that this field embeds another Kubernetes object (with its own `apiVersion`, `kind`, `metadata`). The apiserver will validate that those fields are present and the metadata is structurally valid. Used in CRDs that wrap other resources (a `Workload` CRD that embeds a PodSpec, for example):

```yaml
template:
  type: object
  x-kubernetes-embedded-resource: true
  x-kubernetes-preserve-unknown-fields: true
```

### 5.6 `x-kubernetes-validations` (already covered in §4)

This is also an extension, even though it has its own section here because of how consequential CEL is.

---

## 6. Subresources: `status` and `scale`

A "subresource" is an alternate REST endpoint for the same object, with different verbs, RBAC, and write semantics. CRDs support two: `status` and `scale`.

### 6.1 The `status` subresource

```yaml
subresources:
  status: {}
```

When you enable this, three things happen:

1. A new endpoint appears at `/apis/g/v/.../<name>/status`. Writes to this URL update *only* the object's `.status` subtree.
2. Writes to the *main* URL (`/apis/g/v/.../<name>`) ignore any `.status` field the user submits. The user cannot write status.
3. The object's `.metadata.generation` is incremented only when `.spec` changes (writes to the main URL); status updates do not bump it.

This is the substrate of the `spec`/`status` discipline from chapter 08:

```
   ┌─────────────────────────────────────────────────────────────────┐
   │           Main URL (PUT/PATCH/APPLY): writes spec only          │
   │              ↓                                                  │
   │              .metadata.generation++ when spec actually changes  │
   │              ↓                                                  │
   │              .status changes here are IGNORED                   │
   │                                                                 │
   │           /status URL (PUT/PATCH): writes status only           │
   │              ↓                                                  │
   │              .spec is read-only here                            │
   │              ↓                                                  │
   │              .metadata.generation is NOT incremented            │
   └─────────────────────────────────────────────────────────────────┘
```

**RBAC implication.** The user gets `update` on `postgresclusters` (the main verb). The controller gets `update` on `postgresclusters/status` (the subresource). They are separate RBAC verbs:

```yaml
- apiGroups: [db.example.com]
  resources: [postgresclusters]
  verbs: [get, list, watch, create, update, patch, delete]
- apiGroups: [db.example.com]
  resources: [postgresclusters/status]
  verbs: [get, update, patch]
- apiGroups: [db.example.com]
  resources: [postgresclusters/finalizers]
  verbs: [update]
```

This is how you stop users from forging status (claiming the cluster is healthy when it isn't) and stop the operator from accidentally rewriting spec.

**Status bypasses spec validation.** When the operator writes status, the apiserver does not re-run the spec's CEL rules or OpenAPI validation against the spec. Status has its own schema validation if you supply one, but it cannot fail because of a spec field. This is important for "broken spec" cases — if a user has set an unsatisfiable spec, the operator still has to write `status.conditions[type=Ready, status=False]` even though spec is invalid.

### 6.2 The `scale` subresource

```yaml
subresources:
  scale:
    specReplicasPath:   .spec.replicas
    statusReplicasPath: .status.readyReplicas
    labelSelectorPath:  .status.selector  # optional but needed for HPA
```

The `scale` subresource is what lets HPA, `kubectl scale`, and external autoscalers operate on your CRD. With it, `kubectl scale postgrescluster/pgc-1 --replicas=3` works against your CRD the same way as against a Deployment.

Three JSONPath strings tell the apiserver how to map the *generic* Scale object (which has `.spec.replicas`, `.status.replicas`, `.status.selector`) onto *your* CRD's actual field layout.

- `specReplicasPath` — where to write the new replica count when someone calls /scale.
- `statusReplicasPath` — where to read the current replica count from for /scale GETs.
- `labelSelectorPath` — where to read the LabelSelector string. **HPA requires this.** Without it, HPA cannot find the Pods to scrape metrics from, and `kubectl autoscale postgrescluster` will fail.

The selector path must point to a *string* field containing a serialised label selector (`"app=postgres,cluster=pgc-1"`). The controller is responsible for writing that string to status; it's typically just `metav1.FormatLabelSelector(generatedSelector)`.

### 6.3 What HPA does with the scale subresource

```
   kubectl autoscale postgrescluster/pgc-1 --min=1 --max=5 --cpu-percent=70
                                  │
                                  ▼
                  ┌──────────────────────────────┐
                  │  HPA controller (ch 12)      │
                  │                              │
                  │  every 15s:                  │
                  │  1. GET /scale → replicas    │
                  │  2. read pod metrics by      │
                  │     status.selector          │
                  │  3. compute desired replicas │
                  │  4. PUT /scale.spec.replicas │
                  └──────────────────────────────┘
                                  │
                                  ▼
                  apiserver writes via specReplicasPath
                                  │
                                  ▼
                  your controller sees Spec change
                                  │
                                  ▼
                  reconciles to new replica count
```

This is one of the highest-leverage features in CRDs. Adding the scale subresource and three JSONPath strings unlocks all of Kubernetes' autoscaling ecosystem for a custom resource.

### 6.4 Subresources and generation

`.metadata.generation` is bumped on writes to the main URL. Writes to `/status` do not bump it. The /scale subresource writes via `specReplicasPath` *do* count as spec writes (because they go through the main store's spec), so generation is incremented.

This is the whole basis for the `status.observedGeneration` pattern (chapter 08): the controller records, in status, the generation it last reconciled. Anyone reading the object can tell whether status is up to date by comparing `metadata.generation` with `status.observedGeneration`.

---

## 7. `additionalPrinterColumns`

```yaml
additionalPrinterColumns:
  - name: Ready
    jsonPath: .status.readyReplicas
    type: integer
    description: Number of ready instances
  - name: Phase
    jsonPath: .status.phase
    type: string
    priority: 0     # show by default
  - name: Version
    jsonPath: .spec.version
    type: string
    priority: 0
  - name: NodeSelector
    jsonPath: .spec.nodeSelector
    type: string
    priority: 1     # only show with -o wide
  - name: Age
    jsonPath: .metadata.creationTimestamp
    type: date      # apiserver formats relative
```

What `kubectl get postgrescluster` will display. Columns with `priority: 0` show always; `priority: 1` only with `-o wide`. The `type: date` field is special-cased to render as "3d2h" relative to now, just like the built-in Age column.

Five columns is roughly the right number. Pick fields that a sysadmin would want to scan: phase, readiness, age, version, and one or two distinguishing characteristics. Avoid putting raw status conditions here (they're too verbose); put the *derived* phase ("Ready" / "Reconciling" / "Failed").

This is also where you debug whether your status fields are even being written. If `Ready` shows `<none>` for all your clusters, your reconciler isn't writing `.status.readyReplicas`.

---

## 8. Multiple Versions and the Storage Version

A CRD can serve multiple versions of the resource simultaneously. Every served version is fully usable by clients; *one* of them is the storage version that the apiserver writes to etcd.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                                                                     │
   │   GET /apis/g/v1beta1/...   GET /apis/g/v1/...   GET /apis/g/v2/... │
   │           │                       │                       │         │
   │           ▼                       ▼                       ▼         │
   │   ┌──────────────────────────────────────────────────────────────┐  │
   │   │                  CRD generic handler                          │  │
   │   └──────────────────────────────────────────────────────────────┘  │
   │           │                       │                       │         │
   │           └─────────┬─────────────┴────────────┬──────────┘         │
   │                     ▼                          ▼                    │
   │            convert to storage version v1   read from etcd           │
   │                     │                          │                    │
   │                     ▼                          ▼                    │
   │            ┌────────────────────────────────────────┐               │
   │            │             etcd                       │               │
   │            │  /registry/g/postgresclusters/...      │               │
   │            │  stored as JSON of v1 (storage ver)   │               │
   │            └────────────────────────────────────────┘               │
   │                     ▲                                               │
   │                     │ on the way back, convert to the version       │
   │                     │ the client asked for                          │
   │                     │                                               │
   │             ┌───────┴───────────┐                                   │
   │             ▼                   ▼                                   │
   │        v1beta1 response    v2 response                              │
   │                                                                     │
   └─────────────────────────────────────────────────────────────────────┘
```

### 8.1 Per-version fields

Every entry in `spec.versions[]` has:

```yaml
- name: v1
  served: true              # is this version reachable at all?
  storage: true             # is this THE storage version? exactly one!
  deprecated: false
  deprecationWarning: ""
  schema: { openAPIV3Schema: ... }
  subresources: { status: {}, scale: {...} }
  additionalPrinterColumns: [...]
```

Crucial properties:

- **Exactly one version has `storage: true` at any time.** The apiextensions controller refuses CRDs with zero or multiple storage versions.
- **Every version can have a *different* schema, set of subresources, set of printer columns.** They are independent.
- **`served: false`** means the URL is gone for that version, but the *stored* objects (if any were written under that version) are still in etcd as that version's bytes; you need to migrate them.
- **`deprecated: true`** causes the apiserver to emit a warning header on every response for that version. `deprecationWarning` is the text. Clients (kubectl, k9s, controllers) print it.

### 8.2 Schema differences between versions

Versions exist precisely to let the schema *evolve*. v1beta1 may have a `replicas` integer; v1 may have an `instances` array. The conversion strategy (§9) is what bridges the two.

You can also use multiple versions with the *same* schema but different `subresources` or `additionalPrinterColumns` — for instance, you added the `scale` subresource in v1 but not v1beta1. The storage representation is the same, so conversion is trivial (`None`).

### 8.3 Storage version migration

Changing the storage version is a *write* operation across every existing object. There is no automatic re-write when you flip the `storage:` flag. To migrate:

1. Promote the new version: set `storage: true` on the new version, `storage: false` on the old one. The CRD now serves both, and new writes go to the new format.
2. Wait. Existing objects in etcd are still in the old bytes format. The apiserver will convert them on read.
3. Run the **storage version migrator** (`storage-version-migrator`, a separate project) which lists every object and `kubectl get -o yaml | kubectl apply -f -` (effectively) to rewrite them in the new storage version.
4. Once migration is complete and you have verified every object is in the new format, mark the old version `served: false` and remove its schema in a later release.

Skipping step 3 means that years later, when you finally remove the old version, you discover etcd still has objects in the old format that the new apiserver can't deserialize. This is one of the silent CRD upgrade footguns.

The `status.storedVersions` field on the CRD records which versions are present in etcd. The apiserver maintains it; do not modify it manually.

---

## 9. Conversion: None vs Webhook

The CRD's `spec.conversion` block tells the apiserver how to convert between versions when (a) reading from etcd in version X but the client requested version Y, or (b) writing in version X when the storage version is Y.

```yaml
conversion:
  strategy: None
```

or

```yaml
conversion:
  strategy: Webhook
  webhook:
    conversionReviewVersions: [v1]
    clientConfig:
      service:
        namespace: db-system
        name: pg-operator-webhook
        path: /convert
        port: 443
      caBundle: <base64>
```

### 9.1 `strategy: None`

The trivial strategy. The apiserver assumes that *every served version is identical at the JSON level*, except for the `apiVersion` string. If you have two versions that differ only in printer columns, subresources, deprecation status, or *no* schema-level changes at all, `None` is correct.

This is a stricter requirement than people realize. Adding *any* new required field to v1, or renaming any field, or changing types, breaks `None`. The apiserver will happily route a v1beta1 object through and serve it back as v1 with the same bytes — and your v1 client will see fields that don't match v1's schema.

Use `None` only when you genuinely have two synonymous versions (typically during a deprecation grace period).

### 9.2 `strategy: Webhook`

You run an HTTPS service. The apiserver posts a `ConversionReview` to it for every read/write that crosses a version boundary. The webhook returns the converted objects. Detail in §10.

The webhook is on the critical read path for *every API call* to a non-storage-version URL. It must be fast (single-digit ms) and reliable. Webhook downtime breaks reads of objects.

### 9.3 Conversion review wire format

```json
// POST /convert
{
  "apiVersion": "apiextensions.k8s.io/v1",
  "kind": "ConversionReview",
  "request": {
    "uid": "8e9cf5f9-...",
    "desiredAPIVersion": "db.example.com/v1",
    "objects": [
      {
        "apiVersion": "db.example.com/v1beta1",
        "kind": "PostgresCluster",
        "metadata": { "name": "pgc-1", "namespace": "default" },
        "spec":   { "version": "15", "replicas": 3, "storage": { "size": "10Gi" } },
        "status": { "phase": "Ready" }
      }
    ]
  }
}
```

Response:

```json
{
  "apiVersion": "apiextensions.k8s.io/v1",
  "kind": "ConversionReview",
  "response": {
    "uid": "8e9cf5f9-...",
    "result": { "status": "Success" },
    "convertedObjects": [
      {
        "apiVersion": "db.example.com/v1",
        "kind": "PostgresCluster",
        "metadata": { "name": "pgc-1", "namespace": "default" },
        "spec": {
          "version": "15",
          "instances": [
            { "name": "default", "replicas": 3, "storage": { "size": "10Gi" } }
          ]
        },
        "status": { "phase": "Ready" }
      }
    ]
  }
}
```

`uid` must match the request. `convertedObjects` must be in the same order, same length as `objects`. The conversion is *batched*: the apiserver may send many objects at once (a List call) and you must convert all of them.

---

## 10. Conversion Webhook Implementation

A conversion webhook is conceptually simple — a pure function on JSON. The traps are operational.

### 10.1 The conversion graph

You have N versions, conversions can go in either direction, and the apiserver does not promise an N×N matrix — it expects you to *hub* through the storage version.

```
   Common pattern: hub-and-spoke conversion
   
        v1alpha1 ◄────────► v1   (storage hub)
                              ▲
                              │
        v1beta1 ◄─────────────┤
                              │
        v2      ◄─────────────┘

   Conversions go via v1. v1beta1 → v2 = (v1beta1 → v1) ∘ (v1 → v2).
```

This is the kubebuilder convention. Pick a hub (usually the highest stable version), implement `ConvertTo(hub)` and `ConvertFrom(hub)` on every spoke, and the framework composes for any other pair.

### 10.2 A Go implementation sketch

```go
// api/v1beta1/postgrescluster_conversion.go

package v1beta1

import (
    "sigs.k8s.io/controller-runtime/pkg/conversion"
    v1 "github.com/example/pg-operator/api/v1"
)

// ConvertTo converts this v1beta1 to the Hub (v1).
func (src *PostgresCluster) ConvertTo(dstRaw conversion.Hub) error {
    dst := dstRaw.(*v1.PostgresCluster)
    dst.ObjectMeta = src.ObjectMeta
    dst.Spec.Version = src.Spec.Version

    // v1beta1.Spec.Replicas (int) → v1.Spec.Instances[].Replicas
    dst.Spec.Instances = []v1.InstanceSpec{
        {
            Name:     "default",
            Replicas: src.Spec.Replicas,
            Storage:  v1.StorageSpec(src.Spec.Storage),
        },
    }

    // status: copy what carries over, leave the rest empty
    dst.Status.Phase              = src.Status.Phase
    dst.Status.ReadyInstances     = src.Status.ReadyReplicas
    dst.Status.ObservedGeneration = src.Status.ObservedGeneration
    dst.Status.Conditions         = convertConditions(src.Status.Conditions)
    return nil
}

// ConvertFrom converts from the Hub (v1) to this v1beta1.
func (dst *PostgresCluster) ConvertFrom(srcRaw conversion.Hub) error {
    src := srcRaw.(*v1.PostgresCluster)
    dst.ObjectMeta = src.ObjectMeta
    dst.Spec.Version = src.Spec.Version

    // Lossy: v1 has multiple instances, v1beta1 has one replicas count.
    // Take the first instance and bag the rest into an annotation so it
    // is not silently dropped if the user reads via v1beta1, edits, and
    // writes back.
    if len(src.Spec.Instances) > 0 {
        dst.Spec.Replicas = src.Spec.Instances[0].Replicas
        dst.Spec.Storage  = v1beta1.StorageSpec(src.Spec.Instances[0].Storage)
    }
    if len(src.Spec.Instances) > 1 {
        if dst.Annotations == nil { dst.Annotations = map[string]string{} }
        b, _ := json.Marshal(src.Spec.Instances[1:])
        dst.Annotations["db.example.com/dropped-instances"] = string(b)
    }

    dst.Status.Phase              = src.Status.Phase
    dst.Status.ReadyReplicas      = src.Status.ReadyInstances
    dst.Status.ObservedGeneration = src.Status.ObservedGeneration
    dst.Status.Conditions         = convertConditionsFromV1(src.Status.Conditions)
    return nil
}
```

The `controller-runtime` webhook server (`sigs.k8s.io/controller-runtime/pkg/webhook/conversion`) wires this up automatically when you mark the v1 type as `+kubebuilder:storageversion`.

### 10.3 Rules every conversion webhook must follow

1. **Idempotent.** `Convert(Convert(x, A→B), B→A)` should equal `x` for any field that exists in both versions. The apiserver may call you many times.
2. **Pure.** Do not call the apiserver, do not read configmaps, do not do I/O. The conversion is on the read path for every API call.
3. **Fast.** Single-digit milliseconds. The apiserver imposes a 30-second timeout, but you should be 1000× faster than that.
4. **Lossless or annotated.** If converting A→B drops fields, *write them somewhere* (an annotation) so a B→A round trip can recover them. Otherwise users editing via the old API silently lose data they did not see.
5. **Defensive on `nil`.** Old objects may have missing fields where new ones expect them. Treat missing as default; never panic.
6. **No mutation of metadata except labels/annotations.** Do not change UID, ResourceVersion, Name, Namespace, or CreationTimestamp. The apiserver will reject the conversion result if you change these.
7. **No mutation of `apiVersion`/`kind` beyond switching to the target version's strings.**

### 10.4 Common conversion bugs

- **Stripping a status field.** Converting drops a condition you wrote in the new version because the old version's schema doesn't have it. Solution: every controller writes status only via the *storage* version's URL, never via an old version.
- **Default-on-read drift.** A field is missing in v1beta1 but has a default in v1. Every read of a v1beta1 object that converts up to v1 fills in the default, and a write back to v1beta1 may persist that default into etcd. Solution: don't have defaults that change between versions; or make sure the default is identical.
- **Type widening.** v1beta1 has `replicas: int32`; v1 has `replicas: *int32` (pointer, optional). Conversion `nil → 0` and `0 → nil` are not symmetric. Pick one canonical form.
- **Unbounded conversion fan-out.** A List call with 10,000 objects becomes a `ConversionReview` with 10,000 objects. If your webhook isn't efficient, this stalls the apiserver. Stream-parse the request body, not load-all-into-memory.

### 10.5 Webhook deployment

Same shape as the admission webhooks from chapter 06:

- TLS-secured HTTPS service.
- Certificate rotation via cert-manager (`Certificate` resource → secret → mounted into pod → CA injected into CRD via `cert-manager.io/inject-ca-from`).
- HA: two or more replicas behind a Service; leader election not required (conversion is stateless).
- Monitor: `apiserver_admission_webhook_request_total{type=conversion}`, latency histogram, error rate.

When the conversion webhook is down, *every* read of a non-storage-version URL fails. New writes still work (they go through the storage version path directly), but `kubectl get postgrescluster.v1beta1.db.example.com` will error. This is a serious failure mode; treat the conversion webhook like a critical control-plane component.

---

## 11. Versioning Strategy: v1alpha1 → v1beta1 → v1

Kubernetes API versioning conventions, applied to your CRD:

| Stage    | Stability                                | What you may change             | Storage version? | Deprecation policy           |
|----------|------------------------------------------|----------------------------------|------------------|------------------------------|
| v1alpha1 | Alpha. Don't use in prod.                | Anything, anytime, no warning.   | Initially yes.   | Drop with one minor release. |
| v1beta1  | Beta. Subject to change with notice.     | Add fields; deprecate carefully. | Yes during beta. | At least one minor release.  |
| v1       | Stable. Forever.                         | Add optional fields only.        | Yes, hub.        | One full major release.      |

In practice:

- **v1alpha1.** Ship the schema you think you want. Use `served: true, storage: true`. Make it cluster-admin-only to install. Tell users: "do not depend on this." Most projects spend 6–12 months here.
- **Promote to v1beta1.** Add `v1beta1` as a new version, `storage: true`, with the schema you actually want. Keep v1alpha1 served but `storage: false`, deprecated, and provide conversion. Run the storage-version migrator. After one or two releases, drop v1alpha1 entirely.
- **Promote to v1.** Add v1, repeat. Now you owe the world API stability.

The fundamental rule: **once an object has been stored under a version, removing that version requires either keeping the conversion path alive forever, or running a migration.**

### 11.1 The forgotten alpha API

Most CRDs *never make it out of v1alpha1*. They ship, some early adopters use them, the project moves on, and v1alpha1 lingers because nobody wants to write the conversion. This is fine *if* the operator authors commit to never breaking v1alpha1 in incompatible ways. It is catastrophic if they don't.

Decide on day one which it is. The CRD spec field `spec.versions[].deprecated: true` is one signal; release notes saying "we will break v1alpha1 between minor versions" is another. Be loud.

### 11.2 What "breaking" means inside a single version

Inside a single served version, the apiserver enforces *some* compatibility — pruning works only with the current schema, so adding a required field to v1 will break every existing v1 object on the next write. You generally cannot:

- Add a required field without a default.
- Tighten validation (lower maximum, higher minimum, narrower enum, stricter regex). This breaks objects that already match the loose schema but not the tight one.
- Change a field's `type`. (You should bump the version instead.)
- Remove an enum value that existing objects use.

You *can*:

- Add optional fields (with or without `default`).
- Loosen validation (raise maximum, lower minimum, widen enum, looser regex).
- Add new `additionalPrinterColumns`.
- Add new `x-kubernetes-validations` rules — *but only if you know all existing objects satisfy them.* A new CEL rule applied to existing objects will reject the next write of any object that violates it.
- Toggle `served:` and `storage:` (with care, as in §8).

If you must break, bump the version.

---

## 12. CRD Storage in etcd

Built-in resources are stored in etcd as **protobuf**-encoded objects (with a recognized prefix indicating the encoding). CRDs are stored as **JSON** of an `Unstructured` (a generic map). There is no per-CRD protobuf generation in upstream Kubernetes — the apiserver does not know your Go types.

```
   etcd key:   /registry/db.example.com/postgresclusters/default/pgc-1
   etcd value: <single byte 'k' or '{' indicating JSON> + UTF-8 JSON bytes
```

Practical implications:

### 12.1 Larger storage footprint

JSON is bigger than protobuf for the same data. A `PostgresCluster` with a couple of dozen fields and a few status conditions might be 4–8 KB; the equivalent built-in resource might be 2–4 KB. Multiply by thousands of objects and CRD-heavy clusters have noticeably bigger etcd databases.

### 12.2 Larger watch traffic

Every watch event sends the JSON. Built-in types send protobuf. A controller watching 10,000 of your CRDs uses roughly 2× the network and CPU of a controller watching 10,000 Deployments.

### 12.3 Slower deserialization in clients

`json.Unmarshal` is slower than protobuf. controller-runtime watches typically deserialize JSON into typed Go structs, which is fine for most controllers, but if you have a high-volume CRD (10k objects, 100 updates/sec) the CPU shows up.

### 12.4 No partial decoding

With a built-in's protobuf, an apiserver can sometimes shortcut and not decode the whole object. With CRD JSON, every read decodes the whole thing.

### 12.5 Mitigations

- Keep CRD objects small. If you find yourself stuffing kilobytes of free-form config into one CRD, split it.
- Use `metadata.resourceVersion`-only watches (the `Metadata` partial object metadata API) when you only need to know "an object changed" not "what it now contains."
- Set CRD-specific informer cache scoping (§27) so a controller doesn't watch 10k objects when it only cares about a few.

---

## 13. RBAC for CRDs

A new CRD comes with no default permissions. Every action — get, list, watch, create, update, patch, delete, plus subresources — must be granted explicitly.

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: postgrescluster-admin
rules:
  - apiGroups: [db.example.com]
    resources: [postgresclusters]
    verbs: [get, list, watch, create, update, patch, delete, deletecollection]
  - apiGroups: [db.example.com]
    resources: [postgresclusters/status]
    verbs: [get, update, patch]
  - apiGroups: [db.example.com]
    resources: [postgresclusters/scale]
    verbs: [get, update, patch]
  - apiGroups: [db.example.com]
    resources: [postgresclusters/finalizers]
    verbs: [update]
```

Subresource notes:

- `postgresclusters/status` is its own RBAC target. Grant `update` to the operator's service account; do *not* grant it to end users. Otherwise users can write status, which lets them lie about cluster health.
- `postgresclusters/scale` lets the holder use `/scale`. HPA needs it on the resource it scales.
- `postgresclusters/finalizers` is the *finalizer* subresource — required for the operator to add finalizers to objects in *other* namespaces (or cluster-scoped objects) without owning them. Without this, a deletion can hang forever because the GC can't strip a finalizer it doesn't have permission to write.

Aggregate roles: a common pattern is to add `rbac.authorization.k8s.io/aggregate-to-admin: "true"`, `aggregate-to-edit`, `aggregate-to-view` labels to ClusterRoles so the built-in admin/edit/view roles automatically pick up your CRD permissions:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: postgrescluster-aggregate-to-admin
  labels:
    rbac.authorization.k8s.io/aggregate-to-admin: "true"
    rbac.authorization.k8s.io/aggregate-to-edit: "true"
rules:
  - apiGroups: [db.example.com]
    resources: [postgresclusters]
    verbs: [get, list, watch, create, update, patch, delete]
```

Now any user with `admin` or `edit` on a namespace can manage `PostgresCluster` objects in that namespace, without each cluster operator having to write per-CRD bindings.

---

## 14. The Operator Pattern

A CRD without a controller is just a typed ConfigMap. The *Operator* is the controller — a long-running process that watches the CRD, reconciles it to real-world state, and writes back observed state to `.status`. The pattern was named by CoreOS in 2016 (the etcd-operator was the prototype), and the term has come to mean *"a Kubernetes controller for an application that encodes operational knowledge."*

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                       THE OPERATOR PATTERN                          │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                     │
   │     ┌─────────────────────┐                                         │
   │     │      Custom         │  user / GitOps writes this              │
   │     │      Resource       │                                         │
   │     │      (.spec)        │                                         │
   │     └──────────┬──────────┘                                         │
   │                │                                                    │
   │                │ watch via informer                                 │
   │                ▼                                                    │
   │     ┌─────────────────────┐                                         │
   │     │      Operator       │     +───────────────────────┐           │
   │     │      Process        │────►│ owned Pods            │           │
   │     │  (controller-       │     │ owned Services        │           │
   │     │   runtime)          │     │ owned StatefulSets    │           │
   │     │                     │     │ owned ConfigMaps      │           │
   │     └──────────┬──────────┘     │ cloud LBs             │           │
   │                │                │ DNS records           │           │
   │                │                │ external DBs          │           │
   │                ▼                +───────────────────────┘           │
   │     ┌─────────────────────┐                                         │
   │     │      .status        │     observed reality flows back here    │
   │     │      conditions     │     via status subresource              │
   │     │      observedGen    │                                         │
   │     └─────────────────────┘                                         │
   │                                                                     │
   └──────────────────────────────────────────────────────────────────────┘
```

The operator embodies *operational knowledge that used to live in runbooks*. Examples:

- **etcd-operator** (deprecated but historic): grew the cluster by adding members one at a time, ran defragmentation, rotated TLS certs.
- **prometheus-operator**: generates Prometheus, Alertmanager, and ServiceMonitor config from CRs. Reloads Prometheus on changes.
- **cert-manager**: watches `Certificate` CRs, talks to ACME providers, writes the resulting cert/key to a `Secret`, renews before expiry.
- **CloudNativePG / Zalando postgres-operator / Crunchy PGO / KubeDB**: provision Postgres clusters with HA failover, backups, point-in-time restore, version upgrades.
- **strimzi**: provision Kafka clusters with rebalancing, broker upgrades, mTLS.

What every operator does, in one sentence: *"watch a CRD, materialize a set of subordinate resources, drive a third-party system, write observed state back to status."* The pattern repeats forever.

### 14.1 What an operator is not

- **Not a generic agent.** An operator is single-purpose. It knows about one (or a few related) CRD types.
- **Not a one-shot script.** It reconciles continuously, forever. Compared to Helm (§28), this is the *biggest* design difference.
- **Not an admission webhook.** Webhooks (ch 06) validate/mutate objects in flight; operators reconcile after the fact, in a separate process.
- **Not part of the apiserver.** The apiserver doesn't know operators exist. They are just clients.

### 14.2 Anatomy

```
   ┌────────────────────────────────────────────────────────────────────┐
   │                          Operator Process                          │
   ├────────────────────────────────────────────────────────────────────┤
   │                                                                    │
   │   ┌────────────────┐    ┌────────────────┐                         │
   │   │  Webhook       │    │  Metrics       │                         │
   │   │  Server        │    │  Server        │                         │
   │   │  (mutation +   │    │  (Prometheus)  │                         │
   │   │   validation,  │    └────────────────┘                         │
   │   │   conversion)  │                                               │
   │   └────────────────┘    ┌────────────────┐                         │
   │                         │  Health        │                         │
   │   ┌────────────────┐    │  /readyz,/livez│                         │
   │   │  Leader        │    └────────────────┘                         │
   │   │  Election      │                                               │
   │   │  (ch 08)       │                                               │
   │   └────────┬───────┘                                               │
   │            │                                                       │
   │            │ if I am leader, then:                                 │
   │            ▼                                                       │
   │   ┌──────────────────────────────────────────────────────────┐     │
   │   │             controller-runtime Manager                   │     │
   │   │   ┌──────────────────────────────────────────────┐       │     │
   │   │   │    Informers / Cache (shared across all      │       │     │
   │   │   │    Reconcilers in the binary)                │       │     │
   │   │   └──────────────────────────────────────────────┘       │     │
   │   │   ┌─────────────────┐  ┌─────────────────┐  ┌─────────┐  │     │
   │   │   │ Reconciler A    │  │ Reconciler B    │  │  ...    │  │     │
   │   │   │ PostgresCluster │  │ Backup          │  │         │  │     │
   │   │   └─────────────────┘  └─────────────────┘  └─────────┘  │     │
   │   └──────────────────────────────────────────────────────────┘     │
   │                                                                    │
   └────────────────────────────────────────────────────────────────────┘
```

A single operator binary typically hosts:

- One or more `Reconciler`s, one per CRD it manages.
- Shared informers (the Cache) for every type any reconciler watches.
- An admission-webhook server (defaulting and validation).
- A conversion-webhook server (if multi-version).
- A metrics server on `:8080/metrics`.
- A health probe server on `:8081/healthz` and `/readyz`.
- A leader-election client so only one replica is the active reconciler at a time.

---

## 15. Operator Capability Levels (L1–L5)

The OperatorHub Capability Levels are a 5-stage maturity model published by the Operator Framework community. They describe what an operator *does*, not how it's implemented.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                  OPERATOR CAPABILITY LEVELS                         │
   ├─────────────────────────────────────────────────────────────────────┤
   │                                                                     │
   │   L5 ┌─ AUTO PILOT ──────────────────────────────────────────┐      │
   │      │ Horizontal/vertical scaling, auto-config tuning,      │      │
   │      │ abnormal-detection, scheduling tuning, anomaly        │      │
   │      │ remediation                                            │     │
   │      └────────────────────────────────────────────────────────┘     │
   │   L4 ┌─ DEEP INSIGHTS ───────────────────────────────────────┐      │
   │      │ Operator-driven metrics, alerts, log processing,      │      │
   │      │ workload analytics                                    │      │
   │      └────────────────────────────────────────────────────────┘     │
   │   L3 ┌─ FULL LIFECYCLE ──────────────────────────────────────┐      │
   │      │ App lifecycle: upgrade/downgrade, backup, restore,    │      │
   │      │ fault recovery                                         │     │
   │      └────────────────────────────────────────────────────────┘     │
   │   L2 ┌─ SEAMLESS UPGRADES ───────────────────────────────────┐      │
   │      │ Patch + minor version upgrades for the *operator      │      │
   │      │ and operand*                                           │     │
   │      └────────────────────────────────────────────────────────┘     │
   │   L1 ┌─ BASIC INSTALL ───────────────────────────────────────┐      │
   │      │ Provisioning + configuration of the app                │     │
   │      └────────────────────────────────────────────────────────┘     │
   │                                                                     │
   └─────────────────────────────────────────────────────────────────────┘
```

### L1: Basic Install

The operator can stand up the application. Given a CR, it creates the Deployments/StatefulSets/Services/ConfigMaps that the app needs to run.

### L2: Seamless Upgrades

The operator handles upgrades — both of itself and of the operand (the application it manages). Bumping `spec.version` from `15` to `16` triggers a rolling upgrade with no data loss.

### L3: Full Lifecycle

Backup, restore, failover, scale, rolling upgrade, replication setup. The operator has runbook knowledge encoded for the steady-state lifecycle of the application.

### L4: Deep Insights

The operator integrates with monitoring: exports its own Prometheus metrics, deploys ServiceMonitors for the operand, defines PrometheusRule alerts, possibly ships Grafana dashboards as ConfigMaps.

### L5: Auto Pilot

The operator autonomously responds to operational signals. Auto-scaling beyond HPA (e.g., add a read replica when query latency exceeds a threshold), auto-tuning (resizing PVCs when free space drops), self-healing (restarting an unhealthy node, failing over to a standby).

L5 is rare and aspirational. Most operators that claim L5 in fact stop at L3+L4. There is nothing wrong with that — L3 is genuinely the sweet spot for most workloads.

---

## 16. kubebuilder and operator-sdk Scaffolding

You do not write an operator from scratch. You scaffold one. Two tools dominate:

- **kubebuilder** (`sigs.k8s.io/kubebuilder`), maintained by the Kubernetes SIG API Machinery. Pure Go. Direct controller-runtime, no extra abstraction.
- **operator-sdk** (`github.com/operator-framework/operator-sdk`), maintained by Red Hat / the Operator Framework. Builds on kubebuilder for the Go path, plus Helm and Ansible-based operator types for the non-Go paths.

For Go operators, the projects are nearly identical: both emit kubebuilder-style scaffolding with `controller-runtime` underneath. Operator-sdk adds OLM bundle generation, scorecard tests, and a few extras.

### 16.1 A typical kubebuilder project layout

```
my-operator/
├── PROJECT                       # kubebuilder project descriptor
├── Makefile                      # standard targets: build, test, deploy, manifests, generate
├── Dockerfile
├── go.mod
├── main.go                       # Manager bootstrap
├── api/
│   └── v1/
│       ├── groupversion_info.go  # AddToScheme registration
│       ├── postgrescluster_types.go     # Spec, Status, Conditions
│       └── zz_generated.deepcopy.go     # generated DeepCopy methods
├── internal/
│   └── controller/
│       ├── postgrescluster_controller.go         # Reconciler
│       └── postgrescluster_controller_test.go    # envtest tests
├── config/
│   ├── crd/
│   │   ├── bases/                 # CRD YAML, generated by controller-gen
│   │   │   └── db.example.com_postgresclusters.yaml
│   │   ├── patches/               # conversion-webhook patch, ca-injection patch
│   │   └── kustomization.yaml
│   ├── rbac/                      # ClusterRole, ClusterRoleBinding, Role
│   ├── manager/                   # Deployment for the operator itself
│   ├── webhook/                   # MutatingWebhookConfig, ValidatingWebhookConfig
│   ├── certmanager/               # Certificate, Issuer for the webhook
│   ├── default/                   # top-level Kustomization
│   └── samples/                   # example CRs for documentation
├── hack/
│   └── boilerplate.go.txt
└── test/
    ├── e2e/
    └── utils/
```

### 16.2 The Makefile targets that matter

```makefile
make manifests        # regenerate CRD YAML, RBAC, webhook config from kubebuilder markers
make generate         # regenerate DeepCopy and other code from go types
make install          # apply CRDs to the cluster
make uninstall        # delete CRDs
make deploy           # build, push, deploy the operator
make undeploy
make test             # envtest-based unit tests
make run              # run the controller locally against the current kubeconfig
```

`make manifests` and `make generate` are the two commands you run every time you touch the types. `make run` is the local development loop.

### 16.3 A kubebuilder type with markers

```go
// api/v1/postgrescluster_types.go

package v1

import (
    corev1 "k8s.io/api/core/v1"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PostgresClusterSpec defines the desired state of PostgresCluster
type PostgresClusterSpec struct {
    // Version of Postgres to run.
    // +kubebuilder:validation:Enum=14;15;16;17
    Version string `json:"version"`

    // Instances are the postgres instance specs. At least one is required.
    // +kubebuilder:validation:MinItems=1
    // +kubebuilder:validation:MaxItems=9
    // +listType=map
    // +listMapKey=name
    Instances []InstanceSpec `json:"instances"`

    // Backup is the optional backup configuration.
    // +optional
    Backup *BackupSpec `json:"backup,omitempty"`
}

type InstanceSpec struct {
    // Name is a unique identifier for this instance within the cluster.
    // +kubebuilder:validation:Pattern=`^[a-z0-9-]{1,32}$`
    Name string `json:"name"`

    // Replicas is the number of pod replicas for this instance.
    // +kubebuilder:validation:Minimum=1
    // +kubebuilder:default=1
    Replicas int32 `json:"replicas,omitempty"`

    Storage StorageSpec `json:"storage"`
}

type StorageSpec struct {
    // +kubebuilder:validation:Pattern=`^[0-9]+(Mi|Gi|Ti)$`
    Size             string `json:"size"`
    StorageClassName string `json:"storageClassName,omitempty"`
}

type BackupSpec struct {
    Schedule string `json:"schedule,omitempty"`
    S3       *S3Spec `json:"s3,omitempty"`
}

type S3Spec struct {
    Bucket string `json:"bucket"`
    // +kubebuilder:default=us-east-1
    Region string `json:"region,omitempty"`
}

// PostgresClusterStatus defines the observed state of PostgresCluster
type PostgresClusterStatus struct {
    // ObservedGeneration is the generation of the spec that was last reconciled.
    // +optional
    ObservedGeneration int64 `json:"observedGeneration,omitempty"`

    // Phase is a coarse-grained summary of the cluster lifecycle.
    // +kubebuilder:validation:Enum=Pending;Provisioning;Ready;Degraded;Failed;Deleting
    Phase string `json:"phase,omitempty"`

    // ReadyInstances is the count of instances that are ready.
    // +optional
    ReadyInstances int32 `json:"readyInstances,omitempty"`

    // Selector is a serialized label selector for HPA.
    // +optional
    Selector string `json:"selector,omitempty"`

    // Conditions tracks the cluster's state transitions.
    // +listType=map
    // +listMapKey=type
    // +optional
    Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:subresource:scale:specpath=.spec.instances[0].replicas,statuspath=.status.readyInstances,selectorpath=.status.selector
// +kubebuilder:resource:scope=Namespaced,shortName=pgc;pgcluster,categories=databases
// +kubebuilder:storageversion
// +kubebuilder:printcolumn:name=Version,type=string,JSONPath=`.spec.version`
// +kubebuilder:printcolumn:name=Ready,type=integer,JSONPath=`.status.readyInstances`
// +kubebuilder:printcolumn:name=Phase,type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name=Age,type=date,JSONPath=`.metadata.creationTimestamp`
type PostgresCluster struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`

    Spec   PostgresClusterSpec   `json:"spec,omitempty"`
    Status PostgresClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type PostgresClusterList struct {
    metav1.TypeMeta `json:",inline"`
    metav1.ListMeta `json:"metadata,omitempty"`
    Items           []PostgresCluster `json:"items"`
}
```

The `+kubebuilder:` markers are read by `controller-gen` (`sigs.k8s.io/controller-tools/cmd/controller-gen`) to generate (a) the CRD YAML under `config/crd/bases/`, (b) RBAC manifests under `config/rbac/` from `+kubebuilder:rbac` markers on the reconciler, (c) DeepCopy methods, (d) webhook manifests from `+kubebuilder:webhook` markers.

This is the source of truth. You do not write CRD YAML by hand in a kubebuilder project — you write Go types with markers, and run `make manifests`.

---

## 17. controller-runtime Layers

`sigs.k8s.io/controller-runtime` is the library that sits on top of client-go (chapter 08) and gives operators a higher-level API. It is roughly six layers.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                   controller-runtime layers                         │
   ├─────────────────────────────────────────────────────────────────────┤
   │                                                                     │
   │   ┌───────────────────────────────────────────────────────────┐     │
   │   │              Manager (sigs.k8s.io/.../manager)            │     │
   │   │  ┌─────────────────────────────────────────────────────┐  │     │
   │   │  │   leader election | metrics | health probes |       │  │     │
   │   │  │   webhook server | signal handler | start/stop      │  │     │
   │   │  └─────────────────────────────────────────────────────┘  │     │
   │   └────────────────────────────┬──────────────────────────────┘     │
   │                                │                                    │
   │   ┌────────────────────────────┴──────────────────────────────┐     │
   │   │              Cache (sigs.k8s.io/.../cache)                │     │
   │   │  shared informers per (GVK, namespace, label-selector)    │     │
   │   └────────────────────────────┬──────────────────────────────┘     │
   │                                │                                    │
   │   ┌────────────────────────────┴──────────────────────────────┐     │
   │   │              Client (sigs.k8s.io/.../client)              │     │
   │   │  reads → Cache;  writes → apiserver                       │     │
   │   └────────────────────────────┬──────────────────────────────┘     │
   │                                │                                    │
   │   ┌────────────────────────────┴──────────────────────────────┐     │
   │   │              builder (sigs.k8s.io/.../builder)            │     │
   │   │  For(&MyCR{}).Owns(&Pod{}).Watches(&Secret{}, h).Complete │     │
   │   └────────────────────────────┬──────────────────────────────┘     │
   │                                │                                    │
   │   ┌────────────────────────────┴──────────────────────────────┐     │
   │   │           Controller (sigs.k8s.io/.../controller)         │     │
   │   │  workqueue | predicates | event handlers | rate limiters  │     │
   │   └────────────────────────────┬──────────────────────────────┘     │
   │                                │                                    │
   │   ┌────────────────────────────┴──────────────────────────────┐     │
   │   │       Reconciler (sigs.k8s.io/.../reconcile.Reconciler)   │     │
   │   │                  YOUR CODE: Reconcile(ctx, req)           │     │
   │   └───────────────────────────────────────────────────────────┘     │
   │                                                                     │
   └─────────────────────────────────────────────────────────────────────┘
```

### 17.1 Manager

```go
// main.go
mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
    Scheme:                 scheme,
    Metrics:                metricsserver.Options{BindAddress: ":8080"},
    HealthProbeBindAddress: ":8081",
    LeaderElection:         true,
    LeaderElectionID:       "pg-operator.db.example.com",
    LeaderElectionNamespace: "db-system",
    LeaseDuration:          ptr.To(15 * time.Second),
    RenewDeadline:          ptr.To(10 * time.Second),
    RetryPeriod:            ptr.To(2 * time.Second),
    WebhookServer: webhook.NewServer(webhook.Options{
        Port:    9443,
        CertDir: "/tmp/k8s-webhook-server/serving-certs",
    }),
    Cache: cache.Options{
        // narrow the cache — see §27
        DefaultNamespaces: map[string]cache.Config{
            "db-system":      {},
            "tenant-a":       {},
            "tenant-b":       {},
        },
    },
})
if err != nil { os.Exit(1) }

if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil { os.Exit(1) }
if err := mgr.AddReadyzCheck("readyz",  healthz.Ping); err != nil { os.Exit(1) }

if err := (&controllers.PostgresClusterReconciler{
    Client: mgr.GetClient(),
    Scheme: mgr.GetScheme(),
}).SetupWithManager(mgr); err != nil { os.Exit(1) }

if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil { os.Exit(1) }
```

The Manager owns shared things — the cache, the client, the webhook server, the metrics endpoint, leader election, signal handling. You give it Reconcilers and it runs them. When the process is asked to stop (SIGTERM), the Manager cancels every reconciler's context and the Stop() returns.

### 17.2 Cache

The Cache is a set of shared informers (chapter 08). Every Reconciler in the binary shares the same cache. If two reconcilers both want to watch Pods, there is one informer for Pods, one event stream, one in-memory index. This is *the* memory-saving primitive in controller-runtime.

You can scope the cache by namespace (`DefaultNamespaces`), by label selector, or by field selector. §27 covers the cost model.

### 17.3 Client

```go
type Client interface {
    Get(ctx, key, obj) error      // reads from Cache
    List(ctx, list, opts...) error // reads from Cache
    Create(ctx, obj, opts...) error
    Update(ctx, obj, opts...) error
    Patch(ctx, obj, patch, opts...) error
    Delete(ctx, obj, opts...) error
    Status() SubResourceClient    // for status writes
    SubResource(name) SubResourceClient  // for /scale, /eviction, etc.
}
```

Reads go to the cache (no apiserver round-trip; they may be stale by ~50ms-RTT). Writes go straight to the apiserver. This is the right default and is *intentional*. Reconcilers should never bypass the cache to "get fresh data" — fresher data doesn't fix the level-triggered semantics, and reading from the apiserver on every reconcile floods it.

### 17.4 builder

The fluent API that wires up an informer + workqueue + predicates + reconciler:

```go
func (r *PostgresClusterReconciler) SetupWithManager(mgr ctrl.Manager) error {
    return ctrl.NewControllerManagedBy(mgr).
        For(&dbv1.PostgresCluster{}).             // primary type to watch
        Owns(&appsv1.StatefulSet{}).               // owned subordinate
        Owns(&corev1.Service{}).
        Owns(&corev1.Secret{}).
        Owns(&corev1.ConfigMap{}).
        Watches(                                   // non-owned dependent
            &corev1.Secret{},
            handler.EnqueueRequestsFromMapFunc(r.findClustersForSecret),
            builder.WithPredicates(predicate.GenerationChangedPredicate{})).
        WithOptions(controller.Options{
            MaxConcurrentReconciles: 4,
            RateLimiter: workqueue.NewItemExponentialFailureRateLimiter(
                100*time.Millisecond, 5*time.Minute),
        }).
        Complete(r)
}
```

This is the *only* place outside the Reconcile function where wiring lives. Read it carefully — many reconciliation bugs are wiring bugs (forgot `Owns`, wrong predicate, no rate limiter).

### 17.5 Reconciler

```go
func (r *PostgresClusterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error)
```

Your code. See §18.

---

## 18. A Full Reconciler: Spec, Status, Finalizer, Conditions

The canonical operator reconciler. Read every line; this is the template you'll write 50 times in your career.

```go
package controllers

import (
    "context"
    "errors"
    "fmt"
    "time"

    appsv1  "k8s.io/api/apps/v1"
    corev1  "k8s.io/api/core/v1"
    apierrors "k8s.io/apimachinery/pkg/api/errors"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/apimachinery/pkg/runtime"
    "k8s.io/apimachinery/pkg/types"
    "k8s.io/apimachinery/pkg/util/intstr"
    "k8s.io/utils/ptr"
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/builder"
    "sigs.k8s.io/controller-runtime/pkg/client"
    "sigs.k8s.io/controller-runtime/pkg/controller"
    "sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
    "sigs.k8s.io/controller-runtime/pkg/handler"
    "sigs.k8s.io/controller-runtime/pkg/log"
    "sigs.k8s.io/controller-runtime/pkg/predicate"
    "sigs.k8s.io/controller-runtime/pkg/reconcile"

    dbv1 "github.com/example/pg-operator/api/v1"
)

const (
    pgClusterFinalizer = "postgresclusters.db.example.com/finalizer"

    condTypeReady        = "Ready"
    condTypeProvisioned  = "Provisioned"
    condTypeBackupActive = "BackupActive"

    reasonReconciling    = "Reconciling"
    reasonProvisioned    = "Provisioned"
    reasonDegraded       = "Degraded"
    reasonDeleting       = "Deleting"
)

// +kubebuilder:rbac:groups=db.example.com,resources=postgresclusters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=db.example.com,resources=postgresclusters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=db.example.com,resources=postgresclusters/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services;configmaps;secrets,verbs=get;list;watch;create;update;patch;delete

type PostgresClusterReconciler struct {
    client.Client
    Scheme *runtime.Scheme
}

func (r *PostgresClusterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    logger := log.FromContext(ctx).WithValues("postgrescluster", req.NamespacedName)

    // 1. Get the CR. NotFound → it was deleted, we're done.
    var pgc dbv1.PostgresCluster
    if err := r.Get(ctx, req.NamespacedName, &pgc); err != nil {
        if apierrors.IsNotFound(err) {
            return ctrl.Result{}, nil
        }
        return ctrl.Result{}, err
    }

    // 2. Handle deletion via finalizer.
    if !pgc.DeletionTimestamp.IsZero() {
        return r.finalize(ctx, &pgc)
    }

    // 3. Ensure the finalizer is registered before we create any external state.
    if !controllerutil.ContainsFinalizer(&pgc, pgClusterFinalizer) {
        controllerutil.AddFinalizer(&pgc, pgClusterFinalizer)
        if err := r.Update(ctx, &pgc); err != nil {
            return ctrl.Result{}, err
        }
        // Update modifies ResourceVersion; the next reconcile will see the new object.
        return ctrl.Result{Requeue: true}, nil
    }

    // 4. Initialize status if first reconciliation.
    if pgc.Status.Phase == "" {
        setCondition(&pgc, condTypeReady, metav1.ConditionFalse, reasonReconciling, "initializing")
        pgc.Status.Phase = "Provisioning"
        if err := r.statusUpdate(ctx, &pgc); err != nil {
            return ctrl.Result{}, err
        }
        // continue; we don't return — we still need to make progress
    }

    // 5. Reconcile subordinate resources.
    sts, err := r.reconcileStatefulSet(ctx, &pgc)
    if err != nil {
        setCondition(&pgc, condTypeProvisioned, metav1.ConditionFalse, reasonDegraded, err.Error())
        pgc.Status.Phase = "Degraded"
        _ = r.statusUpdate(ctx, &pgc)
        return ctrl.Result{}, err
    }

    if _, err := r.reconcileHeadlessService(ctx, &pgc); err != nil {
        return ctrl.Result{}, err
    }

    if _, err := r.reconcileCredentialsSecret(ctx, &pgc); err != nil {
        return ctrl.Result{}, err
    }

    // 6. Compute observed state.
    var readyInstances int32
    if sts != nil {
        readyInstances = sts.Status.ReadyReplicas
    }

    var desired int32
    if len(pgc.Spec.Instances) > 0 {
        desired = pgc.Spec.Instances[0].Replicas
    }

    // 7. Update status. Use a strategic-merge patch so we don't war with SSA.
    orig := pgc.DeepCopy()
    pgc.Status.ObservedGeneration = pgc.Generation
    pgc.Status.ReadyInstances = readyInstances
    pgc.Status.Selector = fmt.Sprintf("app=postgres,cluster=%s", pgc.Name)
    if readyInstances >= desired && desired > 0 {
        pgc.Status.Phase = "Ready"
        setCondition(&pgc, condTypeReady, metav1.ConditionTrue, reasonProvisioned, "all instances ready")
        setCondition(&pgc, condTypeProvisioned, metav1.ConditionTrue, reasonProvisioned, "provisioned")
    } else {
        pgc.Status.Phase = "Provisioning"
        setCondition(&pgc, condTypeReady, metav1.ConditionFalse, reasonReconciling,
            fmt.Sprintf("%d/%d instances ready", readyInstances, desired))
    }

    if err := r.Status().Patch(ctx, &pgc, client.MergeFrom(orig)); err != nil {
        return ctrl.Result{}, err
    }

    logger.V(1).Info("reconciled",
        "phase", pgc.Status.Phase,
        "ready", readyInstances,
        "desired", desired,
        "observedGeneration", pgc.Status.ObservedGeneration)

    // 8. If we're not Ready yet, keep nudging the queue — but don't poll tightly.
    if pgc.Status.Phase != "Ready" {
        return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
    }
    return ctrl.Result{}, nil
}

// finalize handles deletion: tear down external state, then strip the finalizer.
func (r *PostgresClusterReconciler) finalize(ctx context.Context, pgc *dbv1.PostgresCluster) (ctrl.Result, error) {
    logger := log.FromContext(ctx)
    if !controllerutil.ContainsFinalizer(pgc, pgClusterFinalizer) {
        return ctrl.Result{}, nil
    }

    pgc.Status.Phase = "Deleting"
    setCondition(pgc, condTypeReady, metav1.ConditionFalse, reasonDeleting, "deletion in progress")
    _ = r.statusUpdate(ctx, pgc)

    // Tear down external state (cloud LBs, DNS, backup buckets, etc.).
    // STS / Service / Secret are owned via ownerReferences and the GC will
    // delete them automatically once the finalizer is gone. The finalizer
    // only exists for state the GC does NOT know about.
    if pgc.Spec.Backup != nil && pgc.Spec.Backup.S3 != nil {
        if err := r.deleteBackupObjects(ctx, pgc); err != nil {
            logger.Error(err, "backup teardown failed; will retry")
            // Do NOT remove the finalizer; let the next reconcile try again.
            return ctrl.Result{RequeueAfter: time.Minute}, nil
        }
    }

    controllerutil.RemoveFinalizer(pgc, pgClusterFinalizer)
    if err := r.Update(ctx, pgc); err != nil {
        return ctrl.Result{}, err
    }
    return ctrl.Result{}, nil
}

// reconcileStatefulSet ensures the STS exists with the right spec.
func (r *PostgresClusterReconciler) reconcileStatefulSet(
    ctx context.Context, pgc *dbv1.PostgresCluster,
) (*appsv1.StatefulSet, error) {
    sts := &appsv1.StatefulSet{
        ObjectMeta: metav1.ObjectMeta{
            Name:      pgc.Name,
            Namespace: pgc.Namespace,
        },
    }
    op, err := controllerutil.CreateOrUpdate(ctx, r.Client, sts, func() error {
        // Set the owner reference so the GC cascades on parent delete.
        if err := controllerutil.SetControllerReference(pgc, sts, r.Scheme); err != nil {
            return err
        }
        sts.Spec = appsv1.StatefulSetSpec{
            ServiceName: pgc.Name + "-headless",
            Replicas:    ptr.To(pgc.Spec.Instances[0].Replicas),
            Selector: &metav1.LabelSelector{
                MatchLabels: map[string]string{"app": "postgres", "cluster": pgc.Name},
            },
            Template: corev1.PodTemplateSpec{
                ObjectMeta: metav1.ObjectMeta{
                    Labels: map[string]string{"app": "postgres", "cluster": pgc.Name},
                },
                Spec: corev1.PodSpec{
                    Containers: []corev1.Container{{
                        Name:  "postgres",
                        Image: fmt.Sprintf("postgres:%s", pgc.Spec.Version),
                        Ports: []corev1.ContainerPort{{
                            Name:          "postgres",
                            ContainerPort: 5432,
                        }},
                        ReadinessProbe: &corev1.Probe{
                            ProbeHandler: corev1.ProbeHandler{
                                TCPSocket: &corev1.TCPSocketAction{
                                    Port: intstr.FromString("postgres"),
                                },
                            },
                        },
                    }},
                },
            },
        }
        return nil
    })
    if err != nil {
        return nil, err
    }
    log.FromContext(ctx).V(2).Info("statefulset reconciled", "op", op)
    return sts, nil
}

// statusUpdate updates only the status subresource.
func (r *PostgresClusterReconciler) statusUpdate(ctx context.Context, pgc *dbv1.PostgresCluster) error {
    return r.Status().Update(ctx, pgc)
}

// setCondition replaces or appends a condition, idempotently.
func setCondition(pgc *dbv1.PostgresCluster, condType string, status metav1.ConditionStatus, reason, message string) {
    now := metav1.Now()
    for i, c := range pgc.Status.Conditions {
        if c.Type == condType {
            if c.Status != status {
                pgc.Status.Conditions[i].LastTransitionTime = now
            }
            pgc.Status.Conditions[i].Status = status
            pgc.Status.Conditions[i].Reason = reason
            pgc.Status.Conditions[i].Message = message
            pgc.Status.Conditions[i].ObservedGeneration = pgc.Generation
            return
        }
    }
    pgc.Status.Conditions = append(pgc.Status.Conditions, metav1.Condition{
        Type:               condType,
        Status:             status,
        LastTransitionTime: now,
        Reason:             reason,
        Message:            message,
        ObservedGeneration: pgc.Generation,
    })
}

func (r *PostgresClusterReconciler) SetupWithManager(mgr ctrl.Manager) error {
    return ctrl.NewControllerManagedBy(mgr).
        For(&dbv1.PostgresCluster{}).
        Owns(&appsv1.StatefulSet{}).
        Owns(&corev1.Service{}).
        Owns(&corev1.Secret{}).
        Watches(
            &corev1.Secret{},
            handler.EnqueueRequestsFromMapFunc(r.findClustersForSecret),
            builder.WithPredicates(predicate.LabelChangedPredicate{}),
        ).
        WithOptions(controller.Options{
            MaxConcurrentReconciles: 4,
        }).
        Complete(r)
}

func (r *PostgresClusterReconciler) findClustersForSecret(ctx context.Context, obj client.Object) []reconcile.Request {
    // Reverse lookup: which PGCs reference this Secret?
    var list dbv1.PostgresClusterList
    if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
        return nil
    }
    var reqs []reconcile.Request
    for _, pgc := range list.Items {
        if pgc.Spec.Backup != nil && pgc.Spec.Backup.S3 != nil {
            // If this is the credentials secret, enqueue.
            if obj.GetName() == pgc.Name + "-backup-creds" {
                reqs = append(reqs, reconcile.Request{
                    NamespacedName: types.NamespacedName{
                        Namespace: pgc.Namespace,
                        Name:      pgc.Name,
                    },
                })
            }
        }
    }
    return reqs
}
```

Read the structure: **Get → handle deletion via finalizer → register finalizer → init status → reconcile children with CreateOrUpdate → compute observed state → patch status → requeue if not done**. Every operator you ever write follows this shape.

### 18.1 Discipline points

- **Spec is read; status is written.** The reconciler reads `pgc.Spec.*` but the only field of `pgc` it *writes back to the cluster* is `pgc.Status.*`, via `r.Status().Patch()`. Never call `r.Update(ctx, &pgc)` with status changes — that goes through the main URL and silently drops them.
- **Use `r.Status().Patch`, not `Update`.** Patch is mergeable; Update is RV-checked and conflicts on concurrent writes.
- **`observedGeneration` is set on every condition.** Any reader of the object can tell whether status came from the latest spec by comparing `metadata.generation` to `status.observedGeneration` (or per-condition `observedGeneration`).
- **Conditions are a *map* by `type`.** The status schema declares `x-kubernetes-list-type: map, x-kubernetes-list-map-keys: [type]`. `setCondition` updates in place, never duplicating. Two reconcilers (or this one across restarts) merge without colliding.
- **Finalizer is added *before* any external state is created.** If the order were reversed — create external state first, then add finalizer — a crash between the two would leak the external state. With the order correct, the worst case is "we have a finalizer but no external state," which is benign.
- **Finalizer is removed *after* external state is gone.** The finalize handler is allowed to retry forever; only when teardown is verified complete does it strip the finalizer. Until then, the API object stays in the cluster with a deletionTimestamp.
- **`CreateOrUpdate`** is the controller-runtime helper that does Get→mutate→Update with optimistic concurrency. It is the right way to ensure a subordinate resource. Each call returns an `OperationResult` (`Created`/`Updated`/`Unchanged`) which is useful for metrics.
- **No mutation of `pgc.Spec`.** Setting `pgc.Spec.Backup.S3.Region` from the reconciler ("if not set, default to us-east-1") would cause endless reconcile loops with GitOps (the GitOps tool applies its empty value back, you set it again, and so on). Defaults belong in the CRD schema or an admission webhook, never the reconciler.

---

## 19. Watching Dependent Resources: `Owns` and `Watches`

A reconciler that only watches its own CRD type is half-blind. When an owned StatefulSet's status changes, the reconciler needs to know. When a referenced Secret rotates, the reconciler needs to know. controller-runtime gives you three primitives.

### 19.1 `For`

```go
For(&dbv1.PostgresCluster{})
```

The primary type. Every event on a `PostgresCluster` enqueues itself.

### 19.2 `Owns`

```go
Owns(&appsv1.StatefulSet{})
```

Tells the controller: "I create StatefulSets, and on each, I set an `ownerReference` back to a PostgresCluster. Watch StatefulSets, and when one changes, look up its owner and enqueue *that*."

Mechanically, this:

1. Adds a shared informer on StatefulSets to the cache.
2. Wires an event handler that runs `EnqueueRequestForOwner` — extracts `metadata.ownerReferences[?(@.controller==true)]` and emits a reconcile request for that key.
3. Combined with `controllerutil.SetControllerReference(parent, child, scheme)` in your reconciler when you create the child, this gives you the correct loop:

```
   user updates PGC.spec    →    reconciler sees PGC event
                               ↓
                               creates/updates STS
                               ↓
                               STS status changes
                               ↓
                               Owns enqueues PGC
                               ↓
                               reconciler updates PGC.status
```

`Owns` only fires for objects whose *controller* ownerRef points to a `For` type. Multiple non-controller ownerRefs are ignored. This is correct because exactly one controller can be in charge.

### 19.3 `Watches`

```go
Watches(
    &corev1.Secret{},
    handler.EnqueueRequestsFromMapFunc(r.findClustersForSecret),
    builder.WithPredicates(predicate.LabelChangedPredicate{}),
)
```

For objects you *don't* own but *care about* — referenced secrets, ConfigMaps you didn't create, external resources. You supply:

- The type.
- A *mapping function*: given an event on the watched object, return zero-or-more reconcile.Requests for primary objects to enqueue.
- Optional predicates to filter the event stream.

The most common use case: secret rotation.

```go
// A user (or cert-manager) updates a Secret holding postgres credentials.
// Watches says: when this Secret changes, find every PostgresCluster that
// references it and enqueue them, so each reconciler can roll the
// connection or restart the pod.

func (r *PostgresClusterReconciler) findClustersForSecret(
    ctx context.Context, obj client.Object,
) []reconcile.Request {
    var list dbv1.PostgresClusterList
    if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
        return nil
    }
    var requests []reconcile.Request
    for _, pgc := range list.Items {
        if pgc.Spec.Auth != nil && pgc.Spec.Auth.SecretName == obj.GetName() {
            requests = append(requests, reconcile.Request{
                NamespacedName: types.NamespacedName{
                    Namespace: pgc.Namespace,
                    Name:      pgc.Name,
                },
            })
        }
    }
    return requests
}
```

This is how secret rotation triggers a rolling restart of dependent Deployments in cert-manager, sealed-secrets, external-secrets, vault-secrets-operator, and every operator that consumes mounted secrets.

### 19.4 Predicates

A predicate is a filter that says "this event is interesting; enqueue" or "this event is noise; drop." Built-in predicates:

- `GenerationChangedPredicate{}` — only fire when `metadata.generation` changes. Skips status-only updates.
- `LabelChangedPredicate{}` — only fire when labels change.
- `AnnotationChangedPredicate{}` — only fire when annotations change.
- `ResourceVersionChangedPredicate{}` — fire on every change including no-op (the default if you don't specify).

You can also compose with `predicate.And` / `predicate.Or` / `predicate.Not`, or write a custom predicate.

```go
For(&dbv1.PostgresCluster{},
    builder.WithPredicates(predicate.GenerationChangedPredicate{})).
```

This is critical for high-volume CRDs: by default every status write enqueues a reconcile, and your own status writes will trigger another reconcile, and so on. `GenerationChangedPredicate` on the primary type prevents the loop.

---

## 20. Webhook-Based Defaulting and Validation

CRDs support `default:` (schema) and `x-kubernetes-validations` (CEL). When you need more — defaulting based on a *lookup* (e.g., "if Region is empty, default to the cluster's region annotation"), validation that involves *other objects*, or complex multi-step transformations — you write an admission webhook (chapter 06 covers webhook plumbing in depth).

kubebuilder scaffolds these for you:

```bash
kubebuilder create webhook --group db --version v1 --kind PostgresCluster --defaulting --programmatic-validation
```

This produces `api/v1/postgrescluster_webhook.go`:

```go
package v1

import (
    "context"
    "fmt"

    apierrors "k8s.io/apimachinery/pkg/api/errors"
    runtime "k8s.io/apimachinery/pkg/runtime"
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/webhook"
    "sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

// +kubebuilder:webhook:path=/mutate-db-example-com-v1-postgrescluster,mutating=true,failurePolicy=fail,sideEffects=None,groups=db.example.com,resources=postgresclusters,verbs=create;update,versions=v1,name=mpostgrescluster.kb.io,admissionReviewVersions=v1

type PostgresClusterDefaulter struct{}

func (d *PostgresClusterDefaulter) Default(ctx context.Context, obj runtime.Object) error {
    pgc := obj.(*PostgresCluster)
    for i := range pgc.Spec.Instances {
        if pgc.Spec.Instances[i].Replicas == 0 {
            pgc.Spec.Instances[i].Replicas = 1
        }
    }
    if pgc.Spec.Backup != nil && pgc.Spec.Backup.S3 != nil && pgc.Spec.Backup.S3.Region == "" {
        pgc.Spec.Backup.S3.Region = "us-east-1"
    }
    return nil
}

// +kubebuilder:webhook:path=/validate-db-example-com-v1-postgrescluster,mutating=false,failurePolicy=fail,sideEffects=None,groups=db.example.com,resources=postgresclusters,verbs=create;update,versions=v1,name=vpostgrescluster.kb.io,admissionReviewVersions=v1

type PostgresClusterValidator struct{}

func (v *PostgresClusterValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
    return v.validate(ctx, nil, obj.(*PostgresCluster))
}
func (v *PostgresClusterValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
    return v.validate(ctx, oldObj.(*PostgresCluster), newObj.(*PostgresCluster))
}
func (v *PostgresClusterValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
    return nil, nil
}

func (v *PostgresClusterValidator) validate(ctx context.Context, oldPGC, newPGC *PostgresCluster) (admission.Warnings, error) {
    var warnings admission.Warnings
    if oldPGC != nil && newPGC.Spec.Version < oldPGC.Spec.Version {
        return nil, apierrors.NewBadRequest("downgrades are not supported")
    }
    if len(newPGC.Spec.Instances) > 1 && newPGC.Spec.Backup == nil {
        warnings = append(warnings, "running multi-instance without backup is discouraged")
    }
    return warnings, nil
}

func (in *PostgresCluster) SetupWebhookWithManager(mgr ctrl.Manager) error {
    return ctrl.NewWebhookManagedBy(mgr).
        For(in).
        WithDefaulter(&PostgresClusterDefaulter{}).
        WithValidator(&PostgresClusterValidator{}).
        Complete()
}
```

### 20.1 CEL vs webhooks for validation

We already discussed this in §4.3. The short version: prefer CEL inside the CRD; fall back to a webhook when CEL can't express it.

### 20.2 Defaulting: schema vs webhook

| Case                                                              | Tool        |
|-------------------------------------------------------------------|-------------|
| Static default value: `region: us-east-1`                         | Schema `default:` |
| Conditional default: "if HA, default replicas=3 else 1"           | Webhook     |
| Lookup default: "use the namespace's annotation as default region"| Webhook     |
| Default based on `oldObj` (carry-over default on update)          | Webhook     |

Schema defaults are simpler, faster, and guaranteed to run (no webhook to be down). Use them whenever possible. Use webhooks only for the cases above.

### 20.3 Webhook TLS lifecycle

The same as admission webhooks in chapter 06: a Service in the operator's namespace, certs issued by cert-manager and rotated automatically, the CA bundle injected into the `MutatingWebhookConfiguration` and `ValidatingWebhookConfiguration` (and `CustomResourceDefinition.spec.conversion.webhook.clientConfig.caBundle`) via `cert-manager.io/inject-ca-from`.

A common bundling pattern: one operator binary serves four kinds of webhooks on the same TLS endpoint — mutating admission, validating admission, conversion, and (rarely) custom subresource hooks. controller-runtime's `webhook.Server` multiplexes them by path.

---

## 21. OLM: ClusterServiceVersion, Subscription, CatalogSource, OperatorGroup

The Operator Lifecycle Manager (`olm.operatorframework.io`) is itself an operator — the meta-operator — whose job is to install, upgrade, and uninstall *other* operators. It is the package manager of the Kubernetes ecosystem. OpenShift ships it by default; on vanilla Kubernetes you install it explicitly (`olm.operatorframework.io/install`).

OLM is built around four resources.

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                              OLM                                     │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                      │
   │   ┌────────────────────────┐                                         │
   │   │     CatalogSource      │   a Pod that serves a catalog of CSVs   │
   │   │  e.g., operatorhubio   │   (an image index, gRPC-served)         │
   │   └───────────┬────────────┘                                         │
   │               │                                                      │
   │               │ "what packages exist?"                               │
   │               ▼                                                      │
   │   ┌────────────────────────┐                                         │
   │   │     Subscription       │   user says "install postgres-operator  │
   │   │  channel=stable        │   from this catalog, channel stable,    │
   │   │  installPlanApproval=  │   approve automatically"                │
   │   │   Automatic            │                                         │
   │   └───────────┬────────────┘                                         │
   │               │                                                      │
   │               ▼ OLM generates an InstallPlan,                        │
   │   ┌────────────────────────┐  resolves dependencies                  │
   │   │     InstallPlan        │                                         │
   │   └───────────┬────────────┘                                         │
   │               ▼                                                      │
   │   ┌────────────────────────┐                                         │
   │   │   ClusterServiceVersion│   the operator's bundle manifest:       │
   │   │    (CSV)               │   Deployment, RBAC, CRDs, webhooks,     │
   │   │   "the package"        │   icon, description, install modes      │
   │   └───────────┬────────────┘                                         │
   │               │                                                      │
   │               ▼                                                      │
   │   ┌────────────────────────┐                                         │
   │   │    OperatorGroup       │   "this operator may install into       │
   │   │                        │    these namespaces" (AllNamespaces /   │
   │   │                        │    OwnNamespace / SingleNamespace /     │
   │   │                        │    MultiNamespace)                      │
   │   └────────────────────────┘                                         │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
```

### 21.1 CatalogSource

A pod that serves a gRPC API listing available operators. Public catalogs include `operatorhubio-catalog`, `community-operators`, `certified-operators`. You can also run a private one (your company's internal catalog).

```yaml
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: operatorhubio-catalog
  namespace: olm
spec:
  sourceType: grpc
  image: quay.io/operatorhubio/catalog:latest
  displayName: Community Operators
  publisher: OperatorHub.io
  updateStrategy:
    registryPoll:
      interval: 60m
```

### 21.2 Subscription

The user's intent to install something.

```yaml
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: postgres-operator
  namespace: operators
spec:
  channel: stable
  name: postgres-operator
  source: operatorhubio-catalog
  sourceNamespace: olm
  installPlanApproval: Automatic   # or Manual (gate on a human)
  config:
    env:
      - name: WATCH_NAMESPACES
        value: "ns-a,ns-b"
```

### 21.3 OperatorGroup

Defines which namespaces an operator manages.

```yaml
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: operators
  namespace: operators
spec:
  targetNamespaces:
    - tenant-a
    - tenant-b
```

Modes:

- **AllNamespaces** (`targetNamespaces: []`): operator watches every namespace. Strongest, but most expensive (informer caches everything).
- **OwnNamespace** (`targetNamespaces: [<own-ns>]`): operator only manages its own namespace.
- **SingleNamespace** (one entry): operator watches one tenant namespace.
- **MultiNamespace** (multiple entries): the case shown above.

### 21.4 ClusterServiceVersion (CSV)

The big one. A CSV is a YAML document that describes:

- The operator's Deployment.
- The CRDs the operator owns and the CRDs it requires from other operators.
- The RBAC permissions the operator needs.
- The webhooks (admission + conversion) it serves.
- Display metadata (icon, description, keywords, maturity).
- The install modes it supports (which of the OperatorGroup modes above).
- The replacement chain (which CSV this one replaces).

```yaml
apiVersion: operators.coreos.com/v1alpha1
kind: ClusterServiceVersion
metadata:
  name: postgres-operator.v1.5.0
spec:
  displayName: Postgres Operator
  description: |
    Manages Postgres clusters: provisioning, HA failover, backups, restores,
    rolling upgrades.
  maturity: stable
  version: 1.5.0
  replaces: postgres-operator.v1.4.0
  minKubeVersion: 1.24.0
  keywords: [database, postgres]
  maintainers:
    - name: Example DB Team
      email: db-team@example.com
  installModes:
    - { type: OwnNamespace,     supported: true }
    - { type: SingleNamespace,  supported: true }
    - { type: MultiNamespace,   supported: true }
    - { type: AllNamespaces,    supported: false }
  install:
    strategy: deployment
    spec:
      clusterPermissions:
        - serviceAccountName: postgres-operator
          rules: [...]
      deployments:
        - name: postgres-operator
          spec:
            replicas: 2
            selector: { matchLabels: { app: postgres-operator } }
            template:
              metadata: { labels: { app: postgres-operator } }
              spec:
                serviceAccountName: postgres-operator
                containers:
                  - name: manager
                    image: quay.io/example/postgres-operator:v1.5.0
  customresourcedefinitions:
    owned:
      - name: postgresclusters.db.example.com
        version: v1
        kind: PostgresCluster
        displayName: Postgres Cluster
        description: A managed Postgres cluster.
  webhookdefinitions:
    - type: ValidatingAdmissionWebhook
      generateName: vpgc.kb.io
      admissionReviewVersions: [v1]
      containerPort: 9443
      targetPort: 9443
      rules: [...]
    - type: MutatingAdmissionWebhook
      generateName: mpgc.kb.io
      admissionReviewVersions: [v1]
      containerPort: 9443
      targetPort: 9443
      rules: [...]
    - type: ConversionWebhook
      generateName: cpgc.kb.io
      admissionReviewVersions: [v1]
      containerPort: 9443
      targetPort: 9443
      conversionCRDs:
        - postgresclusters.db.example.com
```

### 21.5 Upgrade flow

A new CSV (`postgres-operator.v1.6.0`) lands in the catalog. OLM:

1. Sees the new CSV via `CatalogSource` poll.
2. Generates an `InstallPlan` if the Subscription is `Automatic` (or waits for human approval if `Manual`).
3. Apply phase: applies the new CSV's CRD definitions (with new schemas), the new Deployment (which rolls out), the new webhook configurations.
4. The old CSV's `replaces` chain ensures the previous Deployment is removed once the new one is Ready.

**The breaking-schema trap.** If `v1.6.0`'s CRD schema removes a field, deletes an enum value, or tightens validation in a way that some live objects don't satisfy, the OLM upgrade fails on the validation step *or* (worse) succeeds and the old objects can't be read. There is no automatic schema-migration. The discipline: every CRD schema change is additive across minor versions, and if it isn't, bump the *CRD* version (§8, §11), not just the operator version.

### 21.6 Uninstall

OLM removes the Deployment, the webhook configurations, the RBAC, and the Subscription. By default it does *not* remove the CRDs or the custom resources, because removing a CRD cascades to deletion of every instance, which usually deletes managed external state — a foot-gun if uninstall was meant to be temporary.

OLM 1.x and OLM v1 (the newer, still-evolving design) differ in how aggressive cleanup is. Always confirm before uninstalling an operator in production.

---

## 22. OperatorHub.io

OperatorHub.io is the public registry for OLM bundles. Its catalog is served by the `operatorhubio-catalog` CatalogSource above. Anyone can submit an operator via PR to the `k8s-operatorhub/community-operators` repo on GitHub; the PR runs CI (scorecard tests, manifest validation) before merging.

For an operator author this is the equivalent of publishing to a package manager. You build an OLM bundle:

```
my-operator-bundle/
├── manifests/
│   ├── postgres-operator.clusterserviceversion.yaml
│   └── db.example.com_postgresclusters.yaml
├── metadata/
│   └── annotations.yaml
└── tests/
    └── scorecard/config.yaml
```

Build with `operator-sdk bundle validate ./bundle`, push to a registry, submit a PR with a `bundle.Dockerfile` reference. Once merged, your operator appears on operatorhub.io and is installable from any OLM-enabled cluster with a one-click install.

There are also commercial / certified registries (Red Hat Certified Operators, Red Hat Marketplace), and clouds run their own (AWS Marketplace for Containers, Google Cloud Operators).

---

## 23. Real-World Operators

A non-exhaustive map of operators you will encounter in production. Reading their source — they are all open — is the single best way to learn the patterns.

### 23.1 Databases

| Operator                                  | What it manages                              | Notable                                                                       |
|-------------------------------------------|-----------------------------------------------|-------------------------------------------------------------------------------|
| **CloudNativePG** (`cloudnative-pg/cloudnative-pg`) | Postgres clusters                              | Pure StatefulSet-free design; HA with streaming replication; pg_basebackup. |
| **Zalando postgres-operator**             | Postgres clusters                              | Tied to Zalando's stack; mature; cron-driven backups via WAL-E.              |
| **Crunchy PGO** (`CrunchyData/postgres-operator`) | Postgres clusters                              | Enterprise-targeted; full HA + PgBouncer + pgBackRest.                       |
| **KubeDB** (AppsCode)                     | Polyglot — Postgres, MySQL, Mongo, Elastic, etc. | One framework, many engines.                                                  |
| **MongoDB Community Operator**            | MongoDB ReplicaSets                            | Maintained by MongoDB Inc.                                                    |
| **Percona Operators**                     | MySQL/MongoDB/Postgres XtraDB clusters         | Multi-engine, multi-flavor.                                                   |
| **Strimzi** (`strimzi/strimzi-kafka-operator`) | Kafka, Connect, MirrorMaker, Bridge, ZK         | Reference Kafka operator; CNCF graduated.                                     |
| **Confluent for Kubernetes**              | Kafka + Schema Registry + ksqlDB               | Commercial.                                                                   |
| **Cassandra K8ssandra-operator**          | Cassandra + Reaper + Medusa                    | Multi-DC; uses the cass-operator under the hood.                              |
| **Redis Enterprise Operator**             | Redis Enterprise clusters                      | Commercial; manages REC + REDB.                                               |
| **Spotahome/redis-operator**              | Redis Sentinel                                 | Open-source community standard for Redis HA.                                  |
| **OT-Redis-Operator** (OpsTree)           | Redis clusters/Sentinel                        | Lightweight alternative.                                                      |
| **Elastic Cloud on Kubernetes (ECK)**     | Elasticsearch + Kibana + APM + Beats           | Maintained by Elastic.                                                        |

### 23.2 Infrastructure

| Operator                                  | What it manages                                                              |
|-------------------------------------------|-------------------------------------------------------------------------------|
| **cert-manager** (`cert-manager/cert-manager`) | TLS certificates — ACME, Vault, self-signed. CRDs: `Certificate`, `Issuer`. |
| **prometheus-operator** (`prometheus-operator/prometheus-operator`) | Prometheus, Alertmanager, ServiceMonitor, PodMonitor, PrometheusRule. The canonical infra operator. |
| **sealed-secrets** (`bitnami-labs/sealed-secrets`) | Encrypted Secrets you can commit to Git.                                |
| **external-secrets** (`external-secrets/external-secrets`) | Sync from Vault/AWS Secrets Manager/GCP Secret Manager/etc into Secrets. |
| **velero** (`vmware-tanzu/velero`)        | Backup/restore of cluster resources and PVs.                                  |
| **trivy-operator**                        | Scheduled vulnerability scans of running workloads.                           |
| **gatekeeper** (`open-policy-agent/gatekeeper`) | OPA policy enforcement via admission. CRDs: `ConstraintTemplate`, `Constraint`. |
| **kyverno**                               | Policy engine. CRDs: `ClusterPolicy`, `Policy`.                               |
| **descheduler-operator**                  | Periodically reshuffles pods to satisfy newer constraints.                    |
| **vertical-pod-autoscaler**               | Recommends/sets pod CPU+mem requests.                                         |
| **node-feature-discovery (NFD)** + **gpu-operator** | Hardware feature labels + driver installation.                              |

### 23.3 Networking

| Operator                                  | What it manages                                                               |
|-------------------------------------------|-------------------------------------------------------------------------------|
| **Istio operator / istioctl**             | Istio control plane and configuration.                                        |
| **Cilium CNI**                            | Cilium dataplane; CRDs: `CiliumNetworkPolicy`, `CiliumClusterwideNetworkPolicy`, `CiliumEgressGatewayPolicy`, etc. |
| **Calico operator**                       | Calico dataplane + Felix configuration.                                       |
| **Linkerd operator** (Buoyant)            | Linkerd control plane.                                                        |
| **MetalLB operator**                      | Bare-metal LoadBalancer ranges.                                               |
| **kuma**                                   | Kuma service mesh.                                                            |

### 23.4 Cloud

| Operator                                  | What it manages                                                               |
|-------------------------------------------|-------------------------------------------------------------------------------|
| **AWS Controllers for Kubernetes (ACK)**  | AWS resources via CRDs: S3 buckets, RDS, DynamoDB, IAM, etc.                  |
| **Config Connector** (Google)             | GCP resources via CRDs.                                                       |
| **Azure Service Operator (ASO)**          | Azure resources via CRDs.                                                     |
| **Crossplane** (§24)                      | Cloud-agnostic via Providers.                                                 |

These cloud operators are essentially "Kubernetes-as-control-plane for your cloud account." Apply a `Bucket` CR; an S3 bucket appears. Delete the CR; the bucket is gone (finalizer ensures it). Tag drift in the cloud → reconciled away or recorded in status.

---

## 24. Crossplane: Compositions and XRDs

Crossplane (`crossplane/crossplane`) is an operator that lets you *compose* CRDs and provision cloud resources using them. It deserves its own section because it inverts the usual operator authoring model.

### 24.1 Providers

A Provider is a packaged operator that exposes one cloud's APIs as CRDs.

```yaml
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-aws-s3
spec:
  package: xpkg.upbound.io/upbound/provider-aws-s3:v1
```

Installing this gives you `Bucket`, `BucketPolicy`, etc., all in the `s3.aws.upbound.io` group.

### 24.2 CompositeResourceDefinition (XRD)

An XRD defines a *composite* CRD — a higher-level abstraction. Example: `XPostgresCluster`, which is *your platform's* representation of a Postgres cluster, independent of cloud:

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: CompositeResourceDefinition
metadata:
  name: xpostgresclusters.platform.example.com
spec:
  group: platform.example.com
  names:
    kind: XPostgresCluster
    plural: xpostgresclusters
  claimNames:
    kind: PostgresCluster
    plural: postgresclusters
  versions:
    - name: v1alpha1
      served: true
      referenceable: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                parameters:
                  type: object
                  required: [size, region]
                  properties:
                    size:   { type: string, enum: [small, medium, large] }
                    region: { type: string }
```

### 24.3 Composition

A Composition tells Crossplane *what to create* for each instance of an XRD claim:

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: postgrescluster-aws
spec:
  compositeTypeRef:
    apiVersion: platform.example.com/v1alpha1
    kind: XPostgresCluster
  mode: Pipeline
  pipeline:
    - step: render-rds
      functionRef: { name: function-go-templating }
      input:
        apiVersion: gotemplating.fn.crossplane.io/v1beta1
        kind: GoTemplate
        source: Inline
        inline:
          template: |
            apiVersion: rds.aws.upbound.io/v1beta1
            kind: Instance
            spec:
              forProvider:
                engine: postgres
                instanceClass: {{ .observed.composite.resource.spec.parameters.size | sizeToClass }}
                region: {{ .observed.composite.resource.spec.parameters.region }}
```

### 24.4 What it all means

Application teams write `PostgresCluster` (the claim — a friendlier name). Platform teams write the XRD and Composition. Behind the scenes, Crossplane materializes RDS instances, security groups, parameter groups, IAM, monitoring — whatever the Composition says. Swapping cloud is changing the Composition, not the application code.

This is L3+ operator behaviour applied to *cloud infrastructure* itself. The tradeoff is significant cognitive load (XRDs, claims, compositions, providers, functions) for very high abstraction power.

---

## 25. Multi-Cluster Operators

Most operators run inside one cluster, watching one apiserver. Multi-cluster operators run in one cluster (or a control-plane cluster) and reconcile state across many clusters.

### 25.1 Why

- **Fleet operators.** A central operator that pushes the same config (PrometheusRule, NetworkPolicy) to many tenant clusters.
- **Federated workloads.** A `FederatedDeployment` that spawns Deployments in N member clusters.
- **Workspace-based platforms.** Each "workspace" is a logical cluster boundary, even if backed by one physical cluster.

### 25.2 multi-cluster-runtime

`sigs.k8s.io/multicluster-runtime` is an experimental extension of controller-runtime that lets a single Manager watch multiple clusters. Each cluster gets its own Cache + Client, and reconcile requests carry a cluster ID.

```go
// Pseudocode
mgr, _ := multicluster.NewManager(ctrl.GetConfigOrDie(), multicluster.Options{
    Provider: kubeconfigprovider.New("/etc/clusters/*.kubeconfig"),
})

(&FleetReconciler{}).SetupWithManager(mgr)
```

The Reconciler receives `req.ClusterName` along with `req.NamespacedName`. It uses `mgr.GetClient(req.ClusterName)` to access the right cluster.

### 25.3 KCP

KCP (`kcp-dev/kcp`) is a different model: a Kubernetes-API-compatible apiserver without Pods, with a notion of *workspaces*. Each workspace is its own apiserver-like surface. Controllers run in or around KCP, sync state to physical clusters via *syncers*, and reconcile across many workspaces.

KCP and multicluster-runtime are both early; production multi-cluster setups today usually pick one of:

- **Argo CD ApplicationSet** with cluster generators (chapter 31).
- **Cluster API** for cluster-of-clusters provisioning (separate concern).
- **Hub-and-spoke custom operator** that maintains its own kubeconfig per spoke and reconciles each.

### 25.4 The leader-election trap across clusters

If your multi-cluster operator is running as multiple replicas, the leader-election lease lives in *one* cluster (typically the hub). A leader operator can reconcile *any* of the spoke clusters; the lease is just for "which operator process is in charge."

But: if the hub apiserver is down, the lease cannot be renewed and *every* spoke reconciliation stops. This is an availability dependency from spokes onto the hub that does not exist in single-cluster operators. Plan for it (e.g., per-spoke local-cache rendezvous, or accept the dependency and harden the hub).

---

## 26. Testing Operators

Operators are unusually hard to test because they combine "Go code that mutates objects" with "the apiserver, which has nontrivial semantics" with "a third-party system whose state we drive." Three layers of tests, each with the right tool.

### 26.1 envtest

`sigs.k8s.io/controller-runtime/pkg/envtest` spins up a real `kube-apiserver` and `etcd` binary as subprocesses, gives you their kubeconfig, and lets your Reconciler run against them. There is no kubelet, no scheduler, no controllers — *just the API surface*. This is the right test environment for almost every reconciler.

```go
// suite_test.go
var (
    testEnv   *envtest.Environment
    cfg       *rest.Config
    k8sClient client.Client
)

func TestMain(m *testing.M) {
    testEnv = &envtest.Environment{
        CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
        ErrorIfCRDPathMissing: true,
    }
    var err error
    cfg, err = testEnv.Start()
    if err != nil { panic(err) }

    scheme := runtime.NewScheme()
    _ = clientgoscheme.AddToScheme(scheme)
    _ = dbv1.AddToScheme(scheme)

    k8sClient, err = client.New(cfg, client.Options{Scheme: scheme})
    if err != nil { panic(err) }

    // Start the manager in a goroutine.
    mgr, _ := ctrl.NewManager(cfg, ctrl.Options{Scheme: scheme})
    (&PostgresClusterReconciler{Client: mgr.GetClient(), Scheme: scheme}).SetupWithManager(mgr)
    go mgr.Start(ctx)

    code := m.Run()
    testEnv.Stop()
    os.Exit(code)
}

func TestReconcileCreatesStatefulSet(t *testing.T) {
    pgc := &dbv1.PostgresCluster{
        ObjectMeta: metav1.ObjectMeta{Name: "pgc-1", Namespace: "default"},
        Spec: dbv1.PostgresClusterSpec{
            Version: "16",
            Instances: []dbv1.InstanceSpec{{
                Name: "main", Replicas: 1,
                Storage: dbv1.StorageSpec{Size: "1Gi"},
            }},
        },
    }
    require.NoError(t, k8sClient.Create(ctx, pgc))

    require.Eventually(t, func() bool {
        var sts appsv1.StatefulSet
        err := k8sClient.Get(ctx, client.ObjectKey{Namespace: "default", Name: "pgc-1"}, &sts)
        return err == nil
    }, 10*time.Second, 100*time.Millisecond)
}
```

The crucial property: envtest uses the *real* apiserver, so CRD schema validation, CEL rules, conversion webhooks (you can wire them in), and admission webhooks all run. You can test "does my reconciler do the right thing when the apiserver rejects this write?" Yes — because the apiserver in envtest *does* reject it.

### 26.2 Fake client

`sigs.k8s.io/controller-runtime/pkg/client/fake` provides a `Client` that stores objects in an in-memory map. It is much faster than envtest but does not run the apiserver — so it does not enforce schema validation, doesn't run admission, doesn't run CEL, doesn't fire watches, doesn't increment generation correctly until recent versions. Use it for *unit* tests of pure functions in the reconciler that take a Client and return a value, not for full Reconcile tests.

```go
import "sigs.k8s.io/controller-runtime/pkg/client/fake"

c := fake.NewClientBuilder().
    WithScheme(scheme).
    WithObjects(somePGC).
    WithStatusSubresource(&dbv1.PostgresCluster{}).
    Build()

result, err := r.Reconcile(ctx, request)
```

The fake client improved a lot over the last two years (status subresource support, generation tracking, indexing). But it still cannot run admission webhooks or CEL. Any test that depends on those — most reconciler tests do — should use envtest.

### 26.3 Chainsaw and kuttl

End-to-end tests that exercise *real* clusters (typically `kind` or `k3d` for CI).

- **kuttl** (`kuttl.dev`): a test runner that lets you write tests as YAML — apply this, wait for these conditions, assert this state. Mature, widely used, slowed in development.
- **chainsaw** (`kyverno/chainsaw`): newer, more capable test runner with similar YAML-driven approach plus richer assertion semantics, JSON-schema/CEL assertions, parallel test cases.

A typical chainsaw test:

```yaml
apiVersion: chainsaw.kyverno.io/v1alpha1
kind: Test
metadata:
  name: pgc-basic
spec:
  steps:
    - try:
        - apply: { file: pgc.yaml }
        - assert:
            file: pgc-ready.yaml
    - cleanup:
        - delete: { file: pgc.yaml }
        - error:
            file: pgc-deleted.yaml
```

where `pgc-ready.yaml` asserts:

```yaml
apiVersion: db.example.com/v1
kind: PostgresCluster
metadata:
  name: pgc-1
status:
  phase: Ready
  conditions:
    - { type: Ready, status: "True" }
```

E2E tests are slow (minutes per case) and brittle (any cluster flake breaks them), but they're the only way to test admission + conversion + reconcile + child controllers all together. Run them in CI on every PR.

### 26.4 The pyramid

```
                    ┌──────────────────┐
                    │   E2E (chainsaw) │   slow, integrated, brittle
                    └──────────────────┘
                  ┌────────────────────────┐
                  │    envtest tests       │   fast (sec each), real apiserver
                  └────────────────────────┘
                ┌──────────────────────────────┐
                │    unit tests (fake client + │  fastest, narrow
                │    pure functions)           │
                └──────────────────────────────┘
```

Most of your tests should be envtest. A few unit tests for pure helpers (label-set builders, condition merges) and a handful of chainsaw tests for cross-cutting smoke tests.

---

## 27. Performance: Informer Memory, Cache Scoping, Backpressure

Operators are not free. Each one is at minimum a couple of informers, each holding the full set of objects of a type, indexed in memory. At scale this is the dominant cost.

### 27.1 The informer memory model

Per object, in informer memory:

- Roughly the size of the decoded Go struct (a deepcopied `PostgresCluster` is maybe 4 KB on a heap; a Pod, 8–20 KB).
- Plus index overhead (the namespace index, label index, custom indexes you registered).

For a controller watching Pods cluster-wide on a 5000-node cluster running 50 pods each = 250,000 pods × ~12 KB = ~3 GB resident. That's *just* the informer.

### 27.2 Cache scoping

```go
mgr, _ := ctrl.NewManager(cfg, ctrl.Options{
    Cache: cache.Options{
        // Option A: only specific namespaces
        DefaultNamespaces: map[string]cache.Config{
            "tenant-a": {},
            "tenant-b": {},
        },

        // Option B: per-GVK selectors
        ByObject: map[client.Object]cache.ByObject{
            &corev1.Pod{}: {
                Label: labels.SelectorFromSet(labels.Set{
                    "app": "postgres",
                }),
            },
            &corev1.Secret{}: {
                Field: fields.SelectorFromSet(fields.Set{
                    "type": "Opaque",
                }),
            },
        },
    },
})
```

Cache scoping is the single largest lever you have. A label-scoped Pod informer that only matches the operator's own pods cuts a 3 GB informer to a few MB.

The cost is RBAC: every selector you put in the cache becomes a `list+watch` against the apiserver with that selector, which must be permitted by RBAC. Generally fine — you `get,list,watch` on Pods cluster-wide or per-namespace as appropriate.

### 27.3 Per-namespace caches

A `MultiNamespaceCache` was the old way to scope to N namespaces. Since controller-runtime 0.16 it is `DefaultNamespaces` in `cache.Options`. The effect is the same: instead of one cluster-wide informer, you have one informer per namespace, each only listing/watching its namespace. Memory scales linearly with namespaces; for hundreds of namespaces this can become its own cost.

### 27.4 Watch backpressure

The apiserver pushes watch events to clients. If a client doesn't drain, the apiserver buffers and eventually closes the watch (the dreaded `watch closed: too many watchers backlogged`). This manifests in an operator as:

- Reconciliation queue growing.
- `etcd_request_duration_seconds` rising.
- Repeated `relist` events as watches restart.

Causes:

- Single-threaded reconcile + many objects: `MaxConcurrentReconciles: 1` with 10k objects means a slow reconcile creates a queue.
- A reconcile that calls out to a slow external API while holding queue position.
- Too many shared informers stacking up CPU on the apiserver side.

Mitigations:

- Increase `MaxConcurrentReconciles` (but: per-key serialization is preserved; multiple workers help only across keys).
- Predicates to filter noise out before it enqueues.
- Cache scoping (above).
- Move expensive external calls out of the reconcile loop into a separate worker pool that posts results back.

### 27.5 Per-CRD informer cost

Each *new* CRD type the controller watches adds:

- A new watch connection to the apiserver.
- A new in-memory cache.
- Recurring resync overhead.

A cluster with 200 CRDs and operators each watching, on average, 6 of them ends up with ~1200 watch connections from operator pods alone. Each is a goroutine on both sides. This is *generally fine* but it stacks up faster than people expect, and it shows up as apiserver memory growth.

---

## 28. Operator vs Helm

A frequent question: "I have a Helm chart. Why would I write an operator?"

```
   ┌───────────────────────────────────────────────────────────────────────┐
   │                        Helm                  Operator                 │
   ├───────────────────────────────────────────────────────────────────────┤
   │                                                                       │
   │  Model       templated YAML            controller running forever     │
   │  Action      one-shot install/upgrade  continuous reconciliation      │
   │  Rollback    helm rollback (snapshot)  re-apply CR, controller acts   │
   │  State       in the cluster + secret   in the CR + status             │
   │              (release object)                                         │
   │  Drift       not detected; helm does    detected on every loop;       │
   │              not re-apply on its own    auto-corrected                │
   │  Upgrade     templates re-evaluated     controller knows how to       │
   │              client-side                upgrade safely (e.g., bumping │
   │                                         postgres major version w/    │
   │                                         pg_upgrade)                   │
   │  Lifecycle   install/upgrade/uninstall  full lifecycle including      │
   │              are explicit user actions  backups, restores, failover  │
   │  Day-2 ops   user runs scripts          baked into the controller    │
   │  RBAC        broad — Helm runs as user  narrow — controller has its  │
   │                                         own SA with scoped perms     │
   │  Skill       template authors          Go authors                    │
   │                                                                       │
   └───────────────────────────────────────────────────────────────────────┘
```

**Helm is one-shot.** It renders templates and applies them. Nothing checks afterwards that reality matches; if a user `kubectl edit`s a Helm-installed Deployment, Helm does not know and does not undo. Day-2 operations (backups, failovers, restores, version upgrades) are out of scope — you write playbooks.

**An operator is continuous.** A reconcile fires after every event on the watched objects, and every ~10 minutes via resync regardless. Drift is auto-corrected. Day-2 operations are encoded inside the controller.

For *static* applications — pick a config, install, walk away — Helm is plenty. For *stateful* applications — databases, message queues, anything where steady-state operations need expertise — an operator wins. Many projects ship *both*: Helm for "install the operator," then a CR for the actual application. This is the cleanest pattern (the operator itself is static infrastructure; the application is the CR).

A subtler point: operators *cooperate* with GitOps (chapter 31) where Helm does not. GitOps reconciles the CR; the operator reconciles the application. Two independent loops, separated by the spec/status boundary. With Helm, GitOps reconciles every templated Deployment/Service/etc directly, and any controller (HPA, the operator's STS controller) that mutates those fields races GitOps.

---

## 29. Pitfalls: The Long List

Every operator author makes these. Knowing them in advance reduces the count.

### 29.1 Edge-triggered controllers

You read `req.Name`, do work, return. You did not query the current state — you assumed the event told you what changed. Now you missed an event during a watch reconnect and the application is wrong. Chapter 08 covers why this is wrong; the fix is to always `r.Get(ctx, req.NamespacedName, &obj)` at the top of Reconcile and reconcile against *that*, not against any cached idea of what changed.

### 29.2 Spec mutation by the operator

The operator decides that `spec.backup.s3.region` should default to `us-east-1` if empty, and writes it back. GitOps observes the diff, applies the empty value, the operator sets it again, infinite loop. Defaults go in the CRD schema or a defaulting webhook, *never* in the reconciler.

### 29.3 Status overwriting SSA fields

The operator does `r.Status().Update(ctx, &pgc)`, replacing the entire conditions array. Another controller (or this one across a restart with a different code path) had written conditions of types you don't know about. Those get wiped. Use `r.Status().Patch(...)` with a strategic merge or SSA, and declare `conditions` as `x-kubernetes-list-type: map`.

### 29.4 Finalizer never removed

The finalize handler errors out, the operator never strips the finalizer, the object is stuck Terminating forever. The user has to `kubectl patch ... --type=merge -p '{"metadata":{"finalizers":null}}'` to escape, which also abandons whatever cleanup was incomplete. Make finalize idempotent and survivable; log error states; emit metrics; never panic.

### 29.5 Conversion webhook drops fields

You ship v2 with a new field `spec.cipher`, but your `ConvertFrom(v1)` doesn't know about it (it didn't exist in v1). Some user `kubectl get -o yaml`s the object via the v1 endpoint, edits, applies it back. Conversion v1→v2 drops `cipher` because it wasn't in the v1 representation. Object silently loses configuration. Fix: round-trip every field through annotations on lossy conversions, or never reduce the new version's exposed fields below what's in the storage.

### 29.6 No structural schema

You ship a CRD with a loose schema (`additionalProperties: true` everywhere, no types declared). The apiserver refuses to apply it on 1.16+, or pruning misbehaves on older clusters. Fix: write a real OpenAPI schema, every level typed, before shipping.

### 29.7 HPA without scale subresource

You enable the status subresource but forget `scale`. A user runs `kubectl autoscale postgrescluster pgc-1 --min=1 --max=5 --cpu-percent=70`. HPA creates the HPA object, then errors with `cannot find ReplicaController/Deployment/etc for scale target`. Add the scale subresource with all three JSONPaths.

### 29.8 OwnerRef cycles

Two CRDs each set ownerReferences pointing at the other. The Kubernetes GC sees a cycle, refuses to delete either, both are stuck. Owner refs must form a DAG; a child has one *controller* ownerRef and any number of non-controller ownerRefs, none of which can point at descendants.

### 29.9 Reconcile reading from apiserver

The reconciler calls `r.Get` with `client.Options{Raw: ...}` against the apiserver instead of the cache. (Or it calls the typed clientset directly.) On a high-volume CRD, this hammers the apiserver. Read from the cache (the default `r.Get`) and trust eventual consistency.

### 29.10 Long reconcile times

Your reconcile takes 30 seconds because it shells out to AWS to verify a tag. The workqueue backs up; other CRs stall. Move slow external work to a worker pool, post results to a channel, and let the reconcile read those results from a local cache.

### 29.11 Cluster-wide operator without cache scoping

The operator is `AllNamespaces` scope but cares about a single label. Without `cache.Options.ByObject`, every Pod (or Secret, or ConfigMap) cluster-wide is in your informer. The first time you deploy it on a 5000-node cluster you OOM.

### 29.12 Multiple operators on the same CRD

Two vendors ship operators that both `For(&Issuer{})`. They both try to reconcile, both write status, both stomp each other. Symptom: status flapping between two reconciled-by signatures. There is no Kubernetes-level lock; the convention is "one operator per CRD type, register that ownership somewhere visible." If two operators must coexist, namespace them by label selector or field selector.

### 29.13 CSV with hardcoded namespace

Your CSV mentions `db-system` in Deployment.Spec.Template.Spec.Containers[].Env. When OLM installs into a different namespace (per `OperatorGroup.targetNamespaces`), the operator looks for things in the wrong namespace and fails. Always use `$(NAMESPACE)` / downward API for the operator's own namespace; never hardcode.

### 29.14 OLM upgrade with breaking schema

`v1.6.0` removes an enum value from the CRD that some existing objects have. OLM applies the new CRD, the apiserver rejects existing objects on the next write (or on conversion). Live workloads are immutable until you re-enable the old enum or rewrite every object. The discipline is: schema-level changes follow the CRD versioning model (§11), not the operator versioning model.

### 29.15 Cross-cluster without leader election

Your multi-cluster operator runs as two replicas. Both have full credentials for both clusters. They both reconcile. Both create Deployments. Both write status. Half the writes fail with conflicts. Add leader election; without it, multi-replica reconcile is broken.

### 29.16 Updating spec inside Reconcile

A subtler variant of §29.2: the operator updates *its own* CR's spec to expose a derived value ("the actually computed replica count"). This bumps `metadata.generation`, which re-enqueues, which re-reconciles, which writes again. Spec is for users; status is for you.

### 29.17 Not bumping CRD version on incompatible changes

You change `spec.replicas` from `int` to `string` in v1 because "we found a typed library that does it that way." Existing v1 objects can no longer be deserialized; every `kubectl get postgrescluster` errors. There is no rollback (the storage still has the old bytes). The fix is always: incompatible change → new CRD version → conversion webhook → storage migration.

### 29.18 Reconcile depends on event ordering

`If I see an Add, do X; if I see Update, do Y.` This is edge-triggered (§29.1). Plus DeltaFIFO can collapse events. Plus controller restarts see everything as Add via List. Forget event ordering. Reconcile from the current state.

### 29.19 Status conditions without observedGeneration

Conditions tell us "this is the state right now." But which spec did "now" correspond to? Without `observedGeneration` per condition (or at least on the status object), the user can't tell whether a `Ready: True` is from the current spec or a stale one. Always set `observedGeneration: pgc.Generation` when writing conditions.

### 29.20 Reconcile loop in an infinite ratelimited churn

A bug causes Reconcile to return `error` every time. The workqueue's exponential rate limiter retries with backoff. Hours later, the queue is full of retries, the operator is hot, and the user can't tell that anything is wrong because the object's status hasn't changed. Always emit metrics on terminal errors; alert on `controller_runtime_reconcile_errors_total` rising.

### 29.21 Cache reads in webhook handlers

A validating webhook calls `r.Client.Get(...)` to consult some other object. The Client reads from the cache, which may not be primed yet at startup. Symptom: the first few requests after a restart fail validation because referenced objects "don't exist." Webhook handlers should use the *direct* client (`mgr.GetAPIReader()`), which bypasses the cache, or wait for cache sync before serving.

### 29.22 Webhook fails-open by accident

`failurePolicy: Ignore` on a mutating webhook. The webhook crashes; the apiserver shrugs and accepts the object without your mutation. Workloads now run without the sidecar / label / annotation your operator depended on, and your reconciler is confused. `Fail` is the safe default; `Ignore` only when downtime of the webhook must not block production writes — and even then, with thorough alerting.

### 29.23 Missing leader-election lease termination

The operator pod is killed (SIGKILL, OOM, node crash). The leader-election lease is held for its full `LeaseDuration` (typically 15s) before another replica can take over. During those 15s, *nothing* reconciles. Set tight `LeaseDuration` / `RenewDeadline` / `RetryPeriod` for fast failover, balanced against apiserver load from too-fast renews.

### 29.24 RBAC over-grants

The kubebuilder marker `+kubebuilder:rbac:groups="",resources=*,verbs=*` ships an operator with cluster-admin equivalent on core. Security review fails. Always scope to the resources you actually touch; regenerate manifests; review the diff.

### 29.25 Conversion webhook returns mutated metadata

The conversion handler `dst.ObjectMeta = src.ObjectMeta` but then *also* changes `dst.ResourceVersion` or `dst.UID`. The apiserver rejects the conversion with `metadata mismatch`. Conversion is on `spec` and `status` only; copy ObjectMeta verbatim, modify nothing.

### 29.26 Logger from outside the reconcile context

```go
r.Logger.Info("reconciled", "name", req.Name)
```

vs

```go
log.FromContext(ctx).Info("reconciled", "name", req.Name)
```

The first uses a global logger; the second pulls the logger from `ctx`, which controller-runtime has already enriched with `controller`, `controllerGroup`, `controllerKind`, `name`, `namespace`, and `reconcileID`. The second gives you per-reconcile tracing. Use it.

### 29.27 Apply without field manager

`r.Patch(ctx, obj, client.Apply)` without `client.FieldOwner("pg-operator")` uses the default field manager name (`controller-runtime`). Two controllers in the same binary both default and fight over fields. Always set `FieldOwner` explicitly per controller.

### 29.28 Forgetting to add the type to the scheme

`go test` fails with `no kind is registered for the type v1.PostgresCluster in scheme`. You forgot `_ = dbv1.AddToScheme(scheme)` in `main.go` (or in the test bootstrap). Always register every type the operator manages in the same scheme it constructs the Manager with.

---

## 30. TL;DR

**CRDs are the door; operators walk through.** A CRD declares a typed resource. The apiserver enforces the schema, stores it as JSON in etcd, serves it under `/apis/<group>/<version>/...`, and gives you all the kubectl/RBAC/watch/GC machinery for free. An operator is a controller that watches the CRD and reconciles it to real-world state. Together they let you extend Kubernetes without forking.

**The CRD object is `apiextensions.k8s.io/v1.CustomResourceDefinition`** with `group`, `scope` (Namespaced/Cluster), `names`, and `versions[]`. Each version has `served`, `storage` (exactly one is `true`), a structural OpenAPI v3 schema, optional subresources (`status`, `scale`), and optional `additionalPrinterColumns`. Conversion between versions is `None` (identical) or `Webhook` (you serve a ConversionReview API).

**Structural schemas are required.** Every level typed, every field declared. The apiserver prunes unknown fields, applies `default:`s, and runs CEL `x-kubernetes-validations` server-side. CEL replaces 90% of what you used to do in a validating webhook — cross-field rules, immutability, dynamic messages — with no extra moving parts. Use a webhook only when CEL can't express it (cross-object lookups, complex business logic).

**`x-kubernetes-list-type: map`** is the single highest-impact extension you'll set. It tells SSA how lists merge; setting `atomic` (the default) on a list of objects guarantees that two clients will stomp each other. `map` with `x-kubernetes-list-map-keys` makes per-entry ownership work.

**The status subresource separates spec-writes from status-writes** at the URL and RBAC level. Users get `update` on the main URL; the operator gets `update` on `/status`. `metadata.generation` only bumps on spec writes. The `scale` subresource maps your CRD's replica fields onto the generic Scale object, enabling HPA, `kubectl scale`, and any external autoscaler.

**Multi-version CRDs evolve via a hub-and-spoke conversion** in a webhook. Each spoke implements `ConvertTo(hub)` and `ConvertFrom(hub)`; the framework composes for any pair. The webhook must be idempotent, fast, lossless (or annotate dropped fields), and never on the apiserver. Storage version migration is a separate operation — flipping `storage: true` doesn't rewrite existing objects; the storage-version migrator does.

**Versioning strategy: v1alpha1 → v1beta1 → v1.** Each promotion gives more compatibility guarantees. Once you stamp a version `v1`, you can only add optional fields and loosen constraints. Incompatible changes always mean a new version.

**RBAC is per-verb and per-subresource.** `postgresclusters`, `postgresclusters/status`, `postgresclusters/scale`, `postgresclusters/finalizers` are four distinct grants. Use the `aggregate-to-admin/edit/view` labels to attach to built-in roles.

**The operator pattern is the controller pattern from chapter 08, applied to a CRD.** Watch the CR, materialize subordinate resources (Deployments, Services, Secrets, cloud APIs, external systems), write observed state back to `status`. The reconciler is a pure function from object state to side effects.

**The Capability Levels (L1–L5)** are a maturity model: L1 install, L2 upgrades, L3 full lifecycle, L4 deep insights, L5 auto pilot. Most production operators are L3+L4.

**kubebuilder/operator-sdk scaffold the project.** Go types with `+kubebuilder:` markers generate the CRD YAML, RBAC, webhooks, DeepCopy. `make manifests && make generate` is the inner loop. `main.go` builds a controller-runtime Manager; `internal/controller/` holds the Reconciler.

**controller-runtime layers are Manager → Cache → Client → builder → Controller → Reconciler.** The Manager owns shared resources (cache, client, webhook server, metrics, leader election). The Cache is a set of shared informers. The Client reads from the Cache and writes to the apiserver. The builder wires `For` + `Owns` + `Watches` + predicates. The Reconciler is your code.

**The canonical reconcile flow:** Get the CR; handle deletion via finalizer; register the finalizer before any external state; init status; reconcile children with `CreateOrUpdate` + `SetControllerReference`; compute observed state; patch status; requeue if not Ready. Spec is read; status is written via `r.Status().Patch()`. Conditions are a map by type, with `observedGeneration` on each.

**Finalizers gate cleanup of external state** that the GC doesn't know about (cloud LBs, DNS records, backup buckets, external DBs). Register *before* creating external state. Remove *after* verifying cleanup completed. The finalize handler retries forever until clean.

**`Owns` enqueues the parent when a child changes.** Set with `controllerutil.SetControllerReference`. `Watches` enqueues something *else* via a mapping function — the secret-rotation pattern: a Secret changes, find every CR that references it, enqueue them all. Predicates filter event streams before they hit the workqueue.

**Webhook-based defaulting + validation** complements CEL: defaults that depend on lookups, validations that consult other objects, mutations that can't be expressed declaratively. kubebuilder scaffolds the server and the webhook configurations. TLS via cert-manager; CA bundle injected via `cert-manager.io/inject-ca-from`.

**OLM packages operators** as CSVs (the manifest), Subscriptions (the user's intent), CatalogSources (the registry), OperatorGroups (the namespace scoping). One-click install on OpenShift and any OLM-enabled cluster. OperatorHub.io is the public registry.

**Real-world operators span everything:** databases (CloudNativePG, Strimzi, Cassandra k8ssandra, MongoDB, Redis-operator, Elastic ECK), infra (cert-manager, prometheus-operator, sealed-secrets, external-secrets, velero, gatekeeper, kyverno), networking (Istio, Cilium, Linkerd, MetalLB), and cloud (ACK, Config Connector, ASO, Crossplane). Reading their source is the best operator-authoring tutorial.

**Crossplane inverts the model:** XRDs define composite types, Compositions define what gets materialized per instance, Providers expose cloud APIs. Application teams write claims; platform teams write Compositions. Cloud-agnostic infrastructure-as-CR.

**Multi-cluster operators** are still emerging (multicluster-runtime, KCP). The simplest production approach is per-cluster operators coordinated by GitOps from a central repo (chapter 31).

**Testing pyramid: envtest > unit tests with fake client > chainsaw/kuttl e2e.** envtest runs a real apiserver and is the right default for reconciler tests. The fake client is fast but can't run admission or CEL. Chainsaw/kuttl exercise the full stack; keep them few and meaningful.

**Performance: cache scoping is the lever.** A cluster-wide Pod informer on 5000-node clusters is gigabytes; a label-selected one is megabytes. Per-namespace caches help in fleet-of-namespaces scenarios. Predicates filter before enqueueing. `MaxConcurrentReconciles` helps when reconciles are independent but per-key serialization is preserved.

**Operator vs Helm: continuous reconciliation vs one-shot install.** Helm is fine for static apps; an operator wins the moment day-2 operations are non-trivial. The hybrid pattern — Helm to install the operator, CR to deploy the app — is the cleanest.

**Pitfalls are predictable:** spec mutation by the operator, status overwriting SSA fields, finalizer never removed, conversion dropping fields, missing structural schema, HPA without scale subresource, ownerRef cycles, reconcile reading from apiserver, overly broad RBAC, cluster-wide cache without scoping, two operators on the same CRD, OLM upgrade with breaking schema, missing `observedGeneration` on conditions, edge-triggered logic, webhook fail-open accidents, leader-election lease too long, missing field manager on Apply. Each one is somebody's outage; knowing them in advance turns each into a five-minute conversation in design review instead of a postmortem.

The CRD-plus-operator pair is the recursive primitive that lets Kubernetes be Kubernetes for *whatever you have*. Once you can write a CRD, a schema, a Reconciler, a finalizer, a status with conditions, and a conversion webhook, you can teach the Kubernetes apiserver about *anything* — and every kubectl, every dashboard, every GitOps engine, every RBAC policy, every backup tool, every security scanner already knows how to read it. That is the whole point.
