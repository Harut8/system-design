# Network Policy and Segmentation: Default-Deny, Tiers, and Zero-Trust East-West

The default Kubernetes network is the loudest endorsement of the platform's "batteries-included-but-removable" philosophy: every pod can talk to every other pod, in every namespace, on every port, with no encryption, no identity, and no audit trail. The flat pod network is not a bug — it is the contract that makes Services, DNS, gossip protocols, sidecars, and operators all work without any per-workload configuration. But it is also, by default, the perfect substrate for lateral movement. An attacker who lands a single shell — a compromised dependency in a build job, a leaked token, a vulnerable Java deserializer — is exactly one hop away from your payment service, your secrets backend, and your apiserver.

NetworkPolicy is the Kubernetes API that takes this flat L3/L4 fabric and makes it segmentable. It is, deliberately, a small API: it has no concept of identity beyond pod and namespace labels, no L7 understanding, no notion of deny rules, no priorities. It is a *contract* between the cluster operator (who writes policies) and the CNI plugin (who enforces them). The semantics are defined in the Kubernetes API server, but the actual packet drops happen in iptables, ipsets, or eBPF maps owned by Calico, Cilium, kube-router, Antrea, or whatever CNI you chose in chapter 15.

This chapter is about the gap between what NetworkPolicy says it does and what your cluster actually drops. We will cover the vanilla NetworkPolicy spec in painful detail — every selector-vs-selector trap, every DNS pitfall, every CIDR gotcha — and then climb the abstraction stack: Calico GlobalNetworkPolicy and tiers (cluster-scoped, ordered, with explicit Deny), Cilium CiliumNetworkPolicy and L7 awareness (HTTP verbs, gRPC methods, Kafka topics, FQDN allowlists), and finally the new vendor-neutral AdminNetworkPolicy/BaselineAdminNetworkPolicy that landed beta in Kubernetes 1.29 and which is, finally, the API the platform team always wanted.

Then we will talk about what NetworkPolicy *cannot* do — host-network pods, egress IP control, identity-based authentication, encryption, L7 authorization beyond the simplest cases — and what fills those gaps: HostEndpoints, egress gateways, service meshes, mTLS, and SPIFFE. By the end you will know exactly which control plane is responsible for which packet, in which order they are evaluated, and which observability tool to reach for when a policy "should be working but the pod still can't reach the database."

The audience is the staff engineer who has to defend a multi-team cluster against both a determined external attacker and a careless internal one, without breaking the platform contract that workloads expect.

---

## Table of Contents

