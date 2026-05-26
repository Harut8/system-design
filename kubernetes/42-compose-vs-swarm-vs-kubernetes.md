# Docker Compose vs Swarm vs Kubernetes: Choosing the Right Orchestrator

Three orchestrators, three philosophies, three different right answers. This chapter is the **decision framework** — not a feature list. The goal is to put a staff engineer in a room where "we're outgrowing Compose; should we go to Swarm or Kubernetes?" gets a real answer in under fifteen minutes, with reasoning that doesn't sound like a vendor pitch.

The short version, before we earn it:

- **Compose** is a developer-experience tool with optional single-host deployment. Stay there for dev environments, integration tests, and "one box does it" deployments.
- **Swarm** is "Compose for many hosts, with HA built in." It is genuinely good for what it does — but its community and feature velocity have collapsed since 2018, and you should not start a new project on it in 2026 unless you have a specific reason.
- **Kubernetes** is the industry standard. Steep on the way up, vast and pluggable on the plateau. Anything serious lands here, and the smaller orchestrators are increasingly downstream consumers of K8s primitives.

The rest of this chapter explains *why*, dimension by dimension, and gives you a decision tree.

---

## Table of Contents

1. [The Three Models at a Glance](#1-the-three-models-at-a-glance)
2. [Architecture: What's Actually Running](#2-architecture-whats-actually-running)
3. [State Model: Imperative vs Declarative Reconciliation](#3-state-model-imperative-vs-declarative-reconciliation)
4. [Networking](#4-networking)
5. [Storage and Volumes](#5-storage-and-volumes)
6. [Service Discovery and Load Balancing](#6-service-discovery-and-load-balancing)
7. [Rolling Updates and Deploy Strategies](#7-rolling-updates-and-deploy-strategies)
8. [Scaling: Manual, Reactive, Predictive](#8-scaling-manual-reactive-predictive)
9. [Secrets and Config](#9-secrets-and-config)
10. [Multi-Tenancy and RBAC](#10-multi-tenancy-and-rbac)
11. [Observability and Operations](#11-observability-and-operations)
12. [Extensibility: CRDs, Plugins, Webhooks](#12-extensibility-crds-plugins-webhooks)
13. [Cost and Operational Burden](#13-cost-and-operational-burden)
14. [Ecosystem and Hiring](#14-ecosystem-and-hiring)
15. [Failure Modes and Blast Radius](#15-failure-modes-and-blast-radius)
16. [Migration Paths and Lock-In](#16-migration-paths-and-lock-in)
17. [The Decision Tree](#17-the-decision-tree)
18. [When to Use Each: Concrete Profiles](#18-when-to-use-each-concrete-profiles)
19. [TL;DR](#19-tldr)

---

## 1. The Three Models at a Glance

| Dimension | Compose | Swarm | Kubernetes |
|---|---|---|---|
| Scope | Single host (mostly) | Multi-host cluster | Multi-host cluster |
| Architecture | CLI → Docker Engine API | Manager nodes (Raft) + workers | Control plane (etcd + API server + controllers) + nodes |
| Declarative? | YAML, but no controller; client-side state | YAML + controllers reconciling cluster state | YAML + many controllers reconciling cluster state |
| HA built in | No | Yes (managers in Raft, services replicated) | Yes (control plane HA, deployments, statefulsets) |
| Update strategy | Stop/start | Rolling updates with constraints | Rolling, blue-green, canary, custom |
| Scaling | Manual (`--scale`) | Manual (`docker service scale`) | Manual + HPA + VPA + KEDA + ClusterAutoscaler |
| Service discovery | DNS via embedded resolver | Embedded DNS + VIP per service | CoreDNS + ClusterIP + Endpoints + EndpointSlice |
| Load balancing | None (round-robin DNS) | Routing mesh (IPVS) | kube-proxy + service mesh + ingress controllers |
| Secrets | tmpfs files (plain) | Encrypted at rest in Raft, mounted as tmpfs | etcd (encrypted at rest), mounted as files or env |
| RBAC | None | Limited (manager/worker roles) | Full RBAC (users, SAs, roles, bindings) |
| Extensibility | None | Limited (plugins for network/volume) | Massive (CRDs, operators, admission webhooks, API aggregation) |
| Ecosystem | Small | Shrinking | Industry standard |
| Learning curve | Low | Medium | High |
| Operational burden | Very low | Low-medium | Medium-high (managed K8s mitigates) |
| Best for | Dev, tests, single-host prod | Small/medium clusters with simple needs | Production at any scale, custom workloads, multi-tenancy |

The deeper differences are in *what* state model and reconciliation each one uses. That's where actual decisions get made.

---

## 2. Architecture: What's Actually Running

### Compose

- A binary on your laptop.
- Reads YAML, calls Docker Engine API.
- No daemon, no cluster, no shared state.
- If you SSH to another host and run `docker compose ps`, you see nothing — there's no global registry of Compose stacks.

The Docker daemon itself runs on the host. Compose is just a smart client.

### Swarm

When you `docker swarm init`, the Docker daemon enters **Swarm mode**. The daemon now has two roles:

- **Manager node:** participates in a Raft consensus group (3 or 5 managers typical). Maintains the cluster's desired state.
- **Worker node:** receives tasks from managers, runs them.

```
┌──────────────────────────────────────────────────────┐
│  SWARM CLUSTER                                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │ Manager 1   │  │ Manager 2   │  │ Manager 3   │  │
│  │  (Raft)     │  │  (Raft)     │  │  (Raft)     │  │
│  └─────┬───────┘  └──────┬──────┘  └──────┬──────┘  │
│        │                 │                │          │
│  ┌─────▼───────┐  ┌──────▼──────┐  ┌──────▼──────┐  │
│  │ Worker A    │  │ Worker B    │  │ Worker C    │  │
│  │ (containers)│  │ (containers)│  │ (containers)│  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
└──────────────────────────────────────────────────────┘
```

State is in the managers' Raft log. The Docker daemon process handles both engine-level container management and Swarm orchestration. No separate processes.

This is elegantly minimal. It is also why Swarm cannot grow features quickly — every feature must fit into the Docker daemon's process model.

### Kubernetes

A separation of concerns, each with its own process:

```
┌──────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE                                                   │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌──────────────┐│
│  │ kube-      │ │ etcd       │ │ kube-        │ │ controller-  ││
│  │ apiserver  │ │ (KV store) │ │ scheduler    │ │ manager      ││
│  └────────────┘ └────────────┘ └──────────────┘ └──────────────┘│
└──────────────────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────┐
│  NODES                                                           │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐                │
│  │ kubelet     │ │ kubelet     │ │ kubelet     │                │
│  │ kube-proxy  │ │ kube-proxy  │ │ kube-proxy  │                │
│  │ runtime     │ │ runtime     │ │ runtime     │                │
│  └─────────────┘ └─────────────┘ └─────────────┘                │
└──────────────────────────────────────────────────────────────────┘
```

Five primary control-plane processes (apiserver, etcd, scheduler, controller-manager, cloud-controller-manager) plus per-node processes (kubelet, kube-proxy, CNI, CSI). Each has narrow responsibilities. New features arrive as new controllers or new APIs without rewriting the daemon.

The architectural difference matters because **Swarm's growth is bounded by what fits cleanly into the Docker daemon; Kubernetes' growth is bounded only by the API server's extension surfaces**. The two are not in the same growth regime.

---

## 3. State Model: Imperative vs Declarative Reconciliation

The deepest difference is the **reconciliation model**.

### Compose: Snapshot Imperative

`docker compose up` is a one-shot operation:

1. Read YAML.
2. Compute diff vs running state.
3. Apply the diff (create/update/destroy containers).
4. Exit (or follow logs).

Once Compose exits, nothing watches the state. If a container dies and `restart: always` is set, the *Docker daemon* restarts it — Compose has nothing to do with that. If a container has the wrong image (because you modified the YAML on another host and someone manually ran `docker run`), Compose doesn't know until you run `up` again.

Compose is essentially `make` for containers: declarative inputs, imperative apply.

### Swarm: Cluster-Wide Reconciliation

Services in Swarm are declared and **continuously reconciled by manager nodes**:

```
docker service create --name api --replicas 5 myorg/api:1.5
```

If a node dies, manager nodes reschedule the 5 replicas onto remaining nodes. If a task crashes, it's restarted. If you update the image, a rolling update propagates the change. The manager's Raft log holds the desired state; every change to that log triggers reconciliation.

This is real reconciliation. It is a much closer cousin to Kubernetes than to Compose.

### Kubernetes: Many Controllers, One Source of Truth

Kubernetes goes further: *every* concept is a controller pattern.

- A `Deployment` controller watches `Deployments` and creates `ReplicaSets`.
- A `ReplicaSet` controller watches `ReplicaSets` and creates `Pods`.
- The scheduler watches unscheduled `Pods` and binds them to nodes.
- The kubelet on each node watches its `Pods` and runs them.
- A `HorizontalPodAutoscaler` controller watches metrics and updates `Deployment.spec.replicas`.
- A custom operator might watch a CRD and create `Deployments`, `Services`, and `Secrets`.

The same pattern, recursively applied. Each controller is small, narrow, and replaceable. The result is a system where state transitions are explicit and observable.

The practical consequence: **drift in Kubernetes is detected and corrected automatically; drift in Compose has to be detected by you.**

---

## 4. Networking

### Compose

Default: one bridge network per project. Services on the same network resolve each other by service name (Docker embedded DNS). Pretty simple.

Limitations:

- One host only.
- No load balancing beyond round-robin DNS.
- No network policy / segmentation beyond network membership.

### Swarm

Overlay networks span multiple hosts using VXLAN. Services have a virtual IP (VIP) that the **routing mesh** load-balances across replicas using IPVS. A service exposed on port 80 is reachable on port 80 of *any* node in the swarm — the routing mesh proxies the request to a node with a healthy replica.

This is **legitimately good** for many use cases. The routing mesh is L4 (no HTTP-aware features), but it works without ingress controllers, service meshes, or anything else. You stand up a Swarm cluster and `docker service create --publish 80:80 myorg/api` — done.

Limitations:

- L4 only. No path-based routing, no HTTP headers, no TLS termination (need Traefik or Caddy as a service for that).
- VXLAN encapsulation overhead (~5-10% throughput on small packets).
- No network policy in the K8s sense; you get network *membership* but no fine-grained rules.

### Kubernetes

The pod-per-IP model: every pod gets a routable IP via CNI. Services (ClusterIP/NodePort/LoadBalancer) are virtual constructs implemented by kube-proxy (iptables/IPVS) or eBPF (Cilium). NetworkPolicies allow L3/L4 segmentation. Service meshes (Istio, Linkerd, Cilium) add L7 features: mTLS, retries, circuit breaking, traffic shifting, observability.

Ingress controllers (NGINX, Traefik, HAProxy, AWS ALB, GCE) handle external-to-cluster traffic at L7. Gateway API generalizes this.

The cost: much more to configure. CNI to choose, kube-proxy mode to pick, ingress controller to install, optionally a service mesh, optionally external DNS. Most managed K8s services (EKS, GKE, AKS) bundle most of this.

The benefit: anything you can imagine doing at the network layer, you can do. Multi-cluster service mesh? Yes. Pinning a service to a specific NIC? Yes. mTLS everywhere? Yes. Routing 1% of traffic to a canary based on a header? Yes.

For most production workloads, this is the right level of abstraction. For "I want one server with five containers reachable on port 80," it's wildly overkill.

---

## 5. Storage and Volumes

### Compose

Named volumes on the host's filesystem. Bind mounts to host directories. That's it. No cluster-level concept of storage.

### Swarm

Same as Compose, but with **volume plugins**: third-party drivers can provide cluster-aware storage (REX-Ray, Portworx, GlusterFS, NFS). The plugins are mounted into the daemon and provide volumes that move with services across nodes.

In practice, the volume plugin ecosystem is **mostly dead** — REX-Ray is unmaintained, Portworx focuses on K8s, GlusterFS exists but you're maintaining a distributed FS by hand. Most production Swarm users either:

- Use NFS or SMB shared filesystems mounted as bind mounts on every node.
- Pin services to specific nodes (using constraints) and use local volumes.
- Push state to managed services (RDS, ElastiCache) outside Swarm.

### Kubernetes

CSI (Container Storage Interface) is the standard, and **every major storage vendor has a CSI driver**. EBS, GCE PD, Azure Disk, Ceph, Portworx, Longhorn, OpenEBS, NFS, S3, MinIO, Rook, Trident (NetApp), VMware vSAN — all CSI.

The model:

- `PersistentVolumeClaim` (the user's request).
- `PersistentVolume` (the actual storage backend).
- `StorageClass` (the template; "give me EBS gp3, 1000 IOPS").
- CSI driver does dynamic provisioning, attachment, mount.

Access modes: `ReadWriteOnce` (one node), `ReadWriteMany` (many nodes), `ReadOnlyMany`.

For stateful workloads, `StatefulSet` gives you stable network identity, ordered rollouts, and one PVC per pod. Cassandra, Kafka, Postgres operators all build on this.

The storage gap is the largest single reason Swarm has lost ground. CSI was developed in the K8s ecosystem and never seriously came to Swarm.

---

## 6. Service Discovery and Load Balancing

### Compose

DNS within the project. No load balancing in the usual sense — when a service has multiple replicas (via `--scale`), the Docker resolver returns multiple A records and the client picks one (usually the first).

### Swarm

Each service has a VIP. Clients connect to `tasks.servicename` (DNS round-robin across all task IPs) or `servicename` (the VIP, IPVS-load-balanced). The routing mesh exposes services on every node's `published port`, balanced via IPVS to a healthy task on any node.

This is "load balancer included." Real but L4.

### Kubernetes

Services are abstractions over endpoints. ClusterIP for in-cluster traffic, NodePort/LoadBalancer for external. kube-proxy (iptables/IPVS) or eBPF (Cilium) implements the load balancing.

Beyond that, **service meshes** provide:

- Locality-aware load balancing (prefer same-AZ).
- Outlier detection (kick a misbehaving pod out of the pool).
- Weighted traffic shifting (10% to v2).
- Retries with backoff.
- Circuit breakers.
- mTLS.
- L7 routing.

If you need any of this, you're on Kubernetes. There's no Swarm equivalent.

---

## 7. Rolling Updates and Deploy Strategies

### Compose

`docker compose up` recreates containers whose config has changed. The order is parallel per-service. No coordination, no traffic awareness, no rollback. If you need rolling updates, you write a bash script wrapping `compose`.

### Swarm

```
docker service update --image myorg/api:1.6 api
```

The manager updates replicas one at a time (configurable parallelism), waits for the new ones to be healthy, then proceeds. Rollback on failure is built in (`docker service rollback`). Update parameters:

```yaml
deploy:
  update_config:
    parallelism: 1
    delay: 10s
    failure_action: rollback
    monitor: 30s
    max_failure_ratio: 0.1
    order: start-first         # vs stop-first
  rollback_config:
    parallelism: 0             # rollback all at once
    delay: 0s
    order: stop-first
```

For most needs, this is enough. Limited to "rolling": no blue-green, no canary by traffic percentage, no header-based routing during rollout.

### Kubernetes

Built-in `Deployment` strategy: `RollingUpdate` (default) with `maxSurge`/`maxUnavailable`. `Recreate` strategy for "kill all, then start fresh."

Beyond built-in:

- **Argo Rollouts:** canary with traffic splitting, blue-green, experiment phases, automated promotion based on metrics.
- **Flagger:** automated progressive delivery using Prometheus or Datadog as the gate.
- **Service mesh + ingress:** route N% by header, percentage, or session.

For mature deploy strategies (canary by metric, automated rollback on SLO violation), K8s is the only game in town.

---

## 8. Scaling: Manual, Reactive, Predictive

### Compose

`docker compose up --scale worker=5`. Manual. Local to one host. Done.

### Swarm

`docker service scale api=10`. Manual. Across the cluster. No autoscaling built in. You can wire external automation (a script + Prometheus) but it's bespoke.

### Kubernetes

- **HorizontalPodAutoscaler (HPA):** scale based on CPU/memory or custom metrics.
- **VerticalPodAutoscaler (VPA):** adjust pod requests/limits.
- **KEDA:** event-driven autoscaling — scale based on Kafka lag, SQS depth, Postgres rows, custom Prometheus queries. Scale-to-zero supported.
- **Cluster Autoscaler / Karpenter:** add/remove nodes based on pending pods.
- **Pod Disruption Budgets:** ensure availability during cluster operations.

The composability is the point: a Kafka consumer scales on lag (KEDA), and the cluster grows nodes to fit (Karpenter), and a PodDisruptionBudget prevents draining all replicas at once. Each piece is independent.

---

## 9. Secrets and Config

### Compose

Plain files on disk, mounted as tmpfs into containers. No encryption at rest, no rotation, no audit.

### Swarm

Built-in `docker secret create` and `docker config create`. Secrets are stored encrypted at rest in the Raft log, mounted as tmpfs files (`/run/secrets/<name>`). Rotation requires creating a new secret and updating the service (no in-place rotation).

This is a step up — the secret bytes are encrypted in the cluster state. Still no audit trail, no rotation policy, no integration with external secret managers (Vault).

### Kubernetes

`Secret` resources are stored in etcd. By default, they are base64-encoded but not encrypted. **Encryption at rest must be enabled explicitly** (KMS provider in the apiserver config). Once enabled, secrets are encrypted in etcd, mounted as files or env vars.

Beyond that:

- **External Secrets Operator:** pulls from Vault, AWS Secrets Manager, GCP Secret Manager, etc., into K8s Secrets, with rotation.
- **Sealed Secrets / SOPS / age:** encrypted secrets in git, decrypted in cluster.
- **CSI Secrets Store driver:** mount secrets directly from Vault/cloud providers without ever creating a K8s Secret.
- **Vault Agent Injector:** sidecar injects secrets into pod filesystem.

RBAC controls who can read which secrets. Audit logs record every access. Rotation is automatable.

For regulated environments, K8s is the only orchestrator with the controls. Swarm secrets are reasonable for small ops; Compose secrets are toys.

---

## 10. Multi-Tenancy and RBAC

### Compose

None. The Docker daemon trusts whoever can reach its socket. Multi-tenancy means "different hosts" or "rootless Docker per user."

### Swarm

Two roles: manager and worker. There's no per-service RBAC, no namespace concept. Multi-tenancy in Swarm is "separate Swarms per tenant."

### Kubernetes

Full RBAC: users, service accounts, roles, role bindings, cluster roles. Namespaces provide resource isolation. Network policies provide network isolation. ResourceQuotas and LimitRanges provide capacity isolation.

Multi-tenancy patterns:

- **Soft multi-tenancy:** namespaces + RBAC + network policy + resource quotas. Works if tenants are not adversarial.
- **Hard multi-tenancy:** virtual clusters (vcluster), separate clusters per tenant, sandboxed runtimes (Kata, gVisor). For untrusted workloads.

If you need to host multiple teams on one cluster, K8s is the only choice. Swarm-of-Swarms or Compose-of-Compose just multiplies the operational burden.

---

## 11. Observability and Operations

### Compose

`docker compose logs`, `docker compose ps`. That's the toolkit. Metrics, traces, structured logging: bring your own (Prometheus + Loki + Tempo running as Compose services pointed at each other).

### Swarm

`docker service logs`, `docker service ps`. The same minimal toolkit, extended to the cluster scope. Same BYO observability.

### Kubernetes

A real ecosystem:

- **Prometheus operator:** automatic service discovery, ServiceMonitors, AlertManager.
- **OpenTelemetry operator:** auto-injection of traces.
- **Loki + Promtail or Fluentd DaemonSets:** log aggregation.
- **Grafana operator:** dashboard management.
- **kube-state-metrics, metrics-server:** built-in cluster metrics.
- **kubectl events, kubectl top, kubectl describe:** standardized debugging.

Vendors (Datadog, New Relic, Honeycomb) integrate via DaemonSets and operators. Everyone in the cluster gets observability "for free" once the platform team installs the stack once.

---

## 12. Extensibility: CRDs, Plugins, Webhooks

### Compose

None. Compose is what Compose is.

### Swarm

Limited plugins for networking, volumes, secrets, logging. The plugin API is essentially deprecated — Docker has not invested in new plugin types for years.

### Kubernetes

This is K8s' superpower. The extension surfaces:

- **CRDs (Custom Resource Definitions):** invent your own resource type. The apiserver treats it like a first-class object. Combined with a controller, this is how operators are built.
- **Operators:** controllers that manage CRDs. The pattern that gave us Postgres-as-a-Pod (Zalando, CrunchyData), Kafka-as-a-Pod (Strimzi), Redis-as-a-Pod, hundreds more.
- **Admission webhooks:** intercept every API request to validate or mutate. Used by Kyverno, OPA Gatekeeper, sigstore-policy-controller.
- **API aggregation:** add entire new APIs to the apiserver (metrics, custom).
- **CNI plugins:** swap out the network layer.
- **CSI plugins:** swap out the storage layer.
- **CRI plugins:** swap out the container runtime.
- **Device plugins:** GPUs, FPGAs, RDMA, custom hardware.
- **Scheduler framework:** plug into the scheduling decision pipeline.

The result: anything you want, somebody has probably built. Anything you want and nobody has built, you can build.

Swarm has none of this surface area. Compose, of course, even less.

---

## 13. Cost and Operational Burden

### Compose

Near zero. A host. A docker install. A compose file. Backups are your problem.

### Swarm

Modest. Three managers + N workers. Docker engine maintains itself. Updates are a `docker swarm update` away. Backups are still your problem (Raft state, volumes).

### Kubernetes (self-hosted)

Substantial. Cluster lifecycle, etcd backups, control plane upgrades, CNI/CSI maintenance, version skew policies. A team of platform engineers is typical for non-trivial clusters.

### Kubernetes (managed: EKS, GKE, AKS)

Modest. Cloud provider manages the control plane. You manage workloads and node groups. Costs:

- EKS: ~$75/month control plane + node costs.
- GKE Standard: ~$75/month control plane (free with Autopilot, but Autopilot has per-pod pricing).
- AKS: free control plane (you pay nodes only).

Plus add-ons (Datadog, GitOps, secrets management) that are roughly comparable across orchestrators in absolute cost but proportionally larger if your cluster itself is small.

The crossover point — when K8s is cheaper than Compose+ad-hoc scripts — is usually around "we have 3+ engineers managing infrastructure" or "we have more than ~10 services." Below that, Compose wins on cost; above it, K8s wins on leverage.

---

## 14. Ecosystem and Hiring

In 2026:

- **Kubernetes** is the assumed competency for senior infra/devops/SRE roles. Most candidates have K8s on their resume. Most vendor tools integrate K8s first, others later.
- **Swarm** has a small but loyal user base. New tooling generally does not target it. CNCF's center of gravity is K8s.
- **Compose** is universal as a developer tool. Every developer has used it. Most know basics.

This matters for hiring (you can find K8s talent), for vendor support (your APM/security/cost-management tools work on K8s), and for long-term maintenance (K8s is going to be around in a decade; Swarm's future is less certain).

---

## 15. Failure Modes and Blast Radius

### Compose

Single point of failure: the host. If the host dies, everything dies. Recovery time depends on your ops:

- Host reboots: containers restart automatically (with `restart: unless-stopped`).
- Host disk full: containers crash, restart loop until disk is freed.
- Daemon crash: rare, but everything dies until daemon restarts.
- Compose file lost: rebuild from git.

Blast radius: one host. Failure detection: external monitoring on the host.

### Swarm

Manager failures: as long as a quorum (majority) of managers is alive, Swarm continues. Lose quorum → Swarm halts (no new tasks scheduled, but running tasks continue).

Worker failures: tasks are rescheduled to surviving workers. Some downtime per service while replicas come up elsewhere.

Network partitions: Raft handles them; isolated minority partition can't make changes.

Famous Swarm failure mode: **DNS resolution flakiness in overlay networks under high churn.** Not catastrophic, but pages on-call.

Blast radius: typically one service or one node. Failure detection: built-in (manager monitors tasks).

### Kubernetes

Control plane failures: etcd quorum loss halts the control plane (existing pods continue running). API server failure stops new operations. Scheduler failure stops new scheduling. Controllers failing stops reconciliation.

Node failures: pods are rescheduled. Service endpoints update. kube-proxy on other nodes adjusts routing.

Famous K8s failure modes:

- **etcd disk filled or slow:** the entire control plane degrades. Cluster-wide impact.
- **kube-apiserver overloaded by a misbehaving controller:** the API becomes slow, all controllers slow down.
- **Misconfigured admission webhook:** every API request fails, cluster effectively locked. (Solution: webhook timeouts and failure policies.)
- **CNI bug:** pod networking broken cluster-wide. Hard to debug.
- **Cascading evictions under disk pressure:** kubelet starts evicting pods, pods reschedule to other nodes which also have disk pressure, the cycle accelerates.

Blast radius can be very large because K8s components are coupled through the API server. Mitigations exist (PodPriority, PodDisruptionBudgets, separate node pools, multi-cluster) but they require engineering.

---

## 16. Migration Paths and Lock-In

The good news: container images are portable. The same `myorg/api:1.5.0` runs on Compose, Swarm, K8s, ECS, Fargate, Nomad, you-name-it.

The bad news: the orchestration manifests are not portable. Compose YAML → K8s manifests requires translation (Kompose helps; hand-editing inevitable). Swarm → K8s mostly requires re-authoring.

Lock-in dimensions:

- **Manifest format:** moderately locked per orchestrator.
- **Networking primitives:** locked (K8s Service ≠ Swarm Service).
- **Storage primitives:** locked (PVC ≠ Swarm volume).
- **CI/CD pipelines:** typically rewritten on migration (different deploy verbs, different image-pinning conventions).
- **Observability:** mostly portable; metrics and logs and traces don't care about orchestrator.

Practical migration paths:

- **Compose → Swarm:** Mostly painless. Same YAML format. Mostly works. (Migrating away from a system whose ecosystem is shrinking.)
- **Compose → Kubernetes:** Re-author manifests. Translate volumes to PVCs, secrets to Secrets, depends_on to readiness gates. Several weeks of work for a non-trivial stack.
- **Swarm → Kubernetes:** Similar to Compose → K8s. The conceptual model is closer (services, replicas, rolling updates), but the K8s configuration surface is much larger.
- **Kubernetes → Compose/Swarm:** Rare. Usually a "we adopted K8s prematurely and want to simplify" story. Doable but you lose features.

---

## 17. The Decision Tree

```
┌─ Are you in development / running tests / integration testing?
│   └─ YES → COMPOSE.
│
├─ Single host, no HA needed, simple deploy, you control the host?
│   └─ YES → COMPOSE in production. Add Watchtower for image updates,
│            Caddy/Traefik for TLS, restic for backups.
│            Reassess at "more than 2 hosts" or "more than 10 services."
│
├─ Multi-host, you want HA, but K8s feels like too much, and you don't
│  need autoscaling, custom controllers, or service mesh?
│   └─ Consider SWARM. But check: is the team large enough to absorb
│      the eventual K8s migration cost? If no, Swarm. If you're a
│      growing org expecting >50 engineers in 2 years, just start
│      with K8s.
│
├─ Multi-host, need autoscaling, want managed services for orchestration,
│  have or can hire infra expertise, expect to grow?
│   └─ KUBERNETES. Managed (EKS/GKE/AKS) unless you have a reason
│      to self-host.
│
├─ Highly regulated, need fine-grained RBAC, audit logs, multi-tenancy,
│  attestation, network policy?
│   └─ KUBERNETES. Nothing else has the controls.
│
├─ Need to run stateful workloads at scale with operator-managed
│  databases, message brokers, etc.?
│   └─ KUBERNETES. The operator ecosystem only lives here.
│
└─ Edge / IoT / lightweight environments?
    ├─ Single node: COMPOSE or single-node k3s.
    └─ Multiple nodes: k3s, MicroK8s, or k0s (lightweight K8s).
       Swarm fits here historically but is being eclipsed by k3s.
```

---

## 18. When to Use Each: Concrete Profiles

### Compose is right for:

- A startup's dev environments (`docker compose up`, you have a stack).
- CI integration tests (`docker compose up --abort-on-container-exit`).
- A side project deployed to a $5/month VPS.
- A small internal tool with 3 services on one host.
- A demo or workshop.

### Swarm is right for (rare in 2026):

- A small team that has already invested in Swarm and is operating successfully — staying put is sometimes better than migrating.
- An air-gapped or on-prem environment where K8s lifecycle is too much, but you need HA and basic multi-host orchestration.
- A team with deep Docker expertise and minimal capacity to learn K8s, deploying a few dozen services across a few nodes, no autoscaling needs.

For new projects in 2026, the default should be K8s (managed) unless the team is genuinely better served by Compose.

### Kubernetes is right for:

- Anything with serious production workloads.
- Anything that needs autoscaling, especially scale-to-zero.
- Anything that needs multi-tenancy.
- Anything with regulatory requirements.
- Anything with custom controllers or operators.
- ML platforms, data platforms, internal developer platforms.
- Multi-region / multi-cluster topologies.
- Teams growing past ~10 engineers managing more than a handful of services.

---

## 19. TL;DR

- **Compose:** developer tool with optional single-host deployment. Stop scrolling — for dev, this is the answer. For production, only if "one host" is enough.
- **Swarm:** lovely architecture, dying ecosystem. Don't start new projects here unless you have a specific reason. Migrate when you can.
- **Kubernetes:** the industry standard. Real complexity, real leverage, real ecosystem. Use a managed flavor (EKS/GKE/AKS) to amortize the operational burden.

The differences that drive the decision:

1. **State model.** Compose is imperative; Swarm and K8s are declarative with reconciliation. Past trivial deployments, reconciliation is mandatory.
2. **Extensibility.** Compose has none; Swarm has dwindling plugins; K8s has CRDs/operators/webhooks/the works. If you want to build internal platforms, this is where the line gets drawn hard.
3. **Storage.** CSI lives in K8s. Swarm's storage story is fragile. Stateful workloads → K8s.
4. **Ecosystem.** Hiring, vendor integrations, community tooling — all tilt to K8s. Swarm has loyal users but no momentum.
5. **Cost.** Compose is essentially free; K8s has a real ops cost, mostly mitigated by managed offerings.

**The right mental model:** Compose, Swarm, and K8s are not "the same product at different complexity levels." They are three different products with overlapping uses. The migration cost between them is high (re-authoring manifests, retraining the team, adjusting CI/CD). **Pick the right one for your 18-month-out state, not your today state.**

For most teams in 2026, that means: **Compose for dev and tests, Kubernetes (managed) for production.** Swarm has a narrow remaining niche, and it's getting narrower.
