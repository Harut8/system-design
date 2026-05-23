# GitOps, Helm, and Kustomize: A Staff-Level Deep Dive

A staff-engineer reference for how desired state actually gets into a Kubernetes cluster in 2026. By the time you reach this chapter you have a cluster (ch 03), an apiserver (ch 05), admission (ch 06), RBAC (ch 07), controllers (ch 08), workload controllers (ch 12, 13), networking (ch 14–17), storage (ch 19), autoscaling (ch 22), and the ability to extend the API with CRDs and operators (ch 23). You can run *anything*. What you have not yet decided is **who pushes the YAML, where the YAML lives, who notices when reality drifts from it, and how a fresh cluster ever gets its first object**. That is the GitOps problem, and it has eaten the deployment-tooling space.

This chapter sits between operators (ch 23 — operators publish CRs; GitOps engines manage who owns which fields of those CRs) and cluster lifecycle (ch 32 — DR is "reapply the Git state to a freshly-bootstrapped cluster") and multi-cluster (ch 26 — `ApplicationSet` is how a single ArgoCD drives a fleet). If chapter 08 taught you the reconcile loop in client-go, this chapter is about a *meta-reconcile loop* whose desired state lives in Git, whose actual state lives in etcd, and whose error term is `git diff`.

We will go through the four GitOps principles, pull versus push, the four roles in the pipeline, ArgoCD's seven processes and three core CRDs, sync waves and phases, health assessment with Lua, `ApplicationSet` and its seven generators, drift detection, `ignoreDifferences`, Server-Side Apply with Argo, multi-tenancy via `AppProject`, secret management, notifications, the entire Flux GitOps Toolkit (six controllers, ten or so CRDs), Flux image automation, Helm v3 from `Chart.yaml` to release secrets to hooks to library charts, Kustomize from `kustomization.yaml` to overlays to components, the perennial Helm-vs-Kustomize debate, render-then-apply pipelines, Argo Rollouts and Flagger for progressive delivery, PR previews, multi-cluster GitOps topologies, the bootstrap pattern, sealed-secrets and ESO and SOPS, the `spec.replicas` fight with HPA, observability of the GitOps engine itself, and a long ledger of anti-patterns and pitfalls that the next platform team is statistically going to hit.

If chapter 23 was "how do you extend the API," this is "now that you have ten thousand objects across forty clusters authored by sixty teams, who is the source of truth?" The answer — boring, correct, and battle-tested — is **a Git repository continuously reconciled by a controller that has nothing else to do**.

---

## Table of Contents