1.  [The Threat Model](#1-the-threat-model)
2.  [The NetworkPolicy API: A Tour](#2-the-networkpolicy-api-a-tour)
3.  [Selectors: Pod, Namespace, IPBlock](#3-selectors-pod-namespace-ipblock)
4.  [The Big Selector Traps](#4-the-big-selector-traps)
5.  [Default-Deny: The Foundational Pattern](#5-default-deny-the-foundational-pattern)
6.  [Additive (Union) Semantics](#6-additive-union-semantics)
7.  [Egress and the DNS Problem](#7-egress-and-the-dns-problem)
8.  [NetworkPolicy Enforcement by CNI](#8-networkpolicy-enforcement-by-cni)
9.  [Calico GlobalNetworkPolicy and HostEndpoints](#9-calico-globalnetworkpolicy-and-hostendpoints)
10. [Calico Tiers: Ordered Evaluation](#10-calico-tiers-ordered-evaluation)
11. [Cilium CiliumNetworkPolicy: L7 and FQDN](#11-cilium-ciliumnetworkpolicy-l7-and-fqdn)
12. [CiliumClusterwideNetworkPolicy](#12-ciliumclusterwidenetworkpolicy)
13. [AdminNetworkPolicy and BaselineAdminNetworkPolicy (1.29+)](#13-adminnetworkpolicy-and-baselineadminnetworkpolicy-129)
14. [The Pass Action and Layered Authority](#14-the-pass-action-and-layered-authority)
15. [Egress Gateways](#15-egress-gateways)
16. [NetworkPolicy + Service Mesh: Order of Evaluation](#16-networkpolicy--service-mesh-order-of-evaluation)
17. [Pod-Level Firewalls and the hostNetwork Bypass](#17-pod-level-firewalls-and-the-hostnetwork-bypass)
18. [Zero-Trust Patterns](#18-zero-trust-patterns)
19. [Common Recipe Library](#19-common-recipe-library)
20. [CIDR Semantics Gotchas](#20-cidr-semantics-gotchas)
21. [Testing NetworkPolicy](#21-testing-networkpolicy)
22. [Day-2: Auditing, Drift, and Enforcement](#22-day-2-auditing-drift-and-enforcement)
23. [Performance: iptables vs eBPF Maps](#23-performance-iptables-vs-ebpf-maps)
24. [Observability: Hubble, FlowLogs, Counters](#24-observability-hubble-flowlogs-counters)
25. [Pitfalls](#25-pitfalls)
26. [TL;DR](#26-tldr)

---

## 1. The Threat Model

Before we read a single YAML manifest, fix in your head exactly which attacks NetworkPolicy is and is not designed to stop.

### 1.1 What It Does Address

NetworkPolicy is a *segmentation* control. It exists to enforce the principle of least connectivity: a workload may only talk to the network peers it provably needs, and only on the ports it provably needs, regardless of any application bug, library CVE, or compromised credential.

The two attack classes it directly mitigates are:

- **Lateral movement.** An attacker who has compromised one pod (say, `web/frontend-7d4c` via a deserialization vulnerability in a Java library) attempts to reach other pods or services to escalate. With no NetworkPolicy, the attacker can scan the entire pod CIDR, hit the apiserver, hit etcd if exposed, scrape the metadata service, and pivot to the database. With a strict NetworkPolicy, the attacker's blast radius collapses to *exactly the peers the policy allows*.
- **Exfiltration via egress.** A compromised pod attempts to dial out — to an attacker-controlled C2 server, a public pastebin, a crypto-mining pool, or even legitimate cloud APIs (S3, KMS) for data theft. Egress NetworkPolicy restricts this to a known allowlist of destinations.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    LATERAL MOVEMENT WITHOUT NETPOL                       │
│                                                                          │
│   ┌─────────┐    1. CVE-2024-XXX    ┌────────────┐                       │
│   │ Internet│──────────────────────▶│ frontend   │ (compromised)         │
│   └─────────┘                       └─────┬──────┘                       │
│                                           │ free egress, free east-west │
│                                           │                              │
│                          ┌────────────────┼────────────────┐             │
│                          ▼                ▼                ▼             │
│                    ┌──────────┐    ┌──────────┐    ┌──────────┐          │
│                    │ payments │    │ apiserver│    │ database │          │
│                    └──────────┘    └──────────┘    └──────────┘          │
│                                                                          │
│                    ▼ also: metadata service, secrets, kubelet ▼          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 1.2 What It Does NOT Address

NetworkPolicy is a *packet filter at the pod boundary*. It is not, and was never intended to be:

- **Authentication.** It cannot prove that the peer at the other end of the TCP connection is who its IP claims to be. Pod IPs are recycled. A compromised neighbor pod in the same namespace looks identical to the legitimate caller. *Cryptographic identity is the job of a mesh (mTLS) or SPIFFE/SPIRE.*
- **L7 authorization** (in vanilla NP). It cannot say "service A may call `GET /v1/users` but not `DELETE /v1/users`." Cilium and Istio AuthorizationPolicy can; vanilla NP cannot.
- **Encryption.** Packets allowed by NP still travel in plaintext on the pod overlay unless your CNI does transparent encryption (Cilium WireGuard, Calico WireGuard, Istio mesh).
- **Host-network traffic.** Pods with `hostNetwork: true` share the node's network namespace and are *not* selectable by pod-level NetworkPolicy on most CNIs. They are firewalled, if at all, by Calico HostEndpoint or by node-level iptables.
- **Outbound NAT identity.** NP can restrict *which destinations* a pod may reach, but not *which source IP* the cluster appears as to the outside world. That is the job of egress gateways and cloud NAT gateways.
- **Layer-1/2 attacks.** ARP spoofing, MAC flooding, switch-port hijacking — these are below the abstraction NP operates at.
- **DNS data leakage.** A pod with permission to query CoreDNS can encode arbitrary data in subdomain labels (`<base64-payload>.attacker.com`) and exfiltrate via the DNS resolver itself. Mitigation requires DNS-firewalling (Cilium's DNS visibility, CoreDNS plugins, or external DNS proxies).
- **Compromised privileged pods.** A pod with `CAP_NET_ADMIN` and `hostNetwork: true` can rewrite iptables. NP cannot save you from a pod that can edit the rules. *PodSecurity admission must prevent such pods from being created in the first place.*

This last bullet generalizes: NetworkPolicy is **defense in depth**. It assumes the rest of the stack — admission, RBAC, image signing, runtime security — is doing its job. If your image is malware, NP slows the attacker down but does not stop them.

---

## 2. The NetworkPolicy API: A Tour

NetworkPolicy is a namespaced object. Its full schema lives at `staging/src/k8s.io/api/networking/v1/types.go` in `kubernetes/kubernetes`, type `NetworkPolicy`. Read that file once; it is shorter than this section.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-server-allow
  namespace: payments
spec:
  podSelector:
    matchLabels:
      app: api-server
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: frontend
          namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: web
        - ipBlock:
            cidr: 10.0.0.0/16
            except:
              - 10.0.5.0/24
      ports:
        - protocol: TCP
          port: 8080
          endPort: 8090
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
    - to:
        - podSelector:
            matchLabels:
              app: postgres
      ports:
        - protocol: TCP
          port: 5432
```

### 2.1 The Five Fields

Five fields, in evaluation order:

1.  **`podSelector`** — *which pods does this policy apply to?* A label selector (`matchLabels` or `matchExpressions`) over pods *in the same namespace as the NetworkPolicy*. An empty `podSelector: {}` matches every pod in the namespace. A missing `podSelector` is a schema error; the empty object is the explicit "all pods" form. The selected pods are called the *target set*, the *subject*, or the *selected pods*. I'll use "target set."
2.  **`policyTypes`** — *which directions does this policy regulate?* A list containing some subset of `[Ingress, Egress]`. If `policyTypes` is omitted, the API server infers it from whether `ingress` or `egress` arrays are present. This inference is the source of subtle bugs; always set `policyTypes` explicitly.
3.  **`ingress`** — a list of allow-rules for traffic *into* the target set. Each element has optional `from` (a list of peers) and optional `ports` (a list of ports). An empty list `ingress: []` plus `policyTypes: [Ingress]` is the default-deny-ingress idiom; *omitting* `ingress` while declaring `policyTypes: [Ingress]` has the same effect.
4.  **`egress`** — symmetric to `ingress`, but for traffic *out of* the target set. Each element has optional `to` and optional `ports`.
5.  **`metadata.namespace`** — implicit but critical. The policy applies *only* to pods in this namespace.

### 2.2 Ingress Rule Structure

```yaml
ingress:
  - from:                       # OR across this list
      - podSelector: { … }      # peer 1: any namespace where it lives
        namespaceSelector: { … } # AND-ed when in the same item (TRAP)
      - ipBlock:
          cidr: 192.168.0.0/16
          except:
            - 192.168.1.0/24
    ports:                       # OR across this list
      - protocol: TCP
        port: 8080
      - protocol: TCP
        port: 8443
```

The rule is satisfied if traffic comes from *any* peer in `from` *and* targets *any* port in `ports`. Multiple ingress rules are themselves OR-ed: traffic is allowed if it matches at least one rule across the policy *and* any other policy that selects the same pod (we'll get to additive semantics in §6).

### 2.3 Port Specification

`port` may be a number (1-65535) or a *named port* — the string name of a `containerPort` declared on the target pod's spec. Named ports are useful when port numbers vary across deployments but the meaning ("http") is stable.

`endPort` (added in 1.21, GA in 1.25) creates a range: `port: 32000, endPort: 32767` matches every port in `[32000, 32767]`. Required: `endPort >= port`, both numeric (not named), same protocol. The CNI must advertise support; see the apiserver's feature-gate `NetworkPolicyEndPort`. Cilium and Calico both support it; older Antrea versions did not.

`protocol` may be `TCP` (default), `UDP`, or `SCTP`. SCTP support requires CNI advertisement and is rare outside telco workloads.

### 2.4 What Is *Not* in the Schema

It is worth listing, explicitly, the features people expect but vanilla NP does not have:

- **No `deny` action.** Every rule is an *allow*. Denials are implicit: if a pod is selected by *any* NetworkPolicy with `policyTypes: [Ingress]`, all ingress is denied except what rules explicitly allow. There is no way to write "everyone may reach me except this one bad pod" in vanilla NP. Use ANP or Calico for that.
- **No priorities.** Policies are evaluated as a set; there is no "policy A wins over policy B." See §6.
- **No L7.** No HTTP methods, no paths, no headers, no gRPC, no SNI. CiliumNetworkPolicy adds these; vanilla does not.
- **No FQDN.** You cannot write `to: { host: api.stripe.com }`. CiliumNetworkPolicy `toFQDNs`, Calico `DNSPolicy`, or a sidecar proxy is required.
- **No identity.** Selectors are over labels, which are mutable Kubernetes data, not cryptographic identity. A pod that adopts a `team=payments` label is, as far as NP is concerned, on the payments team. RBAC must restrict who may write that label.
- **No node selection.** You cannot say "only pods on nodes with label `secure=true` may receive." Some CNIs add `nodeSelector` extensions; vanilla NP does not.
- **No logging / counters / audit.** Vanilla NP has no field that says "log when this rule fires." Calico and Cilium add this on their extended objects.

The narrowness of vanilla NP is *deliberate*. It is the minimum surface that every CNI can be expected to implement consistently. Everything beyond it is vendor-specific until ANP/BANP (chapter §13) lands GA.

---

## 3. Selectors: Pod, Namespace, IPBlock

There are exactly three peer kinds in vanilla NP:

```
peer = { podSelector?, namespaceSelector? } | { ipBlock }
```

### 3.1 `podSelector`

A `metav1.LabelSelector` over pods. By default, *only pods in the same namespace as the NetworkPolicy*. To select pods in a different namespace you must add `namespaceSelector`.

```yaml
# Select pods labelled app=frontend in THIS namespace
from:
  - podSelector:
      matchLabels:
        app: frontend
```

The selector follows standard `matchLabels` / `matchExpressions` semantics; see `staging/src/k8s.io/apimachinery/pkg/apis/meta/v1/types.go`.

### 3.2 `namespaceSelector`

A `metav1.LabelSelector` over namespaces. *Without* `podSelector` in the same peer item, it means "all pods in every namespace matching this selector."

Since 1.22, every namespace gets an automatic label `kubernetes.io/metadata.name: <namespace name>`, which means you can finally select a namespace by name via label selector. (Before 1.22, you had to label namespaces yourself or use the namespace-named label convention.)

```yaml
# Select all pods in the "monitoring" namespace
from:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: monitoring
```

### 3.3 `ipBlock`

A CIDR with optional carve-outs. *Cannot be combined* with `podSelector` or `namespaceSelector` in the same peer item (the API server rejects the mix at validation).

```yaml
from:
  - ipBlock:
      cidr: 10.0.0.0/8
      except:
        - 10.0.5.0/24       # carve out a /24 from the /8
        - 10.0.7.42/32      # carve out a single IP
```

`ipBlock` is the only way to express peers that are *not* Kubernetes pods — bastion hosts, on-prem networks, cloud VPCs, the apiserver VIP (sometimes), etc. We will see in §20 that `ipBlock` semantics interact with pod-CIDR vs node-CIDR vs service-CIDR in unintuitive ways.

### 3.4 The Three Selector Combinations Inside One Peer

This is the central trap of NetworkPolicy. Read it twice.

```yaml
# Case A — namespaceSelector AND podSelector in the SAME peer item:
from:
  - namespaceSelector:
      matchLabels:
        env: prod
    podSelector:
      matchLabels:
        app: frontend
# Meaning: pods labelled app=frontend that ALSO live in a namespace labelled env=prod
# This is logical AND.

# Case B — namespaceSelector AND podSelector in DIFFERENT peer items:
from:
  - namespaceSelector:
      matchLabels:
        env: prod
  - podSelector:
      matchLabels:
        app: frontend
# Meaning: (all pods in any env=prod namespace)  OR  (pods labelled app=frontend in MY namespace)
# This is logical OR.

# Case C — only podSelector:
from:
  - podSelector:
      matchLabels:
        app: frontend
# Meaning: pods labelled app=frontend IN THE SAME NAMESPACE AS THE POLICY.
# It does NOT mean "everywhere in the cluster."
```

If you write Case B intending Case A, you accidentally allow *every pod in every env=prod namespace* to talk to your target, regardless of label. This has caused real outages and real CVEs.

```
┌─────────────────────────────────────────────────────────────────────────┐
│             SAME PEER ITEM vs DIFFERENT PEER ITEMS                       │
│                                                                          │
│   - namespaceSelector: {env: prod}            ──► (env=prod) AND         │
│     podSelector: {app: frontend}                   (app=frontend)        │
│                                                                          │
│   - namespaceSelector: {env: prod}            ──► (any pod in env=prod)  │
│   - podSelector: {app: frontend}                   OR                    │
│                                                    (app=frontend in MY ns)│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. The Big Selector Traps

There are five traps in NetworkPolicy selectors that catch even experienced operators. Memorize them.

### 4.1 The `matchLabels: {}` Trap

An empty `matchLabels` matches **every** object, not zero objects.

```yaml
podSelector:
  matchLabels: {}      # matches EVERY pod
```

This is consistent with the rest of Kubernetes (an empty label selector is the universal selector) but is the opposite of every other system's intuition (an empty filter list usually filters everything out).

Combine this with case-B above:

```yaml
from:
  - namespaceSelector: {}
    podSelector: {}
# Meaning: every pod in every namespace in the cluster.
# In a vanilla policy this is "allow from anywhere in the cluster."
```

This is sometimes what you want (`metrics-server`, ingress-controllers). It is rarely what you intended to type.

### 4.2 The Missing `namespaceSelector` Trap

If your `from` has only a `podSelector`, it implicitly scopes to the *policy's own namespace*. Cross-namespace traffic is dropped.

```yaml
# In namespace "payments"
ingress:
  - from:
      - podSelector:
          matchLabels:
            app: frontend
# Frontend pods in "web" namespace will be BLOCKED.
# Only frontend pods in "payments" will be allowed.
```

Symmetric trap on egress: a `to: [{ podSelector: { app: postgres } }]` in namespace `payments` will not reach the `app: postgres` pods in `databases`.

### 4.3 The `policyTypes` Omission Trap

If you omit `policyTypes`, the API server infers it from whether `ingress`/`egress` arrays are present.

```yaml
spec:
  podSelector: { matchLabels: { app: foo } }
  ingress:
    - from: [...]
# Inferred policyTypes: [Ingress]. Egress is unaffected.
```

But:

```yaml
spec:
  podSelector: { matchLabels: { app: foo } }
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [...]
# Egress is in policyTypes but the egress array is absent → DEFAULT DENY EGRESS.
```

This is the standard idiom for "deny all egress except what I list elsewhere," but if you wrote it by accident you just broke every outbound connection from `app: foo`. Always write `policyTypes` explicitly; never rely on the inference.

### 4.4 The Self-Selecting Policy Trap

A policy that selects pod `A` and lists `A` in its `from` does *not* automatically allow loopback or intra-pod traffic. Containers in the same pod share the network namespace and can talk via `localhost` without traversing any policy. But two replicas of the same Deployment do *not* share a network namespace — they have different pod IPs — so a policy that selects `app=foo` and forgets to allow `from: { podSelector: { app: foo } }` will block replica-to-replica traffic (which often breaks gossip protocols like Cassandra, Elasticsearch, etcd, NATS).

```yaml
# In namespace "datastore"
ingress:
  - from:
      - podSelector: { matchLabels: { app: cassandra } }   # ← required for self-gossip
    ports:
      - protocol: TCP
        port: 7000     # internode
      - protocol: TCP
        port: 7001     # internode TLS
```

### 4.5 The Service VIP Trap

NetworkPolicy applies *after* `Service` translation, on the pod-to-pod packet, but *before* return packets (which have already been un-DNATed by conntrack on most CNIs). What this means in practice: if a client pod calls `cluster-ip:80`, the packet's destination is rewritten to `pod-ip:8080` by kube-proxy, *then* arrives at the target pod where ingress NetworkPolicy is evaluated against the *source pod IP*, not the cluster IP. So `from: { ipBlock: { cidr: <service-cidr> } }` is essentially never what you want — service CIDRs don't appear as source IPs on the wire.

We expand on this in §20.

---

## 5. Default-Deny: The Foundational Pattern

The single most important NetworkPolicy in any cluster is the default-deny one. Without it, NP is fundamentally additive: if no policy selects a pod, *all* traffic is allowed; if any policy selects a pod for a direction, only *explicitly allowed* traffic in that direction is permitted.

### 5.1 The Default-Deny-All Policy

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: payments
spec:
  podSelector: {}                # all pods in this namespace
  policyTypes:
    - Ingress
    - Egress
  # No ingress: array → deny all ingress
  # No egress: array  → deny all egress
```

This policy is the floor. Every other policy in the namespace builds on top. Once you have applied it, *nothing in the namespace can talk to anything else, including itself*, until you write specific allows.

### 5.2 Default-Deny by Direction

You can split ingress and egress:

```yaml
# Just deny ingress; egress is unrestricted by THIS policy (but may be restricted by others)
spec:
  podSelector: {}
  policyTypes: [Ingress]
```

```yaml
# Just deny egress
spec:
  podSelector: {}
  policyTypes: [Egress]
```

A common operational pattern is to roll out default-deny-ingress first (low blast radius) and add default-deny-egress later, after every workload's egress requirements have been audited and explicit allow rules written. Most outages come from rolling out default-deny-egress without allowing CoreDNS.

### 5.3 Default-Allow Patterns (Less Common)

There is also a vanilla "default-allow" you can write, useful when you want to opt some pods out of a stricter policy:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-all-egress-for-system-pods
  namespace: monitoring
spec:
  podSelector:
    matchLabels:
      tier: system
  policyTypes: [Egress]
  egress:
    - {}   # empty rule object = allow all egress
```

`- {}` (an empty peer item with no ports, no to, no nothing) is the "allow everything" idiom. It is rarely written deliberately; more often it appears in policies generated by tooling that means "no restriction in this direction."

### 5.4 The Default-Deny Boot Order

When you apply default-deny to a *running* namespace, every existing connection that does not match the allow set is *immediately* dropped on most CNIs (Cilium, Calico) because policy enforcement happens at every packet, not at connection establishment. iptables/conntrack-based modes have subtler behavior: an established conntrack entry can sometimes survive a policy reload (because conntrack tracks the flow's state machine, not the policy). Do not rely on this. Treat default-deny as a hard cutover.

Recommended sequence:

1.  Write all per-app allow policies first.
2.  Apply them; verify with `cilium connectivity test` or `np-test` that allowed paths work and unallowed paths still work *(because no deny is in effect yet)*.
3.  Apply default-deny-ingress.
4.  Verify allowed paths still work; unallowed ingress should now be blocked.
5.  Apply default-deny-egress.
6.  Watch for CoreDNS, apiserver, registry breakage. Fix.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                  DEFAULT-DENY ROLLOUT TIMELINE                          │
│                                                                          │
│   t=0    Open cluster. Everything talks to everything.                  │
│            │                                                             │
│            │  Apply per-workload ALLOW policies                          │
│   t=1    Open cluster + redundant allows. Same behavior, no break.      │
│            │                                                             │
│            │  Apply default-deny-INGRESS                                 │
│   t=2    Pods only receive from explicitly-allowed peers.               │
│            │                                                             │
│            │  Apply default-deny-EGRESS                                  │
│   t=3    Pods only call out to explicitly-allowed peers.                │
│            │                                                             │
│            ▼  Run for one week; mine flow logs for would-be denies.     │
│   t=10   Steady state.                                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Additive (Union) Semantics

The single semantic rule of vanilla NetworkPolicy is:

> If a pod is selected by zero policies in a given direction, all traffic in that direction is allowed.
> If a pod is selected by one or more policies in a given direction, traffic in that direction is allowed if it is allowed by at least one of the selecting policies.

In set-theoretic terms: the *allow set* for a pod in a direction is the **union** of the allow sets of every policy that selects it for that direction. There is no AND across policies. There is no priority. There is no Deny.

```
┌─────────────────────────────────────────────────────────────────────────┐
│             ADDITIVE SEMANTICS — UNION OF ALLOW SETS                     │
│                                                                          │
│      Pod P is selected by Policy A, Policy B, Policy C (Ingress).       │
│                                                                          │
│      Policy A allows: from frontend                                      │
│      Policy B allows: from monitoring                                    │
│      Policy C allows: from kube-system (DNS only)                        │
│                                                                          │
│      Effective ingress to P:                                             │
│      ───────────────────────                                             │
│      from frontend  ∪  from monitoring  ∪  from kube-system:53          │
│                                                                          │
│      There is no way to write "B and C must agree" or                   │
│      "C overrides A".                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 6.1 Implications for Policy Composition

- **Policies compose by addition, not by intersection.** If team Alpha writes a policy that allows `from: frontend` and team Beta writes one that allows `from: monitoring`, the pod receives traffic from *both*. Neither team's policy alone can *restrict* the other's. This is great for layering team responsibilities; it is terrible if you wanted "platform deny overrides app allow."
- **You cannot subtract.** There is no way to write a policy that says "remove the allowance that another policy added." If another team has opened a hole, you cannot patch it without removing or modifying their policy.
- **You cannot deny.** A pod with `evil=true` cannot be denied by writing a deny policy. The only way to deny `evil=true` in vanilla NP is to write *every other* allow policy to explicitly exclude `evil=true` — which is operationally infeasible.

These limitations are *the* motivation for AdminNetworkPolicy (§13), Calico tiers (§10), and CiliumClusterwideNetworkPolicy (§12). All three of those introduce a mechanism for explicit deny outside the union semantics.

### 6.2 Implications for Default-Deny

Default-deny is built on the same union semantics. It "wins" not because of any priority — it is just a policy that selects every pod (`podSelector: {}`) and adds an empty allow set. The union of "empty" with "frontend" is "frontend"; the union of "empty" with nothing is empty.

This means **the order of `kubectl apply` does not matter**. All policies are evaluated as a set at packet time, by the CNI, against the *current state* of the API.

### 6.3 Composing Allows from Multiple Sources

The recommended composition pattern in large clusters:

- One *platform* policy per namespace: `allow egress to coredns + apiserver`.
- One *namespace-default-deny* policy per namespace, applied by a controller (Kyverno generate, ArgoCD app-of-apps).
- One *app-specific* policy per workload, written by the app team, listing the workload's specific peers.

The union of these gives every pod a sane baseline (DNS + apiserver) plus the app team's needs.

---

## 7. Egress and the DNS Problem

The single most common production breakage is "I applied default-deny-egress and now my pods can't resolve DNS."

Pods resolve `service.namespace.svc.cluster.local` (and external names) by sending a UDP query to the CoreDNS service VIP (typically `10.96.0.10:53` or whatever cluster IP your kube-dns Service has). With default-deny-egress, that UDP packet is dropped at the egress hook on the client pod's veth, *before* it ever reaches the kube-proxy chain that would DNAT it.

Symptoms: `dig`, `nslookup`, every HTTP client, every database driver, all hang for 5-30 seconds (the resolver's retry budget) before failing with "no such host" or "connection refused" against the wrong IP (because some resolvers fall back to `/etc/hosts` or to literal IP guesses).

### 7.1 The Mandatory DNS Egress Allow

Every default-deny-egress policy must be accompanied by an explicit allow for CoreDNS. The canonical form:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-coredns-egress
  namespace: payments
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

Notice:

- **Both UDP and TCP port 53.** UDP for normal queries, TCP for responses too large for UDP (>512 bytes by default; DNSSEC, EDNS0, and large records all force TCP fallback).
- **`namespaceSelector` AND `podSelector` in the SAME peer item.** This is the AND form: we want pods labelled `k8s-app: kube-dns` *in the* `kube-system` namespace. The OR form would let *any* pod in kube-system serve DNS, which is almost certainly not what you want.
- **The `k8s-app: kube-dns` label** is standard for both CoreDNS and the older kube-dns. Verify it on your cluster: `kubectl -n kube-system get pods -l k8s-app=kube-dns`.

### 7.2 What Happens to the Service VIP?

The egress packet is destined for `10.96.0.10:53` (the cluster IP). NetworkPolicy on the source side evaluates the *final* destination — *after* the kube-proxy DNAT step — on most CNIs. Cilium and Calico both resolve service VIPs into endpoint IPs before applying egress policy, so a `to: { podSelector: { k8s-app: kube-dns } }` will match the actual CoreDNS pod IPs.

This is implementation-defined and not part of the API contract. Older Calico (<3.20) and some Antrea versions applied egress policy against the service VIP, requiring you to write `to: { ipBlock: { cidr: 10.96.0.10/32 } }` instead. Test before you trust.

### 7.3 NodeLocal DNSCache

If your cluster uses NodeLocal DNSCache (the `node-local-dns` DaemonSet on each node, listening on `169.254.20.10`), the pod's resolver is configured to query the local cache, not the cluster CoreDNS service. The egress rule must change accordingly:

```yaml
egress:
  - to:
      - ipBlock:
          cidr: 169.254.20.10/32
    ports:
      - protocol: UDP
        port: 53
      - protocol: TCP
        port: 53
```

`169.254.20.10` is a link-local IP, not a pod IP, not a service IP — it lives on the loopback device of every node. Most CNIs handle this specially; some don't and require a HostEndpoint rule. Verify with `kubectl debug node/... -- iptables -L`.

### 7.4 DNS as an Exfiltration Channel

Even with DNS egress allowed, a compromised pod can encode arbitrary data in subdomain labels — `<base64-data>.attacker.com` — and the cluster CoreDNS will faithfully forward those queries to the upstream resolver, which forwards them to the attacker. NP cannot prevent this; CoreDNS-level filtering or Cilium DNS visibility can.

Cilium provides `toFQDNs` rules that *whitelist* the FQDNs a pod may resolve, and the Cilium DNS proxy enforces this by sniffing DNS responses at the CoreDNS sidecar. See §11.

---

## 8. NetworkPolicy Enforcement by CNI

The NetworkPolicy API is uniform; the implementation is not. Knowing which CNI you have and how it enforces is essential for debugging and for understanding which extensions are available.

### 8.1 Calico (Felix + iptables / eBPF)

Calico's enforcement agent is **Felix**, a DaemonSet that runs on every node. Felix watches the Kubernetes API for NetworkPolicy, Pod, Namespace, and Endpoints, plus Calico's own `GlobalNetworkPolicy`, `NetworkPolicy` (Calico CRD, different from k8s NetworkPolicy), `HostEndpoint`, `WorkloadEndpoint`, `IPPool`, and `Profile` objects.

The default dataplane is iptables. Felix translates policy into iptables rules in a separate `cali-*` chain, hooked into FORWARD, INPUT, and OUTPUT. Pod-to-pod traffic on the same node traverses a `cali-fw-<endpoint-id>` chain on egress from the source pod's veth and a `cali-tw-<endpoint-id>` chain on ingress to the destination.

Felix uses **ipsets** (Linux kernel hashed IP sets) to translate label selectors into IP membership lists. When a label selector matches 10,000 pods, Felix maintains an ipset with 10,000 entries; the iptables rule becomes `-m set --match-set cali40s:foo src` (an O(1) hash lookup) instead of 10,000 individual rules.

```
┌─────────────────────────────────────────────────────────────────────────┐
│              CALICO IPTABLES ENFORCEMENT (simplified)                    │
│                                                                          │
│  kubectl apply -f netpol.yaml                                            │
│       │                                                                  │
│       ▼                                                                  │
│  kube-apiserver  ──watch──►  Felix on every node                         │
│                                  │                                       │
│                                  ├── compute pods matching selectors    │
│                                  ├── compute IP membership per selector │
│                                  ├── update ipsets:                     │
│                                  │     ipset add cali40s:abc <pod-ip>   │
│                                  └── write iptables rules:              │
│                                        -A cali-tw-eth0 -m set           │
│                                        --match-set cali40s:abc src      │
│                                        -j MARK --set 0x10000            │
│                                        -A cali-tw-eth0 -m mark          │
│                                        --mark 0x10000/0x10000 -j ACCEPT │
│                                                                          │
│       │                                                                  │
│       ▼                                                                  │
│  Pod packet ──► veth ──► cali-fw-... ──► cali-tw-... ──► dest pod      │
└─────────────────────────────────────────────────────────────────────────┘
```

The Felix source code lives at `projectcalico/calico` (formerly `projectcalico/felix`), with the iptables generation in `felix/iptables/` and the policy-to-rule logic in `felix/dataplane/linux/`.

The eBPF dataplane mode (Calico 3.13+) replaces the iptables steps with eBPF programs attached at the TC layer of each veth. Policy is compiled to eBPF maps; the per-packet path is shorter than iptables for very large rule sets.

### 8.2 Cilium (Identity + eBPF Maps)

Cilium does not use IP-based selectors at all. It assigns each pod a numeric **security identity** (a 16-bit integer) derived from a canonicalized set of "security-relevant labels," and stores the mapping `identity → labels` in a cluster-wide KVStore (etcd or the kvstore CRD).

Policy is compiled into a per-endpoint **policy map** — an eBPF hash map keyed by `(identity, port, protocol, direction)`. On every packet, the eBPF program loaded onto the veth looks up the packet's source identity (from a packet's source IP via another map) and consults the policy map. Allow or drop, in O(1) per packet, with no iptables involvement.

```
┌─────────────────────────────────────────────────────────────────────────┐
│              CILIUM IDENTITY-BASED ENFORCEMENT                           │
│                                                                          │
│  Pod labels:                                                             │
│    {app=frontend, env=prod, team=web}                                    │
│       │                                                                  │
│       │  canonicalize → hash → assign numeric identity 42                │
│       ▼                                                                  │
│  Identity 42                                                             │
│       │                                                                  │
│       ▼                                                                  │
│  CiliumIdentity CRD: 42 → {app=frontend, env=prod, team=web}             │
│                                                                          │
│  Pod IP 10.244.0.5 ──► identity 42 (stored in ipcache map)              │
│                                                                          │
│  Per-pod policy map (BPF_MAP_TYPE_HASH):                                 │
│    key: (identity=42, port=8080, proto=tcp, dir=ingress)                │
│    val: allow                                                            │
│                                                                          │
│  Packet arrives at dest pod's veth                                       │
│    1. eBPF reads src IP from packet                                      │
│    2. ipcache lookup: 10.244.0.5 → identity 42                          │
│    3. policy map lookup: (42, 8080, tcp, ingress) → allow                │
│    4. accept                                                             │
└─────────────────────────────────────────────────────────────────────────┘
```

The Cilium policy code lives at `cilium/cilium/pkg/policy/`. The identity allocator is in `pkg/identity/`. The eBPF map types are defined in `bpf/lib/maps.h`. See chapter 16 for the deep eBPF dive.

The identity model has profound consequences:

- Adding a new pod with an *existing* identity (same labels as 1000 other pods) does **not** require any policy recomputation. The identity already has its row in the policy map; the pod just joins.
- Adding a new pod with a *new* identity triggers identity allocation, ipcache update, and *potentially* policy recomputation if the new identity matches a selector in some policy. This is far less common at steady state.
- A pod's identity is *not* its pod IP. Two pods with the same labels share an identity. This is how Cilium scales to 100k pods without iptables blowing up.

### 8.3 Flannel (No-op)

Flannel has no NetworkPolicy implementation. `kubectl apply -f netpol.yaml` succeeds (the apiserver stores the object), but no enforcement happens. This is a recurring CKAD/CKA exam trap and a real production CVE pattern.

The standard fix is to run **Flannel + Calico-policy-only** mode: Flannel handles IPAM and overlay, Calico runs in `kubernetes-backend` mode without managing IPAM, just enforcement. The combination is called "Canal" historically; modern installs typically just install Calico standalone or use Cilium.

If you `kubectl get networkpolicy -A` and see policies but `kubectl exec -- nc -zv blocked-host port` succeeds, suspect Flannel-only.

### 8.4 AWS VPC CNI

The AWS VPC CNI assigns each pod a routable VPC IP (no overlay). It does **not** implement NetworkPolicy itself. The two production options:

- **VPC CNI + Calico policy-only.** Install Calico with `CALICO_NETWORKING_BACKEND=none` and `CLUSTER_TYPE=k8s`. Calico's Felix watches NetworkPolicy and programs iptables on each node. Connectivity stays via VPC CNI; enforcement via Calico.
- **VPC CNI + Cilium chained.** EKS 1.21+ supports chaining Cilium on top of VPC CNI. Cilium handles policy and observability; VPC CNI handles IPAM. The Cilium documentation calls this "AWS ENI mode with chaining."
- **EKS native: VPC CNI's own NetworkPolicy support.** Added in late 2023, the VPC CNI agent now supports vanilla NP enforcement using eBPF programs attached at the ENI's TC layer. It does not support Calico/Cilium extensions; just the upstream API. Enable via `enableNetworkPolicy: true`.

### 8.5 Other CNIs

- **Antrea** (VMware): implements NetworkPolicy + its own `AntreaNetworkPolicy` and `ClusterNetworkPolicy` with tier support. Open-vSwitch dataplane.
- **kube-router**: implements NetworkPolicy via iptables and ipset, similar to Calico but lighter weight. No CRD extensions.
- **Weave Net**: implements NetworkPolicy via its own ulogd-style daemon. Has been declining in popularity since the project lost active maintenance.
- **Kindnet** (used by `kind`): no NetworkPolicy enforcement. Same trap as Flannel. Use Cilium/Calico in `kind` if you're testing NP.

### 8.6 The Compatibility Matrix

| CNI                | Vanilla NP | endPort | SCTP | Named ports | Notes                            |
|--------------------|------------|---------|------|-------------|----------------------------------|
| Calico             | ✅         | ✅      | ✅   | ✅          | + GlobalNetworkPolicy + Tiers    |
| Cilium             | ✅         | ✅      | ❌*  | ✅          | + CNP/CCNP + L7 + FQDN           |
| Flannel            | ❌         | n/a     | n/a  | n/a         | No enforcement                   |
| AWS VPC CNI        | ✅ (1.14+) | ✅      | ❌   | partial     | Native eBPF since v1.14.0        |
| Antrea             | ✅         | ✅      | ✅   | ✅          | + AntreaNetworkPolicy            |
| kube-router        | ✅         | partial | ❌   | partial     | Lightweight                      |
| Weave (EOL)        | ✅         | ❌      | ❌   | partial     | Limited maintenance              |

(* Cilium has had partial SCTP support gated behind feature flags; verify version-specific.)

---

## 9. Calico GlobalNetworkPolicy and HostEndpoints

Vanilla NetworkPolicy has two structural limits:

- It is **namespaced**, so you cannot write a single object that applies cluster-wide.
- It selects **pods**, so it cannot regulate traffic to/from the node itself (kubelet, the host network, NodePort kube-proxy chain, host-network pods).

Calico's `GlobalNetworkPolicy` and `HostEndpoint` CRDs fix both.

### 9.1 GlobalNetworkPolicy

A cluster-scoped object that selects workload endpoints (pods) and host endpoints (nodes) by label. It supports the same matching primitives as Calico's namespaced `NetworkPolicy` CRD plus a few extras: `tier` membership, `order` (a float used to break ties within a tier), an `action` field (`Allow | Deny | Log | Pass`), and `serviceAccountSelector`.

```yaml
apiVersion: projectcalico.org/v3
kind: GlobalNetworkPolicy
metadata:
  name: deny-egress-to-metadata-service
spec:
  tier: platform-floor          # see §10 on tiers
  order: 100                    # lower = evaluated first within tier
  selector: all()               # every workload endpoint
  types: [Egress]
  egress:
    - action: Deny
      destination:
        nets:
          - 169.254.169.254/32  # cloud metadata service
    - action: Pass              # let lower tiers decide for everything else
```

Things to note:

- **`action: Deny`.** GlobalNetworkPolicy has explicit deny; vanilla NP does not. This is the headline feature.
- **`tier: platform-floor`.** Calico evaluates policies in tier order (§10). The default tier is `default`. Tiers above default are evaluated first.
- **`order: 100`.** Within a tier, lower order is evaluated first. Ties are broken by name.
- **`selector: all()`.** Calico's selector syntax is a CEL-like expression language, not a `matchLabels` map. `all()` matches everything; `app == 'foo' && env == 'prod'` is the equality form; `has(team)` checks for label existence.
- **`action: Pass`.** Falls through to the next tier (§14). Without `Pass`, the policy *implicitly denies* anything not explicitly allowed within the tier.

The Felix code that compiles GlobalNetworkPolicy is at `projectcalico/calico/felix/policy/` (search for `globalNetworkPolicy`).

### 9.2 HostEndpoint

A `HostEndpoint` CRD represents the *node's* network interface (typically `eth0`). It is what allows Calico policy to apply to traffic entering or leaving the node itself, not just pods.

```yaml
apiVersion: projectcalico.org/v3
kind: HostEndpoint
metadata:
  name: node-eth0
  labels:
    role: worker
spec:
  node: ip-10-0-1-23.ec2.internal
  interfaceName: eth0
  expectedIPs:
    - 10.0.1.23
```

Once a HostEndpoint exists, GlobalNetworkPolicies that select it apply to packets entering/leaving that interface — *including* packets to/from host-network pods, NodePort traffic, kubelet on port 10250, sshd on port 22, and everything else on the node.

This is also the **biggest footgun** in Calico: applying a default-deny policy to a HostEndpoint can sever kubelet from the apiserver. The recommended idiom is to use `failsafePorts` and `applyOnForward: false` carefully:

```yaml
apiVersion: projectcalico.org/v3
kind: GlobalNetworkPolicy
metadata:
  name: host-default-deny
spec:
  selector: has(role) && role == 'worker'
  applyOnForward: true     # also apply to forwarded packets (pods on this node)
  preDNAT: false           # don't apply before DNAT (use for kube-proxy interactions)
  doNotTrack: false        # use raw table (untracked) — for very high-throughput hosts
  types: [Ingress, Egress]
  ingress: []              # implicit deny
  egress: []
```

The Calico `FelixConfiguration` resource has a `FailsafeInboundHostPorts` field; ports listed there are *never* dropped regardless of policy. Default includes 22, 53, 67, 68, 179, 5473, 6443, 2379, 2380. Always verify before applying a global default-deny.

### 9.3 When to Use GlobalNetworkPolicy

- **Platform invariants** that no tenant may override: "no pod may reach 169.254.169.254," "no pod outside kube-system may reach 10.96.0.1 (apiserver) except via the in-cluster service."
- **Host-level firewall** rules: "only the bastion VPC may SSH to nodes," "only the load balancer subnet may hit NodePort range."
- **Cross-namespace cluster-wide allows**: "every namespace's pods may reach metrics-server in the monitoring namespace on port 4443."

Use namespaced NetworkPolicy for application-team-owned rules. Use GlobalNetworkPolicy for platform-team-owned rules. The tier model (§10) makes the boundary enforceable.

---

## 10. Calico Tiers: Ordered Evaluation

Tiers are Calico's mechanism for **layering** authority. Each tier is a named bucket of policies; tiers are evaluated in priority order; within a tier, policies are evaluated in `order`. The first policy to match the packet wins — its action (`Allow | Deny | Pass`) decides the packet's fate, *except* that `Pass` falls through to the next tier.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       CALICO TIER EVALUATION                             │
│                                                                          │
│   Packet arrives at workload endpoint                                    │
│       │                                                                  │
│       ▼                                                                  │
│   ┌─────────────────────┐                                                │
│   │ Tier: security      │ order=0 (highest priority)                     │
│   │ - deny-metadata     │ ──► matches? Deny ──► drop                     │
│   │ - allow-monitoring  │                                                │
│   └────────┬────────────┘                                                │
│            │ no match, OR Pass                                           │
│            ▼                                                             │
│   ┌─────────────────────┐                                                │
│   │ Tier: platform      │ order=100                                      │
│   │ - allow-dns         │ ──► matches? Allow ──► accept                  │
│   │ - allow-apiserver   │                                                │
│   └────────┬────────────┘                                                │
│            │ no match, OR Pass                                           │
│            ▼                                                             │
│   ┌─────────────────────┐                                                │
│   │ Tier: default       │ order=1000 (always last)                       │
│   │ - tenant policies   │ ──► matches? Allow/Deny                        │
│   └────────┬────────────┘                                                │
│            │ no match within tier                                        │
│            ▼                                                             │
│   ┌─────────────────────┐                                                │
│   │ Implicit deny       │                                                │
│   │ (if any tier        │                                                │
│   │  selected the       │                                                │
│   │  endpoint)          │                                                │
│   └─────────────────────┘                                                │
└─────────────────────────────────────────────────────────────────────────┘
```

### 10.1 Tier Object

```yaml
apiVersion: projectcalico.org/v3
kind: Tier
metadata:
  name: security
spec:
  order: 0          # tiers evaluated in ascending order
```

Tiers are sparse: a packet that does not match any policy in a tier *and* is not selected by any policy in that tier falls through to the next tier without any deny. Only if at least one policy in a tier *selects the endpoint* but no policy *matches the packet* does the tier's implicit-deny fire.

### 10.2 The Standard Three-Tier Model

The recommended layout in a multi-tenant Calico cluster:

- **`security` tier (order 0).** Owned by the security team. RBAC restricts who may write policies in this tier. Contains absolute denies (metadata service, cross-cluster), absolute allows (audit log shipping), and policies that may safely `Pass` if not matched.
- **`platform` tier (order 100).** Owned by the platform team. Contains the "every pod must allow DNS to CoreDNS, apiserver, and image-pulls" rules. Ends with `Pass` actions.
- **`default` tier (order 1000).** Owned by tenant teams. Each team writes their own `NetworkPolicy` and Calico `NetworkPolicy` here. Default-deny lives here.

```yaml
# In tier 'security': deny pod-to-cloud-metadata
apiVersion: projectcalico.org/v3
kind: GlobalNetworkPolicy
metadata:
  name: deny-metadata
spec:
  tier: security
  order: 10
  selector: all()
  types: [Egress]
  egress:
    - action: Deny
      destination:
        nets: [169.254.169.254/32]
    - action: Pass         # everything else: defer to next tier
```

```yaml
# In tier 'platform': allow DNS for every pod
apiVersion: projectcalico.org/v3
kind: GlobalNetworkPolicy
metadata:
  name: platform-allow-dns
spec:
  tier: platform
  order: 10
  selector: all()
  types: [Egress]
  egress:
    - action: Allow
      destination:
        selector: k8s-app == 'kube-dns'
      protocol: UDP
      destination.ports: [53]
    - action: Pass         # other egress: defer to default tier
```

The combined effect: every pod is *denied* metadata-service access (by security tier), *allowed* DNS (by platform tier), and *otherwise* subject to whatever the tenant team wrote in the default tier.

### 10.3 Tier Mistakes

- **Forgetting `Pass`.** A policy in a tier with no `Pass` action causes implicit deny at the end of that tier for any endpoint the tier selects. If you wrote `selector: all()` and no `Pass` rule, you have just default-denied every workload endpoint in the cluster.
- **Wrong tier order.** Lower `order` evaluates first. The `security` tier should be `order: 0` (or 10), not `order: 1000`. A common typo is `order: 100` for `security` and `order: 10` for `default`, which inverts the model.
- **RBAC drift.** Tiers are useless if every tenant can write to the security tier. Use Kubernetes RBAC to restrict the `tier.projectcalico.org` resource:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: tenant-policy-writer
rules:
  - apiGroups: ["projectcalico.org"]
    resources: ["networkpolicies", "tier.networkpolicies"]
    resourceNames: ["default"]    # only the default tier
    verbs: ["create", "update", "patch", "delete", "get", "list", "watch"]
```

---

## 11. Cilium CiliumNetworkPolicy: L7 and FQDN

CiliumNetworkPolicy (CNP) is Cilium's superset of vanilla NetworkPolicy. It is namespaced, and adds:

- **L7 rules** for HTTP, gRPC, Kafka, and DNS.
- **`toFQDNs`** for DNS-based egress allowlists.
- **`toEntities`** for special destinations: `host`, `world`, `cluster`, `kube-apiserver`, `health`, `unmanaged`.
- **`fromCIDRSet` / `toCIDRSet`** with `except` (similar to ipBlock).
- **`serviceAccount` selectors.**
- **ICMP rules.**
- **Egress to L4 services with HTTP rewriting** (rare; mostly a mesh feature).

CNP is the daily-driver policy object in any Cilium cluster.

### 11.1 The CNP Shape

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: frontend-egress
  namespace: web
spec:
  endpointSelector:
    matchLabels:
      app: frontend
  egress:
    # Allow DNS (with FQDN sniffing)
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: UDP
          rules:
            dns:
              - matchPattern: "*"
    # Allow HTTPS to specific FQDNs
    - toFQDNs:
        - matchName: api.stripe.com
        - matchPattern: "*.amazonaws.com"
      toPorts:
        - ports:
            - port: "443"
              protocol: TCP
    # Allow GET-only to internal API
    - toEndpoints:
        - matchLabels:
            app: api
      toPorts:
        - ports:
            - port: "8080"
              protocol: TCP
          rules:
            http:
              - method: "GET"
                path: "/v1/.*"
```

### 11.2 L7 Rules: HTTP

The `http` rules let you allow only specific HTTP methods, paths, and headers. Under the hood, Cilium installs an **Envoy proxy** (or a built-in Go HTTP parser for simple rules) into the datapath. The eBPF program redirects matching packets to the proxy via a TPROXY hook; the proxy parses HTTP, applies rules, and forwards or drops.

```yaml
rules:
  http:
    - method: "GET"
      path: "/v1/users/.*"
    - method: "POST"
      path: "/v1/users"
      headers:
        - "X-Api-Version: v1"
```

This is genuine L7 enforcement: the path `/v1/users/123` is allowed but `/v1/users/123/delete` is not (unless it matches another rule), and `DELETE /v1/users/123` will be dropped even if `/v1/users/.*` is in the path list.

Caveat: L7 visibility requires that the traffic *not be TLS-encrypted to Cilium*. If the pod opens HTTPS directly to the destination, Cilium sees only encrypted bytes; the HTTP rules cannot apply. There are three ways around this:

- Use Cilium's mTLS termination at the proxy (sidecar-less mesh; chapter 16).
- Terminate TLS at an ingress proxy/sidecar, with Cilium policy applied to the plaintext side.
- Use SNI-based filtering (`tls: {sni: api.stripe.com}`) in CNP — this matches on the TLS Client Hello, not on HTTP. Less granular but no TLS termination needed.

### 11.3 L7 Rules: Kafka

```yaml
rules:
  kafka:
    - role: "produce"
      topic: "orders"
    - role: "consume"
      topic: "orders"
      clientID: "order-processor"
```

Cilium parses Kafka wire protocol via its Envoy filter. Useful for restricting consumer/producer access by topic.

### 11.4 L7 Rules: DNS Visibility

The `dns` rule is essential for `toFQDNs`. It tells Cilium to sniff DNS responses and remember which IPs were resolved for which names:

```yaml
- toEndpoints:
    - matchLabels:
        k8s-app: kube-dns
        io.kubernetes.pod.namespace: kube-system
  toPorts:
    - ports:
        - port: "53"
          protocol: UDP
      rules:
        dns:
          - matchPattern: "*.stripe.com"
          - matchName: "api.example.com"
```

Cilium then enforces that only DNS queries matching the patterns are forwarded; the resolved IPs are cached and added to a per-pod "allowed CIDR" set, which the L4 policy can match against. This is how `toFQDNs` works in practice — it is implemented as a cooperative DNS-snooping + L4 dynamic-CIDR mechanism, not as a static FQDN matcher.

### 11.5 `toFQDNs`

```yaml
egress:
  - toFQDNs:
      - matchName: "api.stripe.com"
      - matchPattern: "*.s3.amazonaws.com"
    toPorts:
      - ports:
          - port: "443"
            protocol: TCP
```

This requires that the DNS-visibility rule (above) be in place too — Cilium needs to see the DNS responses to populate the dynamic CIDR set. Without DNS visibility, `toFQDNs` will not work.

The **FQDN bypass via direct IP** trap: a compromised pod that knows the IP of `api.stripe.com` (`13.225.139.111` or whatever) can dial it directly via L4, bypassing DNS — and `toFQDNs` will *not* allow it because that IP was never resolved through the snooped DNS. This is by design — but operators sometimes assume `toFQDNs` is an IP-based allowlist when it is actually a DNS-correlated one. Direct-IP traffic falls through to L4 policy.

### 11.6 `toEntities`

Cilium has a small set of named "entities" representing classes of destination:

- `host` — the local node's host namespace.
- `remote-node` — other nodes' host namespaces.
- `world` — anything outside the cluster (the internet).
- `cluster` — anything inside the cluster.
- `init` — pods that haven't been assigned an identity yet (rare).
- `unmanaged` — pods not under Cilium control (legacy).
- `health` — Cilium's own health endpoints.
- `kube-apiserver` — the Kubernetes API server (special handling because its IP can move).

```yaml
egress:
  - toEntities:
      - kube-apiserver
    toPorts:
      - ports:
          - port: "443"
            protocol: TCP
```

`toEntities: [kube-apiserver]` is the canonical way to allow pods to talk to the apiserver without hardcoding its IP, which can change on managed cloud K8s after control-plane upgrades.

### 11.7 ICMP Rules

```yaml
egress:
  - toEntities: [world]
    icmps:
      - fields:
          - type: 8     # echo request
            family: IPv4
```

Vanilla NP cannot match ICMP at all (the API only knows TCP/UDP/SCTP). CNP can. Useful for liveness probes from external load balancers, or for explicitly *denying* outbound ping.

### 11.8 `serviceAccountSelector`

CNP allows selecting by **ServiceAccount** rather than label, which is more identity-correlated than labels (labels are mutable; SA names are tied to RBAC):

```yaml
endpointSelector:
  matchLabels:
    "io.cilium.k8s.policy.serviceaccount": "payments-sa"
```

Cilium auto-injects the label `io.cilium.k8s.policy.serviceaccount: <sa-name>` on every pod, so you can select by SA via `matchLabels`. This is the closest vanilla approximation; for cluster-wide SA-based selection, CCNP (next section) has `serviceAccounts` as a top-level peer kind.

---

## 12. CiliumClusterwideNetworkPolicy

`CiliumClusterwideNetworkPolicy` (CCNP) is the cluster-scoped twin of CNP. Same fields, same L7 features, but the `endpointSelector` matches across all namespaces, not just one.

```yaml
apiVersion: cilium.io/v2
kind: CiliumClusterwideNetworkPolicy
metadata:
  name: cluster-default-deny
spec:
  endpointSelector: {}        # every endpoint
  ingressDeny:                # NEW: explicit deny
    - fromEntities: [world]
  egress:
    - toEntities: [kube-apiserver]
      toPorts:
        - ports: [{ port: "443", protocol: TCP }]
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports: [{ port: "53", protocol: UDP }]
        rules:
          dns:
            - matchPattern: "*"
```

The `ingressDeny` / `egressDeny` fields (Cilium 1.11+) are the explicit-deny mechanism, analogous to Calico's `action: Deny`. They are *strictly stronger* than implicit deny — a deny rule blocks traffic even if another CNP/CCNP allows it. This breaks the strict union semantics that vanilla NP has, but the break is precise and well-documented.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                  CILIUM POLICY COMPOSITION                               │
│                                                                          │
│   For each (endpoint, direction):                                        │
│                                                                          │
│     1. Compute all matching CCNP and CNP policies                        │
│     2. If any *Deny rule matches the packet → DROP                       │
│     3. Else if any *allow rule matches → ACCEPT                          │
│     4. Else if any policy selected the endpoint → DROP (implicit)        │
│     5. Else → ACCEPT (no policy selects this endpoint)                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 12.1 CCNP Use Cases

- **Cluster-wide default-deny** without writing a NetworkPolicy in every namespace.
- **Platform-team baseline rules** that no tenant may override (e.g., `egressDeny: { toCIDR: [169.254.169.254/32] }`).
- **Cross-namespace allows** that are too broad for a namespaced policy (e.g., "every pod may talk to monitoring/prometheus on port 9090").

---

## 13. AdminNetworkPolicy and BaselineAdminNetworkPolicy (1.29+)

For years, the gap was painful: Calico and Cilium each had a cluster-wide, deny-capable, priority-ordered policy object, but they were *vendor-specific*. A workload portable across both CNIs could not use either. The upstream solution is `AdminNetworkPolicy` (ANP) and `BaselineAdminNetworkPolicy` (BANP), defined by the `network-policy-api` working group (kubernetes-sigs/network-policy-api), beta in 1.29, GA target 1.31+.

The objects live in API group `policy.networking.k8s.io/v1alpha1`. Verify your CNI implements them — as of late 2024, Cilium (1.14+), Calico (3.27+), Antrea, and OVN-Kubernetes all have partial or complete support.

### 13.1 AdminNetworkPolicy (ANP)

Cluster-scoped, priority-ordered (integer; lower = higher priority), three actions: `Allow`, `Deny`, `Pass`. Selects subjects by `namespaces` (a namespace selector) and/or `pods` (a namespace + pod selector pair).

```yaml
apiVersion: policy.networking.k8s.io/v1alpha1
kind: AdminNetworkPolicy
metadata:
  name: deny-egress-to-metadata
spec:
  priority: 10        # lower = evaluated first
  subject:
    namespaces: {}    # all namespaces
  egress:
    - name: "deny-metadata"
      action: Deny
      to:
        - networks:
            - 169.254.169.254/32
    - name: "allow-dns"
      action: Allow
      to:
        - pods:
            namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: kube-system
            podSelector:
              matchLabels:
                k8s-app: kube-dns
      ports:
        - portNumber:
            protocol: UDP
            port: 53
```

Key points:

- **`priority`** is an integer from 0 to 1000. Lower priorities are evaluated first.
- **`subject.namespaces`** vs **`subject.pods`** — choose one. The shape is more explicit than vanilla NP's overloaded peer kinds.
- **`action`** is mandatory on each rule. `Allow`, `Deny`, or `Pass`.
- **`Pass`** falls through to the next-lower-priority ANP, then to NetworkPolicy, then to BANP. See §14.
- **Peers** in `to`/`from` use distinct keys: `pods`, `namespaces`, `networks` (CIDRs), `nodes` (NodeSelector). This is much clearer than the overloaded vanilla NP peer.

### 13.2 BaselineAdminNetworkPolicy (BANP)

Cluster-scoped, **at most one** per cluster (singleton; name must be `default`). It is evaluated *last*, after all ANP and after all NetworkPolicy. It is the "default if nothing else decided" layer.

```yaml
apiVersion: policy.networking.k8s.io/v1alpha1
kind: BaselineAdminNetworkPolicy
metadata:
  name: default
spec:
  subject:
    namespaces: {}
  ingress:
    - name: "deny-all-ingress-by-default"
      action: Deny
      from:
        - networks:
            - 0.0.0.0/0
  egress:
    - name: "deny-all-egress-by-default"
      action: Deny
      to:
        - networks:
            - 0.0.0.0/0
```

BANP provides cluster-wide default-deny that tenant NetworkPolicy can *override on a per-pod basis* without having to write a default-deny in every namespace. This is the single most useful feature for platform teams.

### 13.3 Evaluation Order

```
┌─────────────────────────────────────────────────────────────────────────┐
│            ANP / NP / BANP EVALUATION ORDER (1.29+)                      │
│                                                                          │
│   For each packet:                                                       │
│                                                                          │
│   1. Match against all ANPs in PRIORITY ORDER (low first).               │
│        For each matching rule:                                           │
│          - Allow ──► ACCEPT, stop                                        │
│          - Deny  ──► DROP, stop                                          │
│          - Pass  ──► move to step 2                                      │
│        If no ANP matches the packet ──► move to step 2                   │
│                                                                          │
│   2. Match against all NetworkPolicy (v1) for this pod/direction.        │
│        UNION of allows applies:                                          │
│          - If any rule allows ──► ACCEPT, stop                           │
│          - If any policy selected the pod/dir but no rule matched        │
│              ──► move to step 3 (implicit-deny-but-defer-to-BANP)        │
│          - If no policy selects the pod/dir ──► move to step 3           │
│                                                                          │
│   3. Match against BANP (singleton).                                     │
│          - Allow ──► ACCEPT                                              │
│          - Deny  ──► DROP                                                │
│          - No match ──► step 4                                           │
│                                                                          │
│   4. IMPLICIT ALLOW.                                                     │
│        (i.e., absent any opinion from any layer, the packet passes.)    │
└─────────────────────────────────────────────────────────────────────────┘
```

Note carefully:

- ANP can **terminate** with an Allow or Deny — NP and BANP never see the packet.
- NetworkPolicy's implicit deny *only applies within the NP layer*. If NP selects the pod and nothing matches, the packet does **not** stop at "NP says drop"; it falls through to BANP. This is a change from vanilla-NP-alone, where NP's implicit deny was the terminal step.
- BANP is the actual cluster-wide default. If you want pods to be denied by default *only if no NP selects them*, use BANP-Deny + tenant NPs that allow what they need.

### 13.4 The Pass Action

`Pass` in an ANP means: "I refuse to decide; defer to the next layer." This is critical for letting platform-team ANPs coexist with tenant NPs.

Example: a platform team wants to *deny* pod-to-metadata-service at the ANP layer, *allow* DNS for everyone, and *defer* everything else to tenant policy.

```yaml
apiVersion: policy.networking.k8s.io/v1alpha1
kind: AdminNetworkPolicy
metadata:
  name: platform-baseline
spec:
  priority: 10
  subject:
    namespaces: {}
  egress:
    - action: Deny
      to:
        - networks: [169.254.169.254/32]
    - action: Allow
      to:
        - pods:
            namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
            podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports:
        - portNumber: { protocol: UDP, port: 53 }
    - action: Pass
      to:
        - networks: [0.0.0.0/0]    # everything else — defer to NP
```

Tenants can then write namespaced NetworkPolicy that further restricts or allows. The platform's deny on `169.254.169.254/32` is non-overridable; everything else is tenant-decided.

### 13.5 Source Code Pointers

- `kubernetes-sigs/network-policy-api` — the API definitions, conformance suite, examples.
- `cilium/cilium/pkg/policy/api/` — Cilium's ANP/BANP translation layer.
- `projectcalico/calico/libcalico-go/lib/apis/v3/` — Calico's ANP/BANP support.

The conformance test at `kubernetes-sigs/network-policy-api/conformance/` is the source of truth for what "implementing ANP" means.

---

## 14. The Pass Action and Layered Authority

The `Pass` action is the conceptual heart of the layered model. It is the mechanism by which a higher-authority layer can say "this decision is not mine to make; ask the next layer."

The same idea exists in Calico (an `action: Pass` policy in a tier falls through to the next tier) and in the upstream ANP. It is the *only* way to compose layered authority without forcing every layer to know about every other.

### 14.1 Why Pass Is Needed

Imagine a security team writes "deny pod-to-metadata-service for every pod in every namespace." Without Pass, that policy must also *allow* every other peer that any tenant could ever need, because the policy must be the sole decider for the packets it matches. That is operationally impossible.

With Pass, the security team writes:

- Deny metadata service.
- Pass everything else.

And tenant policies fill in the rest. The security team's invariant (no metadata access) is unbreakable; the tenant's flexibility (whatever else they need) is preserved.

### 14.2 Pass vs Implicit Deny

In vanilla NP, the absence of a matching allow rule is an implicit deny. In ANP, the absence of a matching rule is *fallthrough* to the next layer; only an explicit Deny rule drops. This is a deliberate semantic shift: ANP is composable, NP is not.

### 14.3 Common Pass Patterns

- **Platform default at high priority**: deny known-bad destinations (metadata service, public bastion ranges, peer cloud VPCs), then `Pass` everything else.
- **Per-team ANPs at mid priority**: allow team-specific cross-namespace patterns, then `Pass` to fall through to namespaced NP.
- **BANP default-deny**: catch everything not allowed by anyone with a final Deny.

```yaml
# Top-level: deny exfil to internet from PCI namespaces
priority: 5
subject:
  namespaces:
    matchLabels: { compliance: pci }
egress:
  - action: Deny
    to: [{ networks: [0.0.0.0/0] }]
    notPorts:                        # ANP supports notPorts to allow loopback-y exceptions
      - portNumber: { protocol: TCP, port: 443 }
        # ...wait, this is too permissive. Be careful.
```

(The above is illustrative of how a too-broad ANP can create a footgun. Most teams use Allow + Pass + BANP-Deny rather than a top-down Deny.)

---

## 15. Egress Gateways

NetworkPolicy lets you restrict *which destinations* a pod may reach. It does **not** let you control which source IP that traffic appears as to the destination. For external services that authenticate by source IP — many legacy APIs, on-prem databases reached over VPN, partner services, IP-allowlisted SaaS — you need every pod's outbound traffic to appear from a small, stable set of IPs, regardless of which node the pod runs on.

This is the egress gateway problem.

### 15.1 The Conventional Solution: Cloud NAT

In a managed cluster on a cloud provider, outbound pod traffic typically goes:

```
Pod (10.244.x.x) ──SNAT──► Node (10.0.x.x) ──VPC route──► NAT Gateway (3.4.5.6) ──► Internet
```

The NAT Gateway IP is stable. Every pod appears to come from `3.4.5.6` (or a small pool). This is the cheapest solution and the right one for "we just need a stable external IP."

But it has limits:

- One NAT IP per AZ; cross-AZ failover may shift the apparent source.
- The IP belongs to the whole cluster (or VPC); no per-namespace differentiation.
- No selector — you cannot say "only payments pods get the special IP."

For per-tenant egress IPs you need an egress gateway.

### 15.2 Calico EgressGateway

Calico EgressGateway runs a *gateway pod* (or DaemonSet) on dedicated nodes; tenant pods are configured to route their outbound traffic through the gateway pod, which SNATs to its own IP.

```yaml
apiVersion: projectcalico.org/v3
kind: EgressGateway
metadata:
  name: payments-egress
  namespace: payments
spec:
  ipPools:
    - name: egress-ip-pool
  replicas: 2
  template:
    spec:
      nodeSelector:
        egress-gateway: "true"
      containers:
        - name: egress
          image: calico/egress-gateway:v3.27.0
```

```yaml
apiVersion: projectcalico.org/v3
kind: NamespaceProfile
metadata:
  name: payments
spec:
  egressGateway:
    selector: "egress-zone == 'payments'"
```

Pod traffic destined for non-pod, non-service IPs is routed via the egress gateway. The destination sees the gateway's IP as the source.

### 15.3 Cilium EgressGatewayPolicy

```yaml
apiVersion: cilium.io/v2
kind: CiliumEgressGatewayPolicy
metadata:
  name: payments-to-vendor
spec:
  selectors:
    - podSelector:
        matchLabels:
          io.kubernetes.pod.namespace: payments
          app: payment-processor
  destinationCIDRs:
    - 198.51.100.0/24
  egressGateway:
    nodeSelector:
      matchLabels:
        egress-gateway: "true"
    interface: eth0
    egressIP: 10.0.99.10
```

Cilium installs eBPF redirect rules: traffic from selected pods to selected CIDRs is encapsulated and sent to the gateway node, where it is SNATed to `egressIP` and forwarded.

### 15.4 The Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       EGRESS GATEWAY TOPOLOGY                            │
│                                                                          │
│   Worker node A                Worker node B               Gateway node  │
│   ┌─────────────┐              ┌─────────────┐             ┌──────────┐  │
│   │ pod-1       │              │ pod-2       │             │ gw-pod   │  │
│   │ 10.244.0.5  │              │ 10.244.1.7  │             │10.0.99.10│  │
│   └──────┬──────┘              └──────┬──────┘             └────┬─────┘  │
│          │                            │                         │       │
│          ├────────────────────────────┴─────────────────────────┘       │
│          │  (eBPF redirect / IP-in-IP / VXLAN)                          │
│          │                                                              │
│          ▼                                                              │
│   ┌─────────────────────────────────────────────────────┐               │
│   │  Gateway pod SNATs to 10.0.99.10 and forwards to    │               │
│   │  the VPC default gateway → NAT GW or direct route   │               │
│   └──────────────────────────┬──────────────────────────┘               │
│                              ▼                                          │
│                       External service                                  │
│                       (sees source 10.0.99.10)                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 15.5 Egress Gateway Gotchas

- **Health checking.** The gateway pod can crash, be drained, or lose connectivity. Without explicit health checks, traffic to the gateway becomes a black hole. Calico has `egressIPHealth`; Cilium has `egressGateway.healthPort`.
- **Single point of failure.** Two gateway replicas on different AZs is the minimum.
- **MTU.** Encapsulating pod traffic for delivery to the gateway adds 20-50 bytes of overhead. The egress path's MTU must accommodate, or you get fragmentation, or worse, silent black-holing of large packets (the dreaded "small packets work, big don't" symptom).
- **Reverse path filtering.** The gateway must SNAT, or the destination's reply packet has no idea how to return to the original pod. If SNAT misfires, packets get dropped at the source pod's veth on `rp_filter`.
- **Stateful firewalls.** External services with stateful firewalls expect symmetric flows. If two gateway replicas handle the same flow alternately (ECMP), the destination firewall drops "asymmetric" packets. Use consistent hashing for gateway selection.

---

## 16. NetworkPolicy + Service Mesh: Order of Evaluation

Most production clusters layer at least two policy systems: NetworkPolicy (L4) and a service mesh (L7 with mTLS identity). When they coexist, you must understand the **packet's evaluation order**.

### 16.1 The Two Layers

- **NetworkPolicy** runs at the CNI layer — eBPF on the veth, or iptables on the host. It decides whether a packet may flow between two pod IPs on a specific L4 port. It has no notion of TLS, HTTP method, or service account identity (beyond what the SA label exposes).
- **Service Mesh** (Istio, Linkerd, Consul) runs as a sidecar (Envoy, linkerd2-proxy) or, in sidecar-less mode (Cilium service mesh, Istio Ambient), as a node-level proxy. It decides whether a *request* (HTTP, gRPC) may flow from one service identity (SPIFFE/SA) to another, based on `AuthorizationPolicy`.

```
┌─────────────────────────────────────────────────────────────────────────┐
│           PACKET PATH WITH NETPOL + MESH (sidecar mode)                  │
│                                                                          │
│   Client pod                                                            │
│   ┌──────────────────┐                                                  │
│   │ app container    │                                                  │
│   │   send req       │                                                  │
│   └────────┬─────────┘                                                  │
│            │ localhost:8080 (intercepted by iptables redirect)          │
│            ▼                                                            │
│   ┌──────────────────┐                                                  │
│   │ envoy sidecar    │ ◄── client-side mesh policy                      │
│   │   add mTLS       │                                                  │
│   └────────┬─────────┘                                                  │
│            │ pod-to-pod TCP (mTLS-encrypted)                            │
│            ▼                                                            │
│   ┌──────────────────┐                                                  │
│   │ veth-egress      │ ◄── EGRESS NetworkPolicy (CNI)                   │
│   └────────┬─────────┘                                                  │
│            │                                                            │
│   ─────────┼────────── network ──────────────                           │
│            │                                                            │
│   ┌────────▼─────────┐                                                  │
│   │ veth-ingress     │ ◄── INGRESS NetworkPolicy (CNI)                  │
│   └────────┬─────────┘                                                  │
│            │                                                            │
│   ┌────────▼─────────┐                                                  │
│   │ envoy sidecar    │ ◄── server-side mesh policy                      │
│   │   terminate mTLS │                                                  │
│   └────────┬─────────┘                                                  │
│            │ localhost                                                  │
│   ┌────────▼─────────┐                                                  │
│   │ app container    │                                                  │
│   └──────────────────┘                                                  │
│                                                                         │
│   NP fires twice; mesh policy fires twice. Both must allow for the      │
│   request to succeed.                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 16.2 The Coexistence Rules

- **Both must allow.** NetworkPolicy at L4 and mesh AuthorizationPolicy at L7 are *both* evaluated, and the strictest wins. There is no override; if either denies, the packet/request is dropped.
- **NP sees the sidecar.** When Envoy is the sidecar, the actual TCP connection from the sender's pod is *to its own sidecar* (localhost — invisible to NP) and then *from its sidecar to the destination pod* (which NP sees). If NP allows pod-to-pod but disallows sidecar-to-pod (because the sidecar has a different SA label), the mesh will be silently broken.
- **NP and pod identity.** A common pattern: `from: { podSelector: { app: web } }` in NP. But with Istio sidecar mode, the *real* source identity is the Envoy. If your NP keys off labels and Envoy shares the pod's labels (it does — sidecars share the pod's label set), this works. If your NP keys off SA, same — sidecars share the pod's SA.

### 16.3 Recommendation

- Use NP for **coarse L4 segmentation**: "namespace A may reach namespace B on port 443 only."
- Use mesh for **fine-grained L7 authorization with identity**: "the `orders-service` SA may call `POST /orders` on the `payments-service`."
- Use NP as the **inviolable floor** — if mesh fails or is bypassed (a pod without the mTLS-required sidecar), NP still drops the packet.

### 16.4 The Ambient / Sidecarless Case

Cilium service mesh and Istio Ambient eliminate the sidecar; the proxy runs once per node (ztunnel for Istio, the Cilium agent for Cilium). The packet path simplifies — NP and mesh policy are *both* applied by the node-level dataplane, in a defined order documented per implementation. Cilium evaluates NP first, then L7 mesh policy, all in the same eBPF-and-Envoy pipeline.

---

## 17. Pod-Level Firewalls and the hostNetwork Bypass

NetworkPolicy selects **pods**, where "pod" means an object with its own network namespace. Pods with `hostNetwork: true` *share the node's network namespace* and have no separate veth pair. The CNI never sees their traffic on a pod interface.

```yaml
spec:
  hostNetwork: true   # ← bypasses NetworkPolicy
  containers:
    - name: scary
      image: ...
```

Pods with `hostNetwork: true` are typically:

- Critical system daemons: `kube-proxy`, `node-exporter`, `cilium-agent`, `calico-node`.
- Some ingress controllers (when running in host-port mode rather than service-LB mode).
- Workloads that need direct access to host networking primitives (e.g., raw sockets, low-numbered ports).

NetworkPolicy does **not** apply to host-network pods on most CNIs. They send and receive traffic *as the node*, on the node's IP, through the node's iptables (which NP does not touch).

### 17.1 The Defense

- **PodSecurity admission** must restrict who may set `hostNetwork: true`. In a multi-tenant cluster, only platform-managed DaemonSets (kube-system) should have it. Use the `restricted` PSS profile which forbids hostNetwork.
- **Calico HostEndpoint** + GlobalNetworkPolicy can apply rules to the node's interface itself, which covers traffic from host-network pods (because that traffic uses the node IP). See §9.
- **Cilium host firewall**: enabling `--enable-host-firewall` extends Cilium policy to the host network namespace. CCNP policies with `nodeSelector` apply to the node itself.

### 17.2 Why This Matters

A pod with `hostNetwork: true` and `CAP_NET_ADMIN` can:

- Bind to any port on the node, including `:10250` (kubelet), `:6443` (apiserver if it's on this node), `:2379` (etcd if colocated).
- Read every packet entering/leaving the node via `tcpdump`.
- Modify iptables and route tables.
- Spoof packets with the node's IP as source.

This is why `hostNetwork: true` is *the* privilege escalation surface in Kubernetes. NP cannot defend against it, ever. Admission must.

---

## 18. Zero-Trust Patterns

"Zero trust" in the network sense means: no two services trust each other by virtue of being in the same network. Every connection is authenticated and authorized; default state is deny.

### 18.1 The Four Layers of Zero-Trust East-West

1.  **Default-deny at network layer** (NetworkPolicy or BANP).
2.  **Identity-based selection** (Cilium identity, mesh SPIFFE).
3.  **Encryption in transit** (mesh mTLS or CNI-level WireGuard).
4.  **L7 authorization** (mesh AuthorizationPolicy or Cilium L7 CNP).

### 18.2 Per-Namespace Default-Deny + Tiered Allow

The recommended baseline:

```yaml
# Layer 1 — BANP cluster-wide default-deny
apiVersion: policy.networking.k8s.io/v1alpha1
kind: BaselineAdminNetworkPolicy
metadata:
  name: default
spec:
  subject:
    namespaces: {}
  ingress:
    - action: Deny
      from: [{ networks: ["0.0.0.0/0"] }]
  egress:
    - action: Deny
      to: [{ networks: ["0.0.0.0/0"] }]
```

```yaml
# Layer 2 — Platform ANP: allow DNS + apiserver for everyone
apiVersion: policy.networking.k8s.io/v1alpha1
kind: AdminNetworkPolicy
metadata:
  name: platform-baseline
spec:
  priority: 100
  subject:
    namespaces: {}
  egress:
    - action: Allow
      to:
        - pods:
            namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
            podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports:
        - portNumber: { protocol: UDP, port: 53 }
        - portNumber: { protocol: TCP, port: 53 }
    - action: Allow
      to:
        - networks: ["10.0.0.0/24"]   # apiserver subnet
      ports:
        - portNumber: { protocol: TCP, port: 6443 }
```

```yaml
# Layer 3 — Per-namespace tenant NetworkPolicy
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-allow-frontend
  namespace: payments
spec:
  podSelector: { matchLabels: { app: api } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: { matchLabels: { app: frontend } }
          namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: web } }
      ports:
        - protocol: TCP
          port: 8080
```

The net effect:

- Without any tenant policy, every pod in every namespace is denied everything (by BANP) except DNS and apiserver (by platform ANP).
- Tenant policies open specific holes for their own application traffic.
- Platform invariants (no metadata service, no cross-cluster pod-to-pod) live in ANPs above the tenant layer and cannot be overridden.

### 18.3 Identity + Encryption

The packet-level segmentation above is necessary but not sufficient. Add:

- **Cilium WireGuard transparent encryption** between nodes. Set `enableEncryption: wireguard` in the Helm values. Every pod-to-pod packet is wrapped in WireGuard between the source and destination nodes. No service mesh required.
- **Cilium mTLS** at the proxy layer for service-to-service authentication. Or Istio sidecar/Ambient mTLS. Either way, the application sees a verified peer identity at the proxy.

### 18.4 What "Zero Trust" Does Not Mean

It does not mean "everything is encrypted, therefore we are secure." A compromised pod can present its own valid SPIFFE identity to peers; the peers will trust it because the identity is cryptographically valid. Zero-trust is *about scope of trust*, not about absence of trust. Each service trusts only what its policy says it should trust, even from cryptographically-valid peers.

---

## 19. Common Recipe Library

A handful of patterns recur in every cluster. Memorize them.

### 19.1 Allow Within Same Namespace

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
  namespace: payments
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {}      # any pod IN THIS NAMESPACE
```

### 19.2 Allow Ingress Controller

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-controller
  namespace: payments
spec:
  podSelector:
    matchLabels:
      app: api
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
          podSelector:
            matchLabels:
              app.kubernetes.io/name: ingress-nginx
      ports:
        - protocol: TCP
          port: 8080
```

### 19.3 Allow Egress to CoreDNS + apiserver Only

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-platform-egress
  namespace: payments
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: kube-system }
          podSelector:
            matchLabels: { k8s-app: kube-dns }
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    - to:
        - ipBlock:
            cidr: 10.0.0.1/32     # apiserver clusterIP (verify!)
      ports:
        - protocol: TCP
          port: 443
```

### 19.4 Allow Specific Cross-Namespace Pair

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-to-api
  namespace: payments
spec:
  podSelector:
    matchLabels:
      app: api
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: web }
          podSelector:
            matchLabels: { app: frontend }
      ports:
        - protocol: TCP
          port: 8080
```

### 19.5 Allow Prometheus Scrape

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-prometheus-scrape
  namespace: payments
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: monitoring }
          podSelector:
            matchLabels: { app.kubernetes.io/name: prometheus }
      ports:
        - protocol: TCP
          port: 9090         # the /metrics endpoint
        - protocol: TCP
          port: 8080         # often app + /metrics on same port
```

A repeating production bug: forgetting `/metrics` is on the same port as app traffic. Prometheus scrapes break, alerting goes silent, and someone notices three days later.

### 19.6 Allow Database Egress

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-egress-to-postgres
  namespace: payments
spec:
  podSelector:
    matchLabels: { app: api }
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: databases }
          podSelector:
            matchLabels: { app: postgres, tier: primary }
      ports:
        - protocol: TCP
          port: 5432
```

### 19.7 Cilium L7: GET-only to Read API

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: readonly-to-api
  namespace: web
spec:
  endpointSelector:
    matchLabels: { app: reporting }
  egress:
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: payments
            app: api
      toPorts:
        - ports: [{ port: "8080", protocol: TCP }]
          rules:
            http:
              - method: "GET"
                path: "/v1/.*"
```

---

## 20. CIDR Semantics Gotchas

`ipBlock` selectors look simple — they're just CIDRs. They are not simple. The packet you are filtering may have a different IP than you expect.

### 20.1 Pod CIDR vs Node CIDR vs Service CIDR

A Kubernetes cluster has at least three CIDR ranges:

- **Pod CIDR**: the range from which pods get IPs. E.g., `10.244.0.0/16`. The CNI hands these out per node from a per-node subnet (e.g., node A gets `10.244.0.0/24`, node B gets `10.244.1.0/24`).
- **Node CIDR**: the range from which *nodes* get IPs (their `eth0` addresses). E.g., `10.0.0.0/16`. This is typically the VPC subnet, not chosen by Kubernetes.
- **Service CIDR**: the range from which Services get cluster IPs. E.g., `10.96.0.0/12`. This is a virtual range; no real network traffic uses these IPs — they exist only as DNAT targets at the kube-proxy / cilium level.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                CLUSTER CIDR LAYOUT (typical)                             │
│                                                                          │
│   Node CIDR     10.0.0.0/16     (VPC subnet)                            │
│     ├─ node A: 10.0.1.10                                                │
│     └─ node B: 10.0.1.11                                                │
│                                                                          │
│   Pod CIDR      10.244.0.0/16   (cluster pod range)                     │
│     ├─ node A subnet: 10.244.0.0/24                                     │
│     │    ├─ pod-1: 10.244.0.5                                           │
│     │    └─ pod-2: 10.244.0.6                                           │
│     └─ node B subnet: 10.244.1.0/24                                     │
│          ├─ pod-3: 10.244.1.7                                           │
│          └─ pod-4: 10.244.1.8                                           │
│                                                                          │
│   Service CIDR  10.96.0.0/12    (virtual; no real IPs)                  │
│     ├─ kubernetes Service: 10.96.0.1                                    │
│     ├─ kube-dns Service:   10.96.0.10                                   │
│     └─ ... others                                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

When pod-1 talks to pod-2:

- Source IP on the wire: `10.244.0.5` (pod IP).
- Destination IP on the wire: `10.244.0.6` (pod IP).
- Node IPs are *not* in the packet headers (unless overlay encapsulation wraps the inner packet — but the inner packet still has pod IPs).

When pod-1 talks to `kubernetes` Service `10.96.0.1`:

- Application sends to `10.96.0.1:443`.
- kube-proxy / Cilium DNATs to the actual apiserver endpoint (`10.0.1.5:6443`).
- After DNAT, the packet has source `10.244.0.5`, destination `10.0.1.5`.
- NetworkPolicy on the *outbound* side typically applies *after* DNAT, so `to: { ipBlock: { cidr: 10.96.0.0/12 } }` matches *nothing* on Cilium/Calico (the service CIDR is gone by the time policy fires) — but `to: { ipBlock: { cidr: 10.0.0.0/16 } }` (the node CIDR) does match.

### 20.2 The Pod-CIDR-in-ipBlock Trap

```yaml
egress:
  - to:
      - ipBlock:
          cidr: 0.0.0.0/0       # everything
          except:
            - 10.244.0.0/16     # but not the pod CIDR
```

The intent: "allow egress to the internet, deny egress to other pods." This is broken on most CNIs because:

- Egress to a pod in the same namespace, via the pod CIDR, would be caught by `except`. So far so good.
- But egress to a *Service* (cluster IP) goes to `10.96.x.x`, not `10.244.x.x`. So service traffic is *allowed* (it doesn't match the except) — and after DNAT, you've talked to a pod. The "deny pod CIDR" was bypassed via Service.

To actually deny cross-pod traffic, use *positive* selectors:

```yaml
egress:
  - to:
      - ipBlock:
          cidr: 0.0.0.0/0
          except:
            - 10.0.0.0/8       # entire private space, including pod/node/service CIDRs
            - 172.16.0.0/12
            - 192.168.0.0/16
            - 169.254.0.0/16   # metadata service
```

Or even better: don't use ipBlock for cluster-internal targets. Use podSelector / namespaceSelector for cluster pods and ipBlock for external targets only.

### 20.3 The 0.0.0.0/0 Trap

`cidr: 0.0.0.0/0` matches *every* IP, including:

- Pod IPs (cluster-internal).
- Node IPs (cluster-internal).
- Service VIPs (post-DNAT → pod IPs).
- External IPs.
- Loopback IPs (rare, but counts).
- Cloud metadata service IPs (`169.254.169.254`, `fd00:ec2::254`).

If your intent is "allow external traffic," use the `except` form to carve out internal ranges, but be thorough. The safer pattern is to allow internal traffic via selectors and external traffic via specific CIDRs (e.g., `ipBlock: { cidr: 1.0.0.0/8, except: [...] }` for specific external partners).

### 20.4 IPv6 in ipBlock

If the cluster is dual-stack, you need separate `ipBlock` entries per family:

```yaml
egress:
  - to:
      - ipBlock: { cidr: 10.0.0.0/8 }
      - ipBlock: { cidr: fd00::/8 }
```

A single CIDR cannot mix v4 and v6. Forgetting the v6 half means v6 traffic falls through to default deny — silent breakage if your services dual-resolve.

---

## 21. Testing NetworkPolicy

A NetworkPolicy that isn't tested isn't a policy; it's a wish.

### 21.1 The Minimal Manual Test

```bash
# Allowed path
kubectl run client --rm -it --image=nicolaka/netshoot --labels="app=frontend" \
  -n web -- curl -m 5 api.payments.svc.cluster.local:8080

# Should-be-blocked path
kubectl run rogue --rm -it --image=nicolaka/netshoot --labels="app=evil" \
  -n web -- curl -m 5 api.payments.svc.cluster.local:8080
```

The trap: if you start the rogue pod *with* labels that match an allow policy, you have not tested anything. Run with deliberately-mismatched labels.

### 21.2 `cilium connectivity test`

Cilium ships a battery of connectivity tests:

```bash
cilium connectivity test --include-conn-disrupt-test=false
```

This deploys a set of pods (allow, deny, client, server) across multiple namespaces and verifies that allowed connections succeed and denied ones fail, per the installed policies. The output is a coloured matrix; red cells need investigation.

### 21.3 `np-test` and `netpol-canary`

`np-test` (npedersen/np-test or similar community tools) is a small CLI that, given a NetworkPolicy YAML, deploys synthetic pods on each side of every rule and tests connectivity. Useful in CI: every commit that changes NP runs `np-test` against an ephemeral cluster.

`netpol-canary` is a DaemonSet that emits "I should be reachable" and "I should not be reachable" probes, exposing Prometheus metrics on whether expectations hold. Drift detection.

### 21.4 The Pod-Without-Own-Policy Test

A failure mode: you write a NetworkPolicy that selects pod B, and test from pod A. But pod A may *itself* be selected by a default-deny on egress, so the connection fails for reasons unrelated to pod B's ingress policy. Always test from a pod that you know has unrestricted egress (or has explicit allow to the target).

```bash
# Bad: tests both A's egress AND B's ingress; can't tell which broke
kubectl exec a -- curl b:8080

# Good: explicit allow on the tester
kubectl run test --image=nicolaka/netshoot --labels="role=tester,allow-all-egress=true" -- \
  curl b:8080
# Combined with a Net Policy that allows tester pods to talk to anywhere
```

### 21.5 Policy Coverage Auditing

`audit-mode` in Calico (`logAction: Log` instead of `Deny`) lets you deploy a policy in observation mode: it logs what *would* have been denied without actually denying. Run this for a week before flipping the action to `Deny`. Cilium has `--policy-audit-mode` similarly.

### 21.6 Policy Coverage in CI

The strongest practice is a policy contract test in CI:

1.  Define, in a YAML file, the *expected* allowed and denied flows for each service: `frontend → api: ALLOWED`, `frontend → database: DENIED`.
2.  In CI, deploy the cluster, apply the NetworkPolicies, and use `np-test` or `netpol-explain` to verify every entry in the expected file.
3.  Fail the build on any divergence.

This catches "you added a policy that opened a hole" *before* the policy ships.

---

## 22. Day-2: Auditing, Drift, and Enforcement

NetworkPolicy is not a write-and-forget artifact. It evolves with the workload, and the most common failure mode is **drift**: a new namespace is created, no default-deny is applied to it, and a workload runs unprotected.

### 22.1 Auditing Coverage

A simple coverage audit:

```bash
# List all namespaces
NAMESPACES=$(kubectl get ns -o name | cut -d/ -f2)

# Check each for a default-deny
for ns in $NAMESPACES; do
  count=$(kubectl get networkpolicy -n "$ns" -o json | \
    jq '.items[] | select(.spec.podSelector == {} and (.spec.policyTypes | index("Ingress")))' | wc -l)
  if [ "$count" -eq 0 ]; then
    echo "$ns has NO default-deny-ingress"
  fi
done
```

This script is the seed of an alerting rule. Convert it to a Prometheus metric (custom exporter) or a Kyverno cluster policy.

### 22.2 Kyverno Enforcement

Kyverno can *generate* a default-deny NetworkPolicy automatically when a new namespace is created. This eliminates drift by construction:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: auto-default-deny
spec:
  rules:
    - name: create-default-deny
      match:
        any:
          - resources:
              kinds: [Namespace]
      generate:
        kind: NetworkPolicy
        apiVersion: networking.k8s.io/v1
        name: default-deny
        namespace: "{{request.object.metadata.name}}"
        synchronize: true
        data:
          spec:
            podSelector: {}
            policyTypes: [Ingress, Egress]
```

`synchronize: true` makes Kyverno re-create the policy if a tenant deletes it. Combined with a Kyverno mutation that adds the `default-deny=enforced` label to the namespace, you have a non-bypassable floor.

### 22.3 Kyverno Validation

A second policy *rejects* NetworkPolicies that are too permissive:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: reject-overpermissive-netpol
spec:
  validationFailureAction: Enforce
  rules:
    - name: no-allow-all-from-world
      match:
        any:
          - resources:
              kinds: [NetworkPolicy]
      validate:
        message: "ipBlock 0.0.0.0/0 is forbidden in ingress"
        deny:
          conditions:
            any:
              - key: "{{ request.object.spec.ingress[].from[].ipBlock.cidr }}"
                operator: AnyIn
                value: ["0.0.0.0/0"]
```

### 22.4 OPA / Gatekeeper Equivalent

Same idea with Gatekeeper ConstraintTemplate + Constraint. Lower-level, more flexibility, more work. Most teams have settled on Kyverno for NetworkPolicy auto-generation specifically because the `generate` action is concise.

### 22.5 ArgoCD App-of-Apps

In a GitOps cluster, the platform team owns a "platform-baseline" Application that includes default-deny NetworkPolicies for every namespace, and a script that adds a new policy whenever a new namespace is added to the cluster manifest. Pair this with Kyverno for the runtime-generated case (namespaces created out-of-band).

### 22.6 Periodic Coverage Review

Quarterly: run `np-test` against the full cluster with a known-good expected-flows file. Diff against the file from last quarter. Investigate every new allow.

---

## 23. Performance: iptables vs eBPF Maps

NetworkPolicy enforcement is on the hot path of every packet. The cost matters.

### 23.1 iptables Cost Model

iptables rules are evaluated **linearly** within each chain. A chain with 5,000 rules takes ~5,000 hash-lookups per packet to traverse (in the best case where each rule is a single `-m set` match). At 10 Gbps with 64-byte packets that's ~14M pps, so each rule has a ~15ns budget.

Calico optimizes this by using **ipsets**: a single `-m set --match-set foo src` rule can express "source is one of 10,000 IPs" in O(1). So Calico-on-iptables is far better than naive iptables; the linear cost is in *rule count*, not *IP count*.

At very large scale (10k+ pods, 100+ policies), Calico-iptables can take 10-50µs per packet through the policy chain — a non-trivial cost. Felix in eBPF mode reduces this to 1-2µs by replacing the iptables traversal with a single eBPF program lookup.

### 23.2 Cilium eBPF Map Cost

Cilium's per-endpoint policy map is a hash table. Lookup is O(1), bounded by hash quality and map size. The actual per-packet cost in steady-state is ~200-500 nanoseconds for the policy check, dominated by:

- 1 hash lookup in `cilium_ipcache` (source IP → identity).
- 1 hash lookup in the per-endpoint policy map.
- Some L4 metadata munging.

This is *significantly* faster than iptables-based enforcement at scale. Benchmarks from Cilium's documentation show 10× lower per-packet overhead vs Calico-iptables at 5k+ pods.

### 23.3 Policy Compilation Cost

Both Calico and Cilium have a *compilation* phase: when policy or pod set changes, the dataplane must rebuild its rules/maps. This is not on the packet path but it determines how quickly new pods become reachable.

- **Calico-iptables**: rebuild can take 10s-60s on a 10k-pod cluster with many policies. iptables-restore is the bottleneck.
- **Calico-eBPF**: faster, since it writes eBPF maps not iptables tables. Few seconds.
- **Cilium-eBPF**: identity-based model means most pod changes don't trigger policy recompilation at all — only identity changes do, and those are rare. Sub-second.

### 23.4 Memory

- iptables rules: small, but in kernel memory.
- ipsets: ~24 bytes per entry. 100k pods × 100 selectors × 1k pods/selector worst-case = 240MB. Real clusters are much less.
- Cilium policy maps: ~80 bytes per entry, indexed by (identity, port, proto, dir). 10k identities × 100 policies × 10 ports = 800MB worst-case. Real clusters use sparse maps, far less.

### 23.5 Practical Recommendation

For clusters under 1k pods, any CNI's policy implementation is fine. For 1k-10k, prefer Cilium or Calico-eBPF. For 10k+, Cilium's identity model becomes the most important architectural advantage.

---

## 24. Observability: Hubble, FlowLogs, Counters

A policy you cannot observe is a policy you cannot trust.

### 24.1 Hubble (Cilium)

Hubble is Cilium's observability layer. It captures every flow that crosses an eBPF datapath hook and exposes it via:

- CLI: `hubble observe --to-pod payments/api --verdict DENIED`
- UI: `hubble ui` (a web UI showing a real-time flow graph)
- API: `hubble-relay` exposing gRPC for tooling.

```bash
# All flows denied by policy in the last 5 minutes
hubble observe --verdict DENIED --since 5m

# Flows from a specific pod
hubble observe --from-pod web/frontend-7d4c

# Flows to a specific FQDN (Cilium tracks these for toFQDNs)
hubble observe --to-fqdn api.stripe.com
```

Every flow record includes: source identity (labels), destination identity, port, protocol, verdict (FORWARDED, DENIED, ERROR), policy match (the specific CNP that allowed/denied), and L7 metadata if available (HTTP method, path, Kafka topic).

This is the single most valuable diagnostic tool. "Why is my pod not connecting to the database?" becomes a one-line query.

### 24.2 Calico FlowLogs

Calico Enterprise has FlowLogs that capture similar information. Calico OSS does not have a UI but does have:

- Felix's prometheus metrics: `felix_iptables_rules_total`, `felix_policy_rules_total`.
- Per-policy iptables counter: `iptables -L cali-tw-... -nv` shows per-rule packet/byte counts.

For OSS, the standard pattern is to scrape Felix metrics into Prometheus and alert on `rate(felix_policy_denies_total{...})` — sudden spikes in denies indicate either an attack or a misconfigured policy rollout.

### 24.3 iptables Counters

For any iptables-based CNI:

```bash
# On the node, in the pod's network namespace (or just root namespace for host-NP)
iptables -L -nv -t filter
iptables -L cali-fw-cali12345 -nv      # Calico per-endpoint chain
```

Each line has packet and byte counters. Watching them increment under load is the most direct way to confirm a specific rule is firing.

### 24.4 Conntrack

`conntrack -L` shows current connection-tracking entries. If a connection is established (visible in conntrack), the initial SYN was allowed. Useful for diagnosing "the policy looks right but the connection still drops" — it might be a *return-path* problem (the conntrack entry says ESTABLISHED but Felix's reply-direction rule denied).

### 24.5 The Drop Diagnostic Loop

A standard playbook for "policy is blocking something":

1.  Reproduce the connection attempt with explicit timestamps.
2.  `hubble observe --verdict DENIED --since 30s` (Cilium) or `calicoctl get gnp -o yaml | grep -B5 -A5 Deny` (Calico).
3.  Match the timestamp to a deny verdict; read the policy name in the verdict's `match` field.
4.  Inspect that policy. Decide whether the deny is correct or the policy is wrong.
5.  If wrong, fix the policy; verify with `hubble observe` showing FORWARDED on a retry.

---

## 25. Pitfalls

A field guide to the failure modes. Each is a real production incident pattern.

1.  **Flannel + NetworkPolicy expectation.** You install Flannel, apply NetworkPolicy, and watch nothing happen. Flannel does not enforce. Install Calico-policy-only, or switch CNI.

2.  **`podSelector: {}` confused with no selector.** `matchLabels: {}` is "every pod"; omitting `podSelector` entirely is a schema error. The empty selector is *not* the empty set.

3.  **`namespaceSelector` AND `podSelector` trap.** Two items in `from` means OR; one item with both selectors means AND. We covered this in §3.4. It still bites everyone at least once.

4.  **Missing CoreDNS allow on default-deny-egress.** Symptom: every external hostname-based request fails. Allow UDP+TCP:53 to kube-dns.

5.  **Missing apiserver allow on default-deny-egress.** Jobs and operators that talk to the apiserver via in-cluster `KUBERNETES_SERVICE_HOST` will fail. Allow `to: { entities: [kube-apiserver] }` (Cilium) or `to: { ipBlock: { cidr: <apiserver-ip>/32 } }` (vanilla).

6.  **ANP priority conflicts.** Two ANPs at priority 100 — which wins? Lexicographic on name. Don't rely on it; spread priorities (10, 20, 30, ...) to leave room.

7.  **Egress `ipBlock` including pod CIDR.** Trying to "allow internet, deny intra-cluster" via ipBlock + except is fragile because Services bypass the pod CIDR. Use selectors.

8.  **`toFQDNs` bypass via direct IP.** A compromised pod can dial `13.225.139.111:443` directly without DNS. Cilium's DNS-correlated allowlist won't allow it, but L4 rules with broad CIDRs will. Pair `toFQDNs` with restrictive `toCIDRSet`.

9.  **Mesh sidecar blocked by NP.** You install Istio; sidecar startup fails because NP denies the sidecar's egress to the Istio control plane. Always allow sidecar control-plane traffic before applying default-deny.

10. **`/metrics` blocked.** Prometheus scrape is on port `9090` or `:8080/metrics`. If your app's allow rule only opened `:8080` for app traffic and `/metrics` is on a different port, scraping fails silently. Always include the metrics port.

11. **`hostNetwork: true` bypass.** Pods with hostNetwork are not selected by NetworkPolicy. Restrict via PodSecurity admission.

12. **Egress gateway without health check.** Gateway pod crashes; egress traffic black-holes. Configure `egressIPHealth` (Calico) or `healthPort` (Cilium).

13. **Policy drift on new namespaces.** New namespace, no default-deny. Use Kyverno to auto-generate.

14. **Testing from a pod with its own NP.** You test pod A's ingress by curling from pod B; but B has default-deny-egress and no allow rule. The test fails for B's reasons, not A's. Use a known-clean tester pod.

15. **Implicit `policyTypes` change.** You add `egress: []` to an ingress-only policy meaning to "explicitly deny egress" but accidentally toggle the inferred policyTypes. Always set `policyTypes` explicitly.

16. **Self-gossip blocked.** Stateful services (Cassandra, etcd, NATS) need replicas to talk to each other. Forgetting `from: { podSelector: <self> }` breaks the cluster on rollout.

17. **`/healthz` blocked.** Kubelet probes pods via the node's IP. If NP allows only specific pod IPs as ingress sources, probes from the kubelet (sourced from the node IP) get dropped. Allow node IPs or use Cilium's `host` entity.

18. **Service CIDR in `to: { ipBlock }`.** Service CIDRs (e.g., `10.96.0.0/12`) are virtual. Egress NP almost always sees pod IPs after DNAT. Using service CIDR in ipBlock matches nothing.

19. **`endPort` unsupported by CNI.** You write `port: 32000, endPort: 32767` (NodePort range). Cilium and Calico recent versions support it; old kube-router does not. Verify.

20. **CIDR notation typos.** `10.0.0.0/24` vs `10.0.0.0/16` is a 256× difference in scope. Always review CIDR carefully; many incidents trace to a one-character typo.

21. **Egress to NodeLocal DNSCache missed.** If NodeLocalDNS is enabled, the DNS server IP is `169.254.20.10`, not the cluster CoreDNS IP. Update your DNS-allow policy.

22. **IPv6 unconfigured.** Dual-stack cluster, NP only allows v4. v6 requests fall through to default deny silently.

23. **Calico tier missing `Pass`.** You write a tier policy that does what it's supposed to but lacks a fallthrough action. Other traffic for the selected endpoint is implicitly denied within the tier. Half your app breaks.

24. **Cluster autoscaler scaling new node without policy applied.** New node, new pod CIDR range, new pods. If policies select by pod IP CIDR (`ipBlock`) rather than label, the new node's pods don't match. Use label selectors.

25. **`matchExpressions` with `NotIn` confused with `In`.** `matchExpressions: [{key: app, operator: NotIn, values: [foo]}]` matches every pod whose `app` is set and not equal to `foo` — *not* pods without the `app` label. Use `DoesNotExist` for the latter.

26. **NetworkPolicy on host-network kube-system pods.** Selecting `kube-system/coredns` or `kube-system/metrics-server` by label may not apply if they're host-network (some installations are). Verify with `kubectl get pod -o yaml | grep hostNetwork`.

---

## 26. TL;DR

- **NetworkPolicy is L4 segmentation, namespaced, allow-only, additive (union semantics).** It is the floor of zero-trust east-west, not the ceiling.

- **The threat model: lateral movement and exfiltration.** Not L7 auth, not encryption, not identity, not host-network — those are mesh, CNI encryption, SPIFFE, and admission's jobs.

- **Default-deny is the foundational pattern.** An empty NetworkPolicy with `policyTypes: [Ingress, Egress]` selecting `podSelector: {}` denies everything. Layer specific allows on top.

- **Vanilla NP has no Deny.** Multiple policies UNION their allow sets. To deny, use Calico (`action: Deny`), Cilium (`ingressDeny`/`egressDeny`), or upstream ANP/BANP (1.29+).

- **The `namespaceSelector` + `podSelector` trap.** Same peer item = AND; different items = OR. Get it wrong, accidentally open the cluster.

- **Egress must allow CoreDNS first.** UDP+TCP:53 to `k8s-app: kube-dns` in `kube-system`. Otherwise default-deny-egress kills every hostname-based connection.

- **CNI choice determines enforcement.** Flannel and kindnet: no enforcement. AWS VPC CNI: native eBPF NP since 1.14, or chain Cilium/Calico. Calico: Felix → iptables/eBPF + ipsets. Cilium: identity-based, eBPF maps. The compatibility matrix in §8.6 is the cheat sheet.

- **Calico GlobalNetworkPolicy + Tiers** give cluster-scoped, ordered, deny-capable policy with HostEndpoint extending coverage to the node itself.

- **Cilium CNP/CCNP** adds L7 (HTTP, gRPC, Kafka), `toFQDNs` (DNS-correlated egress allowlists), `toEntities` (named destinations like `kube-apiserver`), and `serviceAccount` selectors.

- **AdminNetworkPolicy + BaselineAdminNetworkPolicy (1.29+ beta)** finally bring cluster-scoped, priority-ordered, Allow/Deny/Pass semantics to the upstream API. Evaluation order: ANP → NetworkPolicy → BANP → implicit allow. `Pass` defers to the next layer; it is the key to composable layered authority.

- **Egress gateways** (Calico EgressGateway, Cilium EgressGatewayPolicy) give pods a stable external source IP for IP-allowlisted partner services. Always health-check the gateway.

- **NetworkPolicy + Service Mesh: both must allow.** NP at L4, mesh at L7 with mTLS identity. Use NP as the floor, mesh as the fine-grained authorization layer.

- **`hostNetwork: true` bypasses NetworkPolicy.** Defend via PodSecurity admission and Calico HostEndpoints.

- **Performance**: Calico-iptables scales linearly with rule count (10-50µs at 10k pods); Cilium eBPF maps stay O(1) with identity-based selection.

- **Observability**: Hubble for Cilium (every flow, every verdict, every L7), Calico FlowLogs (enterprise) or iptables counters + Felix metrics (OSS). A drop without a flow log entry is a drop you'll never diagnose.

- **Day-2**: Kyverno to auto-generate per-namespace default-deny, validate against overpermissive rules. Periodic `np-test` runs in CI to verify expected allow/deny matrix.

- **Pitfalls**: 26 in §25. Memorize at least the first 10.

The contract: every pod is denied everything until proven otherwise. Every allow has a name, an owner, an audit trail, and a test. Every namespace has a default-deny. Every cluster has an ANP that pin policy-team invariants above tenant control. Every connection that succeeds was allowed by name, by intent, by a policy your team wrote, reviewed, and will see again at the next quarterly audit. Anything less is a flat network with extra YAML.
