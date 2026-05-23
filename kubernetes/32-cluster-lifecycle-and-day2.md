# Cluster Lifecycle and Day-2 Operations

How a Kubernetes cluster is born, kept alive, upgraded, rescued from disaster, and ultimately torn down. This chapter is about the operations that aren't reconciled by any controller in the cluster — because they happen *to* the cluster, not *inside* it. kubeadm bootstrap, the PKI tree, the version-skew policy, control-plane and worker upgrades, node drains with PDBs, etcd snapshot/restore, Velero, disaster recovery decision trees, CA rotation, and the dozens of pitfalls that turn a routine upgrade into a midnight page.

If chapters 04 (etcd) and 26 (multi-cluster) tell you what the steady-state cluster looks like, this chapter tells you what happens between the steady states: the violent, irreversible transitions where one wrong flag deletes your audit trail or wedges every apiserver until a human SSHes into a node.

---

## Table of Contents

1. [The Cluster Lifecycle, in One Picture](#1-the-cluster-lifecycle-in-one-picture)
2. [TL;DR](#2-tldr)
3. [Phase Zero: What "Bootstrapping a Cluster" Actually Means](#3-phase-zero-what-bootstrapping-a-cluster-actually-means)
4. [kubeadm init: The Eleven Phases](#4-kubeadm-init-the-eleven-phases)
5. [The PKI Tree Under `/etc/kubernetes/pki`](#5-the-pki-tree-under-etckubernetespki)
6. [Static Pod Manifests and the kubelet Bootstrap Dance](#6-static-pod-manifests-and-the-kubelet-bootstrap-dance)
7. [Joining Worker Nodes: Tokens, CA Hashes, CSR Auto-Approval](#7-joining-worker-nodes-tokens-ca-hashes-csr-auto-approval)
8. [The Version Skew Policy](#8-the-version-skew-policy)
9. [Control-Plane Upgrades](#9-control-plane-upgrades)
10. [Etcd's Implied Upgrade Order](#10-etcds-implied-upgrade-order)
11. [Worker Node Upgrades](#11-worker-node-upgrades)
12. [Surge vs In-Place Node Upgrades](#12-surge-vs-in-place-node-upgrades)
13. [Drain Mechanics: Eviction, DaemonSets, Static Pods](#13-drain-mechanics-eviction-daemonsets-static-pods)
14. [PodDisruptionBudgets: The Drain Throttle](#14-poddisruptionbudgets-the-drain-throttle)
15. [Priority, Preemption, and terminationGracePeriodSeconds](#15-priority-preemption-and-terminationgraceperiodseconds)
16. [etcd Backup: snapshot save](#16-etcd-backup-snapshot-save)
17. [etcd Restore: The Point-in-Time Rewind](#17-etcd-restore-the-point-in-time-rewind)
18. [etcd Defrag: Reclaiming the Hole](#18-etcd-defrag-reclaiming-the-hole)
19. [Backup Strategy: etcd Snapshot vs Velero](#19-backup-strategy-etcd-snapshot-vs-velero)
20. [Velero Architecture](#20-velero-architecture)
21. [Velero Backups, Schedules, and Restores](#21-velero-backups-schedules-and-restores)
22. [Restic / Kopia and CSI Snapshot Integration](#22-restic--kopia-and-csi-snapshot-integration)
23. [The Rebuild-From-GitOps Alternative](#23-the-rebuild-from-gitops-alternative)
24. [DR Scenarios and Procedures](#24-dr-scenarios-and-procedures)
25. [Multi-Region DR Patterns](#25-multi-region-dr-patterns)
26. [Cluster Decommissioning](#26-cluster-decommissioning)
27. [Certificate Renewal and CA Rotation](#27-certificate-renewal-and-ca-rotation)
28. [etcd CA: The Separate Trust Store](#28-etcd-ca-the-separate-trust-store)
29. [Service Account Signing Key Rotation](#29-service-account-signing-key-rotation)
30. [Cluster Autoscaler in the Upgrade Loop](#30-cluster-autoscaler-in-the-upgrade-loop)
31. [Tools Beyond kubeadm](#31-tools-beyond-kubeadm)
32. [Managed Kubernetes Upgrade UX](#32-managed-kubernetes-upgrade-ux)
33. [The "Skip a Minor" Problem](#33-the-skip-a-minor-problem)
34. [Audit, Observability, and Upgrade SLOs](#34-audit-observability-and-upgrade-slos)
35. [Pitfalls](#35-pitfalls)
36. [References and Source Paths](#36-references-and-source-paths)

---

## 1. The Cluster Lifecycle, in One Picture

The lifecycle of a cluster is not a single deploy and a perpetual run. It is a sequence of distinct *day-2* concerns, each with its own tooling, its own failure modes, and its own rollback strategy.

```
                ┌────────────────────────────────────────┐
                │   INSTALL                              │
                │   kubeadm init / Talos / CAPI / k3s    │
                │   Bootstrap CA, etcd, control plane,   │
                │   join workers, install CNI            │
                └──────────────────┬─────────────────────┘
                                   │
                                   ▼
                ┌────────────────────────────────────────┐
                │   OPERATE  (steady state)              │
                │   Apps reconcile, controllers run,     │
                │   nothing changes about the cluster    │
                └──┬───────────────┬─────────────────┬───┘
                   │               │                 │
                   ▼               ▼                 ▼
       ┌───────────────────┐ ┌─────────────┐ ┌──────────────────┐
       │ UPGRADE           │ │ SCALE       │ │ BACKUP           │
       │ apiserver minors, │ │ add/remove  │ │ etcd snapshots,  │
       │ etcd patches,     │ │ workers,    │ │ Velero schedules │
       │ kubelet packages, │ │ rotate AZs, │ │ cert backups,    │
       │ CA renewals       │ │ resize CP   │ │ kubeadm cfg dump │
       └───────────┬───────┘ └──────┬──────┘ └────────┬─────────┘
                   │                │                 │
                   └──────┬─────────┴────────┬────────┘
                          │                  │
                          ▼                  ▼
                ┌────────────────────────────────────────┐
                │   RESTORE  (rare, irreversible)        │
                │   etcd snapshot restore, Velero        │
                │   restore, rebuild-from-GitOps         │
                └──────────────────┬─────────────────────┘
                                   │
                                   ▼
                ┌────────────────────────────────────────┐
                │   DECOMMISSION                         │
                │   Drain workloads to new cluster,      │
                │   release cloud resources, archive     │
                │   PKI + audit logs, delete cluster     │
                └────────────────────────────────────────┘
```

Each phase has different *risk profiles*. Install is forgiving — you can throw away a half-built cluster and start again. Upgrade is unforgiving — every minute the apiserver is at a different version than the kubelets is a minute where the cluster is technically violating its own contract. Restore is the most unforgiving of all: a wrong flag in `etcdctl snapshot restore` can rewrite your cluster's identity and orphan the data you were trying to recover.

The rest of this chapter walks each phase, deep enough that you can SSH into a broken control-plane node at 03:00 and know which file to look at first.

---

## 2. TL;DR

- **kubeadm bootstrap** is eleven phases, in this order: preflight → certs → kubeconfigs → control-plane manifests → etcd manifest → upload-config → mark-control-plane → bootstrap-token → kubelet-finalize → addon (CoreDNS) → addon (kube-proxy). Each is independently re-runnable via `kubeadm init phase`.
- **The PKI** under `/etc/kubernetes/pki` is two CAs (cluster CA and etcd CA), plus the front-proxy CA, plus the service-account signing keypair. Lose `ca.key`, you can't issue new client certs and you can't bring up new control-plane nodes. Lose `sa.key`, every existing SA token becomes unverifiable.
- **Static pod manifests** in `/etc/kubernetes/manifests/` are *watched* by the kubelet, not the apiserver. That is how the control plane bootstraps itself: the kubelet starts the apiserver, the apiserver becomes the source of truth, but the manifests on disk remain the source of truth for the control-plane pods themselves.
- **Version skew**: apiserver is highest; kubelet/kube-proxy can lag by 2 minors (3 in 1.28+); controller-manager / scheduler / CCM are equal to apiserver; kubectl is ±1. Violating this is *quiet*: features that depend on newer API fields silently stop working.
- **Control-plane upgrade**: drain CP node → `kubeadm upgrade apply` on first CP, `kubeadm upgrade node` on rest → upgrade kubelet+kubectl packages → restart kubelet → uncordon. One node at a time. Etcd is upgraded with the kubeadm release; never independently versioned against apiserver if you can avoid it.
- **Worker upgrade**: `kubeadm upgrade node` → `apt/dnf install kubelet kubeadm kubectl` → `systemctl restart kubelet`. Wrap with `kubectl drain` / `kubectl uncordon`.
- **Surge** upgrades (new node alongside old) are the default in EKS/GKE/AKS and CAPI; **in-place** is what bare-metal and kubeadm typically do. Surge is faster and safer but requires +1 node capacity.
- **Drain** = cordon + eviction loop. Eviction respects PDBs (returns 429 if violated). DaemonSets need `--ignore-daemonsets`, emptyDir needs `--delete-emptydir-data`, static pods are skipped entirely.
- **etcd backup**: `etcdctl snapshot save /backup/etcd-<ts>.db`. Self-contained bbolt copy. Verify with `etcdctl snapshot status` (hash, revision, total keys). Schedule every 30 minutes for prod.
- **etcd restore**: stop *all* members and apiservers; `etcdctl snapshot restore --data-dir=/var/lib/etcd-new` on each member with matching `--name` and `--initial-cluster`; swap data dirs; start members one at a time; start apiservers. Restored cluster is at the snapshot's revision — *everything after is lost*.
- **Velero**: cluster object dumper + volume snapshotter, backups to S3/GCS/Azure Blob. Use it for selective restore (single namespace, single workload, cross-cluster migration). Pair it with etcd snapshots for full DR.
- **Rebuild-from-GitOps**: if all desired state is in Git and all data is in an external DB or object store, you don't need backups at all — re-provision a cluster, point ArgoCD at Git, done. Caveat: ConfigMap-stored runtime caches (cert-manager Orders, ArgoCD repo cache, …) are lost.
- **Most outages during upgrades** come from PDB-blocked drains, expired certs, missing `--ignore-daemonsets`, and `--service-account-key-file` configured with only the new key (rejecting old tokens). The pitfalls list at the end has 30+.

---

## 3. Phase Zero: What "Bootstrapping a Cluster" Actually Means

Before any `kubeadm` command runs, you have a Linux box (or three or five) with a kernel, a container runtime, a kubelet binary on disk, and *nothing else Kubernetes-shaped*. There is no apiserver. There is no etcd. The kubelet binary is running as a systemd unit, but it has no kubeconfig, no manifests to act on, and no certificate to authenticate to a server that doesn't exist.

The bootstrap problem is genuinely circular:

```
kubelet wants to talk to apiserver
   │
   │ but the apiserver is a Pod
   ▼
the apiserver Pod is started by kubelet
   │
   │ via /etc/kubernetes/manifests/kube-apiserver.yaml
   ▼
the apiserver needs etcd
   │
   │ etcd is also a static pod
   ▼
both pods need TLS material
   │
   │ which must be on disk before they start
   ▼
the kubelet needs a kubeconfig to report status back
   │
   │ which references a CA + a client cert + a server URL
   ▼
the server URL is localhost:6443 (the apiserver it's about to start)
```

kubeadm resolves this by *staging everything on local disk first*, then telling the kubelet "now go". By the time the kubelet starts watching `/etc/kubernetes/manifests/`, the CA exists, the apiserver manifest exists, the apiserver's serving cert exists, the kubelet's own client cert exists, the kubelet's kubeconfig points to localhost, and etcd's manifest is ready to run alongside.

The genius of kubeadm is recognizing this as a *phased* problem and exposing each phase. You can run `kubeadm init phase certs all` and stop, inspect, then run `kubeadm init phase control-plane apiserver`, and so on. In practice almost nobody does — but it makes recovery, hardening, and CAPI-style automation tractable.

```
DISK-FIRST BOOTSTRAP MODEL

  Time t=0   Nothing exists. kubelet running but idle.
  ───────────────────────────────────────────────────────
  preflight       OS checks, swap off, ports free, modules loaded
  certs           /etc/kubernetes/pki/ populated
  kubeconfigs     /etc/kubernetes/{admin,kubelet,controller-manager,scheduler}.conf
  control-plane   /etc/kubernetes/manifests/{kube-apiserver,kube-controller-manager,kube-scheduler}.yaml
  etcd            /etc/kubernetes/manifests/etcd.yaml
  ──────── kubelet starts seeing manifests; apiserver+etcd come up ────────
  upload-config   kubeadm-config + kubelet-config ConfigMaps created via apiserver
  mark-cp         taint node-role.kubernetes.io/control-plane:NoSchedule
  bootstrap-token bootstrap-token-<id> Secret in kube-system
  kubelet-finalize swap kubelet's bootstrap kubeconfig for the long-lived one
  addon CoreDNS   apply Deployment + Service + ConfigMap
  addon kube-proxy DaemonSet
  ───────────────────────────────────────────────────────
  Time t=~30s    `kubectl get nodes` returns 1 Ready control-plane node.
```

The line in the middle — between disk-first phases and apiserver-driven phases — is the moment the cluster crosses from being a pile of files into being a Kubernetes cluster. Everything after that line uses the apiserver as the source of truth, the way the rest of the system always does.

---

## 4. kubeadm init: The Eleven Phases

Run `kubeadm init` with `--v=5` and you'll see each phase logged. Let's walk them.

```
$ kubeadm init phase --help
Use this command to invoke single phase of the init workflow

Available Commands:
  preflight           Run pre-flight checks
  certs               Certificate generation
  kubeconfig          Generate all kubeconfig files necessary to establish the control plane
  control-plane       Generate all static Pod manifest files necessary to establish the control plane
  etcd                Generate static Pod manifest file for local etcd
  upload-config       Upload the kubeadm and kubelet configuration to a ConfigMap
  upload-certs        Upload certificates to kubeadm-certs
  mark-control-plane  Mark a node as a control-plane
  bootstrap-token     Generates bootstrap tokens used to join a node to a cluster
  kubelet-finalize    Updates settings relevant to the kubelet after TLS bootstrap
  addon               Install required addons for a Kubernetes cluster
```

**Phase 1 — preflight.** Checks: swap is disabled (`/proc/swaps` empty), required kernel modules loaded (`br_netfilter`, `ip_vs`), `net.bridge.bridge-nf-call-iptables=1`, `net.ipv4.ip_forward=1`, ports 6443/10250/10257/10259/2379/2380 free, the container runtime endpoint is reachable, hostname is RFC-1123 compliant, system clock is sane. Most of these are non-negotiable; the rare `--ignore-preflight-errors` should be a code smell.

**Phase 2 — certs.** Generates two CAs and ~10 leaf certs. Source: [`cmd/kubeadm/app/phases/certs/`](https://github.com/kubernetes/kubernetes/tree/master/cmd/kubeadm/app/phases/certs). The CA private keys end up at `/etc/kubernetes/pki/ca.key` and `/etc/kubernetes/pki/etcd/ca.key`. These are the keys-to-the-kingdom files; treat them like database master keys. (Section 5 enumerates the entire tree.)

**Phase 3 — kubeconfigs.** Generates `/etc/kubernetes/{admin,super-admin,kubelet,controller-manager,scheduler}.conf`. Each is a self-contained kubeconfig with embedded client cert + key. `admin.conf` is the cluster-admin credential you copy to `~/.kube/config`. (1.29+ split this into `admin.conf` with `kubeadm:cluster-admins` group and `super-admin.conf` with `system:masters` for break-glass.)

**Phase 4 — control-plane.** Writes three static-pod manifests to `/etc/kubernetes/manifests/`. The kubelet (already running as a systemd unit) watches this directory. The moment a manifest appears, the kubelet starts the corresponding pod *without going through any apiserver* — these are "mirror pods" that show up in `kubectl get pods -n kube-system` later, but they originate from the manifest on disk.

**Phase 5 — etcd.** Writes the etcd static-pod manifest. Same kubelet trigger. Etcd binds to `https://127.0.0.1:2379` and `https://<host-ip>:2380` for peer traffic.

At this point the kubelet has four manifests; it starts all four pods in parallel. The apiserver retries connecting to etcd until etcd's leader election completes. Within ~20 seconds the apiserver is healthy.

**Phase 6 — upload-config.** kubeadm uses the just-created `admin.conf` to talk to the apiserver and create two ConfigMaps in `kube-system`:

```
kubeadm-config       cluster-wide kubeadm settings
kubelet-config       kubelet configuration (every node copies this on join)
```

These are the *cluster's memory* of how it was created. Future `kubeadm upgrade` / `kubeadm join` read these to know what version to target, what pod-CIDR was chosen, etc.

**Phase 7 — mark-control-plane.** Patches the node object with the taint `node-role.kubernetes.io/control-plane:NoSchedule` and the label `node-role.kubernetes.io/control-plane=`. The taint is what keeps user workloads off control-plane nodes. (Pre-1.24 used `node-role.kubernetes.io/master`; the rename was a multi-release deprecation.)

**Phase 8 — bootstrap-token.** Creates a `Secret` in `kube-system` of type `bootstrap.kubernetes.io/token`. The secret carries a 6-character `token-id` and a 16-character `token-secret`. When you run `kubeadm token create` later you make more of these. They are used by joining nodes to authenticate the very first request — see section 7.

**Phase 9 — kubelet-finalize.** Until now the kubelet has been running with a *bootstrap kubeconfig* (`/etc/kubernetes/bootstrap-kubelet.conf`) that authenticates via the bootstrap token. Once the kubelet is up and the apiserver is up, the kubelet submits a CertificateSigningRequest for its own client cert; the CSR is auto-approved (by the `csrapproving` controller, see section 7); the kubelet gets a real client cert; kubeadm rewrites `/etc/kubernetes/kubelet.conf` to point at that cert; the kubelet restarts under the new identity. The bootstrap token has done its job and can be deleted.

**Phase 10 — addon CoreDNS.** Applies the CoreDNS Deployment, Service (clusterIP usually `10.96.0.10`), ServiceAccount, ClusterRole, ClusterRoleBinding, and ConfigMap. Source: [`cmd/kubeadm/app/phases/addons/dns/`](https://github.com/kubernetes/kubernetes/tree/master/cmd/kubeadm/app/phases/addons/dns).

**Phase 11 — addon kube-proxy.** Applies the kube-proxy DaemonSet. Mode defaults to `iptables`. (You can switch to `ipvs` or `nftables` here, or you can omit kube-proxy entirely if you're using Cilium's kube-proxy replacement — `kubeadm init --skip-phases=addon/kube-proxy`.)

When `kubeadm init` finishes, it prints:

```
Your Kubernetes control-plane has initialized successfully!

To start using your cluster, you need to run the following as a regular user:

  mkdir -p $HOME/.kube
  sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
  sudo chown $(id -u):$(id -g) $HOME/.kube/config

You should now deploy a Pod network to the cluster.
Run "kubectl apply -f [podnetwork].yaml" with one of the options listed at:
  https://kubernetes.io/docs/concepts/cluster-administration/addons/

Then you can join any number of worker nodes by running the following on each as root:

kubeadm join 10.0.0.1:6443 --token abcdef.0123456789abcdef \
  --discovery-token-ca-cert-hash sha256:a1b2c3...
```

You install a CNI (Calico/Cilium/Flannel/etc.) before joining workers — without it, Pods can't network and `coredns` stays Pending. After CNI, you join workers; see section 7.

---

## 5. The PKI Tree Under `/etc/kubernetes/pki`

Two CAs, one front-proxy CA, one service-account signing keypair, and leaves for every service that needs TLS. Memorize this tree.

```
/etc/kubernetes/pki/
├── ca.crt                     ← cluster CA (signs client/serving certs for apiserver)
├── ca.key                     ← cluster CA private key (KEEP OFFLINE IDEALLY)
├── apiserver.crt              ← apiserver serving cert; SANs include API LB DNS,
│                                 svc DNS (kubernetes.default.svc), 10.96.0.1, …
├── apiserver.key
├── apiserver-kubelet-client.crt   ← apiserver → kubelet (port 10250) client cert
├── apiserver-kubelet-client.key      group=system:masters → bypasses kubelet AuthZ
├── apiserver-etcd-client.crt  ← apiserver → etcd client cert
├── apiserver-etcd-client.key
├── front-proxy-ca.crt         ← separate CA: apiserver ↔ aggregated APIs auth
├── front-proxy-ca.key
├── front-proxy-client.crt     ← client cert used by apiserver to call aggregated APIs
├── front-proxy-client.key
├── sa.key                     ← private key that signs ServiceAccount JWTs
├── sa.pub                     ← public key the apiserver uses to verify them
└── etcd/
    ├── ca.crt                 ← etcd's OWN CA — distinct from cluster CA
    ├── ca.key
    ├── server.crt             ← etcd serving cert (port 2379)
    ├── server.key
    ├── peer.crt               ← etcd peer cert (port 2380, member-to-member)
    ├── peer.key
    └── healthcheck-client.crt ← used by liveness probe `etcdctl endpoint health`
```

**Why two CAs?** Because the etcd CA is a security boundary. The cluster CA can be rotated more aggressively because losing trust in it only requires re-issuing client certs to a handful of components. The etcd CA, by contrast, signs all peer-to-peer Raft traffic; rotating it without taking the cluster down is genuinely hard. Keeping them separate means an apiserver cert rotation can't accidentally break etcd's Raft quorum.

**Why a third front-proxy CA?** The aggregation layer (ch 24) needs the apiserver to act as a *client* to extension APIs (metrics-server, custom-metrics-apiserver, sample-apiserver). The extension API needs to know "I trust this client because they're authenticated against the apiserver". The front-proxy CA exists exactly so the extension API can verify the apiserver-as-client without having to trust the entire cluster CA. The apiserver passes the original user's identity in `X-Remote-User` / `X-Remote-Group` headers; the extension API trusts those headers only if the request came from a certificate signed by `front-proxy-ca.crt`.

**Why a separate sa.key?** Service-account JWTs need to be verifiable by the apiserver *without* an x509 round-trip. The apiserver signs them with `sa.key` and verifies with `sa.pub`. This pair is also what gets configured for **OIDC discovery** (`--service-account-issuer`) when you want IRSA-style workload identity. Lose `sa.key` and every existing SA token becomes a *signing oracle bomb*; lose `sa.pub` and they all become unverifiable.

**Inspecting a cert:**

```
$ openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -text | head -30
Certificate:
    Data:
        Version: 3 (0x2)
        Serial Number: 5a3c...
        Signature Algorithm: sha256WithRSAEncryption
        Issuer: CN = kubernetes
        Validity
            Not Before: May 23 12:00:00 2025 GMT
            Not After : May 23 12:00:00 2026 GMT      ← 1-year lifetime, see §27
        Subject: CN = kube-apiserver
        ...
        X509v3 Subject Alternative Name:
            DNS:kubernetes, DNS:kubernetes.default,
            DNS:kubernetes.default.svc,
            DNS:kubernetes.default.svc.cluster.local,
            DNS:cp-lb.example.com,
            IP Address:10.96.0.1,
            IP Address:10.0.0.1
```

The 1-year lifetime is the kubeadm default and the source of section 27's pain. `kubeadm certs check-expiration` lets you audit before they bite.

```
$ kubeadm certs check-expiration
[check-expiration] Reading configuration from the cluster...
CERTIFICATE                EXPIRES                  RESIDUAL TIME   EXTERNALLY MANAGED
admin.conf                 May 23, 2026 12:00 UTC   364d            no
apiserver                  May 23, 2026 12:00 UTC   364d            no
apiserver-etcd-client      May 23, 2026 12:00 UTC   364d            no
apiserver-kubelet-client   May 23, 2026 12:00 UTC   364d            no
controller-manager.conf    May 23, 2026 12:00 UTC   364d            no
etcd-healthcheck-client    May 23, 2026 12:00 UTC   364d            no
etcd-peer                  May 23, 2026 12:00 UTC   364d            no
etcd-server                May 23, 2026 12:00 UTC   364d            no
front-proxy-client         May 23, 2026 12:00 UTC   364d            no
scheduler.conf             May 23, 2026 12:00 UTC   364d            no

CERTIFICATE AUTHORITY   EXPIRES                  RESIDUAL TIME   EXTERNALLY MANAGED
ca                      May 23, 2035 12:00 UTC   3650d           no
etcd-ca                 May 23, 2035 12:00 UTC   3650d           no
front-proxy-ca          May 23, 2035 12:00 UTC   3650d           no
```

Note CAs default to 10 years, leaves to 1 year. Section 27 covers renewal.

---

## 6. Static Pod Manifests and the kubelet Bootstrap Dance

`/etc/kubernetes/manifests/` is the only directory that the kubelet treats as *master-of-its-own-truth*. Everything else — the Pods to run, the Services to apply iptables for, the volumes to mount — comes from the apiserver. But the apiserver itself can't be the source of truth for the apiserver. So:

```
                      ┌──────────────────────┐
                      │  systemd: kubelet    │
                      │  --pod-manifest-path=│
                      │  /etc/kubernetes/    │
                      │   manifests          │
                      └──────────┬───────────┘
                                 │ inotify
                                 ▼
              ┌────────────────────────────────────────┐
              │  /etc/kubernetes/manifests/            │
              │   kube-apiserver.yaml                  │
              │   kube-controller-manager.yaml         │
              │   kube-scheduler.yaml                  │
              │   etcd.yaml                            │
              └────────────────────────────────────────┘
                                 │
                                 ▼
              kubelet calls CRI to run these directly as
              Pods on this node, no apiserver involved.
              Once apiserver is up, kubelet creates
              "mirror pods" in the kube-system namespace
              so they're visible via `kubectl get pods -n
              kube-system`. The mirror pod is read-only;
              mutating it doesn't change the running pod.
```

A static pod manifest is a normal `kind: Pod` YAML, but with three significant quirks:

1. It cannot reference a `ServiceAccount` (no SA controller has run yet when these start).
2. It cannot use `kind: Deployment` or any higher controller — those need the apiserver. Static pods are *just pods*.
3. Editing the file is the only way to change them; `kubectl edit pod -n kube-system kube-apiserver-cp1` is a no-op (it edits the mirror, not the source).

`/etc/kubernetes/manifests/kube-apiserver.yaml` looks something like:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: kube-apiserver
  namespace: kube-system
  labels:
    component: kube-apiserver
    tier: control-plane
  annotations:
    kubeadm.kubernetes.io/kube-apiserver.advertise-address.endpoint: 10.0.0.1:6443
spec:
  hostNetwork: true
  priorityClassName: system-node-critical
  containers:
  - name: kube-apiserver
    image: registry.k8s.io/kube-apiserver:v1.30.0
    command:
    - kube-apiserver
    - --advertise-address=10.0.0.1
    - --allow-privileged=true
    - --authorization-mode=Node,RBAC
    - --client-ca-file=/etc/kubernetes/pki/ca.crt
    - --enable-admission-plugins=NodeRestriction
    - --enable-bootstrap-token-auth=true
    - --etcd-cafile=/etc/kubernetes/pki/etcd/ca.crt
    - --etcd-certfile=/etc/kubernetes/pki/apiserver-etcd-client.crt
    - --etcd-keyfile=/etc/kubernetes/pki/apiserver-etcd-client.key
    - --etcd-servers=https://127.0.0.1:2379
    - --kubelet-client-certificate=/etc/kubernetes/pki/apiserver-kubelet-client.crt
    - --kubelet-client-key=/etc/kubernetes/pki/apiserver-kubelet-client.key
    - --kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname
    - --proxy-client-cert-file=/etc/kubernetes/pki/front-proxy-client.crt
    - --proxy-client-key-file=/etc/kubernetes/pki/front-proxy-client.key
    - --requestheader-allowed-names=front-proxy-client
    - --requestheader-client-ca-file=/etc/kubernetes/pki/front-proxy-ca.crt
    - --requestheader-extra-headers-prefix=X-Remote-Extra-
    - --requestheader-group-headers=X-Remote-Group
    - --requestheader-username-headers=X-Remote-User
    - --secure-port=6443
    - --service-account-issuer=https://kubernetes.default.svc.cluster.local
    - --service-account-key-file=/etc/kubernetes/pki/sa.pub
    - --service-account-signing-key-file=/etc/kubernetes/pki/sa.key
    - --service-cluster-ip-range=10.96.0.0/12
    - --tls-cert-file=/etc/kubernetes/pki/apiserver.crt
    - --tls-private-key-file=/etc/kubernetes/pki/apiserver.key
    livenessProbe:
      httpGet:
        host: 10.0.0.1
        path: /livez
        port: 6443
        scheme: HTTPS
      initialDelaySeconds: 10
      periodSeconds: 10
      timeoutSeconds: 15
    volumeMounts:
    - mountPath: /etc/kubernetes/pki
      name: k8s-certs
      readOnly: true
    - mountPath: /etc/ssl/certs
      name: ca-certs
      readOnly: true
    - mountPath: /etc/kubernetes/pki/ca-certificates
      name: usr-share-ca-certificates
      readOnly: true
  volumes:
  - hostPath:
      path: /etc/kubernetes/pki
    name: k8s-certs
  - hostPath:
      path: /etc/ssl/certs
    name: ca-certs
```

Every flag here is the public surface of the apiserver. Section 27's CA rotation is largely about keeping these files (the `--tls-cert-file`, the `--client-ca-file`, the `--service-account-key-file`) consistent across all CP nodes during the swap.

**The atomic-write rule.** Since the kubelet watches `/etc/kubernetes/manifests/` with inotify, a partial write triggers a partial pod definition. `kubeadm` writes via `os.Rename` (atomic on POSIX) into the directory. If you edit by hand, you must do the same: write to a temp file in the same directory, then `mv`. A `vi` save-in-place is OK on most editors because vi uses `rename`, but plenty of editors don't (`gedit` for one). The pitfall is real.

---

## 7. Joining Worker Nodes: Tokens, CA Hashes, CSR Auto-Approval

Joining a node is the *reverse* bootstrap problem. The new node has nothing — no kubeconfig, no CA, no client cert. It needs to:

1. Trust the cluster CA — but how, if it doesn't have `ca.crt`?
2. Authenticate itself — but it has no credentials.

kubeadm solves both with a single magic string:

```
kubeadm join 10.0.0.1:6443 \
  --token abcdef.0123456789abcdef \
  --discovery-token-ca-cert-hash sha256:a1b2c3d4...
```

**Trusting the CA via a hash.** The node connects to `10.0.0.1:6443` and gets back a server cert. It can't validate the chain because it doesn't have `ca.crt` yet. So instead, kubeadm hits the unauthenticated discovery endpoint:

```
GET https://10.0.0.1:6443/api/v1/namespaces/kube-public/configmaps/cluster-info
```

`cluster-info` is a public ConfigMap created by kubeadm during `init`, containing the full kubeconfig of the cluster (CA cert inline) *plus a JWS signature* signed by the bootstrap token. The new node:

- Pulls the (unauthenticated) ConfigMap
- Computes the SHA-256 hash of the CA's SubjectPublicKeyInfo
- Compares to `--discovery-token-ca-cert-hash`
- If match → trust this CA from now on

This is a **trust-on-first-use** model bootstrapped by a hash you carried out-of-band. The hash is short, copy-pasteable, and binds the CA's public key (not the cert, so cert reissue doesn't invalidate it).

**Authenticating with a bootstrap token.** Once the node trusts the CA, it has to identify itself. It uses the same bootstrap token as a bearer credential:

```
Authorization: Bearer abcdef.0123456789abcdef
```

The apiserver's `enable-bootstrap-token-auth` flag teaches it to map `<token-id>.<token-secret>` → the username `system:bootstrap:<token-id>` and the group `system:bootstrappers:kubeadm:default-node-token`. RBAC has a built-in ClusterRoleBinding (`kubeadm:get-nodes` etc.) giving this group the right to create CSRs.

**The CSR.** The new node submits a `CertificateSigningRequest` for its own kubelet client cert (CN=`system:node:<hostname>`, Organization=`system:nodes`). It's then approved automatically by the `csrapproving` controller running in kube-controller-manager, which has built-in logic:

```
if signerName == "kubernetes.io/kube-apiserver-client-kubelet" &&
   user is in "system:bootstrappers:kubeadm:default-node-token" &&
   CN matches "system:node:<requesting-username's-implicit-hostname>" {
    APPROVE
}
```

Source: [`pkg/controller/certificates/approver/sarapprove.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/certificates/approver/sarapprove.go).

The controller-manager signs the CSR using `/etc/kubernetes/pki/ca.crt` + `ca.key`, returns the signed cert. The kubelet writes it to `/var/lib/kubelet/pki/kubelet-client-current.pem`, restarts under the new identity, and posts its first Node object.

**The kubelet serving cert.** Separately, the kubelet also submits a CSR for its *serving* cert (port 10250, used by the apiserver to reach back). That signer is `kubernetes.io/kubelet-serving`. By default it is **not** auto-approved — you must either auto-approve via the bootstrap group RBAC (less common in production) or run a side-car like [`kubelet-csr-approver`](https://github.com/postfinance/kubelet-csr-approver). EKS uses a similar mechanism via the AWS-managed signer.

```
WORKER JOIN TIMELINE

T+0     kubeadm join executes
T+0.1s  TCP connect to 10.0.0.1:6443
T+0.3s  GET /api/v1/namespaces/kube-public/configmaps/cluster-info (unauthenticated)
T+0.4s  Hash match: trust CA. Cache as /etc/kubernetes/pki/ca.crt.
T+0.5s  Bootstrap kubeconfig written to /etc/kubernetes/bootstrap-kubelet.conf
T+0.6s  systemctl start kubelet (or kubelet is already running and reloads)
T+1.0s  kubelet creates CSR for kubelet client cert
T+1.2s  csrapproving controller (on a CP node) approves the CSR
T+1.4s  kubeadm rewrites /etc/kubernetes/kubelet.conf with the new client cert
T+1.6s  kubelet restarts under the long-lived cert
T+1.8s  kubelet creates the Node object; controller-manager sets it Ready (after CNI)
T+~10s  CNI DaemonSet pod lands, network is up, node fully Ready
```

**Token lifecycle.** Default TTL is 24 hours. After expiry, `kubeadm join` will fail. To rotate:

```
$ kubeadm token create --ttl 1h --print-join-command
kubeadm join 10.0.0.1:6443 --token xyz123.abcdefghijklmnop \
  --discovery-token-ca-cert-hash sha256:a1b2c3...
```

Or `kubeadm token list` / `kubeadm token delete`. For automated node provisioners (CAPI, Karpenter), the recommended pattern is to use a *long-lived* token (TTL=0 is allowed but discouraged) scoped to a single bootstrappers group, or to provision via a cloud-init-issued cert directly (CAPI's `KubeadmConfig` does both).

---

## 8. The Version Skew Policy

The most-violated rule in Kubernetes operations.

```
                                +----------------------+
                                |  kube-apiserver      |
                                |  (highest version)   |
                                +----------+-----------+
                                           |
       +-----------------------------------+-----------------------------------+
       |                  |                |                 |                 |
       v                  v                v                 v                 v
  +----------+   +-------------------+   +----------+   +----------+    +----------+
  | kubelet  |   | kube-controller-  |   |   kube-  |   |   cloud- |    | kubectl  |
  | kube-    |   |   manager         |   | scheduler|   | controller|   |          |
  | proxy    |   |                   |   |          |   | -manager  |   |          |
  +----+-----+   +---------+---------+   +-----+----+   +-----+----+    +-----+----+
       |                   |                   |              |                |
       |                   |                   |              |                |
       v                   v                   v              v                v
  Up to 3 minors      EQUAL to            EQUAL to       EQUAL to       ±1 minor of
  BEHIND apiserver    apiserver            apiserver      apiserver      apiserver
  (since 1.28;        (cannot be          (same)         (same)         (works both
  2 before that)      newer or older                                     directions
                      by more than 0)                                    by 1 minor)
```

**Why each rule exists.**

- **apiserver is highest.** It defines the API. Older clients can talk to a newer server (forward-compat), newer clients can't reliably talk to an older server (the server doesn't know the new fields). This pins the upgrade direction: bump apiserver first, then everything else.
- **kubelet/kube-proxy can lag by N minors.** They're on every worker node; you can't realistically upgrade them in lockstep with the control plane in a 5000-node fleet. The window (2 minors pre-1.28, 3 minors in 1.28+) is the project's commitment to "we won't break the wire protocol between apiserver and kubelet for at least N releases."
- **scheduler / controller-manager / CCM are equal.** They share intimate types with the apiserver (the same `pkg/api/...` import paths). A scheduler reading a Pod struct from a newer apiserver can deserialize fields it doesn't know about (forward-compat), but features that depend on those fields silently regress. Worse, controller-manager *writes* objects; a controller from 1.27 writing into a 1.29 apiserver may strip newly added required fields. So they're pinned equal.
- **kubectl ±1.** kubectl uses the OpenAPI discovery doc to drive client-side validation. ±1 minor preserves discovery compatibility.

**The quiet failure mode.** None of these violations are *enforced*. The apiserver doesn't refuse a connection from a too-old kube-proxy. Things just stop working *quietly*:

- A scheduler at 1.26 talking to an apiserver at 1.29 ignores `MatchLabelKeys` topology-spread fields → topology spread silently fails for affected pods.
- A kubelet at 1.25 with an apiserver at 1.30 doesn't know how to interpret native sidecar restart policy → pods that depend on it crash-loop.
- A kube-controller-manager at 1.27 with an apiserver at 1.30 strips `volumeAttributesClassName` on update → PVC modifications break.

This is why the upgrade order is so rigid. Source: [Kubernetes version skew policy](https://kubernetes.io/releases/version-skew-policy/).

---

## 9. Control-Plane Upgrades

A kubeadm-managed control-plane upgrade is one node at a time. Suppose you have three CP nodes (`cp1`, `cp2`, `cp3`) and want to go from 1.29 to 1.30. The full procedure:

```
                  ┌──────────────────────────────────────┐
                  │  PRE-UPGRADE                         │
                  │  • etcd snapshot (etcdctl save)      │
                  │  • back up /etc/kubernetes/          │
                  │  • record kubectl get nodes -o wide  │
                  │  • record API priority levels        │
                  │  • record audit log retention        │
                  │  • SLO baseline (latency, error rate)│
                  └──────────────────────────────────────┘

                       ┌───────────────────┐
                       │ Upgrade cp1       │ ─── kubeadm upgrade plan
                       │ (the "first")     │ ─── kubeadm upgrade apply v1.30.0
                       └────────┬──────────┘ ─── restart kubelet
                                │
                                v
                       ┌───────────────────┐
                       │ Upgrade cp2       │ ─── kubeadm upgrade node
                       │ (a "follower")    │ ─── upgrade kubelet+kubectl
                       └────────┬──────────┘ ─── restart kubelet
                                │
                                v
                       ┌───────────────────┐
                       │ Upgrade cp3       │ ─── kubeadm upgrade node
                       │ (last follower)   │ ─── upgrade kubelet+kubectl
                       └────────┬──────────┘ ─── restart kubelet
                                │
                                v
                  ┌──────────────────────────────────────┐
                  │  Worker nodes (in waves)             │
                  │  drain → kubeadm upgrade node →      │
                  │  upgrade pkg → restart kubelet →     │
                  │  uncordon                             │
                  └──────────────────────────────────────┘
```

**Step-by-step on cp1:**

```
# On cp1
$ apt-mark unhold kubeadm
$ apt update && apt install -y kubeadm=1.30.0-1.1
$ apt-mark hold kubeadm

$ kubeadm version
kubeadm version: &version.Info{Major:"1", Minor:"30", GitVersion:"v1.30.0", ...}

# Tell us what would happen
$ kubeadm upgrade plan
[upgrade/config] Making sure the configuration is correct:
[upgrade/config] Reading configuration from the cluster...
[upgrade/config] FYI: You can look at this config file with 'kubectl -n kube-system get cm kubeadm-config -o yaml'
[preflight] Running pre-flight checks.
[upgrade] Running cluster health checks
[upgrade] Fetching available versions to upgrade to
[upgrade/versions] Cluster version: v1.29.3
[upgrade/versions] kubeadm version: v1.30.0
[upgrade/versions] Target version: v1.30.0
[upgrade/versions] Latest version in the v1.29 series: v1.29.4

Components that must be upgraded manually after you have upgraded the control plane with 'kubeadm upgrade apply':
COMPONENT   NODE      CURRENT   TARGET
kubelet     cp1       v1.29.3   v1.30.0
kubelet     cp2       v1.29.3   v1.30.0
kubelet     cp3       v1.29.3   v1.30.0
kubelet     w1        v1.29.3   v1.30.0
...

Upgrade to the latest stable version:
COMPONENT                 NODE     CURRENT    TARGET
kube-apiserver            cp1      v1.29.3    v1.30.0
kube-controller-manager   cp1      v1.29.3    v1.30.0
kube-scheduler            cp1      v1.29.3    v1.30.0
kube-proxy                          1.29.3    v1.30.0
CoreDNS                             v1.11.1   v1.11.1
etcd                      cp1      3.5.10-0   3.5.12-0

You can now apply the upgrade by executing the following command:
        kubeadm upgrade apply v1.30.0
```

The `kubeadm upgrade plan` step does live readiness checks: it reaches the apiserver, lists nodes, verifies etcd health, and refuses to proceed if the cluster is unhealthy. Re-run it after each step to confirm the plan.

```
# Drain cp1 (move workloads elsewhere — there shouldn't be many on a CP node thanks to the taint)
$ kubectl drain cp1 --ignore-daemonsets --delete-emptydir-data

# Apply the upgrade (on cp1 only)
$ sudo kubeadm upgrade apply v1.30.0
[upgrade/config] Making sure the configuration is correct:
...
[upgrade/version] You have chosen to change the cluster version to "v1.30.0"
[upgrade/versions] Cluster version: v1.29.3
[upgrade/versions] kubeadm version: v1.30.0
[upgrade/confirm] Are you sure you want to proceed? [y/N]: y
[upgrade/prepull] Pulling images required for setting up a Kubernetes cluster
[upgrade/apply] Upgrading your Static Pod-hosted control plane to version "v1.30.0" (timeout: 5m0s)...
[upgrade/staticpods] Writing new Static Pod manifests to "/etc/kubernetes/tmp/kubeadm-upgraded-manifests..."
[upgrade/staticpods] Renewing certificate embedded in "..."
[upgrade/staticpods] Moving new manifest to "/etc/kubernetes/manifests/kube-apiserver.yaml" and backing up old manifest to "/etc/kubernetes/tmp/kubeadm-backup-manifests-2026-05-23.../kube-apiserver.yaml"
[upgrade/staticpods] Waiting for the kubelet to restart the component
[upgrade/staticpods] This can take up to 5m0s
[apiclient] Found 1 Pods for label selector component=kube-apiserver
[upgrade/staticpods] Component "kube-apiserver" upgraded successfully!
[upgrade/staticpods] Moving new manifest to "/etc/kubernetes/manifests/kube-controller-manager.yaml" ...
...
[upgrade/postupgrade] Removing the old taint &Taint{...}
[upgrade/postupgrade] Applying label node-role.kubernetes.io/control-plane='' to Nodes with label node-role.kubernetes.io/master='' (deprecated)
[bootstrap-token] configured RBAC rules to allow Node Bootstrap tokens to get nodes
[addons] Applied essential addon: CoreDNS
[addons] Applied essential addon: kube-proxy

[upgrade/successful] SUCCESS! Your cluster was upgraded to "v1.30.0". Enjoy!
```

What `kubeadm upgrade apply` actually does:

1. Writes new static-pod manifests to `/etc/kubernetes/tmp/...`
2. **Atomically renames** them one at a time into `/etc/kubernetes/manifests/`
3. kubelet detects the change, kills the old container, starts the new image
4. kubeadm polls the apiserver until the new version reports Ready
5. Repeats for controller-manager, scheduler
6. Updates etcd's manifest separately (etcd version bumps follow the kubeadm-bundled etcd)
7. Upgrades the in-cluster `kubeadm-config` and `kubelet-config` ConfigMaps
8. Renews short-lived certs (CSR client cert, apiserver kubelet client cert) as a side effect

Now upgrade the kubelet on cp1:

```
$ apt-mark unhold kubelet kubectl
$ apt install -y kubelet=1.30.0-1.1 kubectl=1.30.0-1.1
$ apt-mark hold kubelet kubectl
$ systemctl daemon-reload
$ systemctl restart kubelet
$ kubectl uncordon cp1
```

Move to cp2 — note we use `kubeadm upgrade node`, not `apply`:

```
# On cp2
$ apt install -y kubeadm=1.30.0-1.1
$ kubectl drain cp2 --ignore-daemonsets --delete-emptydir-data
$ sudo kubeadm upgrade node
[upgrade] Reading configuration from the cluster...
[upgrade] FYI: You can look at this config file with 'kubectl -n kube-system get cm kubeadm-config -o yaml'
[preflight] Running pre-flight checks
[upgrade] Skipping prepull. Not a control plane node.
... (actually this IS a CP node, kubeadm detects it)
[upgrade] Backing up etcd data directory in /var/lib/etcd
[upgrade/staticpods] Writing new Static Pod manifests ...
...

$ apt install -y kubelet=1.30.0-1.1 kubectl=1.30.0-1.1
$ systemctl restart kubelet
$ kubectl uncordon cp2
```

`kubeadm upgrade node` is the idempotent "make this node match the cluster's recorded target version" command. It reads `kubeadm-config` from the apiserver (the cluster's memory, see §4) and writes the appropriate manifests. The first CP node has to use `apply` because it's the one *setting* that recorded target version.

Repeat for cp3. At this point you have a fully 1.30 control plane.

**HA gotcha.** During this whole procedure, the apiserver was running mixed versions briefly:

```
                 t=0          t=10m         t=20m         t=30m         t=40m
cp1   apiserver: 1.29.3       1.30.0        1.30.0        1.30.0        1.30.0
cp2   apiserver: 1.29.3       1.29.3        1.30.0        1.30.0        1.30.0
cp3   apiserver: 1.29.3       1.29.3        1.29.3        1.30.0        1.30.0
                              ────────────────────────────
                                 mixed-version control plane
                                 LB can hit any of them
```

This is *allowed* by the skew policy — different apiserver instances at different minor versions, as long as the kubelet sees only one (it does — it goes through the LB and gets pinned for the connection lifetime). But it's a window where a `kubectl describe` against cp1 sees fields cp2/cp3 don't recognize. Newer fields default to zero-values when round-tripping through the older binary. Keep the window short.

---

## 10. Etcd's Implied Upgrade Order

Etcd's version is pinned in the kubeadm release. `kubeadm upgrade apply v1.30.0` bumps etcd from (say) 3.5.10 to 3.5.12 *as part of the same operation*. This is deliberate: etcd's API guarantees mean a 3.5.12 client can always talk to a 3.5.10 server, but the reverse is not always true. Bumping etcd *during* the apiserver upgrade keeps the relationship simple.

```
┌────────────────────────────────────────────────────────────────┐
│   THE GOLDEN RULE                                             │
│   Never upgrade etcd at the same time as kube-apiserver       │
│   ACROSS DIFFERENT NODES.                                     │
│                                                                │
│   Per-node: kubeadm changes apiserver first, then etcd.       │
│   Across nodes: still rolling, never simultaneous.             │
└────────────────────────────────────────────────────────────────┘
```

When etcd needs a *major* version bump (3.4 → 3.5, planned 3.5 → 3.6), the kubeadm release notes call it out and require its own sub-procedure: roll etcd members one at a time, *separately* from the apiserver. The etcd Raft cluster will tolerate one member out at a time; it will not tolerate the entire cluster being restarted.

```
NEVER DO THIS:
  systemctl restart etcd  on all 3 CP nodes in parallel
  → all members re-elect at once
  → apiservers see no etcd backend for the full election window
  → cluster API blackout

ALWAYS DO THIS:
  ssh cp1 && mv /tmp/new-etcd.yaml /etc/kubernetes/manifests/etcd.yaml
  wait for etcdctl endpoint health from cp1 to return OK
  → repeat on cp2
  → repeat on cp3
  → at all times 2-of-3 members are up, quorum maintained
```

**Etcd skew tolerance.** Etcd v3.5 to v3.5: any minor patch difference is fine. Across major minors (3.4↔3.5): one member at a time, with the leader being the *last* to upgrade. Across major versions (3 → 4 in the future): unprecedented; will require a documented procedure.

The kubeadm-bundled etcd version is in `cmd/kubeadm/app/constants/constants.go`. Stay with what kubeadm ships unless you have a specific reason; out-of-tree etcd versions are unsupported and the first thing anyone will ask you to revert when troubleshooting.

---

## 11. Worker Node Upgrades

Workers are mechanically simpler than CP nodes — no `kubeadm upgrade apply`, just `kubeadm upgrade node`. But there are many more of them, and they carry your actual workloads, so the procedure has more around it.

```
For each worker (or wave of workers):
  1. kubectl drain $WORKER --ignore-daemonsets --delete-emptydir-data
  2. ssh $WORKER
  3. apt-mark unhold kubeadm kubelet kubectl
  4. apt install -y kubeadm=1.30.0-1.1
  5. sudo kubeadm upgrade node             ← refreshes kubelet-config, kube-proxy
  6. apt install -y kubelet=1.30.0-1.1 kubectl=1.30.0-1.1
  7. apt-mark hold kubeadm kubelet kubectl
  8. sudo systemctl daemon-reload
  9. sudo systemctl restart kubelet
  10. kubectl uncordon $WORKER
```

`kubeadm upgrade node` on a worker:
- Re-reads `kubelet-config` from the cluster (pulls the new config the CP upgrade uploaded)
- Updates `/var/lib/kubelet/config.yaml` from that ConfigMap
- Renews kubelet's client cert if close to expiry
- Updates kube-proxy DaemonSet manifest (well, the DaemonSet is updated centrally, but per-node logs are checked)

**Parallelism.** The minimum-safe parallelism is **one worker drained at a time**, plus your PDB tolerance. If you have a PDB allowing 10% disruption and 100 workers, you can drain 10 in parallel. If you have a PDB allowing only one pod down, you can't drain 10 workers if more than one runs a replica of that workload — drains will block on eviction.

In practice, fleet operators (CAPI, Karpenter, EKS, GKE) drain in waves — typically `floor(maxUnavailable * nodeCount)` at a time, with PDBs as the safety net. The drain *itself* is what enforces the safety.

**Per-pool ordering.** Heterogeneous fleets (GPU nodes, ARM nodes, special-purpose nodes) often have separate node pools / MachineDeployments. Upgrade one pool at a time, smallest first (smallest blast radius if the new version is broken). Don't mix.

---

## 12. Surge vs In-Place Node Upgrades

```
                ┌──────────────────────────────────────┐
                │   IN-PLACE                           │
                │   (kubeadm-default, bare-metal)      │
                └──────────────────────────────────────┘

  ┌─────────┐                ┌─────────┐                ┌─────────┐
  │ worker  │ ─── cordon ──> │ worker  │ ── drain ───>  │ worker  │
  │ v1.29   │                │ v1.29   │                │ v1.29   │
  │ 8 pods  │                │ 8 pods  │                │ 0 pods  │
  └─────────┘                └─────────┘                └─────────┘
                                                              │
                                                       upgrade kubelet
                                                       restart kubelet
                                                              │
                                                              v
                                                       ┌─────────┐
                                                       │ worker  │
                                                       │ v1.30   │
                                                       │ 0 pods  │
                                                       └─────────┘
                                                              │
                                                       uncordon
                                                       (pods reschedule)
                                                              │
                                                              v
                                                       ┌─────────┐
                                                       │ worker  │
                                                       │ v1.30   │
                                                       │ 8 pods  │
                                                       └─────────┘
  Total: zero new capacity, but pods evicted twice
  (once to drain old, once to reschedule).
  Risk: pods can't fit elsewhere → drain hangs.


                ┌──────────────────────────────────────┐
                │   SURGE (cloud-native default)        │
                │   (EKS, GKE managed, CAPI, Karpenter) │
                └──────────────────────────────────────┘

  ┌─────────┐                                          ┌─────────┐
  │worker-A │  PROVISION NEW NODE                      │worker-A │
  │ v1.29   │  ──────────────────────────────────>     │ v1.29   │
  │ 8 pods  │                                          │ 8 pods  │
  └─────────┘            ┌─────────┐                   └─────────┘
                         │worker-B │                   ┌─────────┐
                         │ v1.30   │                   │worker-B │
                         │ 0 pods  │                   │ v1.30   │
                         │ Ready   │                   │ 0 pods  │
                         └─────────┘                   └─────────┘
                                                              │
                                                       cordon worker-A
                                                       drain worker-A
                                                              │
                                                              v
                                                       ┌─────────┐ ┌─────────┐
                                                       │worker-A │ │worker-B │
                                                       │ v1.29   │ │ v1.30   │
                                                       │ 0 pods  │ │ 8 pods  │
                                                       └─────────┘ └─────────┘
                                                              │
                                                       terminate worker-A
                                                              │
                                                              v
                                                       ┌─────────┐
                                                       │worker-B │
                                                       │ v1.30   │
                                                       │ 8 pods  │
                                                       └─────────┘
  Total: +1 node capacity briefly (the "surge")
  Pods evicted once. New node validated before old goes away.
  Risk: cloud capacity might not be available; cost spike.
```

**When to choose which:**

| Factor | In-place | Surge |
|---|---|---|
| Capacity overhead | 0 | +1 node (or +maxSurge%) |
| Pod evictions per worker | 2 (drain + reschedule home) | 1 (drain to new node) |
| Drain blocking risk | High (no free space for pods) | Low (new node has free space) |
| Rollback if new version broken | Hard (pods already evicted) | Easy (terminate the new node) |
| Cost | Cheaper (no surge) | More expensive briefly |
| Bare-metal feasibility | Yes | Hard (no on-demand capacity) |
| Used by | kubeadm, bare-metal, k3s | EKS, GKE, AKS, CAPI, Karpenter |

Surge is what every managed K8s does and what every staff engineer should default to. The "+1 node" cost is trivially low for the few hours of an upgrade; the lower risk and easier rollback dominate.

**Karpenter's drift.** Karpenter has a feature called *drift* where if a Node's spec differs from its NodePool's desired spec (different AMI, different instance type, different K8s version), Karpenter replaces the node — provision new, cordon old, drain, terminate. This is surge upgrade implemented as a controller reconcile, not a procedure. It's the model future cluster lifecycle tools will converge on.

---

## 13. Drain Mechanics: Eviction, DaemonSets, Static Pods

```
$ kubectl drain w1 --ignore-daemonsets --delete-emptydir-data --timeout=10m
node/w1 cordoned
Warning: ignoring DaemonSet-managed Pods: kube-system/kube-proxy-w1, kube-system/cilium-xyz
evicting pod default/app-78c-abc12
evicting pod default/app-78c-def34
evicting pod default/redis-0
pod/app-78c-abc12 evicted
pod/app-78c-def34 evicted
pod/redis-0 evicted
node/w1 drained
```

What just happened, under the hood:

```
1. PATCH /api/v1/nodes/w1
   spec.unschedulable = true       (this is "cordon")
   The scheduler will no longer place new pods here.

2. LIST /api/v1/pods?fieldSelector=spec.nodeName=w1
   filter out:
     - DaemonSet pods       (unless --ignore-daemonsets=false → ERROR instead)
     - Mirror pods          (static pods can't be evicted; they are skipped)
     - Pods using emptyDir  (unless --delete-emptydir-data=true → ERROR otherwise)
     - Already-terminating pods

3. For each remaining pod:
     POST /api/v1/namespaces/<ns>/pods/<name>/eviction
       body: { kind: Eviction, deleteOptions: { gracePeriodSeconds: <pod.spec.tgps> } }

4. The Eviction API endpoint:
     - Checks active PodDisruptionBudgets matching this pod
     - If disrupting this pod would violate any PDB → 429 Too Many Requests
     - Otherwise → delete the pod (with grace period)

5. kubectl drain polls until the pod is fully gone (Phase=Succeeded/Failed/disappears).

6. On 429, kubectl drain retries with exponential backoff until --timeout.
```

**Why eviction is not delete.** Eviction is a *policy-aware* delete. It runs through the disruption-budget admission, which is the only consumer of PDBs. A direct `kubectl delete pod` does not consult PDBs — it just removes the pod. Drain uses eviction so that PDBs are honored.

**The DaemonSet skip.** DaemonSet pods are guaranteed-per-node. Evicting one means the DaemonSet controller will immediately try to recreate it on the same (cordoned!) node. The eviction would loop forever. `--ignore-daemonsets` makes drain skip them; *they stay running* during the drain. This is fine because DaemonSets are usually node-local agents (kube-proxy, log shippers, CSI node plugins) that should run for the lifetime of the node.

**Static pods.** Static pods have no eviction endpoint — they're managed by the kubelet, not the apiserver. `kubectl drain` skips them implicitly. This means a kubeadm control-plane node still has `kube-apiserver`, `etcd`, etc. running while "drained". That's correct: you don't want to evict the apiserver, you want to evict user workloads off the CP node before upgrading the kubelet.

**emptyDir.** A pod using `emptyDir` has data only on the local node. Evicting the pod loses that data. `kubectl drain` refuses by default; `--delete-emptydir-data` is explicit consent. Watch for: cache pods, ephemeral databases, scratch volumes. If those matter, *they shouldn't be on emptyDir* — they should be on a PVC.

**Failure modes:**

```
$ kubectl drain w1
node/w1 cordoned
error: unable to drain node "w1" due to error: cannot delete DaemonSet-managed Pods 
   (use --ignore-daemonsets to ignore): kube-system/cilium-xyz; cannot delete Pods 
   with local storage (use --delete-emptydir-data to override): default/cache-xyz, 
   continuing command...
```

Drain refuses to proceed on dangerous categories unless explicitly allowed. Use the flags. Don't use `--force` (which means "delete the pods without going through eviction at all" — bypasses PDBs).

**Drain hangs.** The #1 reason drain hangs is PDB conflict: minAvailable=replicas on a Deployment, so no pod can be evicted without violating the budget. The drain log will say:

```
evicting pod default/redis-0
error when evicting pods/"redis-0" -n "default" (will retry after 5s): 
   Cannot evict pod as it would violate the pod's disruption budget.
```

It will retry forever (until `--timeout`). Section 14 covers PDBs.

---

## 14. PodDisruptionBudgets: The Drain Throttle

A PDB tells the Eviction API "this many of these pods must always be Ready." It's the only mechanism that crosses the line between the application and the cluster operator.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: redis-pdb
  namespace: cache
spec:
  minAvailable: 2          # OR maxUnavailable: 1 (mutually exclusive)
  selector:
    matchLabels:
      app: redis
  unhealthyPodEvictionPolicy: IfHealthyBudget   # 1.27+, default
```

**The eviction check.** When a pod matches a PDB selector and someone POSTs an eviction request, the disruption controller evaluates:

```
currentHealthy = count(pods matching selector AND Phase=Running AND Ready)
desiredHealthy = (minAvailable explicit value) OR
                 (totalReplicas - maxUnavailable)

if currentHealthy - 1 < desiredHealthy:
    return 429 Too Many Requests
else:
    allow eviction
```

So `minAvailable: 2` means "no eviction may take Ready count below 2." If 3 are Ready, you can evict 1. If 2 are Ready, you can't evict any.

**maxUnavailable vs minAvailable.** Both are absolute or percentage. Same outcome, different framing:
- `minAvailable: 2` on a 3-replica Deployment → can disrupt 1 at a time
- `maxUnavailable: 1` on a 3-replica Deployment → can disrupt 1 at a time
- `minAvailable: 2` on a 5-replica Deployment → can disrupt 3 at a time
- `maxUnavailable: 50%` on a 4-replica Deployment → can disrupt 2 at a time

Percentages are rounded down for `maxUnavailable` and up for `minAvailable` (always favoring availability).

**The minAvailable=replicas footgun.** Setting `minAvailable` equal to the replica count makes drain *impossible*:

```yaml
# 3 replicas, minAvailable: 3 → drain is blocked forever
spec:
  minAvailable: 3
  selector:
    matchLabels: { app: redis }
```

Drain wants to evict one pod, but doing so would drop Ready below 3, violating PDB. 429 forever. This is a common copy-paste bug. Use `minAvailable: replicas-1` or `maxUnavailable: 1`.

**Single-replica apps.** A Deployment with `replicas: 1` and a PDB of `minAvailable: 1` is unmodifiable by drain. The "right" answer is either:
- Run 2 replicas (and accept the cost)
- Use `maxUnavailable: 1` (allows the single pod to be evicted, accepting temporary downtime)
- Have an `unhealthyPodEvictionPolicy: AlwaysAllow` so an unhealthy single replica doesn't block drain

**unhealthyPodEvictionPolicy.** Pre-1.27, an unhealthy pod (Pending, CrashLoopBackOff) could still be counted as "currentHealthy" for PDB math depending on policy interpretation, leading to drains stuck on broken pods. 1.27+ defaults `IfHealthyBudget`: an unhealthy pod can be evicted only if the budget is currently being met. `AlwaysAllow`: an unhealthy pod can always be evicted regardless of budget. Use `AlwaysAllow` for stateless apps where "broken pods should not block maintenance".

**Real-world template:**

```yaml
# A solid PDB for a Deployment of N replicas:
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: app-pdb
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: my-app
  unhealthyPodEvictionPolicy: AlwaysAllow
```

This says: "at most one of my replicas may be deliberately disrupted at a time, but if a replica is broken anyway, get it out of the way."

---

## 15. Priority, Preemption, and terminationGracePeriodSeconds

**PriorityClass and drain.** When a pod is evicted from a node and needs to land somewhere else, its PriorityClass determines whether it preempts existing pods on candidate nodes. High-priority workloads land faster during a surge upgrade because they can preempt lower-priority pods on the new node. This is the same scheduling preemption from ch 09; drain just amplifies it.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: business-critical
value: 100000
globalDefault: false
preemptionPolicy: PreemptLowerPriority
description: "Critical business workloads"
```

Pods with this PC will, during a drain, find a new home faster than pods with no PC. They may evict less-important pods elsewhere. Use sparingly — preemption ladders that go all the way up cause cascading evictions across the cluster.

**terminationGracePeriodSeconds (TGPS).** When a pod is evicted, the kubelet sends SIGTERM to PID 1 of each container, waits `TGPS` seconds (default 30), then sends SIGKILL.

```
                 t=0       t=T-N         t=T
  preStop hook   |─────────|             |        runs first if defined
  SIGTERM        |         |◄────────────|        sent at T-N (here T-30s) 
                 |         |             |        per terminationGracePeriodSeconds
  Pod removed    |         |             |
  from EPSlice   |─────────|             |        immediately on delete event
                                          
  SIGKILL                                |◄────  if container hasn't exited by T
```

For drain, this matters because **drain blocks on each pod terminating fully**. If a pod has `terminationGracePeriodSeconds: 600` and ignores SIGTERM, drain waits 10 minutes per pod. Multiply by N pods on the node and you have an evening.

Common cases that bite:
- JVM apps that catch SIGTERM and run a slow shutdown sequence
- Database pods that snapshot before shutting down
- Sidecars (Istio proxy) that wait for the main container to exit
- preStop hooks that sleep "for safety" (`sleep 30`) so iptables rules propagate

The fix is to keep `TGPS` realistic (60s is plenty for most apps) and make sure preStop hooks have actual content rather than blind sleeps. Native sidecars (1.28+, see ch 11) terminate after the main container; this resolves the Istio-proxy-hangs-drain case.

---

## 16. etcd Backup: snapshot save

Etcd is the only stateful component. Backing it up is non-negotiable. The mechanism is built into etcd itself:

```
$ ETCDCTL_API=3 etcdctl \
    --endpoints=https://127.0.0.1:2379 \
    --cacert=/etc/kubernetes/pki/etcd/ca.crt \
    --cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt \
    --key=/etc/kubernetes/pki/etcd/healthcheck-client.key \
    snapshot save /backup/etcd-$(date +%Y%m%d-%H%M%S).db
{"level":"info","ts":"2026-05-23T03:00:00.123Z","caller":"snapshot/v3_snapshot.go:65","msg":"created temporary db file","path":"/backup/etcd-20260523-030000.db.part"}
{"level":"info","ts":"2026-05-23T03:00:00.456Z","caller":"clientv3/maintenance.go:211","msg":"opened snapshot stream; downloading"}
{"level":"info","ts":"2026-05-23T03:00:05.789Z","caller":"snapshot/v3_snapshot.go:80","msg":"saved","path":"/backup/etcd-20260523-030000.db"}
Snapshot saved at /backup/etcd-20260523-030000.db
```

**What the snapshot is.** A self-contained [bbolt](https://github.com/etcd-io/bbolt) (BoltDB) file. It's the same file format etcd uses for its live data dir, just frozen at a point in time. You can scp it anywhere and inspect it offline.

**Verifying:**

```
$ etcdctl --write-out=table snapshot status /backup/etcd-20260523-030000.db
+----------+----------+------------+------------+
|   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE |
+----------+----------+------------+------------+
| 7a3b2c1d |  1842915 |     128944 |     142 MB |
+----------+----------+------------+------------+
```

Always check the result. A snapshot of 0 keys means you backed up an empty cluster (probably wrong endpoint). A snapshot whose size mismatches expectations means the connection dropped. **Always verify the hash after writing the snapshot to durable storage** — bit rot on the backup is what kills you when you need it most.

**Safety properties.**
- Snapshots are *consistent* across the entire keyspace at the snapshot's revision. There's no torn state.
- Snapshots are safe to take from any member, leader or follower. (The follower streams the snapshot from its local view of the Raft log.)
- Snapshots do not block live traffic (modulo IO bandwidth contention).
- Snapshots include compaction history up to the snapshot point; you cannot restore to an arbitrary historical revision *between* snapshots.

**Schedule recommendation.**

| Cluster scope | Frequency | Retention |
|---|---|---|
| Dev/test | 24h | 7 daily |
| Prod, low churn | 1h | 24 hourly + 14 daily |
| Prod, high churn | 15-30min | 24 hourly + 14 daily + 12 monthly |
| Compliance-critical | 5-15min | full history per regulator |

The 30-minute number is a sweet spot: small enough that you lose at most 30 minutes of writes on disaster, large enough that snapshot disk IO is amortized.

**Where to store.** The snapshot must survive the loss of the cluster it came from. Storing it on the same machine as etcd → loses both at once. Storing on a different node in the same cluster → almost as bad. The right answer:

```
etcd CP node ──snapshot──> local file (transient)
                              │
                              ▼
                       upload to off-cluster object storage
                       (S3, GCS, Azure Blob, on-prem MinIO)
                              │
                              ▼
                       cross-region replication (for region DR)
                              │
                              ▼
                       lifecycle policy (retention, glaciering)
```

**Automation example.** A cronjob on each CP node:

```yaml
# /etc/systemd/system/etcd-backup.timer
[Unit]
Description=etcd backup every 30 minutes
[Timer]
OnCalendar=*:0/30
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
# /usr/local/bin/etcd-backup.sh
#!/bin/bash
set -euo pipefail
TS=$(date -u +%Y%m%d-%H%M%S)
SNAP=/var/backups/etcd/etcd-${TS}.db
mkdir -p /var/backups/etcd
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save "${SNAP}"
etcdctl snapshot status "${SNAP}" --write-out=table
aws s3 cp "${SNAP}" "s3://my-cluster-backups/etcd/${TS}.db"
# retention
find /var/backups/etcd/ -name 'etcd-*.db' -mtime +1 -delete
```

A CronJob in the cluster itself is *not* the right answer — if the cluster is down, the CronJob can't run. Run from systemd on the node or from an external scheduler.

**Encrypted etcd.** If you're using [encryption at rest](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/) via `--encryption-provider-config`, the snapshot contains encrypted blobs. Restoring requires the same encryption key. *Back up the encryption config alongside the snapshot.* Losing it makes the snapshot worthless.

---

## 17. etcd Restore: The Point-in-Time Rewind

Restore is the hardest, scariest operation in Kubernetes. It is destructive, time-sensitive, and irreversible. Practice it before you need it.

```
┌──────────────────────────────────────────────────────────────────┐
│  RESTORE PROCEDURE (3-member etcd, restoring from a snapshot)    │
└──────────────────────────────────────────────────────────────────┘

PHASE 1: STOP THE WORLD
   ssh cp1: mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/
   ssh cp2: mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/
   ssh cp3: mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/
   (apiservers stop; kubectl no longer works against them)

   ssh cp1: mv /etc/kubernetes/manifests/etcd.yaml /tmp/
   ssh cp2: mv /etc/kubernetes/manifests/etcd.yaml /tmp/
   ssh cp3: mv /etc/kubernetes/manifests/etcd.yaml /tmp/
   (etcd members stop; the cluster has NO control plane now)

PHASE 2: RESTORE FROM SNAPSHOT (on EACH member)
   ssh cp1:
     etcdctl snapshot restore /backup/etcd-snap.db \
       --name=cp1 \
       --initial-cluster=cp1=https://10.0.0.1:2380,cp2=https://10.0.0.2:2380,cp3=https://10.0.0.3:2380 \
       --initial-cluster-token=etcd-cluster-restored-2026-05-23 \
       --initial-advertise-peer-urls=https://10.0.0.1:2380 \
       --data-dir=/var/lib/etcd-new

   ssh cp2:
     etcdctl snapshot restore /backup/etcd-snap.db \
       --name=cp2 \
       --initial-cluster=cp1=https://10.0.0.1:2380,cp2=https://10.0.0.2:2380,cp3=https://10.0.0.3:2380 \
       --initial-cluster-token=etcd-cluster-restored-2026-05-23 \
       --initial-advertise-peer-urls=https://10.0.0.2:2380 \
       --data-dir=/var/lib/etcd-new

   ssh cp3: (same pattern with --name=cp3)

   IMPORTANT: --initial-cluster-token must be IDENTICAL on all three.
              --name must MATCH the etcd member's name (cp1/cp2/cp3).
              --initial-cluster lists ALL members with their peer URLs.

PHASE 3: SWAP DATA DIRECTORIES
   On EACH node:
     mv /var/lib/etcd /var/lib/etcd.bak       (keep the old dir; do not delete)
     mv /var/lib/etcd-new /var/lib/etcd

PHASE 4: RESTART ETCD ONE AT A TIME
   ssh cp1: mv /tmp/etcd.yaml /etc/kubernetes/manifests/etcd.yaml
   wait for cp1 etcd to be alone-leader (no quorum yet, single member)
   ssh cp2: mv /tmp/etcd.yaml /etc/kubernetes/manifests/etcd.yaml
   wait for 2-member quorum
   ssh cp3: mv /tmp/etcd.yaml /etc/kubernetes/manifests/etcd.yaml
   wait for 3-member quorum
   etcdctl endpoint status --cluster

PHASE 5: RESTART APISERVERS
   ssh cp1: mv /tmp/kube-apiserver.yaml /etc/kubernetes/manifests/kube-apiserver.yaml
   wait for apiserver health
   ssh cp2: same
   ssh cp3: same

PHASE 6: VERIFY
   kubectl get nodes
   kubectl get pods --all-namespaces
   Reconcile any controllers that may have observed the gap (some operators).
```

**Critical details.**

1. **All members must restore from the SAME snapshot.** Restoring different snapshots on different members → each thinks it's a different cluster → no quorum. The `--initial-cluster-token` must match across all members so they recognize each other.

2. **The new data dir must be empty.** `--data-dir=/var/lib/etcd-new` creates a fresh tree. Restoring into a non-empty `--data-dir` errors out.

3. **Member identity changes.** After restore, the cluster has a new cluster ID (different from before). Any cached cluster ID elsewhere (kubelet metadata, audit logs) is stale. Apiservers re-learn this automatically; the cluster-info ConfigMap should also be updated (kubeadm does this).

4. **Don't restore while apiservers are running.** If an apiserver is talking to the old etcd at the moment you swap data dirs, it will see a discontinuity in resource versions and may misbehave. Apiservers MUST be down for the duration.

5. **Don't restore the leader-only.** All members get the same snapshot and join as a new cluster. Don't try to be clever by restoring only one and letting the others sync — they have stale data that disagrees with the restored one.

6. **You are losing everything since the snapshot.** Pods created after the snapshot revision are *gone*. Workloads that were running before the snapshot might be in mid-flight states the controllers will try to reconcile. Anticipate user complaints and surprise re-runs.

**Single-node etcd restore.** Simpler: stop etcd, stop apiserver, `etcdctl snapshot restore --data-dir=/var/lib/etcd-new` (no `--initial-cluster` needed), swap data dirs, restart. Used for kubeadm clusters with a single CP node.

**Restore commands inside the etcd pod (HA).** Since the etcd pod is what owns `/var/lib/etcd`, doing the restore on the host without the pod running works (the volume is just a hostPath). Some operators use a dedicated "restore pod" with the etcdctl binary instead of running etcdctl on the host directly — fine either way, the host volume is what matters.

**etcd Operator restores.** If you run etcd via the [etcd-operator](https://github.com/etcd-io/etcd-operator) or as a StatefulSet in another cluster, the procedure is similar but the operator handles the orchestration. Outside the apiserver-hosting etcd, this is much rarer.

---

## 18. etcd Defrag: Reclaiming the Hole

Etcd keeps every revision in MVCC until compaction. Compaction (auto or `etcdctl compact`) marks revisions as deletable. But the bbolt backing file does not shrink — deleted bytes leave free pages inside the file. Defrag rewrites the file without the holes.

```
BEFORE defrag (bbolt internal layout):
  ┌─────┬─────┬─────┬░░░░░┬─────┬░░░░░┬─────┬░░░░░┬─────┐
  │live │live │live │FREE │live │FREE │live │FREE │live │
  └─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘
  size on disk: 2 GiB (much of it free pages)

AFTER defrag:
  ┌─────┬─────┬─────┬─────┬─────┬─────┐
  │live │live │live │live │live │live │
  └─────┴─────┴─────┴─────┴─────┴─────┘
  size on disk: 600 MiB
```

**The procedure.**

```
# Per-member, NEVER simultaneously
$ etcdctl --endpoints=https://10.0.0.1:2379 defrag
Finished defragmenting etcd member[https://10.0.0.1:2379]

$ etcdctl --endpoints=https://10.0.0.2:2379 defrag
Finished defragmenting etcd member[https://10.0.0.2:2379]

$ etcdctl --endpoints=https://10.0.0.3:2379 defrag
```

**Why one at a time.** Defrag *takes the member offline* while it rewrites the file. With one member offline, you still have quorum (2 of 3). With two offline, you don't. Defrag-while-not-quorum is data-loss territory if the leader fails during the operation.

**Leader last.** Always defrag followers first. Leader defrag triggers a re-election (the leader is unavailable during defrag); doing it last minimizes the number of elections.

```
# Find the leader
$ etcdctl --endpoints=10.0.0.1:2379,10.0.0.2:2379,10.0.0.3:2379 endpoint status --cluster -w table
+-------------------+----------+---------+...
|     ENDPOINT      |    ID    | VERSION |...IS LEADER...
+-------------------+----------+---------+...
| 10.0.0.1:2379     | abc      |  3.5.12 |    false      
| 10.0.0.2:2379     | def      |  3.5.12 |    true       ← leader
| 10.0.0.3:2379     | ghi      |  3.5.12 |    false      
+-------------------+----------+---------+...

# Defrag followers first
$ etcdctl --endpoints=10.0.0.1:2379 defrag
$ etcdctl --endpoints=10.0.0.3:2379 defrag
# Leader last
$ etcdctl --endpoints=10.0.0.2:2379 defrag
```

**Auto defrag.** etcd 3.5+ supports `--experimental-bootstrap-defrag-threshold-megabytes` but it's experimental and limited. Production operators run a cron that defrags after a configured threshold (e.g., when `etcd_mvcc_db_total_size_in_bytes` is 50% larger than `etcd_mvcc_db_total_size_in_use_in_bytes`).

**When to defrag.** After every large object delete (e.g., dropping a namespace with thousands of objects), after every compaction storm, on a weekly cadence as preventative maintenance. Out-of-control etcd sizes are one of the top causes of apiserver latency spikes (see ch 35).

Source: [`etcdctl/ctlv3/command/defrag_command.go`](https://github.com/etcd-io/etcd/blob/main/etcdctl/ctlv3/command/defrag_command.go).

---

## 19. Backup Strategy: etcd Snapshot vs Velero

Two complementary tools, two different scopes.

```
┌─────────────────────────────────────────────────────────────────┐
│   etcd snapshot                                                 │
│   - WHAT: the entire cluster state at a revision                │
│   - GRANULARITY: all-or-nothing                                 │
│   - FORMAT: bbolt file                                          │
│   - RESTORE: full cluster, point-in-time, destructive           │
│   - BLOCK / FILE DATA: not included (only PV objects, not data) │
│   - BEST FOR: full cluster DR, "the cluster is gone, rebuild it"│
│   - TIME TO RESTORE: minutes for the snapshot, but workloads    │
│                       need to re-reconcile after                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│   Velero                                                        │
│   - WHAT: selected API objects + (optional) volume data          │
│   - GRANULARITY: by namespace, label, resource type              │
│   - FORMAT: JSON dumps in an object store + volume snapshots    │
│   - RESTORE: into the same or a different cluster, selectively   │
│   - BLOCK / FILE DATA: yes (via CSI snapshots or Restic/Kopia)   │
│   - BEST FOR: selective restore, cross-cluster migration,        │
│              app-level point-in-time                             │
│   - TIME TO RESTORE: depends on volume restore (minutes to hours)│
└─────────────────────────────────────────────────────────────────┘

       Use BOTH. Their failure modes don't overlap.
```

**Why not just etcd?** etcd snapshots back up *the API objects* but not PV data. The PV object says "I bind to EBS volume vol-abc123"; the snapshot has no idea what's inside vol-abc123. If you restore an etcd snapshot, the PV object reappears, but if vol-abc123 has been deleted (or modified after the snapshot), your application's data is gone or stale.

**Why not just Velero?** Velero backs up by *listing the apiserver*. If the apiserver is down, Velero can't make a backup. If the cluster state is corrupted (a controller writes invalid data en masse), Velero faithfully backs up the corruption. Velero also takes longer to restore an entire cluster from scratch — each object is a separate API create call.

**Combined strategy:**

| Disaster | Primary tool | Why |
|---|---|---|
| Single namespace deleted by accident | Velero | Granular restore |
| One application corrupted | Velero | Selective restore by label |
| Control plane node lost | None needed | HA control plane handles it |
| etcd quorum lost (all CPs gone) | etcd snapshot | Need full cluster bootstrap |
| Entire cluster lost (region failure) | etcd snapshot OR rebuild-from-GitOps + Velero data restore | Depends on RTO/RPO |
| Want to migrate to a new K8s version cluster | Velero | Cross-cluster restore |

The pragmatic default: etcd snapshots every 30 minutes for cluster DR, Velero scheduled backups for selective restore. Total cost is modest, recovery options are maximal.

---

## 20. Velero Architecture

Velero (formerly Heptio Ark) is a Kubernetes-native backup/restore platform. Source: [vmware-tanzu/velero](https://github.com/vmware-tanzu/velero).

```
                  ┌─────────────────────────────────────────────┐
                  │   IN-CLUSTER                                │
                  │                                              │
                  │   ┌──────────────────────────────────┐      │
                  │   │  Velero server (Deployment)       │      │
                  │   │  velero ns                        │      │
                  │   │  - backup controller              │      │
                  │   │  - restore controller             │      │
                  │   │  - schedule controller            │      │
                  │   │  - resticrepo controller          │      │
                  │   │  - reads from kube-apiserver       │      │
                  │   │  - writes JSON to BSL              │      │
                  │   │  - triggers volume snapshots       │      │
                  │   └──────────────────────────────────┘      │
                  │                                              │
                  │   ┌──────────────────────────────────┐      │
                  │   │  node-agent (DaemonSet, optional)│      │
                  │   │  - per-node Restic/Kopia worker  │      │
                  │   │  - reads pod volume contents     │      │
                  │   │  - streams to object store       │      │
                  │   └──────────────────────────────────┘      │
                  └──────────────────┬───────────────────────────┘
                                     │
                                     ▼
            ┌────────────────────────────────────────────────┐
            │   OBJECT STORE (BSL — Backup Storage Location) │
            │   s3://my-velero-bucket/                       │
            │     backups/                                   │
            │       my-backup-20260523/                      │
            │         velero-backup.json (manifest)          │
            │         resource-list.json.gz                  │
            │         <namespace>/<resource>/<name>.json.gz  │
            │     restores/                                  │
            │     schedules/                                 │
            └────────────────────────────────────────────────┘

            ┌────────────────────────────────────────────────┐
            │   VOLUME SNAPSHOT LOCATION (VSL, optional)      │
            │   - Cloud-native snapshots (EBS, GP3, GCE PD)   │
            │   - OR CSI VolumeSnapshot via the snapshot CRD  │
            │   - OR file-level via node-agent + restic       │
            └────────────────────────────────────────────────┘
```

**Components.**

- **Backup controller**: watches `Backup` objects; when one is created, walks the apiserver according to its selectors, dumps each object as JSON to the BSL, and creates VolumeSnapshots (or triggers Restic/Kopia) for PVCs.
- **Restore controller**: watches `Restore` objects; reads the backup from BSL, creates objects in the apiserver (in topological order: namespaces first, then SAs, then ConfigMaps/Secrets, then workloads), waits for PVs to be re-provisioned.
- **Schedule controller**: watches `Schedule` objects; cron-like; creates `Backup` objects on schedule.
- **node-agent DaemonSet** (replacing the old "restic" DaemonSet): per-node file-level backup agent.

**The BackupStorageLocation (BSL):**

```yaml
apiVersion: velero.io/v1
kind: BackupStorageLocation
metadata:
  name: default
  namespace: velero
spec:
  provider: aws
  objectStorage:
    bucket: my-cluster-velero-backups
    prefix: cluster-prod
  config:
    region: us-east-1
    s3ForcePathStyle: "false"
  credential:
    name: cloud-credentials
    key: cloud
  default: true
```

The BSL must exist in object storage *outside* the cluster (S3, GCS, Azure Blob, MinIO running in a different cluster, etc.). The single most common Velero misconfiguration is the BSL bucket living inside the same cluster's resources (e.g., a CephFS volume in the same K8s cluster). Lose the cluster → lose the BSL.

**The VolumeSnapshotLocation (VSL):**

```yaml
apiVersion: velero.io/v1
kind: VolumeSnapshotLocation
metadata:
  name: default
  namespace: velero
spec:
  provider: aws
  config:
    region: us-east-1
```

For AWS EBS, the VSL tells Velero "use AWS-native EBS snapshots stored in this region." For CSI, no VSL is needed — Velero uses the [VolumeSnapshot](https://kubernetes.io/docs/concepts/storage/volume-snapshots/) CRD and lets the CSI driver handle storage details.

---

## 21. Velero Backups, Schedules, and Restores

**A backup:**

```yaml
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: nightly-2026-05-23
  namespace: velero
spec:
  includedNamespaces:
  - prod-app
  - prod-cache
  excludedNamespaces:
  - kube-system
  - velero
  includedResources:
  - "*"
  excludedResources:
  - "events"
  - "events.events.k8s.io"
  - "leases.coordination.k8s.io"   # noisy
  labelSelector:
    matchLabels:
      backup: "yes"
  snapshotVolumes: true
  defaultVolumesToFsBackup: false   # use CSI/cloud snapshots, not Restic
  storageLocation: default
  volumeSnapshotLocations:
  - default
  ttl: 720h  # 30-day retention
  hooks:
    resources:
    - name: postgres-pre-backup-quiesce
      includedNamespaces: [prod-app]
      labelSelector:
        matchLabels:
          app: postgres
      pre:
      - exec:
          container: postgres
          command:
          - /bin/sh
          - -c
          - "psql -c 'SELECT pg_start_backup(''velero'');'"
          timeout: 60s
          onError: Fail
      post:
      - exec:
          container: postgres
          command:
          - /bin/sh
          - -c
          - "psql -c 'SELECT pg_stop_backup();'"
```

The `hooks` block runs commands inside pods *before and after* the volume snapshot. This is how you get application-consistent backups (Postgres `pg_start_backup` quiesces writes; the volume snapshot is then crash-consistent + LSN-consistent).

**A schedule:**

```yaml
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: nightly
  namespace: velero
spec:
  schedule: "0 2 * * *"   # 02:00 UTC every day
  template:
    includedNamespaces: ["*"]
    excludedNamespaces: ["kube-system", "velero"]
    ttl: 720h
    storageLocation: default
    volumeSnapshotLocations: [default]
    defaultVolumesToFsBackup: false
```

Velero creates Backup objects named `nightly-20260523020000` each night. The TTL governs deletion; expired backups are garbage-collected by the schedule controller.

**Schedule conflicts.** Two schedules with overlapping selectors and overlapping cron windows produce parallel backups of the same data. The second backup waits on the first for shared volume snapshots, sometimes failing if the cloud provider rate-limits snapshot APIs. Stagger your schedules.

**A restore:**

```yaml
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: restore-prod-app-from-202605230200
  namespace: velero
spec:
  backupName: nightly-20260523020000
  includedNamespaces:
  - prod-app
  namespaceMapping:
    prod-app: prod-app-restored    # restore to a different namespace
  restorePVs: true
  includedResources:
  - "*"
  excludedResources:
  - "nodes"
  - "events"
  labelSelector:
    matchLabels:
      app: critical
  hooks:
    resources:
    - name: cache-warm
      includedResources: [pods]
      labelSelector:
        matchLabels:
          app: redis
      postHooks:
      - exec:
          container: redis
          command: ["/scripts/warm-cache.sh"]
          waitTimeout: 5m
          execTimeout: 30s
```

**Velero CLI:**

```
$ velero backup create my-backup --include-namespaces prod-app --wait
Backup request "my-backup" submitted successfully.
Waiting for backup to complete. You may safely press ctrl-c to stop the wait.
......
Backup completed with status: Completed. You may check for more information using the commands `velero backup describe my-backup` and `velero backup logs my-backup`.

$ velero backup describe my-backup --details
Name:         my-backup
Namespace:    velero
Phase:        Completed
...
Items backed up: 247
Volume snapshots: 3 of 3 snapshots completed successfully (specify --details for more information)

$ velero restore create --from-backup my-backup
```

**Velero on terraform / Helm / GitOps**: install via the upstream Helm chart [vmware-tanzu/velero](https://github.com/vmware-tanzu/helm-charts/tree/main/charts/velero). The chart parameterizes BSL provider, VSL provider, schedule CRs.

---

## 22. Restic / Kopia and CSI Snapshot Integration

Velero has two distinct mechanisms for backing up volume *contents* (as opposed to just the PVC/PV objects).

**1. CSI snapshots (preferred for cloud-native storage).** Available since Velero 1.4, GA in 1.10+. Velero creates a `VolumeSnapshot` resource referencing the PVC; the CSI driver (e.g., AWS EBS CSI, GCP PD CSI, Ceph CSI) handles the actual block-level snapshot via its `CreateSnapshot` RPC.

```
Velero ─► creates VolumeSnapshot CR ─► VolumeSnapshot controller
                                              │
                                              ▼
                                       CSI snapshotter sidecar
                                              │
                                              ▼
                                       CSI driver CreateSnapshot()
                                              │
                                              ▼
                                       cloud snapshot (EBS snap, etc.)
```

CSI snapshots are *fast* (cloud snapshot APIs are typically O(seconds) regardless of volume size, because they're metadata-level copy-on-write). Restoring is similarly fast. They're cloud-storage-specific; you can't move an EBS snapshot to GCS or restore an EBS snapshot in GKE.

**2. Restic / Kopia node-agent (file-level, cloud-agnostic).** Used when you don't have a CSI snapshotter, or when you want backups that survive cloud account / region migration. Velero deploys a DaemonSet (`node-agent`) that runs Restic or Kopia. For each PVC backed up, the node-agent on the pod's node reads the volume's contents file-by-file and uploads to BSL.

```yaml
# Enable in the Velero install
spec:
  defaultVolumesToFsBackup: true   # opt-in default
  # OR per-pod annotation:
  # metadata.annotations.backup.velero.io/backup-volumes: data,logs
```

```
node-agent pod ──reads /var/lib/kubelet/pods/<podUID>/volumes/.../data──>
                ──encrypts + dedupes──> object store BSL
```

Pros: cloud-agnostic, files are deduped (incremental backups are tiny), can restore to any cluster.

Cons: slow on large volumes (TB takes hours), reads the live filesystem (no consistency guarantee unless you pause writes via hooks), CPU/IO load on the node. Not viable for large databases.

**Kopia vs Restic.** Restic is the older default. Kopia (1.10+) is the new default with much better parallelism and dedupe — pick Kopia for new installs.

**Storage requirements.** Restic/Kopia repositories grow over time. Set lifecycle policies on the BSL bucket to expire old data. Restic in particular has a known memory pressure issue on large repos (the in-memory index can exceed 4 GiB).

**The CSI snapshot driver gotcha.** Many CSI drivers shipped with K8s versions don't include the snapshot sidecar by default — you have to install the [external-snapshotter](https://github.com/kubernetes-csi/external-snapshotter) controller separately. If you try to create a Velero backup with CSI snapshots enabled and no snapshotter installed, Velero waits forever and eventually fails. Validate `kubectl get crd | grep snapshot` returns:

```
volumesnapshotclasses.snapshot.storage.k8s.io
volumesnapshotcontents.snapshot.storage.k8s.io
volumesnapshots.snapshot.storage.k8s.io
```

If those CRDs are missing, install `external-snapshotter` first.

---

## 23. The Rebuild-From-GitOps Alternative

If your discipline is high enough, you might not need backups at all.

```
                     ┌──────────────────────┐
                     │  Git repository       │
                     │  - K8s manifests      │
                     │  - Helm values        │
                     │  - Kustomize overlays │
                     │  - Sealed secrets     │
                     └──────────┬───────────┘
                                │ pull
                                ▼
                     ┌──────────────────────┐
                     │  ArgoCD / Flux        │
                     │  reconciles a fresh   │
                     │  cluster's state from │
                     │  Git                  │
                     └──────────┬───────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │  External state       │
                     │  - RDS / Aurora       │
                     │  - S3 buckets         │
                     │  - object store DBs   │
                     │  - external Vault     │
                     │  - DNS, IAM           │
                     └──────────────────────┘

   To recover: provision a new cluster (CAPI, Terraform), install ArgoCD,
   point at the Git repo, let it reconcile. No Velero needed for app state.
```

**When this works.**
- All workloads are stateless or reference external data (RDS, S3).
- All configuration is in Git (no `kubectl edit` ever).
- Secrets are managed externally (Sealed Secrets pulling from Vault, External Secrets Operator).
- DNS is managed externally and can point to the new cluster's LBs.
- You have practiced this end-to-end (annual DR drill).

**When this breaks.**
- ConfigMaps store runtime caches. `cert-manager` Orders, `argocd` repo cache, `velero` resticrepo state — none are in Git. Lose them, the controllers have to re-do the work (rate-limited by external APIs). cert-manager Orders losing ACME challenges → Let's Encrypt rate limits hit you for weeks.
- Stateful workloads inside the cluster. A Cassandra cluster running on PVCs has no rebuild-from-Git path.
- Cluster-issued certs (private CA via cert-manager) lose their root CA → all client trust breaks until manually replaced.
- Application data that lives only in cluster state. Webhook delivery queues, CRD-stored business state, etc.

**The hybrid.** Rebuild-from-GitOps for *control* and Velero+etcd-snapshot for *data*. This is the production default for serious shops.

---

## 24. DR Scenarios and Procedures

A decision tree for "the cluster is on fire":

```
                          ┌─────────────────────────────────┐
                          │   What's actually broken?       │
                          └────────────────┬────────────────┘
                                           │
        ┌──────────────────────────────────┼──────────────────────────────────┐
        │                                  │                                  │
        ▼                                  ▼                                  ▼
┌────────────────┐               ┌────────────────────┐               ┌──────────────┐
│ A worker node  │               │ A control-plane    │               │ Whole region │
│ is down        │               │ node is down       │               │ is down      │
└───────┬────────┘               └─────────┬──────────┘               └──────┬───────┘
        │                                  │                                  │
        ▼                                  ▼                                  ▼
┌────────────────┐               ┌─────────────────────────────┐    ┌─────────────────┐
│ Wait. The Node │               │ Is etcd quorum still healthy?│   │ Failover to    │
│ controller     │               └─────────────┬───────────────┘    │ secondary       │
│ marks it       │             ┌───────────────┴───────────┐        │ cluster         │
│ NotReady;      │             │                           │        │ (ch 26)         │
│ ReplicaSets    │             ▼                           ▼        │ DNS cutover     │
│ recreate pods. │       ┌──────────────┐         ┌──────────────────┐│ Drain pending │
│ Provision a    │       │ YES. Just    │         │ NO. Quorum lost. ││ writes.        │
│ replacement.   │       │ replace the  │         │ Must restore from││                │
└────────────────┘       │ failed CP.   │         │ snapshot.        │└────────────────┘
                         │              │         │                  │
                         │ kubeadm reset│         │ Procedure §17    │
                         │ on dead one; │         │                  │
                         │ kubeadm join │         │ Worst case:      │
                         │ as CP on new │         │ rebuild from     │
                         │ machine; etcd│         │ GitOps + Velero  │
                         │ adds member  │         │                  │
                         └──────────────┘         └──────────────────┘
```

**Scenario A: lost a worker node.**
- Node controller marks Node NotReady after `--node-monitor-grace-period` (default 40s).
- Pods on that node are marked unhealthy after `--default-not-ready-toleration-seconds` (5m).
- ReplicaSet/StatefulSet controllers create replacements on other nodes.
- No human intervention needed (modulo provisioning a replacement node).
- **Risk**: stuck pods if PV is RWO and the failed node holds the volume attachment. The volumeattachment requires `--max-pod-grace-period-after-node-down` handling; or you do `kubectl delete node` to force volume detach. K8s 1.28+ has `nonGracefulNodeShutdown` taint behavior to automate this.

**Scenario B: lost one control-plane node (still have quorum).**
- Etcd has 3 members, one is gone. 2-of-3 quorum is fine. Apiserver continues serving from the remaining CP nodes.
- Replace the failed node:
  ```
  # On a surviving CP:
  $ kubectl drain cp-dead --ignore-daemonsets --force --delete-emptydir-data
  $ kubectl delete node cp-dead
  $ etcdctl member remove <member-id-of-cp-dead>
  $ etcdctl member add cp-replacement --peer-urls=https://<new-ip>:2380
  # On the new CP node:
  $ kubeadm join <api-server>:6443 --token <token> --discovery-token-ca-cert-hash sha256:... --control-plane --certificate-key <key>
  ```
- The `--certificate-key` is obtained from `kubeadm init phase upload-certs --upload-certs` (uploaded as an encrypted Secret in `kube-system`, retrieves the certs needed for the new CP).

**Scenario C: lost majority of control plane (etcd quorum lost).**
- 2 of 3 etcd members are gone. Quorum is mathematically lost; etcd refuses writes. Apiserver is read-only (sort of) until quorum returns.
- If you can recover one of the failed members → do that first. Restart its etcd pod, let it re-sync.
- If you can't → declare disaster. Restore from snapshot per §17.
- *Force new cluster* (`etcd --force-new-cluster`) is an option that lets a single surviving member declare itself a new 1-member cluster, then add new peers. Useful when the disk is fine but the peers are gone. Documented in the [etcd recovery docs](https://etcd.io/docs/v3.5/op-guide/recovery/).

**Scenario D: lost the entire cluster.**
- Cluster's storage is intact: restore from etcd snapshot in a new cluster bootstrap. Provision 3 fresh CP nodes, run kubeadm with a *static pod manifest for etcd that uses the restored data dir*, bring up apiserver.
- Cluster's storage is gone: rebuild-from-GitOps. Provision new cluster, install ArgoCD, point at Git. Then Velero-restore application data from BSL (which lives off-cluster).

**Scenario E: lost the region.**
- Failover to standby cluster. Section 25.

**RTO/RPO targets.**

| Scenario | RTO (recovery time) | RPO (data loss) |
|---|---|---|
| Worker node | <5 min, automatic | 0 |
| One CP node | <30 min, manual replace | 0 |
| CP quorum | 1-2 hr, manual restore | up to snapshot age (30 min) |
| Entire cluster | 2-6 hr | up to snapshot age |
| Entire region | 5-30 min if standby is warm | depends on data replication |

---

## 25. Multi-Region DR Patterns

For workloads where region failure must not be visible:

```
       ┌────────────────────────────┐         ┌────────────────────────────┐
       │  REGION A (primary)        │         │  REGION B (warm standby)   │
       │                            │         │                            │
       │  Cluster A                 │         │  Cluster B                 │
       │  - all workloads running   │  cross  │  - same manifests applied │
       │  - serving traffic         │  region │  - workloads scaled to 0  │
       │                            │ replicat│    OR running at low      │
       │                            │  ion    │    replicas               │
       │                            │         │                            │
       │  RDS primary               │ ──────► │  RDS read replica          │
       │  S3 with replication       │ ──────► │  S3 destination bucket     │
       │  Velero schedule           │ ─backup►│  Velero BSL accessible     │
       │  etcd snapshot             │ ─copy──►│  S3 cross-region storage   │
       └────────────────────────────┘         └────────────────────────────┘
                       │                                    ▲
                       │                                    │
                       └────────── DNS ────────────────────┘
                                  GeoDNS / Route53 health
                                  check failover to B
```

**Components.**
- **Two clusters**, one per region. Provisioned identically (Terraform / CAPI / IaC). Same K8s versions, same CNI, same Velero install.
- **Application data replicated**: managed database with cross-region replicas, S3 with replication rules, object stores with their own replication.
- **GitOps applies to both**: ArgoCD or Flux deploys the same manifests to both clusters. The standby cluster has the workloads but at `replicas: 0` or scaled down.
- **DNS layer**: Route53 with health checks, or Cloudflare, or an external GLB. When primary fails health checks, DNS flips to standby.
- **Velero cross-region backup**: backups are written to a multi-region or replicated bucket. Either cluster can restore from either bucket.

**Failover procedure (when region A fails):**

```
1. Confirm region A is genuinely down (not a transient network partition).
   - Multiple health checks failing
   - Operator confirmation
   
2. Promote RDS standby to primary in region B.

3. Scale up workloads in cluster B:
   kubectl --context=cluster-B scale deployments -n prod --replicas=10 --all
   (or trigger via ArgoCD by setting the values for cluster B)

4. Wait for B's pods to be Ready and serve traffic.

5. Flip DNS to region B endpoints.

6. Monitor for elevated error rates during the cutover.

7. Once region A returns, decide: failback or run B as new primary?
```

**Async vs sync replication.** If primary writes synchronously to standby, RPO is 0 but write latency is region-to-region (tens of ms). Most production setups use async; RPO is whatever the replication lag is (typically seconds). Choose based on the business cost of losing the last few seconds of writes.

**The single hardest part: state convergence after failover.** Workloads that wrote to A *after* the last replicated checkpoint, but *before* the failure, are lost when B takes over. Apps must be idempotent or accept that some writes are lost.

---

## 26. Cluster Decommissioning

Done well, decommissioning is also a maintenance task. Done badly, it leaves orphaned cloud resources billed forever.

```
1. ANNOUNCE.
   Communicate the timeline to all tenants. Stop accepting new workloads.

2. MIGRATE WORKLOADS.
   - Point GitOps at a new target cluster.
   - For each workload, validate it runs in new cluster.
   - DNS cut over per service.
   - Drain progressively (cordon → wait → uncordon if there are issues).

3. BACKUP EVERYTHING.
   - Final etcd snapshot.
   - Final Velero backup, archived long-term (off the cluster).
   - Audit logs archived.
   - PKI archived (regulatory needs may require keeping certs).

4. CONFIRM ZERO STATEFUL WORKLOADS.
   kubectl get pvc --all-namespaces
   kubectl get statefulsets --all-namespaces
   kubectl get pv
   All clean? Proceed.

5. DRAIN AND DELETE NODES.
   for node in $(kubectl get nodes -o name); do
     kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --force
   done
   for node in $(kubectl get nodes -o name); do
     kubectl delete "$node"
   done

6. DELETE THE CLUSTER VIA THE PROVISIONER.
   - CAPI: kubectl delete cluster <name>  (handles cloud teardown automatically)
   - kubeadm + cloud: terraform destroy, or `eksctl delete cluster`, etc.
   - bare-metal: power off, reclaim hardware.

7. SWEEP FOR ORPHANS.
   The single most-missed step. Check the cloud account for:
   - Load Balancers (Service type=LoadBalancer leaves cloud LBs)
   - EBS volumes (PVCs leave volumes if reclaimPolicy=Retain or PVC deletion races)
   - Snapshots (Velero VSL or manual EBS snapshots)
   - Security groups / firewall rules associated with the cluster
   - ENIs (ENI per pod in some CNI setups, e.g., AWS VPC CNI)
   - Route 53 records, DNS records in any zone the cluster managed
   - IAM roles for service accounts (IRSA)
   - S3 buckets named after the cluster

8. CONFIRM BILLING.
   24-48 hours after decommission, check the cloud bill.
   A non-zero "EBS" or "ELB" line item means orphans remain.
```

**The orphaned-LB problem.** Service type=LoadBalancer creates a cloud LB via CCM. If you delete the cluster without first deleting the Service object, the CCM never gets a chance to delete the cloud LB. It runs forever until you find it in the cloud console.

The fix: delete Services *before* deleting the cluster, or rely on CCM finalizers (which require the cluster to be running). CAPI handles this correctly via its cluster deletion finalizer chain.

**The orphaned-EBS problem.** PVs with `reclaimPolicy: Retain` are kept after PVC deletion. If you delete the cluster, the PVs disappear from the apiserver but the underlying cloud volumes remain. Worse, snapshot lifecycle policies don't apply to orphan snapshots from before deletion.

The fix: change reclaimPolicy to Delete before decommissioning, or manually list and delete cloud volumes after.

---

## 27. Certificate Renewal and CA Rotation

kubeadm certs expire after 1 year by default. If you forget, the cluster doesn't *die* — but new components can't authenticate, controllers can't talk, and you discover the problem at the worst possible moment.

**Check expiration:**

```
$ kubeadm certs check-expiration
CERTIFICATE                EXPIRES                  RESIDUAL TIME   ...
admin.conf                 Aug 15, 2026 10:30 UTC   84d             
apiserver                  Aug 15, 2026 10:30 UTC   84d
apiserver-etcd-client      Aug 15, 2026 10:30 UTC   84d
apiserver-kubelet-client   Aug 15, 2026 10:30 UTC   84d
controller-manager.conf    Aug 15, 2026 10:30 UTC   84d
etcd-healthcheck-client    Aug 15, 2026 10:30 UTC   84d
etcd-peer                  Aug 15, 2026 10:30 UTC   84d
etcd-server                Aug 15, 2026 10:30 UTC   84d
front-proxy-client         Aug 15, 2026 10:30 UTC   84d
scheduler.conf             Aug 15, 2026 10:30 UTC   84d
```

**Renew everything on a node:**

```
# On each CP node:
$ sudo kubeadm certs renew all
certificate embedded in the kubeconfig file for the admin to use and for kubeadm itself renewed
certificate for serving the Kubernetes API renewed
certificate the apiserver uses to access etcd renewed
certificate for the API server to connect to kubelet renewed
certificate embedded in the kubeconfig file for the controller manager to use renewed
certificate for liveness probes to healthcheck etcd renewed
certificate for etcd nodes to communicate with each other renewed
certificate for serving etcd renewed
certificate for the front proxy client renewed
certificate embedded in the kubeconfig file for the scheduler manager to use renewed

Done renewing certificates. You must restart the kube-apiserver, kube-controller-manager,
kube-scheduler and etcd, so that they can use the new certificates.
```

The new certs are written to `/etc/kubernetes/pki/` (overwriting old ones) and to the embedded kubeconfigs. To make the static pods pick them up:

```
# Touch the manifest files so kubelet restarts the pods (this re-reads the cert from disk)
$ sudo touch /etc/kubernetes/manifests/kube-apiserver.yaml
$ sudo touch /etc/kubernetes/manifests/kube-controller-manager.yaml
$ sudo touch /etc/kubernetes/manifests/kube-scheduler.yaml
$ sudo touch /etc/kubernetes/manifests/etcd.yaml
# kubelet sees the mtime change, restarts the pods, the new pods load new certs
```

**The kubelet's own cert.** kubelet's client cert (in `/var/lib/kubelet/pki/`) and serving cert auto-rotate via `RotateKubeletClientCertificate` (default) and `RotateKubeletServerCertificate` (opt-in but on in kubeadm) feature gates. They renew themselves via CSR before expiry. Verify with:

```
$ openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates
notBefore=May 23 12:00:00 2026 GMT
notAfter=May 23 12:00:00 2027 GMT
```

**CA rotation.** The CA itself defaults to 10-year lifetime. Rotating the CA mid-cluster-life is a separate, more complex procedure:

1. Generate a new CA cert+key.
2. Concatenate old + new CA certs into a *trust bundle*: apiserver's `--client-ca-file` now contains BOTH CAs.
3. Reissue all leaf certs signed by the new CA.
4. Restart apiservers with the new bundle (they trust clients signed by either CA).
5. Re-issue kubeconfigs (admin, controller-manager, scheduler) with new client certs.
6. Wait for the trust window (existing TLS sessions complete).
7. Remove the old CA from the trust bundle.
8. Restart apiservers (they now trust only the new CA).

This is enormous work; treat the kubeadm CA's 10-year default as a soft real deadline.

**`kubeadm certs renew --use-api`.** Renews certs by submitting CSRs to the apiserver (which signs them with its CA). Useful when the CA key is offline (HSM-backed) and you don't want kubeadm to access it locally. Setup the `csrsigning` controller appropriately.

---

## 28. etcd CA: The Separate Trust Store

`/etc/kubernetes/pki/etcd/ca.crt` is **not** the same as `/etc/kubernetes/pki/ca.crt`. This catches every operator at least once.

```
                  ┌──────────────────────────┐
                  │   /etc/kubernetes/pki/   │
                  │     ca.crt               │  ← "cluster CA" — signs:
                  │     ca.key               │     - apiserver serving
                  │     apiserver.crt        │     - apiserver-kubelet-client
                  │     ...                  │     - admin.conf cert
                  │                          │     - controller-manager.conf cert
                  │     etcd/                │
                  │       ca.crt             │  ← "etcd CA" — DIFFERENT — signs:
                  │       ca.key             │     - etcd peer/server/client certs
                  │       server.crt         │     - apiserver-etcd-client
                  │       peer.crt           │
                  │       healthcheck-client │
                  │     front-proxy-ca.crt   │  ← "aggregation CA" — DIFFERENT
                  └──────────────────────────┘
```

**Implications.**
- `kubeadm certs renew all` renews both. But `kubeadm certs renew apiserver` only renews the cluster-CA-signed apiserver cert, NOT `apiserver-etcd-client.crt`.
- If you rotate the cluster CA, etcd's CA is unaffected — apiserver-etcd-client still works because it's signed by the etcd CA, which didn't change.
- Conversely, rotating the etcd CA *requires* re-issuing apiserver-etcd-client (a client cert signed by the etcd CA, used by apiserver to authenticate to etcd as a TLS client).

**Etcd peer rotation.** Etcd members authenticate to each other via certs signed by the etcd CA. Rotating the etcd CA mid-cluster requires the trust-bundle dance from §27, applied to etcd's `--peer-trusted-ca-file`. Hard to get right. The kubeadm guidance is to plan for the etcd CA's 10-year window and renew leaves only.

**Custom etcd**. If you run etcd as a StatefulSet (rather than kubeadm static pods), the etcd CA is whatever you configured for that operator. The principle stands: separate CA, separate rotation, separate trust window.

---

## 29. Service Account Signing Key Rotation

ServiceAccount tokens are JWTs signed by `--service-account-signing-key-file`. Verifying tokens uses `--service-account-key-file` (which accepts a **comma-separated list of public keys**). This asymmetry is the rotation mechanism.

```
Apiserver flags:
  --service-account-signing-key-file=/etc/kubernetes/pki/sa.key
        ↑
        a SINGLE private key. The apiserver uses this to sign NEW tokens.
        
  --service-account-key-file=/etc/kubernetes/pki/sa.pub,/etc/kubernetes/pki/sa.pub.old
        ↑
        a LIST of public keys. The apiserver accepts tokens signed by ANY of these.
        
Rotation procedure:
  1. Generate new keypair: sa.new (private) + sa.new.pub (public).
  2. Add sa.new.pub to --service-account-key-file (alongside the old one).
     Apiserver now accepts tokens signed by EITHER old or new.
  3. Restart apiservers.
  4. Switch --service-account-signing-key-file to sa.new.
     New tokens are now signed with sa.new.
  5. Restart apiservers.
  6. WAIT for all existing tokens to expire or be re-projected. For projected
     tokens this is the projection expiration (1h default). For legacy long-lived
     tokens, this is FOREVER unless you delete and recreate them.
  7. Once you're confident no token signed by the old key is in circulation,
     remove the old key from --service-account-key-file.
  8. Restart apiservers. Old tokens are now rejected.
```

**The classic mistake.** Setting `--service-account-key-file` to ONLY the new key on the first restart. Now:
- Old projected tokens (still valid for their lifetime) are rejected: 401 errors throughout the cluster.
- Pods that depend on those tokens (mounted via `automountServiceAccountToken`) start failing health checks because they can't talk to the apiserver.
- The "fix" of restarting all those pods is a thundering herd against the apiserver.

Always carry both keys for one full token-lifetime window before removing the old one.

**Bound projected tokens.** With `BoundServiceAccountTokenVolume` (default since 1.21), tokens are issued with a short lifetime (1h default) and auto-refreshed by the kubelet. This makes rotation feasible — wait 1h after switching the signing key, all tokens are now signed by the new key, you can safely drop the old key.

**Workload identity overlap.** If you run with `--service-account-issuer=https://issuer.example.com` and serve a JWKS at that URL for cloud workload identity (IRSA on AWS, Workload Identity on GCP), rotating `sa.key` requires updating the JWKS to include the new public key (both keys during the rotation window) and waiting for cloud-side caches to refresh (~hours). Coordinate.

---

## 30. Cluster Autoscaler in the Upgrade Loop

The Cluster Autoscaler (CA) — or Karpenter — interacts with drain in subtle ways.

```
                                         drain triggered
                                                │
                                                ▼
                                       ┌─────────────────┐
                                       │ kubectl drain   │
                                       │ - cordon node   │
                                       │ - evict pods    │
                                       └────────┬────────┘
                                                │
                                                ▼
                                       ┌─────────────────┐
                                       │ Pods now Pending│
                                       │ on apiserver    │
                                       └────────┬────────┘
                                                │
                                                ▼
                                       ┌─────────────────┐
                                       │ scheduler tries │
                                       │ to bind: no fit │
                                       └────────┬────────┘
                                                │
                                                ▼
                                       ┌─────────────────────────────┐
                                       │ Cluster Autoscaler /        │
                                       │ Karpenter notices Pending   │
                                       │ pods                        │
                                       │ → provisions a new node      │
                                       └────────────┬────────────────┘
                                                    │
                                                    ▼
                                       ┌─────────────────────────────┐
                                       │ New node Ready              │
                                       │ scheduler binds pending     │
                                       │ pods to it                  │
                                       └────────────┬────────────────┘
                                                    │
                                                    ▼
                                       ┌─────────────────────────────┐
                                       │ Original drained node       │
                                       │ now empty                   │
                                       └────────────┬────────────────┘
                                                    │
                                                    ▼
                                       ┌─────────────────────────────┐
                                       │ CA scales down the drained  │
                                       │ node (it's been empty for   │
                                       │ scale-down-unneeded-time)   │
                                       └─────────────────────────────┘
```

**The surge-upgrade pattern in cluster-autoscaler terms.** Surge upgrades work *because* the autoscaler provisions a new node when pods can't fit during drain. With no autoscaler, drain hangs forever in resource-tight clusters.

**Karpenter "drift" upgrade.** Karpenter compares each node's `aws.amazonaws.com/instance-ami-id` (or similar) against the NodePool's `amiSelector`. When they drift (e.g., NodePool updated to point at a new AMI for a new K8s version), Karpenter:

1. Provisions a new node matching the new spec.
2. Cordons the old node.
3. Drains the old node (respects PDBs).
4. Terminates the old node.

All from a single reconcile loop. This is the ergonomic peak of cluster-lifecycle automation. EKS managed node groups do something similar via the AWS API.

**Pitfall: CA removes a node mid-drain.** If you drain a node manually, CA may notice it's unneeded and terminate it before your drain command completes — and before the pods are safely rescheduled. The pods get force-killed instead of gracefully evicted. Fix: set the `cluster-autoscaler.kubernetes.io/scale-down-disabled` annotation on nodes during maintenance, or use `--cluster-autoscaler.kubernetes.io/safe-to-evict=false` on pods.

---

## 31. Tools Beyond kubeadm

kubeadm is the reference, but most production K8s isn't bootstrapped with it directly.

**Cluster API (CAPI).** The K8s-native way to declaratively manage clusters from another cluster. You run a "management cluster" (often kubeadm-bootstrapped) that hosts CAPI controllers; you `kubectl apply -f` Cluster, MachineDeployment, KubeadmControlPlane objects; CAPI reconciles them via cloud providers. See ch 26 for depth. Lifecycle is *also declarative*: upgrade is a spec change. Source: [kubernetes-sigs/cluster-api](https://github.com/kubernetes-sigs/cluster-api).

**Talos Linux.** An immutable, API-driven OS for K8s. No SSH, no shell. The entire OS is configured via a machine-config YAML, applied via the `talosctl` CLI over a mTLS gRPC API. Cluster bootstrap is a `talosctl bootstrap` call. Upgrades replace the OS image atomically (A/B partitions). For shops that want "everything declarative, no node access". Source: [siderolabs/talos](https://github.com/siderolabs/talos).

**k3s / k3sup.** Single-binary K8s for edge and small clusters. Embedded etcd (or SQLite for single-node). `curl -sfL https://get.k3s.io | sh -` and you have a cluster. k3sup is a multi-node bootstrapper on top. Source: [k3s-io/k3s](https://github.com/k3s-io/k3s). See ch 33.

**kops.** Older AWS-focused K8s installer. Predates managed K8s. Still stable; some shops use it for AWS clusters that pre-date EKS. Source: [kubernetes/kops](https://github.com/kubernetes/kops).

**Rancher RKE2.** Enterprise distribution, FIPS-certifiable, government-ready. Built around containerd, single-binary like k3s but more opinionated. Source: [rancher/rke2](https://github.com/rancher/rke2).

**Comparison:**

| Tool | Best for | State storage | Upgrade model | Multi-node |
|---|---|---|---|---|
| kubeadm | Bare metal, learning, custom | etcd | Per-node `kubeadm upgrade` | Yes |
| CAPI | Fleet, declarative | apiserver of mgmt cluster | spec change | Yes |
| Talos | Immutable infra, edge | etcd | OS image rotation | Yes |
| k3s | Edge, single-node | SQLite or embedded etcd | Restart binary | Yes |
| kops | AWS, legacy | S3 + apiserver | `kops update cluster` | Yes |
| RKE2 | Enterprise, gov | etcd | `rke2 update` | Yes |
| EKS/GKE/AKS | "I want a K8s API" | hidden | UI/API call | Hidden |

---

## 32. Managed Kubernetes Upgrade UX

EKS, GKE, AKS abstract the control-plane upgrade entirely. You click a button or call an API:

```
$ aws eks update-cluster-version --name my-cluster --kubernetes-version 1.30
{
  "update": {
    "id": "abc-123-def",
    "status": "InProgress",
    "type": "VersionUpdate",
    "params": [
      { "type": "Version", "value": "1.30" },
      { "type": "PlatformVersion", "value": "eks.1" }
    ],
    "createdAt": "2026-05-23T10:00:00Z"
  }
}
```

The provider:
1. Provisions a new control-plane node (or pod, depending on architecture).
2. Upgrades etcd if needed.
3. Brings up the new control plane behind the same LB endpoint.
4. Tears down the old control plane.

Time: 20-40 minutes typically. **You don't see any of this** — it's the provider's job.

**Worker upgrades.** Managed providers offer:
- **Managed node groups** with rolling update strategies (EKS, AKS). Specify `maxSurge` and `maxUnavailable`.
- **Karpenter** as the modern default for EKS (provisions per-pod, drift-replaces on AMI change).
- **GKE node auto-upgrade** which staggers per-pool.

**Comparison of upgrade defaults:**

| Provider | CP upgrade time | Worker upgrade strategy | Skip minors? |
|---|---|---|---|
| EKS | ~30 min | Managed node groups (surge) or Karpenter | No — must go 1.27 → 1.28 → 1.29 |
| GKE | ~10 min (less for regional) | Auto-upgrade rolling per pool | No |
| AKS | ~20 min | Surge or `maxSurge` | No |

All three enforce *sequential minors*. You cannot jump from 1.27 to 1.29. If your cluster sits on 1.27 too long, you're forced to do two upgrades back-to-back. Plan accordingly.

---

## 33. The "Skip a Minor" Problem

Why does K8s force sequential-minor upgrades?

The version skew policy (§8) says kubelet may lag apiserver by at most 2 minors (3 since 1.28). If your cluster is on 1.27 and you want to go to 1.30:
- After upgrading apiserver to 1.30, kubelets are at 1.27, which is **3 minors behind**.
- That's a skew policy violation. Specifically, the kubelet of 1.27 doesn't speak the 1.30 CRI version, doesn't know about new pod spec fields, etc.

So the only safe path is:
- 1.27 → 1.28 (kubelet skew = 1, OK)
- upgrade kubelets to 1.28
- 1.28 → 1.29 (kubelet skew = 1, OK)
- upgrade kubelets to 1.29
- 1.29 → 1.30
- upgrade kubelets to 1.30

Three full upgrades, each with their own drain windows. Hours of work compounding on top of each other.

**Planning implications.**
- Track K8s release cadence (~3 minors per year).
- Upgrade at *least* once a year, ideally every 4-6 months.
- Letting a cluster stagnate at an old version creates a *forced multi-upgrade* with linear time cost and linear risk cost.
- Test each minor's upgrade in staging *before* doing it in prod, even if the prod plan is the same procedure — release notes between minors can introduce new pre-upgrade requirements (new feature gates, deprecated APIs).

**API deprecation windows.** Some upgrades remove deprecated APIs (e.g., `batch/v1beta1 CronJob` removed in 1.25). If your manifests still use the deprecated version, the upgrade succeeds but your manifests stop being applyable. Tools: [`kubectl deprecations`](https://github.com/rikatz/kubepug) plugin / `kubent` scan your cluster for deprecated API usage.

---

## 34. Audit, Observability, and Upgrade SLOs

A staff-engineer upgrade is not "did it succeed" — it's "did we hold our SLO through it."

**Pre-upgrade baseline (collect 24h before the change):**

```
For each apiserver:
  apiserver_request_total (per verb, per resource)
  apiserver_request_duration_seconds_bucket (p50, p99, p99.9)
  apiserver_admission_step_latency_seconds (per webhook)
  apiserver_storage_objects (etcd object count)
  apiserver_audit_event_total

For etcd:
  etcd_disk_wal_fsync_duration_seconds (p99 < 25ms baseline)
  etcd_disk_backend_commit_duration_seconds (p99 < 50ms baseline)
  etcd_server_leader_changes_seen_total
  etcd_mvcc_db_total_size_in_bytes (vs in_use)
  
For workloads:
  HTTP/gRPC error rate per service
  P99 latency per service
  Saturation metrics (cpu, mem, conn pool)
```

**During upgrade (live):**
- Tail apiserver `/metrics` from each node; rolling p99 windows.
- Watch `kube_node_status_condition` for unhealthy nodes during drain.
- Watch `kubernetes_audit_event_total{verb="patch"}` — large spikes during upgrade are normal (manifest updates), but unusual sustained activity post-upgrade is a controller stuck in a hot reconcile loop.
- Watch your SLO dashboard; pause if it dips.

**Audit log retention.** During the upgrade window, *retain more audit logs* than your normal policy. Set `--audit-log-maxage`, `--audit-log-maxbackup`, `--audit-log-maxsize` generously. When something goes wrong 2 hours after the upgrade, you want every API call from the last 6 hours, not the last 1 hour.

**Post-upgrade comparison.** After the upgrade completes, run the same metric collection for 24 hours and compare:
- p99 latency regression > 10% → investigate
- New error types in audit logs → investigate
- etcd size increase > 30% post-upgrade → investigate (often a new controller's status field is verbose)

**Observability is the only thing that turns an upgrade from "did I roll it back fast enough" into "this regression is X, here's the fix." Don't upgrade without it.**

---

## 35. Pitfalls

A non-exhaustive but representative list. Most of these have bitten a real cluster at a real company at a real time.

1. **Defragging the leader simultaneously with followers.** Quorum loss window during the leader's defrag. Always followers first, leader last.
2. **Restoring etcd while apiservers are still running.** Apiservers cache state; after restore they see "their" objects vanish. Stop apiservers before restoring.
3. **`--initial-cluster-token` mismatch across members during restore.** Members reject each other; no quorum. All members must use the same token.
4. **`kubeadm certs renew` forgotten until the certs expire.** Apiserver can't authenticate to kubelets, controllers can't authenticate to apiserver. The cluster degrades silently. Calendar your renewals.
5. **`--service-account-key-file` configured with only the new key during rotation.** All existing SA tokens become invalid. Use a list with both keys for the transition window.
6. **Skipping minor versions.** kubectl 1.29 against apiserver 1.27 = some commands silently use unsupported fields. Sequential upgrades only.
7. **Long-running pods that ignore SIGTERM.** Drain hangs for `terminationGracePeriodSeconds` per pod. Either fix the pod or set a shorter TGPS.
8. **`kubectl drain` without `--ignore-daemonsets`.** Drain refuses with an error. Always include the flag.
9. **PDB `minAvailable: replicas`.** Drain can never proceed; it's mathematically blocked. Use `replicas - 1` or `maxUnavailable: 1`.
10. **Single-replica app with PDB `minAvailable: 1`.** Drain blocked forever. Either scale to 2 or accept disruption.
11. **Velero backup target inside the same cluster.** Lose the cluster, lose the backup. Put the BSL in object storage outside the cluster.
12. **Overlapping Velero schedules.** Multiple backups racing on the same VolumeSnapshot APIs hit cloud rate limits. Stagger schedules or scope selectors.
13. **Restic without enough storage at the BSL.** Backup quietly fails partway; you don't notice until the restore test. Monitor BSL free space and Velero metrics.
14. **CSI snapshot enabled but no snapshot CRDs installed.** Velero waits forever for VolumeSnapshotContent to bind. Install `external-snapshotter` before enabling CSI snapshots.
15. **Cluster Autoscaler terminating a node mid-drain.** Pods get force-killed. Set `cluster-autoscaler.kubernetes.io/scale-down-disabled` during maintenance.
16. **Expired kubelet client certs on a fleet of workers.** kubelets can't authenticate, nodes go NotReady. `RotateKubeletClientCertificate` feature gate avoids this for clusters that run continuously; reboot-only clusters need manual rotation.
17. **`kubeadm upgrade apply` without first pinning the version in the package manager.** apt-mark unhold + apt upgrade jumps to a newer version than kubeadm-config expects. Always pin: `apt-mark hold kubeadm kubelet kubectl`.
18. **Hand-edited static pod manifest while kubelet is watching.** Partial write triggers a partial pod. Always write to a temp file and rename atomically.
19. **Manual changes to `/etc/kubernetes/manifests/` on a kubeadm-managed cluster.** Next `kubeadm upgrade` overwrites your changes. Track configuration via `kubeadm-config` ConfigMap or move to declarative tooling.
20. **Etcd encryption-at-rest key not backed up with snapshots.** The snapshot is encrypted; without the key it's unreadable. Back up the EncryptionConfiguration alongside the snapshot.
21. **Etcd CA leaf cert (peer or server) renewed but not redistributed to all members.** Members can't authenticate to each other; cluster splits. Renew all and `touch` all manifests within the same window.
22. **PVs with `reclaimPolicy: Retain` left behind during cluster decommission.** Cloud volumes orphaned. Switch to Delete before decommission or sweep cloud manually.
23. **Service type=LoadBalancer deleted after the cluster is gone.** Cloud LB billed forever. Delete Services before tearing down the cluster.
24. **`kubectl delete --force --grace-period=0` used during drain to "speed it up".** Pods removed from apiserver but containers may still be running on the node, holding volumes. Use only on confirmed-dead nodes.
25. **CNI not installed before joining workers.** Workers join, kubelet reports Ready=false because CNI pod can't start. Install CNI before joining workers, or accept the "Ready=false for first 30 seconds" transient.
26. **Joining a worker with a CA hash from an old cluster.** Hash mismatch, `kubeadm join` fails. Always regenerate the join command from the current control plane.
27. **kubeadm token expired.** Default TTL is 24h. For long-lived bootstrappers (CAPI, Karpenter), create a longer-TTL token or use a different bootstrap mechanism.
28. **Backing up etcd from the leader vs follower without noticing the snapshot endpoint.** No real issue (both work), but operators sometimes think only the leader can produce a valid snapshot. Any member's `snapshot save` is correct.
29. **CoreDNS pod stuck Pending after fresh init.** CNI not installed → no pod networking → CoreDNS can't bind. Install CNI immediately after `kubeadm init` succeeds.
30. **`kubeadm upgrade plan` shows the upgrade is possible, but `apply` fails on etcd version mismatch.** Out-of-tree etcd version doesn't match what kubeadm wants. Either stay on kubeadm-bundled etcd or accept that you're on the unsupported path.
31. **A controller (cert-manager, ArgoCD, etc.) configured with `serviceaccount-token` projected token referencing the old `sa.key` after rotation.** When the projection refreshes, it picks up the new key — but if the controller cached the token aggressively, it gets 401s. Restart the controller after SA key rotation.
32. **Velero "FailedValidation" on backup with `defaultVolumesToFsBackup: true` but no node-agent installed.** Velero can't run Restic/Kopia without the DaemonSet. Install node-agent first.
33. **GitOps drift fighting upgrade controllers.** ArgoCD reverts an in-progress upgrade because the cluster's actual state diverges from Git. Use `ignoreDifferences` or sync windows during upgrades.
34. **Upgrading kubeadm without upgrading the static pod images.** kubeadm version doesn't drive the image versions in `/etc/kubernetes/manifests/` directly — `kubeadm upgrade apply` rewrites the manifests with new images. Without it, the binaries on disk are new but the running pods are old. Always `kubeadm upgrade apply`, never just `apt install kubeadm`.
35. **`kubectl cordon` confused with a PDB.** Cordon prevents new pods from being scheduled to the node. It does NOT evict existing pods. Drain is cordon+evict; cordon alone is preparation.

---

## 36. References and Source Paths

- **kubeadm** source: [kubernetes/kubernetes/cmd/kubeadm](https://github.com/kubernetes/kubernetes/tree/master/cmd/kubeadm)
  - Phases: `cmd/kubeadm/app/phases/`
  - Certs: `cmd/kubeadm/app/phases/certs/`
  - Upgrade: `cmd/kubeadm/app/cmd/upgrade/`
  - Constants (etcd version, etc.): `cmd/kubeadm/app/constants/constants.go`
- **etcd** source: [etcd-io/etcd](https://github.com/etcd-io/etcd)
  - Snapshot: `etcdctl/ctlv3/command/snapshot_command.go`
  - Defrag: `etcdctl/ctlv3/command/defrag_command.go`
  - Recovery docs: [etcd recovery](https://etcd.io/docs/v3.5/op-guide/recovery/)
- **Velero**: [vmware-tanzu/velero](https://github.com/vmware-tanzu/velero)
  - Helm chart: [vmware-tanzu/helm-charts](https://github.com/vmware-tanzu/helm-charts/tree/main/charts/velero)
  - Plugins for AWS/GCP/Azure: `vmware-tanzu/velero-plugin-for-{aws,gcp,microsoft-azure}`
- **Cluster API**: [kubernetes-sigs/cluster-api](https://github.com/kubernetes-sigs/cluster-api)
- **Karpenter**: [aws/karpenter-provider-aws](https://github.com/aws/karpenter-provider-aws)
- **Talos**: [siderolabs/talos](https://github.com/siderolabs/talos)
- **k3s**: [k3s-io/k3s](https://github.com/k3s-io/k3s)
- **CSR auto-approval logic**: `pkg/controller/certificates/approver/sarapprove.go`
- **PDB controller**: `pkg/controller/disruption/disruption.go`
- **Eviction handler**: `pkg/registry/core/pod/storage/eviction.go`
- **Version skew policy**: [kubernetes.io/releases/version-skew-policy](https://kubernetes.io/releases/version-skew-policy/)
- **kubeadm upgrade docs**: [kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/)
- **etcd backup docs**: [kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/)
- **external-snapshotter**: [kubernetes-csi/external-snapshotter](https://github.com/kubernetes-csi/external-snapshotter)

---

**The lesson.** Day-2 operations are the part of Kubernetes that *doesn't reconcile itself*. Everything that fills the rest of this folder is automatic — controllers watch, controllers act, controllers heal. But the cluster itself can't upgrade itself, can't back itself up, can't restore itself, can't decommission itself. Those are *external* operations performed on the cluster by humans (or by tools like CAPI that recursively make the *fleet* into a self-reconciling system one level up).

The good news: each operation is small, well-defined, and rehearsable. kubeadm bootstrap is eleven phases. Upgrade is drain-apply-restart per node. etcd backup is one command. etcd restore is six steps. Velero is two CRDs.

The hard part is the *combination*: the order, the version skew, the PDBs, the timing windows, the cert renewals, the orphan-resource cleanup, the standby coordination. The hard part is also rehearsing — every staff team should drill cluster-DR at least annually, and every promotion to staff should require having executed a restore-from-snapshot exercise on a test cluster.

If chapter 04 told you etcd is the heart of Kubernetes, this chapter tells you what to do when the heart stops. Practice now, not at 03:00.