1. [Why GitOps Exists](#1-why-gitops-exists)
2. [The Four GitOps Principles](#2-the-four-gitops-principles)
3. [Pull vs Push GitOps](#3-pull-vs-push-gitops)
4. [The Four Roles in a GitOps Pipeline](#4-the-four-roles-in-a-gitops-pipeline)
5. [Repository Layout Patterns](#5-repository-layout-patterns)
6. [ArgoCD: The Component Graph](#6-argocd-the-component-graph)
7. [ArgoCD Core CRDs: Application, AppProject, ApplicationSet](#7-argocd-core-crds-application-appproject-applicationset)
8. [Application Sync: Manual, Automated, Prune, SelfHeal](#8-application-sync-manual-automated-prune-selfheal)
9. [Sync Waves and Sync Phases](#9-sync-waves-and-sync-phases)
10. [Health Assessment and Custom Lua](#10-health-assessment-and-custom-lua)
11. [App-of-Apps](#11-app-of-apps)
12. [ApplicationSet Generators](#12-applicationset-generators)
13. [Drift Detection and Self-Heal](#13-drift-detection-and-self-heal)
14. [ignoreDifferences: Sharing a Spec with Other Actors](#14-ignoredifferences-sharing-a-spec-with-other-actors)
15. [Server-Side Apply with Argo](#15-server-side-apply-with-argo)
16. [ArgoCD Multi-Tenancy via AppProject](#16-argocd-multi-tenancy-via-appproject)
17. [ArgoCD Secrets](#17-argocd-secrets)
18. [ArgoCD Notifications](#18-argocd-notifications)
19. [Flux: The GitOps Toolkit](#19-flux-the-gitops-toolkit)
20. [Flux Core CRDs](#20-flux-core-crds)
21. [Flux Image Automation](#21-flux-image-automation)
22. [Flux Multi-Tenancy](#22-flux-multi-tenancy)
23. [ArgoCD vs Flux](#23-argocd-vs-flux)
24. [Helm v3 Internals](#24-helm-v3-internals)
25. [Helm Templating: Sprig, Helpers, Capabilities](#25-helm-templating-sprig-helpers-capabilities)
26. [Helm Hooks and Tests](#26-helm-hooks-and-tests)
27. [Helm + ArgoCD / Helm + Flux](#27-helm--argocd--helm--flux)
28. [Kustomize: Resources, Patches, Generators](#28-kustomize-resources-patches-generators)
29. [Kustomize Overlays and Components](#29-kustomize-overlays-and-components)
30. [Helm vs Kustomize: The Honest Comparison](#30-helm-vs-kustomize-the-honest-comparison)
31. [Render-Then-Apply Pipelines](#31-render-then-apply-pipelines)
32. [Progressive Delivery: Argo Rollouts and Flagger](#32-progressive-delivery-argo-rollouts-and-flagger)
33. [PR Previews](#33-pr-previews)
34. [Multi-Cluster GitOps](#34-multi-cluster-gitops)
35. [Bootstrap Pattern](#35-bootstrap-pattern)
36. [Secrets in GitOps](#36-secrets-in-gitops)
37. [The Fight Over spec.replicas](#37-the-fight-over-specreplicas)
38. [Tools Beyond Argo and Flux](#38-tools-beyond-argo-and-flux)
39. [Anti-Patterns](#39-anti-patterns)
40. [Observability of GitOps](#40-observability-of-gitops)
41. [Pitfalls: The Long List](#41-pitfalls-the-long-list)
42. [TL;DR](#42-tldr)

---

## 1. Why GitOps Exists

Imagine you have one cluster, four teams, and a CI server. The way you deploy is `kubectl apply` from a Jenkins/GitHub Actions/CircleCI job, run after a merge to `main`. This works. For a while.

Then you have ten clusters. Now your CI has to authenticate to all ten, hold credentials for all ten, know which manifests target which cluster, and serialize deploys so two teams don't trample each other. Each cluster has to expose an inbound kube-apiserver endpoint to the internet (or to your CI runner pool), which the security team hates.

Then someone runs `kubectl edit deployment` in prod to fix a bad hour at 2am. CI is now wrong about reality. The next deploy mysteriously rolls back the fix; everyone re-pages.

Then a cluster dies. To rebuild it you replay every CI job that ever ran against it, in order, hoping each one is idempotent. They aren't. You spend the weekend manually re-applying.

Then a team forks the manifest repo, doesn't tell anyone, and ships a side-channel deploy. CI doesn't know about it. The wedge between *what CI thinks is deployed* and *what is actually running* widens daily.

This is the problem GitOps solves. The reframe is:

- **The cluster is downstream of Git, not downstream of CI.** CI builds images. Git stores manifests. A controller inside the cluster pulls those manifests and reconciles them. The cluster is a *consumer* of declarative state, not a target of imperative pushes.
- **Reality is continuously checked against Git, not just once at deploy time.** If someone runs `kubectl edit`, the controller notices and either overwrites or alerts.
- **There is exactly one source of truth for what should be running, ever.** That source is a Git ref. Roll back by reverting a commit. Audit by reading the log. Authorize by reading branch protection rules.

The implementation cost is one controller per cluster (Argo or Flux) and the discipline never to do anything in-cluster that isn't reflected in Git. The benefit is that all the failures above stop being unique outages and start being the same well-understood class: *Git is the spec; the controller will figure the rest out*.

Once you internalize this, you stop reading the GitOps chapter as "another tool" and start reading it as "the same level-triggered reconcile pattern from chapter 08, applied to your entire cluster, with the cache replaced by Git". The Argo Application controller and the Flux Kustomization controller are *normal Kubernetes controllers*: they watch CRs, they have informers, they have workqueues, they reconcile a desired state against an actual state. The only twist is that their "desired" cache is a `git clone`.

---

## 2. The Four GitOps Principles

The Weaveworks team (which coined the term in 2017) and the CNCF OpenGitOps working group converged on a canonical four-point definition. Memorize these; they are the rubric you use to evaluate every tool in the space.

1. **Declarative.** The state of the system is expressed declaratively. Not "run these five commands"; rather, "this is what should exist". Kubernetes objects (YAML) are inherently declarative — that's why Kubernetes is the perfect substrate for GitOps. If your tool requires sequential imperative commands to converge, it isn't GitOps.

2. **Versioned and immutable.** The declared state is stored in a system that supports versioning and immutability — i.e., Git. (Or any equivalent: OCI artifacts, S3 buckets with versioning, etc. Most production setups use Git.) Every state is identifiable by a content hash (the commit SHA), every transition is auditable, every change has an author.

3. **Automatically pulled.** Software agents automatically pull the desired state from the source of truth. No human runs `kubectl apply`. No CI pipeline runs `kubectl apply`. A controller in the cluster (Argo's Application controller, Flux's source+kustomize controllers) pulls Git and applies it.

4. **Continuously reconciled.** Software agents continuously observe the actual state and reconcile it against the declared state. This is the level-triggered piece. Drift gets corrected (or at least alerted on) regardless of how it was introduced. Reconciliation runs on a timer (typically every 3 minutes for Argo, configurable for Flux) *plus* on every relevant Git change *plus* on every relevant Kubernetes object change (informer-driven).

That fourth principle is the one most "GitOps-flavored" tools miss. CI pipelines that run `kubectl apply` on merge satisfy 1, 2, and a partial 3 — but they don't continuously reconcile. They fire once and forget. If someone `kubectl edit`s the cluster five minutes later, CI doesn't know. Real GitOps engines continuously diff live state against rendered Git state and either alert or auto-correct.

**Why Kubernetes is the perfect target.** Three reasons. First, Kubernetes' API is fundamentally declarative — every object has a spec (desired) and a status (observed), and the apiserver doesn't care whether you POST or PATCH the same object a thousand times; the final state is what matters. This makes idempotent reconcile trivially achievable. Second, Kubernetes natively supports watch streams — the GitOps controller doesn't have to poll every object every reconcile cycle; it can subscribe to changes. Third, Kubernetes already has RBAC, namespaces, and CRDs — the GitOps controller can express *its own* desired state (Applications, Kustomizations) as Kubernetes objects, store them in etcd, and benefit from all the same machinery.

Compare to imagined GitOps for a fleet of EC2 instances or a Cloud Foundry deployment: you'd have to build the reconcile primitives yourself. Kubernetes hands them to you.

---

## 3. Pull vs Push GitOps

There are two ways the cluster gets desired state.

```
   ┌────────────────────────────── PUSH-BASED ──────────────────────────────┐
   │                                                                        │
   │   developer                                                            │
   │      │  git push                                                       │
   │      ▼                                                                 │
   │   ┌──────┐         ┌──────┐                                            │
   │   │ Git  │────────▶│  CI  │                                            │
   │   └──────┘ webhook └──┬───┘                                            │
   │                       │  kubectl apply (over the internet)             │
   │                       ▼                                                │
   │             ┌─────────────────┐                                        │
   │             │ kube-apiserver  │  ◀── must be reachable from CI         │
   │             │   (cluster)     │       (inbound network exposure)       │
   │             └─────────────────┘                                        │
   │                                                                        │
   │   Pros: simple, familiar, no agent on cluster                          │
   │   Cons: apiserver must be reachable; CI holds credentials; no          │
   │         continuous reconcile; CI is on the deploy critical path        │
   └────────────────────────────────────────────────────────────────────────┘

   ┌────────────────────────────── PULL-BASED ──────────────────────────────┐
   │                                                                        │
   │   developer                                                            │
   │      │  git push                                                       │
   │      ▼                                                                 │
   │   ┌──────┐                                                             │
   │   │ Git  │  ◀── polled / webhook-notified                              │
   │   └──┬───┘                                                             │
   │      │                                                                 │
   │      │  git clone / pull (cluster-initiated, outbound only)            │
   │      ▼                                                                 │
   │   ┌─────────────────────────────────────────────────┐                  │
   │   │ Cluster                                          │                 │
   │   │   ┌─────────────────────┐                       │                  │
   │   │   │  GitOps controller  │  ──┐                  │                  │
   │   │   │  (Argo / Flux)      │    │  apply (in-cluster) │               │
   │   │   └─────────────────────┘    ▼                  │                  │
   │   │            ┌─────────────────────┐              │                  │
   │   │            │   kube-apiserver    │  ◀── no inbound exposure        │
   │   │            └─────────────────────┘              │                  │
   │   └─────────────────────────────────────────────────┘                  │
   │                                                                        │
   │   Pros: cluster can be private; no shared credentials; continuous      │
   │         reconcile; CI not on the deploy critical path                  │
   │   Cons: needs an agent in cluster; one more component to operate       │
   └────────────────────────────────────────────────────────────────────────┘
```

**Why pull dominates.** Three structural reasons.

First, **network direction**. In a pull model the cluster makes an outbound connection to Git (and, for image pulling, to a registry). The cluster's apiserver does not need to be reachable from CI runners, the office network, or the public internet. In a multi-cluster fleet — twenty environments across three regions and two clouds — this is the difference between zero inbound rules and one set of inbound rules per cluster. Security teams love it.

Second, **credential blast radius**. In a push model, your CI has cluster-admin (or close to it) for every cluster you deploy to. If the CI server is compromised, every cluster is compromised. In a pull model, each cluster's GitOps controller has credentials only for its own cluster (and read-only credentials for Git). Compromise of a single cluster doesn't pivot.

Third, **continuous reconciliation**. A push-based CI pipeline runs once per commit and forgets. A pull-based agent runs on a timer (and on watches), so drift is corrected regardless of how it was introduced. The fourth GitOps principle is essentially incompatible with pull, which is why all "real" GitOps tooling — Argo, Flux, Jenkins X, Fleet — is pull-based.

The remaining argument for push is operational simplicity in a tiny shop (one cluster, one team, no fleet ambitions). For anything that grows, pull wins.

A hybrid sometimes appears: CI builds images, updates a manifest in Git (often via image-automation in Flux or a write-back in Argo Image Updater), and the pull-based engine takes it from there. This is the common modern pattern.

---

## 4. The Four Roles in a GitOps Pipeline

A well-run GitOps pipeline has four distinct roles and refuses to let them blur.

```
   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
   │              │      │              │      │              │      │              │
   │  Developer   │─────▶│  Reviewer    │─────▶│   Git repo   │─────▶│   Engine     │
   │  (commits)   │  PR  │  (approves)  │ merge│  (truth)     │ pull │ (Argo/Flux)  │
   │              │      │              │      │              │      │              │
   └──────────────┘      └──────────────┘      └──────────────┘      └──────┬───────┘
                                                                            │ apply
                                                                            ▼
                                                                  ┌──────────────────┐
                                                                  │   cluster        │
                                                                  └──────────────────┘
```

1. **Developer (or platform user).** Writes the desired-state YAML — or, more often, opens a PR that bumps a Helm value, a Kustomize image tag, or a CR field. They never touch the cluster directly. They never even know which cluster their change lands on; that's the engine's problem.

2. **Reviewer.** Reads the PR diff, approves or rejects. This is where policy enforcement happens (does the change violate org rules? did CI green? is the appropriate owner signing off?). Branch protection rules (CODEOWNERS, required approvals, required status checks) are the *only* enforcement mechanism in pure GitOps. There is no in-cluster admission webhook that asks "did Alice approve this?"; the apiserver only ever sees an apply from the trusted engine.

3. **Repository.** The Git repo (or repos) is the source of truth. Every state the cluster has ever been in is reconstructible from a Git ref. Audit is `git log`. Rollback is `git revert`. The repo's history *is* the cluster's history.

4. **Engine.** The GitOps controller (Argo's Application controller, Flux's kustomize-controller, etc.) reads the repo, renders manifests, applies them, watches for drift. The engine is the only writer to the cluster (modulo other in-cluster controllers and operators, which have their own legitimate writes — more on the spec.replicas fight in §37).

The separation matters because **audit** and **least privilege** flow from it.

- Developers have no cluster credentials at all. They have *Git* credentials, scoped to the manifest repo, often scoped to specific directories.
- Reviewers have no cluster credentials. They have *Git review* permissions.
- The engine has *Kubernetes* credentials (typically cluster-admin within scoped projects/namespaces; see §16 for AppProject scoping). It does not have Git write permissions.

This is the inverse of a push pipeline, where CI has both Git read and cluster write — and where a compromise of CI compromises everything.

The audit trail is two-stage. Who proposed it? `git log` on the manifest repo. What did they propose? The diff. Who approved it? PR metadata. When did it land? Merge commit timestamp. When was it applied to the cluster? Argo/Flux logs and the apiserver audit log. Two separate audit systems cover the two halves, and the engine is the bridge — its logs say "I applied commit `abc123` to cluster `prod-us-east`".

---

## 5. Repository Layout Patterns

There are roughly four canonical repo topologies. Pick one and stick with it; mixing causes pain.

### 5.1 Mono-repo with overlay-per-environment

```
manifests/
├── apps/
│   ├── frontend/
│   │   ├── base/
│   │   │   ├── deployment.yaml
│   │   │   ├── service.yaml
│   │   │   └── kustomization.yaml
│   │   └── overlays/
│   │       ├── dev/kustomization.yaml
│   │       ├── staging/kustomization.yaml
│   │       └── prod/kustomization.yaml
│   └── backend/
│       └── ...
└── clusters/
    ├── dev-us-east/
    ├── staging-us-east/
    └── prod-us-east/
```

The platform team owns `clusters/*/` (which Applications/Kustomizations exist on each cluster); product teams own `apps/<their-app>/`. Environments are overlays, not branches.

### 5.2 Repo-per-team

Each team has its own repo. The platform team's `clusters/*` repo references each team's repo via Argo `ApplicationSet` or Flux `Kustomization`. This scales organisationally — teams don't see each other's manifest changes — but adds friction for cross-team dependencies.

### 5.3 Separate "config" and "deploy" repos (render-then-apply)

Source-of-truth repo holds Helm charts and Kustomize bases. A CI job renders them to a separate deploy repo on every merge. The GitOps engine watches only the deploy repo. This pattern is covered in §31; the value is that the deploy repo PR diff shows the *exact* YAML that will hit the cluster.

### 5.4 Cluster-of-clusters

For multi-cluster fleets: one repo describes the platform layer (ArgoCD installation, CRDs, ingress controllers, observability) and is applied to every cluster. Tenants live in per-tenant repos referenced by `ApplicationSet`. The "cluster-of-clusters" is what an `ApplicationSet` with a Cluster generator instantiates.

**Branch-per-environment is the canonical anti-pattern.** Tempting because git-native, fatal because merging fixes from `dev` to `staging` to `prod` is a constant source of "we forgot to cherry-pick" outages. Overlay-per-environment is the answer (§29).

---

## 6. ArgoCD: The Component Graph

ArgoCD is the most-deployed GitOps engine. Its source tree (`argoproj/argo-cd`) splits into seven (or so, depending on version) distinct processes, each a normal Kubernetes Deployment. Understanding which one does what is the difference between debugging in seconds and debugging for hours.

```
                              ┌─────────────────────────────────────┐
                              │              Git repo               │
                              └──────────────┬──────────────────────┘
                                             │ git clone (every 3min default)
                                             ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  argocd namespace                                                        │
   │                                                                          │
   │  ┌────────────────────┐   gRPC   ┌─────────────────────────────────┐    │
   │  │ argocd-server      │ ◀──────▶ │ argocd-application-controller    │    │
   │  │ (UI + API + gRPC)  │          │   (the reconcile engine,         │    │
   │  │  argo-cd/cmd/argocd-server                  sharded by app)     │    │
   │  └────────┬───────────┘          └──────────┬──────────────────────┘    │
   │           │                                  │                           │
   │           │ uses                             │ uses                      │
   │           ▼                                  ▼                           │
   │  ┌────────────────────┐          ┌─────────────────────────────────┐    │
   │  │ argocd-repo-server │ ◀────────│  cache (Redis)                  │    │
   │  │ (clone, render     │          │  argocd-redis                    │    │
   │  │  helm/kustomize)   │          └─────────────────────────────────┘    │
   │  │  cmd/argocd-repo-server                                              │
   │  └────────────────────┘                                                  │
   │                                                                          │
   │  ┌────────────────────────────┐  ┌──────────────────────────────────┐   │
   │  │ argocd-applicationset-     │  │ argocd-notifications-controller  │   │
   │  │ controller                 │  │ (Slack/email/webhook on events) │   │
   │  │ (generators → Applications)│  └──────────────────────────────────┘   │
   │  └────────────────────────────┘                                          │
   │                                                                          │
   │  ┌────────────────────────────┐                                          │
   │  │ argocd-dex-server          │   ◀── OIDC bridge for UI/CLI auth        │
   │  │ (IdP proxy, optional)      │                                          │
   │  └────────────────────────────┘                                          │
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘
                                             │  apply (server-side or normal)
                                             ▼
                                  ┌─────────────────────────┐
                                  │ target kube-apiserver(s)│
                                  └─────────────────────────┘
```

Component by component:

**`argocd-server`** is the UI, the REST API, the gRPC API, and the CLI's endpoint. It owns no controller logic — it's a stateless frontend that reads from Redis and the apiserver and proxies commands to the application controller. Source: `cmd/argocd-server/` and `server/` in `argoproj/argo-cd`. Scale this horizontally; it's the bottleneck for big web UIs and CLI traffic, never for sync throughput.

**`argocd-application-controller`** is the reconciler. It watches `Application` CRs, decides which need syncing, asks repo-server for a rendered manifest, computes the diff against live state, and applies. As of Argo 2.x it is *sharded* — you can run N replicas, each owning a subset of Applications by namespace/cluster hash. The flag is `--shard` plus `ARGOCD_CONTROLLER_REPLICAS`, and the source is `controller/` and `cmd/argocd-application-controller/`. The reconcile timer (default 3m, set by `timeout.reconciliation` in `argocd-cm`) is the floor on how quickly drift is noticed.

**`argocd-repo-server`** clones Git repos, renders manifests (with Helm, Kustomize, raw YAML, or a config-management plugin), and returns the rendered JSON to the application controller. It is stateless and disk-cached. This is the CPU-and-network-heavy component: scale it to handle the *number of distinct app sources*, not the number of clusters. Source: `cmd/argocd-repo-server/` and `reposerver/`.

**`argocd-redis`** caches: rendered manifests, computed diffs, OIDC sessions. Plain Redis. Default deployment is a single replica (acceptable; it's a cache, not state of record). For HA, use `argocd-redis-ha` with Sentinel.

**`argocd-applicationset-controller`** watches `ApplicationSet` CRs and generates child `Application` objects from generators. Source: `applicationset/` and `cmd/argocd-applicationset-controller/`. Note: in older Argo this was a separate project; it's now part of `argo-cd`.

**`argocd-notifications-controller`** watches Applications and fires templated notifications to Slack/Teams/email/webhooks/etc. on state transitions. Source: `notifications_controller/` and `cmd/argocd-notification/`.

**`argocd-dex-server`** is an embedded Dex (`dexidp/dex`) for federating OIDC into Argo's web UI and CLI. Optional — you can also use Argo's local user store or wire a different OIDC IdP directly via the `dex.config` in `argocd-cm`.

**Add-ons in the Argo family** (not strictly part of `argo-cd`):
- `argocd-image-updater` (separate repo, `argoproj-labs/argocd-image-updater`) watches container registries and writes new image tags back to Git.
- `argo-rollouts` (separate repo, `argoproj/argo-rollouts`) is the progressive-delivery controller; see §32.
- `argo-workflows` (separate repo, `argoproj/argo-workflows`) is a workflow engine; not a GitOps tool but commonly used for CI inside the cluster.
- `argo-events` (separate repo, `argoproj/argo-events`) is an event source; used to wire Argo Workflows.

The **mental model**: Argo's "controller" is application-controller; the "renderer" is repo-server; the "frontend" is argocd-server; the "fanout" is applicationset-controller; the "cache" is Redis; the "alerts" are notifications-controller; the "login" is dex. Each is one Deployment. Each you debug independently.

---

## 7. ArgoCD Core CRDs: Application, AppProject, ApplicationSet

Argo's "API" — what platform users actually write — is three CRDs.

### 7.1 Application

The fundamental unit: "this source in Git should be deployed to this destination cluster/namespace."

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: frontend
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io  # cascade-delete managed resources
spec:
  project: ecommerce
  source:
    repoURL: https://github.com/acme/manifests
    path: apps/frontend/overlays/prod
    targetRevision: main
  destination:
    server: https://kubernetes.default.svc   # in-cluster
    namespace: frontend
  syncPolicy:
    automated:
      prune: true        # delete resources removed from Git
      selfHeal: true     # revert in-cluster changes that drift
      allowEmpty: false  # never sync to an empty manifest set
    syncOptions:
      - CreateNamespace=true
      - PrunePropagationPolicy=foreground
      - ServerSideApply=true
      - RespectIgnoreDifferences=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
  revisionHistoryLimit: 10
```

Field by field, the load-bearing parts:

- `spec.project` — names the `AppProject` this Application belongs to. Controls RBAC and permitted sources/destinations. (§16.)
- `spec.source` — single source variant. A `sources` plural (Argo 2.6+) exists for multi-source Applications (e.g., Helm chart from one repo, values file from another).
- `spec.source.repoURL` — Git URL (or Helm chart repo URL, or OCI registry URL).
- `spec.source.path` — directory inside the repo. For Helm charts in an OCI/Helm repo, use `chart` instead.
- `spec.source.targetRevision` — branch, tag, commit SHA, or Helm chart version. Use a tag or SHA for prod; `main` for dev.
- `spec.source.helm` / `spec.source.kustomize` / `spec.source.directory` — type-specific knobs (values files, image overrides, recurse flags). Argo auto-detects whether the source is Helm or Kustomize based on the presence of `Chart.yaml` or `kustomization.yaml`.
- `spec.destination.server` — the target cluster's API server URL. `https://kubernetes.default.svc` means the cluster Argo is running in.
- `spec.destination.namespace` — namespace into which to apply (cluster-scoped resources ignore this; namespaced resources default here if their metadata doesn't override).
- `spec.syncPolicy.automated` — if present, Argo auto-syncs on OutOfSync. If absent, sync is manual (user clicks "Sync" in UI or runs `argocd app sync`).
- `prune` — delete in-cluster resources that have been removed from Git. Without this, removing a Deployment from your repo *does not* delete it from the cluster.
- `selfHeal` — re-apply when drift is detected. Without this, drift is reported as OutOfSync but not corrected.
- `allowEmpty` — guard against an empty Git tree causing wholesale deletion. Set to `false` in prod.
- `syncOptions` — a string array of flags. Common: `CreateNamespace=true` (create the destination namespace if missing), `ServerSideApply=true` (use SSA; see §15), `RespectIgnoreDifferences=true` (apply `ignoreDifferences` during sync, not just diff).
- `ignoreDifferences` — list of fields to ignore in diff/sync. The HPA-vs-replicas example is canonical (§14).
- `revisionHistoryLimit` — how many past sync revisions to keep for rollback. Default 10.

### 7.2 AppProject

The multi-tenancy boundary. Restricts which sources and destinations Applications in this project may target, and grants RBAC roles.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: ecommerce
  namespace: argocd
spec:
  description: "E-commerce team applications"
  sourceRepos:
    - https://github.com/acme/manifests
    - https://github.com/acme/charts
  destinations:
    - server: https://kubernetes.default.svc
      namespace: 'frontend-*'
    - server: https://kubernetes.default.svc
      namespace: 'backend-*'
    - server: https://prod-eu.example.com
      namespace: 'frontend-*'
  clusterResourceWhitelist:
    - group: ''
      kind: Namespace
    - group: rbac.authorization.k8s.io
      kind: ClusterRole
  namespaceResourceBlacklist:
    - group: ''
      kind: ResourceQuota
  roles:
    - name: deployer
      policies:
        - p, proj:ecommerce:deployer, applications, sync, ecommerce/*, allow
      groups:
        - acme:ecommerce-deployers
  syncWindows:
    - kind: deny
      schedule: '0 22 * * *'
      duration: 8h
      applications:
        - '*'
      manualSync: true
```

Why this matters: Argo runs cluster-admin (typically) on every target cluster. Without `AppProject`, any Application could deploy anything anywhere. With `AppProject`, the ecommerce team's apps can only target `frontend-*`/`backend-*` namespaces on specific clusters, can only pull from specific repos, can only create whitelisted cluster-scoped resources, and can't touch ResourceQuotas. `syncWindows` block syncs during change-freeze hours.

### 7.3 ApplicationSet

The fleet driver. Generates Applications from a template plus generators (§12).

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: frontend-per-cluster
  namespace: argocd
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            env: prod
  template:
    metadata:
      name: 'frontend-{{name}}'
    spec:
      project: ecommerce
      source:
        repoURL: https://github.com/acme/manifests
        path: apps/frontend/overlays/{{metadata.labels.env}}
        targetRevision: main
      destination:
        server: '{{server}}'
        namespace: frontend
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

This one ApplicationSet generates one Application per cluster labeled `env=prod`, pointing each at the appropriate overlay. Add a new prod cluster: it gets `frontend` deployed automatically. Delete a cluster: its Application is removed.

---

## 8. Application Sync: Manual, Automated, Prune, SelfHeal

Sync is the action of taking the rendered Git state and pushing it to the cluster.

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  Application reconcile cycle (every 3 min by default)            │
   │                                                                  │
   │  1. repo-server: git clone @ targetRevision                      │
   │  2. repo-server: render manifests (helm template / kustomize)    │
   │  3. application-controller: compare rendered → live              │
   │  4. set status.sync.status: Synced | OutOfSync                   │
   │  5. set status.health.status: Healthy | Degraded | ...            │
   │  6. if OutOfSync AND syncPolicy.automated:                       │
   │       a. acquire lock (per-app)                                  │
   │       b. apply ordered by sync waves and phases                  │
   │       c. wait for health = Healthy (with timeout)                │
   │       d. record sync result in status.operationState             │
   │  7. emit metrics, fire notifications                             │
   └──────────────────────────────────────────────────────────────────┘
```

**Manual sync** means the user (or CI, via `argocd app sync` CLI) explicitly triggers a sync. Argo *still* diffs continuously; it just doesn't auto-apply. This is the model for prod when you want a human in the loop.

**Automated sync** means Argo applies as soon as it detects drift from Git. Three knobs:

- **`prune: true`** — Argo deletes in-cluster resources that are no longer present in Git. Without this, your repo and your cluster will diverge: you can add things via Git, but never remove them.

- **`selfHeal: true`** — When Argo detects that live state differs from Git state (someone ran `kubectl edit`, an admission webhook mutated something, an HPA changed replicas), it re-applies. Without this, drift is *reported* (OutOfSync) but not *corrected*. SelfHeal is what enforces Git as the source of truth.

- **`allowEmpty: false`** — Hard-fail if the rendered manifest set is empty. The safety against a misconfigured kustomization or accidentally-deleted directory wiping a cluster.

**OutOfSync** is a per-resource state. An Application is OutOfSync if any of its tracked resources is OutOfSync. Per-resource OutOfSync reasons:

- The resource doesn't exist in the cluster (missing).
- The resource exists but differs from the rendered Git version.
- An extra resource exists in the cluster that isn't in Git (only flagged if pruning is on).

Argo's diff is field-by-field on the *managed* fields. For client-side-apply (the historical default), Argo manages every field it sets. For server-side-apply (modern; §15), Argo manages only the fields it owns via `fieldManager=argocd-controller`, and ignores fields owned by other actors (HPA, the admission webhook, etc.).

The `Refresh` operation forces an immediate diff without sync. `Hard Refresh` re-clones Git and re-renders, bypassing repo-server's cache.

---

## 9. Sync Waves and Sync Phases

Order matters. You cannot create a `Deployment` before its `CustomResourceDefinition` is established. You cannot run a database migration before the database exists. Argo solves ordering with **sync waves** (an annotation-driven priority within a sync) and **sync phases** (lifecycle hooks around a sync).

### 9.1 Sync waves

Annotation: `argocd.argoproj.io/sync-wave`. Integer (negative or positive). Default 0.

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: postgresqls.acid.zalan.do
  annotations:
    argocd.argoproj.io/sync-wave: "-10"
---
apiVersion: v1
kind: Namespace
metadata:
  name: postgres-system
  annotations:
    argocd.argoproj.io/sync-wave: "-5"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres-operator
  annotations:
    argocd.argoproj.io/sync-wave: "0"
---
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: my-db
  annotations:
    argocd.argoproj.io/sync-wave: "5"
```

Argo applies all wave-`-10` resources, waits for them to be Healthy, then applies wave-`-5`, and so on. Within a wave, no order is guaranteed; across waves, strict ascending order. The canonical use is "CRDs in wave -10, operators in wave 0, CRs in wave 5" so the operator is up by the time the CR lands.

The wait between waves is bounded by the resource's *health* (§10). A `Deployment` in wave -5 is "done" when it reaches Healthy (i.e., available replicas matches desired). A `Namespace` is Healthy as soon as it exists. A custom resource is Healthy per its custom health check.

### 9.2 Sync phases

A sync has five phases, each annotation-controllable:

- **PreSync** — runs before the main Sync phase. Typical use: database migration job, cache warm-up, schema bootstrap.
- **Sync** — the actual apply. Default for any resource without a phase annotation.
- **PostSync** — runs after Sync completes and all resources are Healthy. Typical use: smoke tests, cache invalidation, notify external systems.
- **SyncFail** — runs only if the Sync phase fails. Typical use: cleanup of partially-applied state, alert.
- **Skip** — resource is ignored.

Phase annotation: `argocd.argoproj.io/hook`.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migrate
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: acme/migrator:1.4.0
          command: ["./migrate.sh"]
```

`hook-delete-policy` options: `HookSucceeded` (delete after success), `HookFailed` (delete after failure), `BeforeHookCreation` (delete previous hook before creating new one). Without this, hook resources accumulate.

**Ordering rules:**
1. PreSync hooks run, in wave order, until all are Healthy.
2. Sync phase runs, in wave order, until all are Healthy.
3. PostSync hooks run, in wave order, until all are Healthy.
4. If any of (1)–(3) fail, SyncFail hooks run.

Sync waves and phases compose: PreSync wave -10 runs before PreSync wave 0, which runs before Sync wave -10, which runs before Sync wave 0.

The implementation lives in `controller/sync.go` in `argo-cd`; the relevant types are `SyncTaskWave` and `HookType` in `pkg/apis/application/v1alpha1/types.go`.

---

## 10. Health Assessment and Custom Lua

Sync waves and phases need to know when a resource is "Healthy" — when the next wave can proceed. Argo has built-in health checks for the standard Kubernetes types and an extension mechanism (Lua scripts) for custom resources.

### 10.1 Built-in health

The built-in checks (source: `util/lua/health.lua` and `controller/health/`) cover:

- **Deployment** — Healthy when `status.observedGeneration == metadata.generation` AND `status.updatedReplicas == spec.replicas` AND `status.availableReplicas == spec.replicas`.
- **StatefulSet** — similar: `status.observedGeneration == metadata.generation` AND `status.updatedReplicas == status.replicas` AND `status.readyReplicas == spec.replicas`.
- **DaemonSet** — `status.observedGeneration == metadata.generation` AND `status.updatedNumberScheduled == status.desiredNumberScheduled` AND `status.numberAvailable == status.desiredNumberScheduled`.
- **PersistentVolumeClaim** — Bound or WaitForFirstConsumer.
- **Service** — Healthy if LoadBalancer has an ingress IP, otherwise Healthy by default for ClusterIP.
- **Pod** — Running with all containers Ready, or Succeeded.
- **Job** — Succeeded.
- **Ingress** — Healthy when LoadBalancer status has at least one ingress.
- **CertificateSigningRequest** — Approved + Issued.

Argo Health states are: `Healthy`, `Progressing`, `Degraded`, `Suspended`, `Missing`, `Unknown`. `Progressing` is the "waiting" state; `Degraded` is "this is broken, sync will fail".

### 10.2 Custom Lua scripts

For CRDs, Argo lets you register a Lua script that takes the object and returns a health status. Configured in the `argocd-cm` ConfigMap under `resource.customizations.health.<group>_<kind>`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-cm
  namespace: argocd
data:
  resource.customizations.health.acid.zalan.do_postgresql: |
    hs = {}
    if obj.status ~= nil then
      if obj.status.PostgresClusterStatus ~= nil then
        if obj.status.PostgresClusterStatus == "Running" then
          hs.status = "Healthy"
          hs.message = "Cluster is running"
          return hs
        end
        if obj.status.PostgresClusterStatus == "Creating" or obj.status.PostgresClusterStatus == "Updating" then
          hs.status = "Progressing"
          hs.message = obj.status.PostgresClusterStatus
          return hs
        end
      end
    end
    hs.status = "Progressing"
    hs.message = "Waiting for postgresql status"
    return hs
```

The Lua VM is a sandboxed gopher-lua interpreter; you have access to `obj` (the CR's full JSON) and return a table with `status` and `message`. The check runs every reconcile.

Argo also ships **built-in customisations** for popular CRDs in `resource_customizations/` in the repo (PostgresOperator, Istio VirtualService, cert-manager Certificate, etc.). If your CRD has one upstream, you don't need to write your own.

> Note on `configManagementPlugins`: an older mechanism that ran arbitrary tools to render manifests. **Deprecated** as of Argo 2.4; replaced by sidecar-based plugins on the repo-server. The custom-health-Lua mechanism is what remains for custom resources, and it is *not* deprecated.

---

## 11. App-of-Apps

The classic bootstrap pattern. You have one cluster and want to deploy ten apps to it. Rather than creating ten Applications, you create one Application that points at a directory of Application manifests. Argo discovers them, creates them, and reconciles them transitively.

```
    repo/
    └── apps/
        ├── root.yaml         ← the parent Application
        ├── frontend.yaml     ← child Application
        ├── backend.yaml      ← child Application
        ├── postgres.yaml     ← child Application
        └── ingress.yaml      ← child Application
```

```yaml
# repo/apps/root.yaml — applied manually once
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/acme/cluster-prod-us-east
    path: apps
    targetRevision: main
    directory:
      recurse: false
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

You apply `root.yaml` once. The root Application's source is `apps/`, which contains other Application manifests. Argo sees them, applies them (creating sibling Applications in the `argocd` namespace), and each of *those* reconciles its own source.

```yaml
# repo/apps/frontend.yaml — managed by root
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: frontend
  namespace: argocd
spec:
  project: ecommerce
  source:
    repoURL: https://github.com/acme/manifests
    path: apps/frontend/overlays/prod
    targetRevision: main
  destination:
    server: https://kubernetes.default.svc
    namespace: frontend
  syncPolicy:
    automated: {prune: true, selfHeal: true}
```

Add a new app? Create `repo/apps/new-app.yaml` and push. Remove an app? Delete the file (and `prune: true` removes it from the cluster).

App-of-apps is conceptually clean but **mostly superseded by ApplicationSet** for new setups (§12), because ApplicationSet handles per-cluster fan-out, per-PR previews, and matrix combinations that pure app-of-apps does not. The remaining sweet spot is "I have a fixed, small list of apps; I just want a manifest of manifests" — for which app-of-apps is simpler.

The well-known footgun: **self-reference**. Don't make the root Application include itself as a child (i.e., don't put `root.yaml` in the `apps/` directory the root Application watches). Argo will reconcile it infinitely.

---

## 12. ApplicationSet Generators

`ApplicationSet` is the answer to "I have N clusters / N PRs / N teams / N service instances and I don't want to write N Applications by hand". A generator produces a list of parameter sets; the template renders one Application per set.

Argo ships seven primary generators. Source: `applicationset/generators/` in `argo-cd`.

### 12.1 List

The dumbest generator: an explicit list.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: stable-apps
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - cluster: prod-us-east
            url: https://prod-us-east.example.com
          - cluster: prod-eu-west
            url: https://prod-eu-west.example.com
  template:
    metadata:
      name: 'platform-{{cluster}}'
    spec:
      project: platform
      source:
        repoURL: https://github.com/acme/manifests
        path: platform/overlays/{{cluster}}
        targetRevision: main
      destination:
        server: '{{url}}'
        namespace: platform
```

Use when you want to be explicit, or when generators don't fit your sharding rules.

### 12.2 Cluster

Generates parameters from clusters Argo knows about. A "cluster" in Argo is a Secret in the `argocd` namespace labeled `argocd.argoproj.io/secret-type: cluster`, containing the kubeconfig.

```yaml
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            environment: prod
            region: us-east
```

Variables: `{{name}}`, `{{server}}`, `{{metadata.labels.<key>}}`, `{{metadata.annotations.<key>}}`.

Why this is powerful: register a new cluster with the right labels, the ApplicationSet fans out automatically. Decommission a cluster, the Applications targeting it are deleted. This is the heart of multi-cluster GitOps.

### 12.3 Git

Generates from a Git repository — either by walking directories or by reading files.

**Directory mode:**

```yaml
spec:
  generators:
    - git:
        repoURL: https://github.com/acme/manifests
        revision: main
        directories:
          - path: apps/*
```

One Application per matching directory. Globbing supports negation: `path: apps/*` followed by `path: apps/excluded-app`, `exclude: true`.

**File mode:**

```yaml
spec:
  generators:
    - git:
        repoURL: https://github.com/acme/manifests
        revision: main
        files:
          - path: clusters/*/config.json
```

Each matched JSON or YAML file becomes a parameter set. The file's content is the parameter dictionary. Path captures (`{{path[0]}}`, `{{path.basename}}`) are available.

### 12.4 SCM Provider

One Application per repository in a GitHub/GitLab/Bitbucket/Gitea/Azure DevOps organisation.

```yaml
spec:
  generators:
    - scmProvider:
        github:
          organization: acme
          allBranches: false
          tokenRef:
            secretName: github-token
            key: token
        filters:
          - repositoryMatch: ^service-.*
          - pathsExist: [kubernetes/manifests.yaml]
```

Every repo in `acme` whose name matches `service-*` and which contains `kubernetes/manifests.yaml` becomes an Application. Useful when each microservice has its own repo and contributes its own manifest.

### 12.5 Pull Request

One Application per open PR — the canonical preview-environments pattern.

```yaml
spec:
  generators:
    - pullRequest:
        github:
          owner: acme
          repo: app
          tokenRef:
            secretName: github-token
            key: token
        requeueAfterSeconds: 60
  template:
    metadata:
      name: 'preview-{{number}}'
    spec:
      project: previews
      source:
        repoURL: https://github.com/acme/app
        targetRevision: '{{branch}}'
        path: deploy
      destination:
        server: https://kubernetes.default.svc
        namespace: 'preview-{{number}}'
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
        syncOptions:
          - CreateNamespace=true
```

Open PR #42, namespace `preview-42` is created and the app from that branch is deployed. Close the PR, namespace is torn down. See §33.

### 12.6 Matrix

Cartesian product of two generators. Used when "for every cluster × for every directory, make an Application".

```yaml
spec:
  generators:
    - matrix:
        generators:
          - clusters:
              selector:
                matchLabels:
                  environment: prod
          - git:
              repoURL: https://github.com/acme/manifests
              revision: main
              directories:
                - path: apps/*
```

If you have 3 prod clusters and 5 app directories, you get 15 Applications. Variables from both child generators are merged.

### 12.7 Merge

Like matrix, but joins on a key — *not* a Cartesian product. Used to enrich the output of one generator with data from another.

```yaml
spec:
  generators:
    - merge:
        mergeKeys: [server]
        generators:
          - clusters: {}
          - list:
              elements:
                - server: https://prod-us-east.example.com
                  config: aggressive
                - server: https://prod-eu-west.example.com
                  config: conservative
```

For each cluster known to Argo, look up the matching `config` from the list. The resulting parameters include both the cluster's fields and the looked-up `config`.

**Other generators**: `plugin` (custom generators via HTTP), `clusterDecisionResource` (read parameters from a CR), and the deprecated `clusterGenerator` from older versions.

The ApplicationSet controller is itself a level-triggered reconciler: when generators' inputs change (new cluster, new PR, new directory), the controller diffs the resulting Application set and creates/updates/deletes Applications accordingly.

---

## 13. Drift Detection and Self-Heal

Drift is when live state differs from Git state.

```
                Git state (rendered)                Live state (apiserver)
                       │                                     │
                       └────────────┬────────────────────────┘
                                    │
                                    ▼
                              Argo diff engine
                          (per-resource, per-field)
                                    │
                          ┌─────────┴──────────┐
                          ▼                    ▼
                       Synced              OutOfSync
                    (no action)        ┌─────┴──────────┐
                                       ▼                ▼
                              automated.selfHeal    selfHeal=false
                                   = true              │
                                       │               ▼
                                       │         report only
                                       ▼         (UI shows yellow)
                                  re-apply
```

Detection runs:
1. **On every reconcile timer** (3m default, `timeout.reconciliation` in `argocd-cm`).
2. **On every relevant Kubernetes object change** (informer watch).
3. **On every Git change** (poll every 3m by default, or webhook-driven if you wire one).

Self-heal is purely about *re-applying when drift is detected*. The mechanics:

1. Application controller diffs rendered Git state against live state.
2. If `OutOfSync` and `automated.selfHeal: true`, controller enqueues a sync.
3. Sync runs server-side or client-side apply, depending on `syncOptions`.
4. Whatever drifted gets overwritten.

**Selfheal has a debounce.** Argo will not selfheal more than once every `selfHealTimeout` (default 5 seconds). Without this, a misbehaving operator that re-mutates the spec every reconcile would create a tight write loop.

**SelfHeal does not delete extras.** That's `prune`. The two are independent — you can selfheal without pruning (overwrite drifted fields but keep extra resources), or prune without selfheal (only delete extras on the timer's natural sync; don't aggressively re-apply).

The trade-offs:
- **`selfHeal: true, prune: true`**: maximum hygiene. Anything in the cluster is exactly what's in Git, period. Use for platform services, namespaces, RBAC.
- **`selfHeal: false, prune: true`**: drift is a yellow flag, removals are honoured. Use during phased rollouts where you want to inspect before correcting.
- **`selfHeal: true, prune: false`**: in-cluster modifications get reverted, but you don't want Argo to delete things. Use when there's a co-author writing to the cluster you don't want to fight.

The Flux equivalent: `Kustomization.spec.force: true` plus `prune: true` plus the `interval` field for the reconcile period.

---

## 14. ignoreDifferences: Sharing a Spec with Other Actors

Pure GitOps says "Git is the source of truth for every field of every object". Reality says "the HPA owns spec.replicas, cert-manager owns the cert data in this Secret, the operator owns these status fields". You need to tell Argo not to fight over those fields.

`ignoreDifferences` on the Application spec lists fields to exclude from diff and (with `RespectIgnoreDifferences=true`) from sync.

```yaml
spec:
  ignoreDifferences:
    # HPA owns replicas
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas

    # cert-manager owns the contents of TLS secrets
    - group: ''
      kind: Secret
      name: tls-cert
      namespace: frontend
      jsonPointers:
        - /data

    # External controller owns image tag on this Deployment
    - group: apps
      kind: Deployment
      jqPathExpressions:
        - '.spec.template.spec.containers[] | select(.name == "app") | .image'

    # Ignore an entire annotation that another controller writes
    - group: apps
      kind: Deployment
      managedFieldsManagers:
        - kube-controller-manager
```

Three styles of selector:

- **`jsonPointers`** — RFC 6901 JSON pointers. Best for known, simple paths.
- **`jqPathExpressions`** — jq-style expressions. Best for "any container with name X" or "any array element matching Y".
- **`managedFieldsManagers`** — works only with Server-Side Apply (§15). Argo ignores any field whose owning fieldManager is in this list. This is the *correct* solution for multi-author objects in 2026.

The HPA case (§37) is the most-cited example. Argo applies the Deployment with `replicas: 3`, HPA scales it to 7. Without `ignoreDifferences`, Argo's next reconcile sees `replicas: 3 in Git, 7 in cluster` and reverts. With `jsonPointers: /spec/replicas`, Argo sees no diff. With SSA-based `managedFieldsManagers: [horizontal-pod-autoscaler]`, Argo only owns the fields it set and doesn't even consider `replicas` part of its mandate.

`RespectIgnoreDifferences: true` is required as a `syncOption` for `ignoreDifferences` to apply during *sync*, not just during *diff*. Without it, diff-mode shows the ignored field as Synced, but sync still tries to overwrite. With it, sync skips the ignored field.

Default: too permissive `ignoreDifferences` (e.g., ignoring all of `/spec`) silently kills your ability to detect real drift. Treat each rule as a debt to be paid down.

---

## 15. Server-Side Apply with Argo

Server-Side Apply (SSA) is the apiserver-side mechanism (ch 05) that tracks per-field ownership through `metadata.managedFields`. Each writer declares a `fieldManager` name; the apiserver records which fields each manager owns. Conflicts (two managers trying to set the same field) are resolved or surfaced as errors.

Pre-SSA, Argo used client-side strategic merge patches: render the desired manifest, fetch the live one, diff, apply. This works but has a fundamental flaw — Argo can't tell whether a field it didn't set is "supposed to be there because someone else set it" or "drift to be erased". With SSA, Argo only owns what Argo touches, and `managedFields` makes ownership explicit.

Enable SSA per-Application:

```yaml
spec:
  syncPolicy:
    syncOptions:
      - ServerSideApply=true
```

Or per-resource via annotation:

```yaml
metadata:
  annotations:
    argocd.argoproj.io/sync-options: ServerSideApply=true
```

When SSA is on, Argo applies with `fieldManager=argocd-controller` and `force=true` (by default; configurable). The mutation is a `PATCH` with `Content-Type: application/apply-patch+yaml`.

Combined with `managedFieldsManagers` ignoreDifferences, you get **field-level co-authorship**: Argo writes the fields you put in Git; the HPA writes replicas; cert-manager writes the secret data; nothing fights.

Future direction: Argo and Flux are both moving toward SSA-default for new Applications/Kustomizations. As of 2026, SSA is opt-in but recommended for any new setup.

A subtle SSA gotcha: if you previously applied client-side and then enable SSA, the apiserver attributes pre-existing fields to the legacy fieldManager (typically `before-first-apply`). Argo's first SSA may show "ownership transfer" diffs. The fix: run an initial sync with `ServerSideApply=true` and `Force=true`, or use `kubectl apply --server-side --force-conflicts` once to claim ownership.

---

## 16. ArgoCD Multi-Tenancy via AppProject

A single ArgoCD installation typically serves many teams. The boundary is `AppProject` (introduced in §7.2).

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: payments-team
  namespace: argocd
spec:
  description: "Payments team applications"

  # Only these repos may be sources
  sourceRepos:
    - https://github.com/acme/payments-manifests
    - https://github.com/acme/payments-helm-charts

  # Only these (cluster, namespace) pairs may be destinations
  destinations:
    - server: https://kubernetes.default.svc
      namespace: 'payments-*'
    - server: https://prod-eu.example.com
      namespace: 'payments-*'

  # Only these cluster-scoped resources may be created
  clusterResourceWhitelist:
    - group: ''
      kind: Namespace

  # These cluster-scoped resources are explicitly banned
  clusterResourceBlacklist:
    - group: ''
      kind: PersistentVolume
    - group: rbac.authorization.k8s.io
      kind: ClusterRoleBinding

  # Namespaced resources are allowed unless blacklisted
  namespaceResourceBlacklist:
    - group: ''
      kind: ResourceQuota

  # Roles let team members manage their Applications without admin
  roles:
    - name: developer
      description: "Can sync Applications"
      policies:
        - p, proj:payments-team:developer, applications, sync, payments-team/*, allow
        - p, proj:payments-team:developer, applications, get, payments-team/*, allow
        - p, proj:payments-team:developer, applications, action/*, payments-team/*, allow
      groups:
        - acme:payments-developers
    - name: admin
      description: "Can create/delete Applications"
      policies:
        - p, proj:payments-team:admin, applications, *, payments-team/*, allow
      groups:
        - acme:payments-admins

  # Maintenance windows
  syncWindows:
    - kind: deny
      schedule: '0 18 * * 5'    # Fri 18:00
      duration: 60h             # through Mon 06:00
      applications: ['*']
      manualSync: false         # block even manual sync
      timeZone: America/New_York

  # Signature verification (optional)
  signatureKeys:
    - keyID: 4AEE18F83AFDEB23
```

Architecture:

```
                            ┌───────────────────────────┐
                            │      argocd-server        │
                            │     (RBAC enforcer)       │
                            └─────────────┬─────────────┘
                                          │
                       ┌──────────────────┼─────────────────┐
                       ▼                  ▼                 ▼
                ┌─────────────┐    ┌─────────────┐   ┌─────────────┐
                │ AppProject: │    │ AppProject: │   │ AppProject: │
                │ payments    │    │ ecommerce   │   │ platform    │
                └─────┬───────┘    └─────┬───────┘   └─────┬───────┘
                      │                  │                 │
                      ▼                  ▼                 ▼
                 ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
                 │ Applications   │ │ Applications   │ │ Applications   │
                 │ in payments    │ │ in ecommerce   │ │ in platform    │
                 │ Cannot touch   │ │ Cannot touch   │ │ Owns CRDs,     │
                 │ other teams'   │ │ other teams'   │ │ ingress, mesh, │
                 │ namespaces.    │ │ namespaces.    │ │ observability  │
                 └────────────────┘ └────────────────┘ └────────────────┘
```

**Pattern: platform team owns the ArgoCD installation, every product team owns one or more AppProjects.** The platform team writes AppProjects (since they're cluster-scoped to argocd). Product teams write Applications within their projects. RBAC binds product team groups to project roles.

The ArgoCD RBAC model is Casbin-based. Policies are in `argocd-rbac-cm`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-rbac-cm
  namespace: argocd
data:
  policy.default: role:readonly
  policy.csv: |
    p, role:platform-admin, *, *, */*, allow
    g, acme:platform-team, role:platform-admin
```

Policies have format `p, <subject>, <resource>, <action>, <object>, <effect>`. Group bindings are `g, <user-or-group>, <role>`. The default `role:readonly` lets everyone see everything; tighten this in production.

---

## 17. ArgoCD Secrets

Argo itself stores some secrets — Git credentials, cluster credentials, OIDC client secrets — and these live in regular Kubernetes Secrets in the `argocd` namespace, labeled appropriately.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: payments-repo
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: https://github.com/acme/payments-manifests
  username: argocd
  password: <github-pat>
```

A cluster Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: prod-eu-west
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
    environment: prod
    region: eu-west
type: Opaque
stringData:
  name: prod-eu-west
  server: https://prod-eu-west.example.com
  config: |
    {
      "bearerToken": "<token>",
      "tlsClientConfig": {
        "caData": "<base64-encoded-ca-cert>"
      }
    }
```

**But these are Argo's *own* secrets**, not the application secrets your workload needs. The harder problem is: how do you put application secrets (DB passwords, API keys) into Git safely?

**Never store plaintext secrets in Git.** The three accepted solutions:

### 17.1 Sealed Secrets (Bitnami)

`bitnami-labs/sealed-secrets`. A controller in the cluster holds a private key; you encrypt your secret to its public key. The encrypted blob lives in Git; the controller decrypts to a regular Secret in the namespace.

```yaml
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: db-password
  namespace: payments
spec:
  encryptedData:
    password: AgBy3i4OJSWK+PiTySYZZA9rO43cGDEq...
    username: AgCxYRX1l3...
  template:
    metadata:
      name: db-password
      namespace: payments
    type: Opaque
```

CLI: `kubeseal --controller-namespace sealed-secrets --controller-name sealed-secrets-controller -o yaml < secret.yaml > sealed-secret.yaml`.

Pros: simple, no external dependency, plain-Kubernetes.
Cons: keys are per-cluster (rotating means re-sealing every secret); namespace-scoped by default (re-encrypting if you change namespaces); the controller is a single point of failure for decryption.

### 17.2 External Secrets Operator (ESO)

`external-secrets/external-secrets`. A controller that reads secrets from an external store (AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, HashiCorp Vault, 1Password, GitHub, etc.) and synchronises them into Kubernetes Secrets.

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets
  namespace: payments
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-sa
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: db-creds
  namespace: payments
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets
    kind: SecretStore
  target:
    name: db-creds
    creationPolicy: Owner
  data:
    - secretKey: password
      remoteRef:
        key: prod/payments/db
        property: password
    - secretKey: username
      remoteRef:
        key: prod/payments/db
        property: username
```

Pros: secrets live where they belong (a real secret store with audit, rotation, IAM); Git only holds references. Rotation is automatic — ESO polls every `refreshInterval`.
Cons: external dependency; IAM setup; lag between rotation and pod restart (need to bounce pods or use envFrom + restart-on-secret-change controller).

ESO is the modern default for most cloud setups.

### 17.3 SOPS (Mozilla / sops-secrets-operator)

SOPS encrypts YAML/JSON at the leaf-value level using PGP, age, AWS KMS, GCP KMS, or Azure Key Vault. The encrypted file is human-readable structure with encrypted values.

```yaml
# Decrypted view
apiVersion: v1
kind: Secret
metadata:
  name: db-password
  namespace: payments
type: Opaque
stringData:
  username: ENC[AES256_GCM,data:abc,iv:def,tag:ghi]
  password: ENC[AES256_GCM,data:jkl,iv:mno,tag:pqr]
sops:
  age:
    - recipient: age1abc...
      enc: |
        -----BEGIN AGE ENCRYPTED FILE-----
        ...
```

Flux integrates SOPS natively via `Kustomization.spec.decryption.provider: sops` (§19). Argo integrates via the `helm-secrets` plugin or the `argocd-vault-plugin`.

Pros: keys can be cloud KMS (managed key rotation); diff-friendly (changes to one field don't ripple).
Cons: more setup; tooling complexity.

**Pick one, document it, automate the rotation. The worst secret-management strategy is a mix of three.**

---

## 18. ArgoCD Notifications

`argocd-notifications-controller` watches Applications and fires templated messages on state transitions. Configured via `argocd-notifications-cm`.

Three pieces:

1. **Services**: the *destinations* (Slack, Teams, email, webhook, GitHub commit status, etc.).
2. **Templates**: the *message content*.
3. **Triggers**: the *condition* (Go expression over the Application's state).

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-notifications-cm
  namespace: argocd
data:
  service.slack: |
    token: $slack-token
  service.webhook.github: |
    url: https://api.github.com
    headers:
      - name: Authorization
        value: token $github-token

  template.app-sync-failed: |
    message: |
      Application {{.app.metadata.name}} sync failed.
      Status: {{.app.status.operationState.message}}
      Sync URL: {{.context.argocdUrl}}/applications/{{.app.metadata.name}}
    slack:
      attachments: |
        [{
          "title": "Sync Failed: {{.app.metadata.name}}",
          "color": "#E96D76",
          "fields": [{
            "title": "Repository",
            "value": "{{.app.spec.source.repoURL}}"
          }]
        }]

  trigger.on-sync-failed: |
    - description: Application sync failed
      send:
        - app-sync-failed
      when: app.status.operationState.phase in ['Error', 'Failed']

  trigger.on-health-degraded: |
    - description: Health went Degraded
      send:
        - app-health-degraded
      when: app.status.health.status == 'Degraded'

  subscriptions: |
    - recipients:
        - slack:platform-alerts
      triggers:
        - on-sync-failed
        - on-health-degraded
```

Annotate Applications to subscribe specific channels:

```yaml
metadata:
  annotations:
    notifications.argoproj.io/subscribe.on-sync-failed.slack: payments-alerts
```

Built-in triggers (you can add more): `on-deployed`, `on-health-degraded`, `on-sync-failed`, `on-sync-running`, `on-sync-status-unknown`, `on-sync-succeeded`.

The `when` expression is Go expr-lang. Available variables: `app` (the Application), `context` (URL etc.). For most teams, the default trigger set is sufficient.

---

## 19. Flux: The GitOps Toolkit

Flux v2 (`fluxcd/flux2`) takes the opposite architectural choice from Argo: instead of a few large controllers, **one controller per concern**, all composable.

```
                              ┌─────────────────────────────────────┐
                              │              Sources                │
                              │   GitRepository, OCIRepository,     │
                              │      Bucket, HelmRepository         │
                              └──────────────┬──────────────────────┘
                                             │ status.artifact (tarball URL)
                                             ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  flux-system namespace                                                   │
   │                                                                          │
   │  ┌────────────────────────┐    ┌────────────────────────────────────┐  │
   │  │ source-controller       │    │ kustomize-controller                │  │
   │  │ (fluxcd/source-         │───▶│ (fluxcd/kustomize-controller)       │  │
   │  │  controller)            │    │ reads artifact, kustomize build,    │  │
   │  │ clones git, fetches     │    │ server-side-applies                 │  │
   │  │ OCI, fetches HelmRepo   │    └────────────────────────────────────┘  │
   │  │ produces artifact (tar) │                                              │
   │  └─────────┬──────────────┘    ┌────────────────────────────────────┐  │
   │            │                    │ helm-controller                     │  │
   │            └───────────────────▶│ (fluxcd/helm-controller)            │  │
   │            │                    │ reads chart artifact, helm install/ │  │
   │            │                    │ upgrade, manages release state      │  │
   │            │                    └────────────────────────────────────┘  │
   │            │                                                              │
   │            │              ┌────────────────────────────────────────────┐ │
   │            │              │ notification-controller                     │ │
   │            └─────────────▶│ (fluxcd/notification-controller)            │ │
   │                           │ Alerts → Providers (Slack, MS Teams,      │ │
   │                           │ GitHub, GitLab status, webhooks);          │ │
   │                           │ Receivers (incoming webhooks → reconcile) │ │
   │                           └────────────────────────────────────────────┘ │
   │                                                                          │
   │  ┌────────────────────────────┐  ┌──────────────────────────────────┐ │
   │  │ image-reflector-controller │  │ image-automation-controller      │ │
   │  │ (watches registry, populates  │  (matches policy, opens PR or    │ │
   │  │  ImagePolicy with latest tags)│   commits to Git)                │ │
   │  └────────────────────────────┘  └──────────────────────────────────┘ │
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘
                                             │ apply
                                             ▼
                                  ┌─────────────────────────┐
                                  │ target kube-apiserver(s)│
                                  └─────────────────────────┘
```

Six controllers (the "GitOps Toolkit"):

**`source-controller`** clones Git, fetches OCI artifacts, fetches Helm chart repos, fetches S3/GCS/Azure Blob. It produces an *artifact* (a tarball at a well-known URL inside the cluster) and updates the source CR's status with the artifact URL and revision. It is the *only* thing that reads from external Git/OCI/Bucket sources. Every other controller reads its artifact. Source: `fluxcd/source-controller`.

**`kustomize-controller`** reads an artifact, runs `kustomize build`, and applies the result via Server-Side Apply (always SSA in Flux v2). It manages a `Kustomization` CR. Source: `fluxcd/kustomize-controller`.

**`helm-controller`** reads a chart artifact, renders it with the supplied values, and manages a Helm release (uses the `helm.sh/helm/v3` library directly — no `helm` binary, no Tiller). Manages a `HelmRelease` CR. Source: `fluxcd/helm-controller`.

**`notification-controller`** receives events from the other controllers, dispatches to providers (Slack/Teams/webhooks/Git commit status), and exposes a webhook receiver that can re-trigger sources on incoming pushes. Manages `Alert`, `Provider`, `Receiver` CRs. Source: `fluxcd/notification-controller`.

**`image-reflector-controller`** scans container registries on a schedule, evaluates `ImagePolicy` CRs (semver, regex, or numeric ordering), and writes the resolved tag to the policy's status. Source: `fluxcd/image-reflector-controller`.

**`image-automation-controller`** reads `ImageUpdateAutomation` CRs, applies image policy results to manifest files in Git (via in-Git substitution markers), and commits/pushes back. Source: `fluxcd/image-automation-controller`.

**The composability story**: each controller does one thing. You can run Flux with only source + kustomize (no Helm). You can swap out notification for your own. You can write a controller that reads source-controller's artifacts. The GitOps Toolkit advertises itself as a *kit*; Argo is more of a product.

CLI: `flux` (`fluxcd/flux2`). `flux bootstrap` installs the toolkit and configures Flux to manage its own manifests from a Git repo — Flux from the moment it boots is Git-managed by itself.

---

## 20. Flux Core CRDs

Roughly ten CRDs across the controllers, organised by API group.

### 20.1 source.toolkit.fluxcd.io

**`GitRepository`**:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: payments-manifests
  namespace: flux-system
spec:
  interval: 1m
  url: https://github.com/acme/payments-manifests
  ref:
    branch: main
  secretRef:
    name: github-credentials
  ignore: |
    # ignore everything
    /*
    # except manifests
    !/manifests/
```

Status carries `status.artifact.url` (tarball URL inside the cluster, served by source-controller) and `status.artifact.revision` (commit SHA).

**`OCIRepository`** — same as GitRepository but for OCI artifacts (push manifests as OCI tarballs):

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: payments-manifests
  namespace: flux-system
spec:
  interval: 5m
  url: oci://ghcr.io/acme/payments-manifests
  ref:
    tag: latest
  verify:
    provider: cosign
    secretRef:
      name: cosign-pub
```

**`Bucket`** — fetches from S3-compatible storage.

**`HelmRepository`** — fetches a Helm chart repository's index, makes charts available to `HelmChart` and `HelmRelease`.

**`HelmChart`** — usually generated by `HelmRelease`, represents a fetched chart artifact.

### 20.2 kustomize.toolkit.fluxcd.io

**`Kustomization`**:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: payments-prod
  namespace: flux-system
spec:
  interval: 10m
  path: ./manifests/overlays/prod
  prune: true
  sourceRef:
    kind: GitRepository
    name: payments-manifests
  targetNamespace: payments
  decryption:
    provider: sops
    secretRef:
      name: sops-age
  postBuild:
    substituteFrom:
      - kind: ConfigMap
        name: cluster-vars
      - kind: Secret
        name: cluster-secrets
  patches:
    - target:
        kind: Deployment
        name: api
      patch: |
        - op: replace
          path: /spec/replicas
          value: 5
  healthChecks:
    - kind: Deployment
      name: api
      namespace: payments
  dependsOn:
    - name: payments-crds
  timeout: 5m
  retryInterval: 1m
```

Key fields:
- `path` — directory within the source artifact containing the `kustomization.yaml`.
- `prune: true` — delete in-cluster resources not in Git (equivalent of Argo's prune).
- `sourceRef` — which source to read.
- `decryption.provider: sops` — built-in SOPS support.
- `postBuild.substituteFrom` — variable substitution from ConfigMaps/Secrets (Flux-specific; not pure Kustomize).
- `patches` — strategic merge or JSON6902 patches applied *after* `kustomize build`.
- `healthChecks` — like Argo's health waits; the Kustomization is not Ready until these check Healthy.
- `dependsOn` — Flux's equivalent of sync waves; declarative ordering between Kustomizations.

### 20.3 helm.toolkit.fluxcd.io

**`HelmRelease`**:

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: prometheus
  namespace: monitoring
spec:
  interval: 10m
  chart:
    spec:
      chart: kube-prometheus-stack
      version: '>=55.0.0 <56.0.0'
      sourceRef:
        kind: HelmRepository
        name: prometheus-community
        namespace: flux-system
      interval: 1h
  values:
    grafana:
      enabled: true
      adminPassword: ${grafana_password}
    prometheus:
      prometheusSpec:
        retention: 30d
  valuesFrom:
    - kind: ConfigMap
      name: prometheus-overrides
  install:
    remediation:
      retries: 3
  upgrade:
    remediation:
      retries: 3
      remediateLastFailure: true
    cleanupOnFail: true
  rollback:
    cleanupOnFail: true
  driftDetection:
    mode: enabled
    ignore:
      - paths: ["/spec/replicas"]
        target:
          kind: Deployment
  test:
    enable: true
```

Powerful pieces:
- `chart.spec` — embedded HelmChart-like definition. The helm-controller creates a HelmChart for you.
- `values` and `valuesFrom` — inline values plus references to ConfigMaps/Secrets.
- `install.remediation.retries` — retry install N times before giving up.
- `upgrade.remediation.remediateLastFailure: true` — automatically roll back if last release failed.
- `driftDetection.mode: enabled` — Flux compares live state to Helm-rendered state and corrects drift.
- `test.enable: true` — run `helm test` after install/upgrade.

### 20.4 notification.toolkit.fluxcd.io

**`Provider`** (the destination):

```yaml
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Provider
metadata:
  name: slack
  namespace: flux-system
spec:
  type: slack
  channel: platform-alerts
  secretRef:
    name: slack-webhook
```

**`Alert`** (what to send when):

```yaml
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Alert
metadata:
  name: payments-alerts
  namespace: flux-system
spec:
  providerRef:
    name: slack
  eventSeverity: error
  eventSources:
    - kind: Kustomization
      name: payments-prod
    - kind: HelmRelease
      name: prometheus
```

**`Receiver`** (incoming webhook → re-reconcile):

```yaml
apiVersion: notification.toolkit.fluxcd.io/v1
kind: Receiver
metadata:
  name: github-receiver
  namespace: flux-system
spec:
  type: github
  events:
    - "ping"
    - "push"
  secretRef:
    name: github-webhook-token
  resources:
    - kind: GitRepository
      name: payments-manifests
```

Configure GitHub to send webhooks to `https://flux-webhook.example.com/hook/<id>`; on push, Flux re-fetches the GitRepository immediately instead of waiting for the interval.

### 20.5 image.toolkit.fluxcd.io

Three CRDs: `ImageRepository` (which registry/repo to scan), `ImagePolicy` (which tag to pick), `ImageUpdateAutomation` (where to commit the update).

---

## 21. Flux Image Automation

The "no human commit" CD path. CI builds an image and pushes it to a registry. Flux's image-reflector-controller scans the registry, picks the newest tag matching policy, and image-automation-controller commits the new tag to Git. Flux then redeploys.

```
       CI builds & pushes              image-reflector              image-automation
              │                       polls registry              writes to Git
              ▼                              │                          │
     ghcr.io/acme/api:1.5.2  ────────────────┘                          │
                                             │                          │
                                             ▼                          ▼
                                  ImagePolicy.status.latestImage   git push origin main
                                  = ghcr.io/acme/api:1.5.2          (modified manifests)
                                                                          │
                                                                          ▼
                                                                  source-controller
                                                                  fetches new commit
                                                                          │
                                                                          ▼
                                                                kustomize-controller
                                                                  applies new tag
```

**ImageRepository** — what to scan:

```yaml
apiVersion: image.toolkit.fluxcd.io/v1beta2
kind: ImageRepository
metadata:
  name: api
  namespace: flux-system
spec:
  image: ghcr.io/acme/api
  interval: 5m
  secretRef:
    name: ghcr-pull-token
```

**ImagePolicy** — which tag is "current":

```yaml
apiVersion: image.toolkit.fluxcd.io/v1beta2
kind: ImagePolicy
metadata:
  name: api
  namespace: flux-system
spec:
  imageRepositoryRef:
    name: api
  policy:
    semver:
      range: '>=1.0.0 <2.0.0'
```

Policy options: `semver` (semantic-version range), `alphabetical` (lexical ordering), `numerical` (numeric ordering). Filters: `tagFilter` (regex), `extract` (capture group).

**ImageUpdateAutomation** — what to write where:

```yaml
apiVersion: image.toolkit.fluxcd.io/v1beta2
kind: ImageUpdateAutomation
metadata:
  name: api-automation
  namespace: flux-system
spec:
  interval: 5m
  sourceRef:
    kind: GitRepository
    name: payments-manifests
  git:
    checkout:
      ref:
        branch: main
    commit:
      author:
        name: Flux Bot
        email: flux@acme.com
      messageTemplate: |
        Automated image update
        Files: {{range $filename, $_ := .Updated.Files}}{{$filename}}{{end}}
    push:
      branch: main
  update:
    path: ./manifests
    strategy: Setters
```

In the manifest YAML, you annotate the image field with a setter marker:

```yaml
spec:
  containers:
    - name: api
      image: ghcr.io/acme/api:1.4.1  # {"$imagepolicy": "flux-system:api"}
```

When `ImagePolicy.status.latestImage` resolves to a new tag, image-automation-controller rewrites the manifest line and commits.

**The risk**: ImagePolicy that accidentally picks pre-release tags (`v1.5.0-rc1`). Always guard with regex or semver range that excludes pre-releases. Always require human review of image-update commits if your branch protection allows it — `push.branch` can be a PR branch (e.g., `flux-image-updates`) that requires merge approval before reaching `main`.

---

## 22. Flux Multi-Tenancy

Flux's multi-tenancy story is different from Argo's. Where Argo uses `AppProject` to scope Applications, Flux uses **regular Kubernetes namespaces and RBAC**, and the controllers honour cross-namespace references.

A `Kustomization` in tenant namespace `team-a` references a `GitRepository` in `team-a`. The kustomize-controller runs `kustomize build` and applies *as a specific ServiceAccount* declared in the Kustomization:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: team-a-apps
  namespace: team-a
spec:
  serviceAccountName: team-a-reconciler  # impersonate this SA
  sourceRef:
    kind: GitRepository
    name: team-a-manifests
    namespace: team-a
  path: ./
  prune: true
  interval: 5m
```

The `team-a-reconciler` ServiceAccount has RBAC scoped to what `team-a` is allowed to touch. The kustomize-controller — which runs as cluster-admin — impersonates this SA via `kubectl --as` semantics (or, more precisely, by setting the impersonation headers on its apiserver client). So even though the controller is privileged, the *applies* it performs are limited to what the tenant SA can do.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: team-a-reconciler
  namespace: team-a
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: team-a-reconciler
  namespace: team-a
subjects:
  - kind: ServiceAccount
    name: team-a-reconciler
    namespace: team-a
roleRef:
  kind: ClusterRole
  name: edit
  apiGroup: rbac.authorization.k8s.io
```

The platform team owns one cluster-wide flag — `--no-cross-namespace-refs` on the controllers — to forbid a Kustomization in `team-a` from referencing a GitRepository in `team-b`. With this flag set, multi-tenancy is enforced structurally: a tenant can only reference sources in their own namespace.

In Argo, AppProject is application-aware multi-tenancy (limits which destinations the engine writes to). In Flux, RBAC + impersonation is Kubernetes-native multi-tenancy (limits what the engine *can* write because it's running as the tenant). Both work; Argo's is more centralised, Flux's leans on the existing K8s model.

---

## 23. ArgoCD vs Flux

The honest comparison. Both are mature, both are CNCF Graduated, both work, and team preference is the dominant factor.

| Dimension | ArgoCD | Flux |
|---|---|---|
| Architecture | A handful of services (server, controller, repo-server, etc.) | One controller per concern (Toolkit) |
| UI | Rich web UI, real-time visualisation | CLI-first (`flux` command); UI via Weave GitOps Enterprise (paid) or `capacitor` (open-source) |
| API surface | `Application`, `AppProject`, `ApplicationSet` | `GitRepository`/`OCIRepository`/`Bucket`, `Kustomization`, `HelmRelease`, `Alert`/`Provider`/`Receiver`, `ImagePolicy`/`ImageRepository`/`ImageUpdateAutomation` |
| Multi-cluster | One ArgoCD pointing at N clusters (hub) is the common model | Often one Flux per cluster, optionally syncing common config (federated) |
| Multi-tenancy | `AppProject` (engine-level) | Namespace + ServiceAccount impersonation (K8s-native) |
| Drift detection | Yes | Yes (`Kustomization.spec.force`, `HelmRelease.spec.driftDetection`) |
| Self-heal | `syncPolicy.automated.selfHeal: true` | Always-on for Kustomizations (`prune` + `force`); HelmRelease has `driftDetection.mode` |
| Helm support | Native (renders with embedded Helm) | Native (helm-controller manages full release lifecycle) |
| Kustomize support | Native | Native |
| Image automation | Argo Image Updater (separate, less integrated) | Built-in via image-reflector + image-automation |
| Progressive delivery | Argo Rollouts (separate) | Flagger (separate) |
| Secrets | Plugin-based (argocd-vault-plugin, helm-secrets) | Built-in SOPS support; ESO for vault stores |
| Webhook | One config per repo | `Receiver` CR — multi-tenant, declarative |
| Auth (UI/CLI) | Dex / built-in users / OIDC | Cluster RBAC only (use kubectl/flux CLI) |
| OCI artifacts as source | Yes | Yes (`OCIRepository`) |
| Notifications | argocd-notifications-controller | notification-controller (`Alert`/`Provider`) |
| Resource customisations | Lua scripts | Kustomization healthChecks + KStatus |

**When to pick Argo:**
- You want a polished UI for app status across many teams.
- You're comfortable with a centralised engine (hub model).
- Your operators interact with humans who want to click "Sync".
- You need `ApplicationSet` fan-out features (especially PR generator) out of the box.

**When to pick Flux:**
- You want a more modular system; multiple controllers, each one swappable.
- You prefer everything in Kubernetes RBAC, no second-tier RBAC system.
- Image-update automation is a core requirement.
- One Flux per cluster (federated model) matches your blast-radius story.

In practice many shops run both — Flux for platform/infrastructure components (where the controller-per-concern story is appealing and you want one Flux per cluster), Argo for product applications (where the UI and ApplicationSet are valuable). The two coexist fine; they don't fight if their AppProjects/namespaces don't overlap.

---

## 24. Helm v3 Internals

Helm is the package manager for Kubernetes. A *chart* is a versioned bundle of templated manifests; a *release* is an installed instance of a chart in a namespace.

### 24.1 Architecture

Helm v3 is **client-only**. There is no server-side component (Tiller — Helm v2's in-cluster gRPC service — is gone). The `helm` binary speaks directly to the apiserver using your kubeconfig, and stores release state as Secrets in the release namespace.

```
   ┌──────────────────────────────────────────────────────────────┐
   │  user host                                                    │
   │  ┌──────────┐    reads chart       ┌─────────────────────┐   │
   │  │  helm    │ ◀─────────────────── │  chart directory    │   │
   │  │  binary  │                       │  or chart repo (URL)│   │
   │  └────┬─────┘                       └─────────────────────┘   │
   │       │                                                        │
   │       │ kubeconfig + apiserver                                 │
   │       ▼                                                        │
   └──────────────────────────────────────────────────────────────┘
           │
           ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  cluster                                                      │
   │  ┌─────────────────────┐                                      │
   │  │ kube-apiserver      │                                      │
   │  └─────────┬───────────┘                                      │
   │            │                                                   │
   │            ▼                                                   │
   │  ┌─────────────────────────────────────────────────────┐     │
   │  │  release Secret (sh.helm.release.v1.<name>.v<N>)    │     │
   │  │  in the release namespace                            │     │
   │  │  contains gzipped+base64 release JSON                │     │
   │  └─────────────────────────────────────────────────────┘     │
   │                                                                │
   │  rendered resources (Deployment, Service, ConfigMap, ...)     │
   │  with metadata.labels.app.kubernetes.io/managed-by=Helm       │
   └──────────────────────────────────────────────────────────────┘
```

The "release storage" is a Kubernetes Secret per release version per release. Naming: `sh.helm.release.v1.<release-name>.v<revision>`. Type: `helm.sh/release.v1`. Payload: a gzipped, base64-encoded JSON blob containing manifest, values, hooks, status, history.

```bash
$ kubectl get secret -n monitoring -l owner=helm
NAME                                       TYPE                 DATA   AGE
sh.helm.release.v1.prometheus.v1           helm.sh/release.v1   1      30d
sh.helm.release.v1.prometheus.v2           helm.sh/release.v1   1      15d
sh.helm.release.v1.prometheus.v3           helm.sh/release.v1   1      1d
```

You can `kubectl get secret sh.helm.release.v1.prometheus.v3 -o jsonpath='{.data.release}' | base64 -d | gunzip` to inspect the raw release.

Helm v3 source: `helm/helm`, especially `pkg/action/`, `pkg/release/`, `pkg/storage/`.

### 24.2 Chart Structure

```
mychart/
├── Chart.yaml                   ← chart metadata
├── values.yaml                  ← default values
├── values.schema.json           ← optional JSON schema for values
├── templates/                   ← rendered into manifests
│   ├── _helpers.tpl             ← shared template snippets
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── configmap.yaml
│   ├── tests/                   ← `helm test` resources
│   │   └── test-connection.yaml
│   └── NOTES.txt                ← shown after install
├── charts/                      ← subcharts (vendored dependencies)
│   └── postgresql/
├── crds/                        ← CRDs (installed before templates)
│   └── mychart-crd.yaml
└── .helmignore
```

**Chart.yaml:**

```yaml
apiVersion: v2
name: payments
description: A Helm chart for the payments service
type: application                # or "library"
version: 1.2.3                    # chart version (semver)
appVersion: "2.4.0"               # app version (informational)
keywords:
  - payments
  - api
maintainers:
  - name: Payments Team
    email: payments@acme.com
dependencies:
  - name: postgresql
    version: "12.x.x"
    repository: https://charts.bitnami.com/bitnami
    condition: postgresql.enabled
    alias: db
icon: https://acme.com/payments-logo.png
home: https://acme.com/payments
sources:
  - https://github.com/acme/payments
```

**values.yaml:**

```yaml
replicaCount: 3

image:
  repository: ghcr.io/acme/api
  pullPolicy: IfNotPresent
  tag: ""  # default to .Chart.AppVersion

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    memory: 512Mi

service:
  type: ClusterIP
  port: 80

ingress:
  enabled: false
  className: nginx
  hosts:
    - host: api.example.com
      paths:
        - path: /
          pathType: Prefix

postgresql:
  enabled: true
  auth:
    database: payments
    username: payments
```

**A template:**

```yaml
# templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "payments.fullname" . }}
  labels:
    {{- include "payments.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "payments.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "payments.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: 8080
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
```

**_helpers.tpl:**

```yaml
{{- define "payments.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "payments.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "payments.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```

The `crds/` directory is special: its contents are installed *before* templates render, only on first install (Helm v3 deliberately does not upgrade CRDs — too risky). For CRD upgrades, manage them separately or use a CRD-management chart.

The `.helmignore` (like `.gitignore`) controls what's packaged.

---

## 25. Helm Templating: Sprig, Helpers, Capabilities

Templates use Go's `text/template` with the **Sprig** library on top (`Masterminds/sprig`), plus Helm-specific built-ins.

### 25.1 The built-in objects

Inside a template, you have:
- `.Values` — merged values (defaults + user-supplied).
- `.Chart` — fields of Chart.yaml (`.Chart.Name`, `.Chart.Version`, `.Chart.AppVersion`).
- `.Release` — release info: `.Release.Name`, `.Release.Namespace`, `.Release.IsInstall`, `.Release.IsUpgrade`, `.Release.Revision`, `.Release.Service` (always "Helm").
- `.Capabilities` — cluster capabilities: `.Capabilities.KubeVersion.Major`, `.Capabilities.KubeVersion.Minor`, `.Capabilities.APIVersions.Has "networking.k8s.io/v1/Ingress"`.
- `.Files` — read non-template files from the chart (`.Files.Get "config.json"`, `.Files.Glob`, `.Files.AsConfig`, `.Files.AsSecrets`).
- `.Template` — current template name and base path.
- `.Subcharts` — subchart values (for parent charts).

### 25.2 Sprig functions

Hundreds. Categories: string manipulation (`upper`, `lower`, `trim`, `replace`, `split`, `printf`), math, lists (`first`, `last`, `slice`), dicts (`get`, `set`, `merge`), encoding (`b64enc`, `b64dec`, `toYaml`, `toJson`), cryptography (`sha256sum`, `genCA`, `genSelfSignedCert`), dates, regex, defaults (`default`, `required`, `coalesce`, `empty`).

The five you'll use every day:

- **`default`**: `{{ .Values.image.tag | default .Chart.AppVersion }}` — use AppVersion if tag is empty.
- **`required`**: `{{ required "value.foo is required" .Values.foo }}` — fail render with a message if missing.
- **`toYaml` + `nindent`**: `{{- toYaml .Values.resources | nindent 12 }}` — emit a value as nested YAML with correct indent.
- **`include`**: `{{ include "payments.labels" . }}` — call a named template (defined with `{{- define ... -}}`) and capture its output.
- **`tpl`**: `{{ tpl .Values.someTemplate . }}` — render a value-as-template (useful for letting users supply small templates in values).

### 25.3 Capabilities and APIVersions

The escape hatch for cross-version compatibility:

```yaml
{{- if .Capabilities.APIVersions.Has "networking.k8s.io/v1/Ingress" }}
apiVersion: networking.k8s.io/v1
{{- else if .Capabilities.APIVersions.Has "networking.k8s.io/v1beta1/Ingress" }}
apiVersion: networking.k8s.io/v1beta1
{{- else }}
apiVersion: extensions/v1beta1
{{- end }}
kind: Ingress
```

This is how a single chart can target Kubernetes 1.18 through 1.32 without forking.

### 25.4 Library charts

A chart with `type: library` exports template definitions only (no rendered resources). Other charts depend on it to share helpers:

```yaml
# library chart's templates/_pod.tpl
{{- define "common.pod" -}}
spec:
  containers:
    - name: {{ .name }}
      image: {{ .image }}
      resources: {{- toYaml .resources | nindent 8 }}
{{- end }}
```

Application charts:

```yaml
dependencies:
  - name: common
    version: 1.0.0
    repository: "@library-repo"
    import-values:
      - defaults
```

Library charts reduce duplication when ten microservices share the same Deployment skeleton. The downside: another chart to version and release.

---

## 26. Helm Hooks and Tests

Hooks are jobs that run at specific lifecycle events. Six built-in hook types:

- `pre-install` — before any resource is created on first install
- `post-install` — after all resources created on first install
- `pre-upgrade` / `post-upgrade` — around upgrades
- `pre-rollback` / `post-rollback` — around rollbacks
- `pre-delete` / `post-delete` — around uninstall
- `test` — `helm test` runs these

Declare via annotation:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "payments.fullname" . }}-db-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: ghcr.io/acme/migrator:{{ .Chart.AppVersion }}
          command: ["./migrate.sh"]
```

`helm.sh/hook-weight` orders hooks of the same phase (lower runs first).

`helm.sh/hook-delete-policy` options:
- `before-hook-creation` — delete previous hook before creating new one (default).
- `hook-succeeded` — delete after successful run.
- `hook-failed` — delete after failure.

Hooks are not part of the release manifest; they run, complete, and (typically) get cleaned up. That has consequences: a hook resource is not part of `helm uninstall`'s purview unless you set the right delete policy.

### Helm tests

`helm test <release>` runs resources annotated with `helm.sh/hook: test`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: {{ include "payments.fullname" . }}-test
  annotations:
    "helm.sh/hook": test
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  restartPolicy: Never
  containers:
    - name: curl
      image: curlimages/curl:8.5.0
      command:
        - sh
        - -c
        - |
          curl -fsS http://{{ include "payments.fullname" . }}/health
```

`helm test` exit code is 0 if all test Pods succeed. Argo runs these via `argocd app sync --strategy=apply` when the chart has hooks; Flux runs them when `HelmRelease.spec.test.enable: true`.

---

## 27. Helm + ArgoCD / Helm + Flux

### 27.1 ArgoCD + Helm

Argo's `Application` for a Helm chart:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: prometheus
  namespace: argocd
spec:
  project: platform
  source:
    repoURL: https://prometheus-community.github.io/helm-charts
    chart: kube-prometheus-stack
    targetRevision: 55.0.0
    helm:
      releaseName: prometheus
      values: |
        grafana:
          enabled: true
          adminPassword: changeme
      parameters:
        - name: prometheus.prometheusSpec.retention
          value: 30d
      valueFiles:
        - $values/clusters/prod/prometheus-values.yaml
  sources:                       # multi-source for valueFiles in a separate repo
    - repoURL: https://github.com/acme/manifests
      ref: values
    - repoURL: https://prometheus-community.github.io/helm-charts
      chart: kube-prometheus-stack
      targetRevision: 55.0.0
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

Argo renders with `helm template`, not `helm install`. Consequence: **no Helm release Secret is created**. The Helm `Release` object isn't tracked. `helm list` returns nothing. From Helm's perspective, the resources weren't installed by Helm.

This is by design — Argo wants Git to be the source of truth and Helm-release state introduces a second source. The downside: `helm rollback` does nothing useful, hooks have to be re-implemented via Argo sync waves, and chart `test`s aren't automatic.

There's an opt-in: `syncOptions: [HelmTemplate=true]` (the default) versus `[HelmInstall=true]` (rare, uses `helm install` and creates a real release).

### 27.2 Flux + Helm

Flux's `HelmRelease` uses the real Helm library:

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: prometheus
  namespace: monitoring
spec:
  interval: 10m
  chart:
    spec:
      chart: kube-prometheus-stack
      version: '55.x.x'
      sourceRef:
        kind: HelmRepository
        name: prometheus-community
        namespace: flux-system
  values:
    grafana:
      enabled: true
  install:
    crds: CreateReplace            # CRD upgrade policy
    remediation:
      retries: 3
  upgrade:
    crds: CreateReplace
    remediation:
      retries: 3
      remediateLastFailure: true
  rollback:
    cleanupOnFail: true
  driftDetection:
    mode: enabled
```

Flux creates a *real Helm release* (release Secret in `monitoring` namespace). `helm list -n monitoring` shows it. Hooks fire. `helm rollback` works.

**The split**: Argo's "render and apply" approach gives you a single source of truth (Git) at the cost of losing Helm-release semantics. Flux's "use Helm fully" approach keeps Helm-release semantics at the cost of a second piece of state (the release Secret) that must agree with Git.

Pick based on whether you care about Helm-isms (test, rollback, hooks-on-upgrade) or whether you'd rather have everything be plain `apply`.

---

## 28. Kustomize: Resources, Patches, Generators

Kustomize is the "no templating language" approach. It's a YAML-only tool that composes manifests via *transformations*: take this base, apply these patches, add these generators, set these labels.

### 28.1 The `kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

# Files (or URLs, or other kustomization dirs) to include
resources:
  - deployment.yaml
  - service.yaml
  - configmap.yaml
  - https://github.com/acme/manifests/some-base?ref=v1.2.0

# Configure top-level metadata applied to every resource
namespace: payments
namePrefix: prod-
nameSuffix: -v2

# Add labels and annotations to every resource (also propagates to selectors)
commonLabels:
  team: payments
  environment: prod

commonAnnotations:
  owner: payments-team@acme.com

# Override image tags
images:
  - name: ghcr.io/acme/api          # match by name
    newTag: 1.5.2                   # new tag
  - name: ghcr.io/acme/worker
    newName: ghcr.io/acme/worker-optimized  # new image entirely
    newTag: 2.0.1

# Generate ConfigMaps
configMapGenerator:
  - name: app-config
    literals:
      - LOG_LEVEL=info
      - REGION=us-east
    files:
      - config.json
      - settings=other-config.yaml
    envs:
      - .env

# Generate Secrets
secretGenerator:
  - name: db-creds
    literals:
      - password=changeme
    type: Opaque

# Control suffix-hashing of generators
generatorOptions:
  disableNameSuffixHash: false
  labels:
    generated: "true"

# Patches
patches:
  - target:
      kind: Deployment
      name: api
    patch: |
      - op: replace
        path: /spec/replicas
        value: 5

  - path: patch-api-resources.yaml
    target:
      kind: Deployment
      name: api

# Replacements (1.21+) — copy a value from one resource to another
replacements:
  - source:
      kind: ConfigMap
      name: cluster-info
      fieldPath: data.cluster-name
    targets:
      - select:
          kind: Deployment
        fieldPaths:
          - spec.template.spec.containers.[name=api].env.[name=CLUSTER].value

# Components (reusable composable units)
components:
  - ../../components/istio-injection
  - ../../components/ratelimit
```

### 28.2 Patches

Three patch styles (unified into `patches` in modern Kustomize, but you'll still see the older forms):

**Strategic merge patch** (the default, knows about Kubernetes types like containers-as-merge-by-name):

```yaml
# patch-api-resources.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            requests:
              cpu: 500m
              memory: 1Gi
```

**JSON 6902 patch** (RFC 6902, surgical):

```yaml
- op: replace
  path: /spec/template/spec/containers/0/image
  value: ghcr.io/acme/api:1.5.2
- op: add
  path: /spec/template/spec/tolerations/-
  value:
    key: spot
    operator: Equal
    value: "true"
    effect: NoSchedule
```

**Inline patch** (under the unified `patches:` key, target selector + patch body):

```yaml
patches:
  - target:
      group: apps
      version: v1
      kind: Deployment
      name: api
    patch: |
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: api
      spec:
        replicas: 5
```

Target selectors support `name`, `namespace`, `kind`, `group`, `version`, `labelSelector`, `annotationSelector`. This is the modern preferred form because one patch can target many resources at once.

### 28.3 Generators and disableNameSuffixHash

By default, `configMapGenerator` and `secretGenerator` append a content hash to the resource name — `app-config-7d8f4b2c`. The point is rolling-update-on-change: when you change the ConfigMap content, the name changes, every Deployment referencing it gets a new pod template hash, and Kubernetes rolls.

But if you want a stable name (e.g., the ConfigMap is referenced by hand-written objects outside Kustomize's scope), set `disableNameSuffixHash: true`.

Kustomize will *automatically rewrite references* to the hashed name if you used standard Kubernetes references (e.g., `envFrom.configMapRef.name: app-config` becomes `envFrom.configMapRef.name: app-config-7d8f4b2c`). This rewriting is configured by the "name reference" subsystem — by default it knows about all standard Kubernetes types; for CRDs you need to extend it via the `nameReference` transformer config.

### 28.4 The `images` transformer

A purpose-built shortcut for the most common need: overriding image tags. Useful in conjunction with image-update automation (the CI updates `kustomization.yaml`'s `images.newTag`, not the Deployment YAML).

### 28.5 Components (1.21+)

A `Kustomization` of kind `Component`:

```yaml
# components/istio-injection/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component

commonAnnotations:
  sidecar.istio.io/inject: "true"

patches:
  - target:
      kind: Namespace
    patch: |
      - op: add
        path: /metadata/labels/istio-injection
        value: enabled
```

Then include in an overlay:

```yaml
components:
  - ../../components/istio-injection
```

Components are like overlays except they're *composable* — multiple components apply to a single base. Use them when "we have N orthogonal toggles" (istio-injection, ratelimit, audit-logging, mtls); use overlays when "we have N environments".

---

## 29. Kustomize Overlays and Components

The canonical layout:

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │  app-frontend/                                                        │
   │  ├── base/                                                            │
   │  │   ├── kustomization.yaml                                           │
   │  │   ├── deployment.yaml                                              │
   │  │   ├── service.yaml                                                 │
   │  │   └── configmap.yaml                                               │
   │  └── overlays/                                                        │
   │      ├── dev/                                                         │
   │      │   ├── kustomization.yaml          ← resources: [../../base]   │
   │      │   ├── replicas-patch.yaml         ← 1 replica                  │
   │      │   └── ingress.yaml                ← dev.acme.com               │
   │      ├── staging/                                                     │
   │      │   ├── kustomization.yaml                                       │
   │      │   ├── replicas-patch.yaml         ← 2 replicas                 │
   │      │   └── ingress.yaml                ← staging.acme.com           │
   │      └── prod/                                                        │
   │          ├── kustomization.yaml                                       │
   │          ├── replicas-patch.yaml         ← 10 replicas                │
   │          ├── ingress.yaml                ← api.acme.com               │
   │          ├── pdb.yaml                    ← prod-only                  │
   │          └── hpa.yaml                    ← prod-only                  │
   └──────────────────────────────────────────────────────────────────────┘

   base/kustomization.yaml:
   ┌──────────────────────────────────────────┐
   │ apiVersion: kustomize.config.k8s.io/v1beta1
   │ kind: Kustomization                       │
   │ resources:                                │
   │   - deployment.yaml                       │
   │   - service.yaml                          │
   │   - configmap.yaml                        │
   │ commonLabels:                             │
   │   app.kubernetes.io/name: frontend        │
   └──────────────────────────────────────────┘

   overlays/prod/kustomization.yaml:
   ┌──────────────────────────────────────────┐
   │ apiVersion: kustomize.config.k8s.io/v1beta1
   │ kind: Kustomization                       │
   │ resources:                                │
   │   - ../../base                            │
   │   - ingress.yaml                          │
   │   - pdb.yaml                              │
   │   - hpa.yaml                              │
   │ namespace: frontend                       │
   │ namePrefix: prod-                         │
   │ commonLabels:                             │
   │   environment: prod                       │
   │ images:                                   │
   │   - name: ghcr.io/acme/frontend           │
   │     newTag: v1.5.2                        │
   │ patches:                                  │
   │   - path: replicas-patch.yaml             │
   │     target:                               │
   │       kind: Deployment                    │
   │       name: frontend                      │
   └──────────────────────────────────────────┘
```

`kustomize build overlays/prod` produces the rendered manifests for prod. The base is *not modified* — overlays don't mutate; they project.

**Multiple overlays for one base** is the entire point: dev, staging, prod, eu, us, canary, on-call-only. Each adds/replaces the bits that differ.

**Don't nest deeply.** Two levels (base → overlay) is comfortable. Three (base → component-overlay → environment-overlay) is the maximum any team can debug. Beyond that, you're better off with multiple bases or a refactor.

**Don't put environment-specific resources in the base.** If only prod has an HPA, the HPA file lives in `overlays/prod/`, not as `enabled: false` in the base.

**Cross-overlay sharing:** if dev and staging both want a config tweak that prod doesn't, you have two choices: (a) put it in both overlays (duplication, but simple), (b) introduce a mid-level overlay (`overlays/non-prod/` that includes the base, then dev and staging include `non-prod/`). Most teams accept the duplication.

The Kustomize source lives in `kubernetes-sigs/kustomize`; the binary is also embedded in `kubectl` (`kubectl apply -k <dir>`).

---

## 30. Helm vs Kustomize: The Honest Comparison

A debate older than Argo and Flux. The answer is "both, often together".

| Dimension | Helm | Kustomize |
|---|---|---|
| Approach | Templating (Go templates) | Patching (overlays) |
| Language | YAML + Go template syntax with Sprig | YAML only |
| Versioning | First-class (Chart.yaml version) | None; rely on Git |
| Packaging | Charts as tarballs; chart repos; OCI artifacts | None; raw dirs |
| Public ecosystem | Massive (artifacthub.io, bitnami, prometheus-community) | Smaller |
| Composability | Subcharts (in `charts/`), library charts | Resources (compose by reference); components |
| Conditionals | `{{- if ... }}` | Patches present-or-absent in overlays |
| Loops | `{{- range ... }}` | None |
| Programmability | Full Go template + Sprig | None (limited to declared transformations) |
| Multi-env | Multiple `values.yaml` (dev.yaml, prod.yaml) | Multiple overlays (`overlays/dev/`, `overlays/prod/`) |
| Image overrides | Via values | First-class (`images:` transformer) |
| Diff transparency | Templates obscure what gets rendered until you `helm template` | Overlays show "this is added/changed" explicitly |
| Lifecycle hooks | First-class (pre-install/post-install/etc.) | None |
| Release tracking | Real (release Secret) | None (you bring your own) |
| Learning curve | Templating language to learn | YAML and a small DSL |
| Best at | Distributing reusable software | Per-environment customisation |
| Worst at | Per-instance one-off changes | Distributing complex parameterised software |

**The decision tree:**

- *Are you distributing an application for others to install?* Helm. Chart repos, semver, values schema, the whole ecosystem.
- *Are you running your own apps in your own clusters, with environments and overlays?* Kustomize. No templating language, transparent diffs.
- *Both?* Yes. The common pattern is **`helm template ... | kubectl apply -k`-style**: use Helm to install upstream charts (Prometheus, cert-manager, nginx-ingress) and Kustomize for your in-house apps. Argo and Flux both let you mix: an Application can be a Helm chart with a Kustomize post-render.

**Helm's quiet killer feature**: `helm template <chart> --values ...` produces rendered YAML. You can use Helm as a *renderer* and pipe the output through Kustomize for environment-specific tweaks:

```yaml
# kustomization.yaml that renders a Helm chart
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
helmCharts:
  - name: prometheus
    repo: https://prometheus-community.github.io/helm-charts
    version: 55.0.0
    releaseName: prometheus
    namespace: monitoring
    valuesFile: values.yaml
patches:
  - path: prom-resources-patch.yaml
    target:
      kind: Deployment
      name: prometheus-server
```

`kustomize build --enable-helm .` runs Helm internally, then patches. Argo and Flux both support this when `--enable-helm` is set on the kustomize-controller (Flux) or the appropriate option on repo-server (Argo).

---

## 31. Render-Then-Apply Pipelines

A modern pattern: render Helm/Kustomize to flat YAML in CI, commit the rendered YAML to a separate "deploy" repo, and have the GitOps engine apply the rendered YAML.

```
                                 source repo
                            (Helm charts, Kustomize bases)
                                       │
                                       │ PR opens / merge
                                       ▼
                                ┌──────────────┐
                                │     CI       │
                                │  helm template / kustomize build
                                │  → rendered YAML files
                                │  optional: sealed-secrets encryption
                                │  optional: cosign sign
                                └──────┬───────┘
                                       │ git commit + push to deploy repo
                                       ▼
                                 deploy repo
                              (rendered YAML, per-environment dirs)
                                       │
                                       │ pull
                                       ▼
                                ┌──────────────┐
                                │ Argo / Flux  │
                                │  apply       │
                                └──────┬───────┘
                                       │
                                       ▼
                                  Kubernetes cluster
```

**Why this is appealing:**

1. **PR diff shows the exact YAML that will hit the cluster.** Reviewers see "here's the new Deployment manifest" rather than "here's the new value of `image.tag`". For security and compliance reviewers, this is invaluable.

2. **Reproducibility.** A commit in the deploy repo is the *complete* state of the cluster at that point. No need to render to inspect.

3. **Decoupled chart version from cluster state.** The chart can change, but until CI re-renders and commits, the cluster state is unchanged.

4. **Easier rollback.** `git revert` in the deploy repo immediately reverts the rendered YAML — no need to figure out which Helm values change reverts which manifest change.

**Why some teams avoid it:**

1. **Two repos to maintain.** PR opens against source; merge triggers CI; CI opens PR against deploy repo. Two sets of branch protection, two sets of CODEOWNERS, two PR review queues.

2. **Larger diffs.** A trivial Helm values change can produce a sprawling rendered YAML diff (replicaCount: 3 → 5 changes every manifest that references it via templating).

3. **Renderer drift.** If the renderer in CI is different from what Argo/Flux's repo-server would use, you can introduce subtle divergence. Pin tools.

4. **Less ecosystem support.** Some Argo features (multi-source Applications, ApplicationSet with chart sources) assume the engine renders, not CI.

A halfway option: **let Argo/Flux render but write rendered YAML to a side branch for auditability.** Some teams run a `kustomize build` in CI just to produce an artifact attached to the PR, while the engine still renders at apply time. You get the diff benefit without the second-repo overhead.

---

## 32. Progressive Delivery: Argo Rollouts and Flagger

GitOps gets you "apply the new spec". Progressive delivery gets you "apply the new spec *gradually*, watching metrics, and roll back if signals go bad". Two leading tools, one per camp.

### 32.1 Argo Rollouts

`argoproj/argo-rollouts`. Replaces `Deployment` with a `Rollout` CR. Supports canary and blue-green strategies, integrates with service meshes and ingress controllers for traffic shifting, runs `AnalysisTemplate` CRs against Prometheus/Datadog/etc.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: api
  namespace: payments
spec:
  replicas: 10
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: ghcr.io/acme/api:1.5.2
          ports:
            - containerPort: 8080
  strategy:
    canary:
      canaryService: api-canary
      stableService: api-stable
      trafficRouting:
        istio:
          virtualService:
            name: api
            routes:
              - primary
      steps:
        - setWeight: 10
        - pause: {duration: 5m}
        - analysis:
            templates:
              - templateName: success-rate
            args:
              - name: service-name
                value: api-canary
        - setWeight: 25
        - pause: {duration: 10m}
        - setWeight: 50
        - pause: {duration: 10m}
        - setWeight: 100
```

`AnalysisTemplate`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
  namespace: payments
spec:
  args:
    - name: service-name
  metrics:
    - name: success-rate
      interval: 1m
      successCondition: result[0] >= 0.99
      failureLimit: 3
      provider:
        prometheus:
          address: http://prometheus.monitoring:9090
          query: |
            sum(rate(http_requests_total{service="{{args.service-name}}",code=~"2.."}[5m]))
            /
            sum(rate(http_requests_total{service="{{args.service-name}}"}[5m]))
```

The Rollout controller progresses through steps, pauses for the configured duration or until analysis succeeds, and rolls back if analysis fails.

### 32.2 Flagger

`fluxcd/flagger`. Operates on standard `Deployment` (no replacement CR) but creates `Canary` CRs that drive the rollout via service mesh / ingress. Supports Istio, Linkerd, App Mesh, Open Service Mesh, NGINX, Contour, Gloo, traefik, Skipper.

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: api
  namespace: payments
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  progressDeadlineSeconds: 600
  service:
    port: 80
    targetPort: 8080
  analysis:
    interval: 1m
    threshold: 5
    maxWeight: 50
    stepWeight: 10
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: smoke-test
        url: http://flagger-loadtester.test/
        timeout: 5s
        metadata:
          type: cmd
          cmd: "curl -sd 'test' http://api-canary.payments:80/test"
```

Flagger creates a "primary" copy of the Deployment (the stable version). On a new image tag in the original, it creates a "canary" copy and incrementally shifts traffic from primary to canary. If metrics stay above thresholds, it promotes; otherwise it rolls back.

**Choose Argo Rollouts if** you're already running Argo and want a unified UI/CLI; you need fine-grained traffic-step control; you like the explicit `Rollout` CR.

**Choose Flagger if** you want to keep using `Deployment`; you're on Flux; you want the simplest possible CR.

Both integrate with Prometheus for analysis, both can roll back automatically, both support manual promotion ("hold canary at 25% until a human clicks promote").

---

## 33. PR Previews

The killer feature for product teams. Every open PR gets its own ephemeral preview environment.

Architecture (using Argo's PR generator):

```
   developer pushes branch                 ┌──────────────────────────────┐
   opens PR #42                            │  ApplicationSet              │
            │                              │  pullRequest generator       │
            │ webhook                      │  → Application "preview-42"  │
            ▼                              │  → namespace preview-42      │
   ┌─────────────────┐                     │  → URL preview-42.acme.com   │
   │  GitHub         │  poll/webhook       └────────┬─────────────────────┘
   │                 │ ───────────────────▶          │
   └─────────────────┘                              │
                                                    ▼
                                              cluster (dev)
                                              ┌─────────────────────┐
                                              │ namespace           │
                                              │ preview-42          │
                                              │   Deployment        │
                                              │   Service           │
                                              │   Ingress           │
                                              └─────────────────────┘

   PR merged or closed              ApplicationSet observes
                                    PR list change, deletes
                                    Application + namespace
```

The `ApplicationSet` with a PR generator (§12.5) generates an Application per open PR. The template uses `{{number}}`, `{{branch}}`, `{{head_sha}}` to namespace each preview:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: app-previews
  namespace: argocd
spec:
  generators:
    - pullRequest:
        github:
          owner: acme
          repo: app
          tokenRef:
            secretName: github-token
            key: token
          labels:
            - "preview-me"          # only PRs with this label
        requeueAfterSeconds: 60
  template:
    metadata:
      name: 'preview-{{number}}'
    spec:
      project: previews
      source:
        repoURL: https://github.com/acme/app
        targetRevision: '{{head_sha}}'
        path: deploy/preview
        helm:
          parameters:
            - name: image.tag
              value: 'pr-{{number}}'
            - name: ingress.host
              value: 'preview-{{number}}.preview.acme.com'
      destination:
        server: https://kubernetes.default.svc
        namespace: 'preview-{{number}}'
      syncPolicy:
        automated: {prune: true, selfHeal: true}
        syncOptions:
          - CreateNamespace=true
```

Production-grade preview environments require:
- **DNS wildcarding** — `*.preview.acme.com` → ingress controller, no per-PR DNS rule.
- **Cert wildcarding** — wildcard cert or per-namespace cert-manager.
- **Resource quotas per preview namespace** — one PR's bug doesn't OOM-kill the cluster.
- **Image build per PR** — CI builds `acme/app:pr-42` on every push.
- **TTL on stale previews** — a PR sitting open for 60 days is rare and unused; garbage collect.
- **Cost discipline** — previews on a separate, smaller cluster, ideally with spot instances.

The Flux equivalent uses `ImagePolicy` + a generator pattern, but the PR-preview model is more naturally an Argo `ApplicationSet` story.

---

## 34. Multi-Cluster GitOps

Two topologies dominate.

### 34.1 Hub model: one Argo, N clusters

```
   ┌───────────────────────────────────────────────────────────────────────┐
   │  hub cluster                                                           │
   │  ┌──────────────────┐                                                  │
   │  │   ArgoCD         │                                                  │
   │  │   + repo-server  │                                                  │
   │  │   + controller   │                                                  │
   │  │   + redis        │                                                  │
   │  └────────┬─────────┘                                                  │
   │           │ kubeconfigs (as Secrets)                                   │
   │           │                                                            │
   └───────────┼────────────────────────────────────────────────────────────┘
               │
               │ apply over the network
               │
   ┌───────────┼────────────────────────┬─────────────────────────────────┐
   ▼           ▼                        ▼                                 ▼
 ┌──────┐  ┌──────┐                ┌──────┐                            ┌──────┐
 │ c1   │  │ c2   │  ...           │ cN-1 │                            │ cN   │
 └──────┘  └──────┘                └──────┘                            └──────┘
```

**Pros:** single pane of glass; one Argo to upgrade; one set of credentials to manage; ApplicationSet with Cluster generator natural.
**Cons:** hub is a SPOF for deploys (cluster offline → no rollouts); apiservers must be reachable from the hub (network exposure); blast radius (compromise the hub, you have N clusters).

### 34.2 Federated model: one Argo (or Flux) per cluster

```
   ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
   │ cluster1 │     │ cluster2 │     │ clusterN │     │ clusterM │
   │  ┌────┐  │     │  ┌────┐  │     │  ┌────┐  │     │  ┌────┐  │
   │  │Flux│  │     │  │Flux│  │     │  │Flux│  │     │  │Flux│  │
   │  └─┬──┘  │     │  └─┬──┘  │     │  └─┬──┘  │     │  └─┬──┘  │
   └────┼─────┘     └────┼─────┘     └────┼─────┘     └────┼─────┘
        │                │                │                │
        └────────────────┴────────────────┴────────────────┘
                                  │
                                  ▼ pull from Git
                            ┌──────────────┐
                            │   Git repo   │
                            │ (cluster dirs)│
                            └──────────────┘
```

**Pros:** no SPOF; each cluster's engine has only its own credentials; no inbound network from a hub; perfect blast-radius isolation.
**Cons:** N engines to upgrade; no single UI for fleet view (Weave GitOps, Capacitor, or third-party fix this); fleet-level fan-out is harder (you set up the same `Kustomization` in each cluster's bootstrap).

Flux is more naturally federated; Argo is more naturally hub. You can do either with either, but the friction is different.

**The hybrid:** one *managing* Argo per region, each pointing at the clusters in its region. Gives you regional blast-radius isolation while keeping per-region UI.

For "true" multi-cluster GitOps with workload propagation, see ch 26: Karmada and Fleet sit *above* Argo/Flux, modelling clusters as resources and propagating workloads across them.

---

## 35. Bootstrap Pattern

The "first commit on a fresh cluster" problem. You just provisioned a new EKS/GKE/AKS cluster. Nothing is installed. How does ArgoCD get installed, configured, and start managing the cluster?

```
   1. provision cluster (ClusterAPI, eksctl, terraform, etc.)
   2. kubectl apply argocd-install.yaml          ← manual, one-time
                              │
                              ▼
              ArgoCD runs
                              │
   3. kubectl apply root-application.yaml         ← manual, one-time
                              │
                              ▼
              root Application points at Git
                              │
                              ▼
              Argo discovers child Applications
                              │
                              ▼
              Cluster catalog deploys automatically:
                - cert-manager
                - external-dns
                - prometheus
                - ingress-nginx
                - more Applications (your apps)
```

After steps 2–3, you never touch the cluster directly again. Every subsequent change is a Git commit.

A common refinement: ship steps 2 and 3 as a **bootstrap chart**:

```yaml
# bootstrap-chart/Chart.yaml
apiVersion: v2
name: argocd-bootstrap
version: 0.1.0
dependencies:
  - name: argo-cd
    version: 5.51.0
    repository: https://argoproj.github.io/argo-helm

# bootstrap-chart/templates/root-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: root
  namespace: argocd
spec:
  project: default
  source:
    repoURL: {{ .Values.gitRepo }}
    path: clusters/{{ .Values.clusterName }}
    targetRevision: main
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

One `helm install argocd-bootstrap ./bootstrap-chart -f values-prod-us-east.yaml` installs Argo AND the root Application pointing at `clusters/prod-us-east/`. The cluster then becomes self-managing.

For Flux, the analogous command is `flux bootstrap github --owner=acme --repository=fleet --path=clusters/prod-us-east --personal`. Flux installs itself and configures itself to manage Git-based reconciliation from the moment it boots.

**The bootstrap chart should also install:**
- Argo/Flux itself
- Cluster credentials (if hub model)
- The root Application/Kustomization
- AppProjects/RBAC (if not in Git)
- SealedSecrets controller (if used) — because the controller has the key, and the key can't be in Git

After bootstrap, *everything else* — including the Argo Application that manages Argo itself — is in Git. Argo can self-update via its own Application.

This pattern is also how disaster recovery works (ch 32): re-provision a cluster, run `helm install argocd-bootstrap`, and Argo recreates the entire cluster state from Git.

---

## 36. Secrets in GitOps

Already covered in §17 (ArgoCD secrets) and §20.2 (Flux SOPS), but worth a consolidated view:

| Tool | Key location | Encryption type | GitOps integration |
|---|---|---|---|
| sealed-secrets | In-cluster controller's private key | Asymmetric (RSA) | Apply SealedSecret CR, controller decrypts |
| External Secrets Operator | External (AWS/GCP/Azure/Vault) | None — secrets stored externally | Apply ExternalSecret CR, controller pulls |
| SOPS | Local PGP/age key OR cloud KMS | AES-GCM per value, key wrapped by KMS | Argo: helm-secrets / vault plugin; Flux: native |
| HashiCorp Vault | Vault server | Vault's own (transit / KV v2) | Via ESO, vault-injector, or vault-secrets-operator |
| AWS Secrets Manager / Parameter Store | AWS | KMS | Via ESO |
| 1Password Connect | 1Password vaults | 1Password's encryption | Via ESO or 1Password Operator |

**The decision is mostly about key rotation:**

- *Self-contained, no cloud dependency:* sealed-secrets. Accept the per-cluster key, accept the re-seal cost on rotation.
- *Cloud-native, automatic rotation:* ESO. Let AWS/GCP rotate the secret in the vault; ESO picks up the change.
- *Multi-tool legacy: ?* SOPS, which works in pure-Git-and-text without requiring an in-cluster controller for encryption (you do need a Secret with the decryption key in the cluster, though).

**Anti-pattern:** plain-text secrets in Git "just for dev". Once a habit, it's a leak waiting to happen. Even dev clusters should use the same secret mechanism as prod, with dev-scoped credentials.

**Anti-pattern:** sealed-secret encrypted with the wrong namespace key. SealedSecrets are by default *namespace-scoped* — re-sealing for a different namespace produces a different ciphertext. Moving a SealedSecret between namespaces requires re-sealing. Watch for this when copying an app between environments.

---

## 37. The Fight Over spec.replicas

The canonical example of multi-author conflict.

The actors:
- **HPA** writes `spec.replicas` based on metrics.
- **GitOps engine** writes `spec.replicas` based on Git.

If both insist, the Deployment oscillates: HPA scales to 7, Argo reverts to 3, HPA scales again, repeat.

### 37.1 The Argo fix

Tell Argo to ignore `spec.replicas`:

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
  syncPolicy:
    syncOptions:
      - RespectIgnoreDifferences=true
```

Then *don't put `spec.replicas` in your Git manifest at all* — let the HPA's default be applied on first creation.

With Server-Side Apply, the cleaner solution:

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      managedFieldsManagers:
        - horizontal-pod-autoscaler
        - kube-controller-manager
  syncPolicy:
    syncOptions:
      - ServerSideApply=true
      - RespectIgnoreDifferences=true
```

This tells Argo: ignore any field owned by the HPA or the controller-manager. You can put `spec.replicas` in Git for the initial creation; once HPA takes over, Argo cedes ownership.

### 37.2 The Flux fix

Flux's `Kustomization` has `spec.force: true` (re-apply) but the right approach is *not to manage replicas via the Kustomization*:

```yaml
spec:
  patches:
    - target:
        kind: Deployment
        name: api
      patch: |
        - op: remove
          path: /spec/replicas    # ← strip replicas from rendered manifest
```

Or, in `HelmRelease`, configure values to *not set* `replicaCount` (some charts let you omit it). Or use the `driftDetection.ignore` field:

```yaml
spec:
  driftDetection:
    mode: enabled
    ignore:
      - paths: ["/spec/replicas"]
        target:
          kind: Deployment
```

### 37.3 The other replicas fight

`spec.replicas` isn't the only field with this problem. The full list of "fields managed by another actor":
- HPA writes Deployment/StatefulSet/Rollout `spec.replicas`.
- `cert-manager` writes Secret `data` and `metadata.annotations.cert-manager.io/*`.
- `external-dns` writes Service `status.loadBalancer.ingress` (well, it doesn't — but it reads it).
- The apiserver itself writes `metadata.uid`, `metadata.resourceVersion`, `metadata.generation`, `status`.
- The HPA writes `status.currentReplicas` etc. on its own object.
- The webhook injector (Istio, Linkerd) injects sidecar containers into Pods.

For every multi-author field, you need the right `ignoreDifferences` rule. SSA-based ownership is by far the cleanest answer; once your cluster is SSA-first, conflicts become declarative ("kube-controller-manager owns this; Argo doesn't").

---

## 38. Tools Beyond Argo and Flux

A short tour of the rest of the space.

**Rancher Fleet** (`rancher/fleet`) — designed for very large fleets (10k+ clusters). Models "cluster groups" and "bundle deployments"; integrates tightly with Rancher's multi-cluster management. Less feature-rich per-app than Argo/Flux but excels at scale.

**Jenkins X** (`jenkins-x/jx`) — once the GitOps-CI alternative; mostly deprecated. The pipeline focus didn't survive Argo/Flux's rise. Historical interest only.

**Werf** (`werf/werf`) — combines image building, Helm-based deployment, and a CI-driver model. Used heavily in CIS / Eastern European tech. Less popular outside its home niche.

**Codefresh GitOps** — commercial product built on top of ArgoCD; adds a managed control plane, a fleet UI, observability. If you're an ArgoCD shop wanting a SaaS layer.

**Atlantis** (`runatlantis/atlantis`) — *not* Kubernetes GitOps; it's Terraform GitOps. But for cluster *infrastructure* (the K8s itself, the VPC, the IAM), Atlantis is the analogue. Often runs alongside Argo/Flux: Atlantis manages infra, Argo manages workloads.

**KubeVela** (`kubevela/kubevela`) — application-centric abstraction layer; uses OAM (Open Application Model). Sits *above* Argo/Flux conceptually. Niche outside Alibaba and a few Chinese tech companies.

**Crossplane** (`crossplane/crossplane`) — covered in ch 23 § 24. Not strictly GitOps; it extends Kubernetes APIs to manage cloud resources. Often paired with Argo: Argo applies Crossplane CRs from Git, Crossplane reconciles them to AWS/GCP/Azure.

**Spinnaker** — pre-K8s-native CD tool, still in some Netflix-DNA shops. Has a Kubernetes provider; not GitOps in the modern sense; loses to Argo/Flux for cluster-first deployments.

For 95% of organisations in 2026, the choice is Argo vs Flux. The above tools cover edge cases and niches.

---

## 39. Anti-Patterns

The collection of "what NOT to do," each is a real outage somewhere.

**Multiple sources of truth.** CI runs `kubectl apply` AND Argo syncs from Git. They fight; reality drifts between deploys. Fix: one source of truth. Pick GitOps and prohibit `kubectl apply` outside of break-glass.

**Manual `kubectl edit` in prod.** Someone fixes an outage by editing a Deployment directly. Argo's selfHeal reverts the fix in 3 minutes. The on-call pages again. Fix: with selfHeal=true, manual edits are forbidden; with selfHeal=false, manual edits are paged on (drift alert). Either way, the *fix* must go to Git.

**Branch-per-environment.** `dev` branch → dev cluster; `staging` branch → staging; `main` → prod. Sounds clean. Then a fix in dev needs to get to prod: rebase, cherry-pick, merge conflict, mistake, page. Fix: overlay-per-environment (§29). One branch, multiple overlays.

**Long-lived secrets in plaintext.** Dev tokens "just to get started" check into Git. Months later they're production credentials. Fix: secret tooling from day one, even in dev.

**GitOps controller without RBAC scoping.** ArgoCD runs as cluster-admin globally. Any Application can deploy anything anywhere, including to `kube-system`. Fix: AppProject per tenant; resource whitelists; restricted destinations.

**App-of-apps with self-reference.** Root Application's source directory contains root.yaml. Infinite reconcile loop. Fix: keep the root manifest outside the directory it watches.

**ApplicationSet generator producing empty list.** Cluster label changes; matcher matches nothing; previously-generated Applications get deleted; cluster wiped. Fix: `goTemplate: true` with `preservedFields`; safety review on generator changes; deny-prune for ApplicationSet.

**Sync-wave omitted on CRDs.** Operator and CR are in the same wave. Apply order is undefined; CR is applied before CRD is established; CR fails to register. Fix: CRDs in wave -10, operator in 0, CRs in 5. Or use `dependsOn` (Flux).

**Long-running PreSync hooks blocking sync.** A 30-minute database migration is a PreSync hook. Sync timeout fires (Argo default 5m for op state). Fix: don't run long migrations as PreSync; trigger them separately, gate the next deploy on success via a Job that checks migration state.

**`ignoreDifferences` too permissive.** Ignoring `/spec` to "make the diff quiet" hides real drift. Fix: each `ignoreDifferences` rule must be justified and named; review periodically.

**Helm uninstall leaving CRDs.** Helm v3 doesn't delete CRDs on uninstall by design. Removing an Application that owns a CRD leaves the CRD and any custom resources orphaned. Fix: separate the CRD chart from the controller chart, or delete CRDs explicitly.

**`commit-on-every-update` spam.** Image-automation commits to `main` on every new tag. The repo accumulates a commit per minute. Fix: commit to a PR branch and require approval; debounce updates; promote on velocity thresholds.

**CRD storage version flipped without conversion.** Operator chart bumps the CRD's storage version from v1beta1 to v1 without a conversion webhook. Existing resources can't be read. Fix: storage version changes need a conversion plan (ch 23).

**Render pipeline emits secrets to plaintext rendered repo.** CI renders a chart with secret values; the rendered YAML in the deploy repo contains those values in cleartext. Fix: render-then-encrypt; SOPS-encrypt the rendered output; or use SealedSecrets/ESO so the deploy repo never contains plaintext.

---

## 40. Observability of GitOps

You deployed a GitOps engine to manage your apps. Who watches the watcher?

### 40.1 ArgoCD metrics

Exposed on each component's `/metrics`. Most useful:

- `argocd_app_info{name, namespace, project, repo, dest_namespace, dest_server, sync_status, health_status}` — gauge of each Application's current state.
- `argocd_app_sync_total{name, namespace, project, phase, dest_server}` — counter of syncs.
- `argocd_app_health_total{name, namespace, project, health_status}` — counter of health-status transitions.
- `argocd_app_reconcile_bucket` / `_count` / `_sum` — histogram of reconcile durations.
- `argocd_git_request_duration_seconds_bucket` — histogram of Git fetch durations (debug repo-server slowness).
- `argocd_kubectl_exec_pending` — backpressure indicator on the application-controller.

Sample alerts:
- `argocd_app_info{sync_status="OutOfSync"}` for >15m → page.
- `argocd_app_info{health_status="Degraded"}` for >5m → page.
- `rate(argocd_app_sync_total{phase="Failed"}[1h]) > 0.01` → ticket.
- `argocd_kubectl_exec_pending > 100` → scale application-controller.

### 40.2 Flux metrics

Exposed by each controller. Naming: `gotk_*` (GitOps Toolkit).

- `gotk_reconcile_condition{kind, name, namespace, type, status}` — per-resource reconcile state.
- `gotk_reconcile_duration_seconds_bucket{kind, name, namespace}` — reconcile latency.
- `gotk_suspend_status{kind, name, namespace}` — is reconciliation suspended?
- `controller_runtime_reconcile_total` and `_errors_total` — standard controller-runtime metrics.

Sample alerts:
- `gotk_reconcile_condition{type="Ready", status="False"}` for >5m → page.
- `rate(controller_runtime_reconcile_errors_total[5m]) > 0.1` → page.
- `time() - gotk_reconcile_last_succeeded_time_seconds > 600` → page.

### 40.3 What to dashboard

- Apps OutOfSync over time (a steady non-zero count is acceptable; spikes are not).
- Sync failures, grouped by Application.
- Repo-server cache hit rate (low = repo-server thrashing).
- Reconcile durations p50/p95/p99 (creeping = scale needed).
- Number of Applications/Kustomizations/HelmReleases (capacity planning).

### 40.4 Argo / Flux audit

Both engines emit Kubernetes Events on every meaningful action. `kubectl get events -n argocd` shows recent sync/health events. For longer history, ship events to a backend (Loki, Elasticsearch).

The apiserver audit log records every `apply` Argo/Flux performs (chs 05, 07). Combined with Git history, you can reconstruct: "who proposed this change, when was it merged, when was it applied, by which engine instance, against which cluster".

---

## 41. Pitfalls: The Long List

A field guide. Each is a real production incident from somewhere.

1. **Engine without RBAC scoping.** ArgoCD or Flux running cluster-admin globally with no AppProject/namespace boundaries. Any Application can write `kube-system`. Fix: AppProject + cluster-resource whitelists; Flux multi-tenancy via ServiceAccount impersonation.

2. **Manifest with templating left unrendered.** A `{{ .Values.foo }}` lands in the cluster as a literal string because Argo applied the raw template file (the source was wrongly typed as plain YAML, not Helm). Fix: explicit `helm.chartPath` and `source.helm` blocks; auto-detection caveats.

3. **`helm template` not idempotent across versions.** A Helm chart that was deterministic at 3.10 isn't at 3.12 (random-default test certificates, ordered iteration, etc.). Suddenly every reconcile reports diff. Fix: pin Helm version in repo-server / kustomize-controller; avoid `randAlphaNum` and `genCA` without `lookup` reuse.

4. **Kustomize patches that don't merge.** Strategic merge patch targets `containers[0]` but the base reorders containers; patch silently misses. Fix: target by `name` (strategic merge merges by name for containers); prefer JSON6902 with explicit paths.

5. **App-of-apps with self-reference.** Root Application's path includes root.yaml. Argo applies root.yaml as a child of root.yaml. Reconcile loop. Fix: keep root.yaml outside the root path; or use ApplicationSet with a generator that excludes root.

6. **ApplicationSet generator producing empty list.** Cluster generator selector typo → 0 clusters match → 0 Applications generated → prune deletes the previously-generated set → cluster wiped. Fix: ApplicationSet has `preservedFields` and `applicationsSync` policies; safety-test changes; `--policy=create-update` for one-way generators.

7. **Sync-wave omitted on CRD-before-CR ordering.** Operator chart bundles CRDs and CRs together; without waves, race causes CR to apply before CRD is established. Fix: CRDs in wave -10, operator in 0, CRs in 5; or Helm `crd-install` hook.

8. **Long-running PreSync hooks.** Database migration as PreSync hook takes 40 minutes; Argo's operation timeout is 5m by default. Fix: use a separate Job that the chart awaits via readinessGate; bump `timeout.hook.expired` in `argocd-cm`.

9. **`ignoreDifferences` too permissive.** Ignoring `/spec` "to quiet diffs" hides real changes. Fix: each rule is justified and the smallest possible JSONPointer.

10. **Sealed-secret encrypted with wrong namespace key.** Copy SealedSecret from `dev` to `prod` namespace without re-sealing; the prod controller can't decrypt because the ciphertext is bound to the dev namespace. Fix: re-seal per namespace; or use `sealedsecrets.bitnami.com/cluster-wide` annotation for cluster-wide secrets.

11. **Image-automation policy that picks pre-release tags.** Semver range `>=1.5.0` matches `1.6.0-rc1`. Pre-release goes to prod. Fix: explicit exclusion in regex; use `semver` policy with strict ordering options; gate via PR rather than direct commit.

12. **Commit-on-every-update spam.** Image-automation commits to `main` on every new tag, every 5 minutes. Repo history is unreadable; PRs merge into dozens of commits. Fix: batch commits; commit to a PR branch; gate by velocity threshold.

13. **CRD storage version flipped without conversion.** Operator upgrade switches CRD `served+storage` from v1alpha1 to v1; existing v1alpha1 objects fail to read. Fix: conversion webhooks (ch 23 §10); never change storage version without one in production.

14. **Helm uninstall leaving CRDs.** Helm v3 leaves CRDs by design. Uninstall the chart, reinstall a different chart with the same CRDs at a different version → schema conflict. Fix: separate CRD chart; `helm.sh/resource-policy: keep` for CRDs you control; explicit CRD lifecycle.

15. **Render pipeline emits secrets to plaintext rendered repo.** CI does `helm template --values prod.yaml` where `prod.yaml` has plaintext passwords; the rendered output goes to a "deploy" repo containing those passwords. Fix: SOPS-encrypt the rendered output; or use SealedSecrets so the rendered output is already-encrypted.

16. **No `prune` on ApplicationSet.** Generator output shrinks (a PR is closed); ApplicationSet still has the old Application; Application stays as-is forever. Fix: ApplicationSet's own prune policy (`syncPolicy.preserveResourcesOnDeletion: false`).

17. **Mixing client-side and server-side apply on the same Application.** First sync was client-side; second sync flips to SSA; field-manager confusion; phantom diffs. Fix: pick one and stick; if migrating, do a one-time `kubectl apply --server-side --force-conflicts`.

18. **Helm chart that uses `lookup` without consideration.** `lookup` returns nil at `helm template` time but real values at `helm install` time; Argo's `helm template` mode produces incomplete output. Fix: avoid `lookup`; use `Capabilities`; render with `--dry-run=server` only.

19. **Long Git histories slowing repo-server.** A 5-year-old manifest repo with 100k commits; every `git clone` is slow. Fix: shallow clones (Argo and Flux both default to `--depth=1`); shard repos by team.

20. **Argo `automated.prune: true` plus a temporarily broken manifest.** A typo in a Kustomization yields zero resources; `prune` deletes everything. Fix: `automated.allowEmpty: false`; canary with `automated.prune: false` initially; review-required branch protection.

21. **Webhook auth on Receivers wrong.** Flux `Receiver` doesn't validate webhook signatures; anyone can trigger reconcile. Fix: `secretRef` with HMAC validation; allow-list source IPs; rate-limit at the ingress.

22. **Argo `selfHealTimeout` too aggressive.** Default is 5s; an operator that writes to the same field every reconcile triggers tight write loops. Fix: increase `selfHealTimeout`; or use `ignoreDifferences` to take Argo out of the loop.

23. **PR generator with no namespace TTL.** Stale PR previews accumulate; cluster fills with `preview-*` namespaces. Fix: ApplicationSet `goTemplate` with TTL annotation; cron job that prunes preview namespaces by age.

24. **Helm `--wait` without timeout.** Argo's `Application.spec.source.helm.wait: true` waits for Helm hooks indefinitely. Fix: combine with explicit timeout; or rely on Argo's health checks instead of Helm wait.

25. **Bootstrap chart applied to wrong cluster.** `helm install argocd-bootstrap -f prod.yaml` against the dev cluster; dev now thinks it's prod. Fix: include cluster-name guard in chart (CR with allowed cluster list); CI applies bootstrap, never humans.

26. **Argo Application points at a moving Git ref.** `targetRevision: main` plus auto-sync = every Git push is a deploy. Sounds good for dev, terrifying for prod. Fix: prod points at semver tags or specific SHAs.

27. **Mixed source `kustomization.yaml`s in one tree.** A Kustomization at directory level + a Kustomization at parent level, each pulling resources differently; ambiguous resource ordering. Fix: only ever one `kustomization.yaml` per directory; one path per Application.

28. **Argo `revisionHistoryLimit: 0`.** No history kept; rollback is impossible. Fix: keep at least 10 revisions.

29. **Flux `dependsOn` cycles.** Kustomization A depends on B, B on C, C on A; nothing reconciles. Fix: tools to detect; explicit DAG documentation.

30. **NotificationController flood on a bad week.** A degraded app trips on-health-degraded every minute; Slack channel becomes useless. Fix: rate-limit at the provider level; trigger conditions with windows (`for: 10m`); aggregate events.

---

## 42. TL;DR

GitOps is the reconcile pattern from chapter 08 applied at the cluster-management level: Git is the desired state, etcd is the observed state, and an in-cluster controller (Argo's Application controller, Flux's kustomize/helm-controller) continuously closes the gap. The four principles — *declarative, versioned, automatically pulled, continuously reconciled* — pick Kubernetes as their natural substrate because Kubernetes is already declarative, watchable, and RBAC-aware.

**Pull beats push** because clusters can stay private (no inbound apiserver exposure), credentials are scoped per-cluster (no CI-as-blast-radius), and continuous reconcile is structurally only possible with an in-cluster agent.

**ArgoCD** ships a small handful of services (server, application-controller, repo-server, redis, applicationset-controller, notifications, optional dex), three core CRDs (`Application`, `AppProject`, `ApplicationSet`), and a feature-rich UI. **Flux** ships one controller per concern (source, kustomize, helm, notification, image-reflector, image-automation), ten-ish CRDs, no UI by default, and a "compose your toolkit" architecture. **Both are mature, both work; team preference dominates the choice.**

**Sync waves and phases** order applies within a sync; **health assessment** (built-in for standard types, Lua for custom) gates wave advancement; **drift detection** plus **self-heal** make Git the only source of truth even against in-cluster fiddling; **`ignoreDifferences`** (and SSA `managedFieldsManagers`) handle multi-author objects like HPA-managed replicas.

**Helm** is templating + packaging + release tracking; its v3 architecture is client-only with release state stored as Secrets. **Kustomize** is YAML-only overlays + patches + generators with no templating language. They are not enemies; the pattern is *Helm for distributed software, Kustomize for environment customisation, often layered together*.

**`ApplicationSet`** + **PR generator** is the killer feature for previews; **Cluster generator** is the killer feature for fleets; **render-then-apply pipelines** are the killer feature for change-control-heavy orgs (the PR diff shows exact YAML).

**Progressive delivery** layers on top: Argo Rollouts (CR-based) or Flagger (Deployment-based) gradually shift traffic, watch Prometheus, promote or roll back.

**Secrets** never go to Git in plaintext: sealed-secrets (in-cluster key, asymmetric), External Secrets Operator (sync from external vaults), or SOPS (encrypted at the value level with KMS keys).

**Multi-cluster** has two topologies: hub (one Argo, N clusters) and federated (one Argo/Flux per cluster). Hub is simpler, federated has better blast radius.

**The bootstrap pattern** is a one-time `helm install argocd-bootstrap` on a fresh cluster; from that command onward the cluster manages itself from Git. Disaster recovery is the same command, applied to a fresh cluster.

**The spec.replicas fight** with HPA is the canonical multi-author conflict: solve via `ignoreDifferences` + JSONPointer, or — cleaner — SSA + `managedFieldsManagers`.

**Anti-patterns** are mostly "multiple sources of truth" (CI + GitOps fighting), "manual edits in prod" (drift), "branch-per-environment" (merge conflicts), "no RBAC scoping" (engine has globe-spanning powers), and "long-lived plaintext secrets". The cure for all of them is the same: **Git is the spec; nothing else writes; the engine is the only applier; everything else is a controller.**

When the chapter reduces to one sentence: **the cluster is downstream of Git, the engine is a level-triggered reconciler, and the only debate worth having is which engine and where the secrets live.** Everything else — Helm vs Kustomize, hub vs federated, PR previews, image automation, sync waves — is implementation detail you bolt onto that one mental model.
