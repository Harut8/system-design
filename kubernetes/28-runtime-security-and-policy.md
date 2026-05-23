# Runtime Security and Policy: From Admission to syscall

Chapter 27 spent its time on what happens *before* a Pod ever reaches the apiserver: signed images, SBOMs, provenance, in-toto attestations, supply-chain hardening. Chapter 06 covered admission — the wall at the apiserver that decides whether a write is accepted. This chapter is about everything that happens *after* admission says yes: the Pod is now running on a node, it has a PID, it can call `execve`, it can `open("/etc/shadow")`, it can `connect()` to anywhere your egress allows. Admission is point-in-time; runtime is continuous. A signed image from an approved registry can still drop a reverse shell on its first request. An attacker who has compromised your CI pipeline has already gotten through chapter 27. An attacker who has stolen a service account token has already gotten past chapter 07. Runtime is the layer that catches the rest.

The audience for this chapter is staff engineers who have to *own* this — write the policies, run the detection stack, get paged at 03:00 when Falco fires, and have to know whether the alert is real, whether to cordon the node, whether to call legal. That means we go deep on the mechanics: Pod Security Admission's three profiles and exactly which fields each blocks; Gatekeeper's ConstraintTemplate and audit controller; Kyverno's autogen and verifyImages rules; ValidatingAdmissionPolicy's CEL surface and how the apiserver evaluates it without a webhook hop; Falco's eBPF driver and how `sys_enter` is hooked; Tetragon's ability to `SIGKILL` from a kprobe; seccomp profile generation from observed syscalls; AppArmor and SELinux on a Kubernetes node; the audit log; the CIS Benchmark; MITRE ATT&CK for Containers; an incident response runbook that is good enough to staple to the wall.

We will write the same nine policies in Kyverno, Gatekeeper, and VAP so the comparison is concrete rather than vibes-based. We will look at real source code from `falcosecurity/falco`, `cilium/tetragon`, `aquasecurity/tracee`, `kyverno/kyverno`, `open-policy-agent/gatekeeper`. And we will end with the meta-concern: policy fatigue, observability of the policy engines themselves, and the compliance stack that wraps all of it.

---

## Table of Contents

1.  [TL;DR](#1-tldr)
2.  [The Runtime Security Threat Model](#2-the-runtime-security-threat-model)
3.  [Defense in Depth: The Full Stack](#3-defense-in-depth-the-full-stack)
4.  [Pod Security Admission (PSA)](#4-pod-security-admission-psa)
5.  [PSA Profiles in Detail: privileged / baseline / restricted](#5-psa-profiles-in-detail-privileged--baseline--restricted)
6.  [PSA Limitations and When You Outgrow It](#6-psa-limitations-and-when-you-outgrow-it)
7.  [OPA Gatekeeper Deep Look](#7-opa-gatekeeper-deep-look)
8.  [A Rego Primer (Just Enough)](#8-a-rego-primer-just-enough)
9.  [Gatekeeper Mutation](#9-gatekeeper-mutation)
10. [Kyverno Deep Look](#10-kyverno-deep-look)
11. [Kyverno Rule Types: validate, mutate, generate, cleanup, verifyImages](#11-kyverno-rule-types-validate-mutate-generate-cleanup-verifyimages)
12. [ValidatingAdmissionPolicy and CEL (In-Process Admission)](#12-validatingadmissionpolicy-and-cel-in-process-admission)
13. [MutatingAdmissionPolicy](#13-mutatingadmissionpolicy)
14. [VAP vs Kyverno vs Gatekeeper: The Decision Matrix](#14-vap-vs-kyverno-vs-gatekeeper-the-decision-matrix)
15. [Policy Library: The Same Nine Policies in Three Engines](#15-policy-library-the-same-nine-policies-in-three-engines)
16. [Falco: Architecture and Rules](#16-falco-architecture-and-rules)
17. [Falco eBPF Driver vs Kernel Module](#17-falco-ebpf-driver-vs-kernel-module)
18. [Tetragon: eBPF Observability and Enforcement](#18-tetragon-ebpf-observability-and-enforcement)
19. [Tracee: CO-RE and Image+Runtime Correlation](#19-tracee-co-re-and-imageruntime-correlation)
20. [Other Runtime Tools: bcc, bpftrace, auditd](#20-other-runtime-tools-bcc-bpftrace-auditd)
21. [Detection vs Prevention: The Spectrum](#21-detection-vs-prevention-the-spectrum)
22. [Seccomp Profiles](#22-seccomp-profiles)
23. [AppArmor and SELinux on Kubernetes](#23-apparmor-and-selinux-on-kubernetes)
24. [Apiserver Audit Log Analysis](#24-apiserver-audit-log-analysis)
25. [Continuous Benchmark Scanning: kube-bench, kubescape, trivy k8s, kube-hunter](#25-continuous-benchmark-scanning-kube-bench-kubescape-trivy-k8s-kube-hunter)
26. [MITRE ATT&CK for Containers](#26-mitre-attck-for-containers)
27. [Incident Response Playbook](#27-incident-response-playbook)
28. [The Compliance Stack: SOC2, ISO27001, PCI, HIPAA](#28-the-compliance-stack-soc2-iso27001-pci-hipaa)
29. [Honeytokens](#29-honeytokens)
30. [Egress Monitoring with Hubble](#30-egress-monitoring-with-hubble)
31. [Mutating Policy as a Hardening Lever](#31-mutating-policy-as-a-hardening-lever)
32. [Policy as Code: Git, Tests, Rollout](#32-policy-as-code-git-tests-rollout)
33. [Policy Fatigue](#33-policy-fatigue)
34. [Observability of the Policy Stack Itself](#34-observability-of-the-policy-stack-itself)
35. [Pitfalls](#35-pitfalls)
36. [Cross-References](#36-cross-references)

---

## 1. TL;DR

- **Two layers, not one.** Admission (06, 27, this chapter §4–§15) stops bad things from being created. Runtime (this chapter §16–§24) stops bad things that are already running. You need both; they catch disjoint failure modes.
- **Pod Security Admission is the floor.** Every namespace gets a `pod-security.kubernetes.io/enforce: baseline` label at minimum. `restricted` for non-system namespaces. PSA is built-in, free, namespace-scoped, three profiles, three modes (enforce/warn/audit), and replaces the deleted PSP.
- **PSA is not enough.** It is namespace-wide, has no per-pod exception, and only fires at pod admission. Policies like "image must be from `registry.corp/`" or "all Pods must have resource limits" require a policy engine.
- **Three policy engines.** Gatekeeper (Rego, OPA-based, strong audit, mutation in beta), Kyverno (YAML, K8s-native, autogen, image verification built in), ValidatingAdmissionPolicy (CEL, in-process, GA in 1.30, low operational overhead, simple rules only). The decision matrix is in §14.
- **VAP is the new default for simple rules.** No webhook hop, no pod to run, cost-bounded by the CEL verifier. If your policy is one expression with no external data lookup, VAP is the right choice.
- **Kyverno is the new default for complex policy.** Mutation, image signing, autogen, generate, cleanup — all native, all YAML. Adoption is overtaking Gatekeeper for greenfield.
- **Gatekeeper still wins when you need Rego.** Cross-resource lookups, transitive constraints, complex set logic. Mature audit controller.
- **Runtime detection lives in the kernel.** Falco hooks `sys_enter`/`sys_exit` with eBPF and matches against rules; Tetragon attaches kprobes/tracepoints and can `SIGKILL` inline; Tracee uses CO-RE and correlates with image scan results. All three are eBPF-first.
- **Detection vs prevention is a spectrum.** Falco alerts. Tetragon can kill. Sandbox runtimes (gVisor, Kata — chapter 29) prevent kernel access entirely. Defense in depth means combining them.
- **Seccomp, AppArmor, SELinux are still relevant.** `RuntimeDefault` seccomp is required by PSA `restricted`. AppArmor profiles via `apparmorProfile` (1.30+ GA); SELinux via `seLinuxOptions`. Custom seccomp profiles ship to `/var/lib/kubelet/seccomp/` and are referenced by `Localhost`.
- **The audit log is the SIEM source of truth.** Configure `--audit-policy-file`, send to a forwarder, query for "who deleted X", "who got cluster-admin", "anonymous request count > 0". CIS Benchmark mandates this.
- **Continuous benchmark scanning is non-negotiable.** kube-bench (CIS), kubescape (NSA+CIS+MITRE), trivy k8s (CVE+config), kube-hunter (offensive). Run on a schedule and on every cluster change.
- **MITRE ATT&CK for Containers is the lingua franca.** Tag your alerts with TA0002 (Execution), T1611 (Escape to Host), T1496 (Resource Hijacking). Your IR team will thank you.
- **Policy as code or it didn't happen.** Policies in Git, PR-reviewed, tested with `kyverno test` / `gator` / `conftest`, rolled out audit-first then enforce. Same workflow you use for Terraform.

---

## 2. The Runtime Security Threat Model

Before we talk about tools, let us be honest about what they are protecting against. A staff engineer who cannot articulate the threat model will spend their budget on the wrong controls.

```
┌────────────────────────────────────────────────────────────────────────┐
│  PHASE                          │  EXAMPLE                            │
├────────────────────────────────────────────────────────────────────────┤
│  1. Initial access              │  CI token leak, SSRF in app,        │
│                                 │  malicious image pulled, dev creds  │
│  2. Execution                   │  app process runs attacker code,    │
│                                 │  curl|bash inside container         │
│  3. Persistence                 │  cron in container, write to PVC,   │
│                                 │  modify ConfigMap, install daemon   │
│  4. Privilege escalation        │  CAP_SYS_ADMIN, suid binary,        │
│                                 │  hostPath escape, dirty pipe        │
│  5. Defense evasion             │  disable seccomp, mask processes,   │
│                                 │  unload AppArmor, kill agent        │
│  6. Credential access           │  /var/run/secrets/...token,         │
│                                 │  steal IMDS, dump etcd              │
│  7. Discovery                   │  kube-api self-subject-review,      │
│                                 │  port scan PodCIDR, DNS sweep       │
│  8. Lateral movement            │  SSH/kubectl exec to next pod,      │
│                                 │  steal SA, hit next service         │
│  9. Collection                  │  exec into DB, dump tables          │
│ 10. Command and control         │  DNS-over-HTTPS tunnel, reverse     │
│                                 │  shell to attacker domain           │
│ 11. Exfiltration                │  upload to attacker S3 / IPFS       │
│ 12. Impact                      │  ransomware in PVC, cryptominer,    │
│                                 │  delete resources, brick cluster    │
└────────────────────────────────────────────────────────────────────────┘
```

This is the MITRE ATT&CK kill chain, condensed. Each phase has a corresponding K8s-native control:

| Phase | Pre-admission control | Runtime control |
|-------|----------------------|-----------------|
| Initial access | Image signing (ch 27), admission policy | Falco rule on `execve` of unexpected binary |
| Execution | Seccomp default, PSA restricted | Tetragon kprobe on `execve` |
| Persistence | Read-only root FS, no PVC writes | Falco "write below /etc" rule |
| Privesc | PSA blocks `CAP_SYS_ADMIN`, no privileged | Tetragon kills on cap raise |
| Defense evasion | Immutable AppArmor profile | Audit log: profile load/unload |
| Credential access | Workload identity (ch 07) — no static SA | Falco "read sensitive file" rule |
| Discovery | RBAC (ch 07), NetworkPolicy (ch 20) | Hubble L7 visibility |
| Lateral movement | NetworkPolicy default-deny | Hubble L7 deny + alert |
| Collection | Encrypted PVC, segment DB by NP | Falco DB-exec rule |
| C2 | Egress NP, DNS allowlist | Hubble DNS anomaly detection |
| Exfiltration | Egress NP, outbound proxy | Bytes-out anomaly |
| Impact | Read-only root, resource limits | Falco crypto-mining rule |

The point is that *no single layer covers all phases*. PSA does nothing about C2 callbacks. NetworkPolicy does nothing about cryptominers. Falco does nothing about a privileged container that was admitted before Falco was installed. You need a *stack* of controls, and each one needs to be designed assuming the layers above it have already failed.

### 2.1 The shift from "perimeter" to "every-pod"

In the legacy world, runtime security was a host-based agent on a server. The unit of compromise was a VM, and the agent watched syscalls system-wide. In Kubernetes the unit of compromise is a *Pod*: a process group inside a container inside a Pod inside a node inside a cluster. An attacker who gets shell in one Pod can move laterally to many more, faster, because the network is flat by default and identity is often shared (default SA, mounted token).

The defensive shift is:

- **From "host agent" to "kernel-attached eBPF agent"** that can see every container's syscalls because they all flow through one kernel.
- **From "firewall at the perimeter" to "NetworkPolicy at every pod" (ch 20).**
- **From "AD identity" to "workload identity" (ch 07) so a stolen token rotates fast.**
- **From "scan on commit" to "scan on every image pull and admit" (ch 27).**
- **From "audit annually" to "audit log streamed to SIEM in real time."**

Runtime security in Kubernetes is the *continuous* version of the static controls. Static controls answer "should this be allowed to exist?" Runtime answers "is what is now happening allowed?" The two questions have different answers because containers do not behave at runtime the way their manifests suggest at admission.

### 2.2 Container escape is the killer phase

Of all the ATT&CK phases, *container escape* (T1611) is the one that turns a contained breach into a cluster compromise. The historical mechanisms — `runc` CVE-2019-5736, dirty pipe (CVE-2022-0847), dirty cred, CAP_SYS_ADMIN+mount, hostPath, hostPID — all bypass the container boundary and land the attacker in the node's root namespace. Once on the node, the attacker has the kubelet credential, the container runtime socket, and physical access to every other Pod's filesystem.

Runtime security cares disproportionately about preventing and detecting escape, because:

- A successful escape *reduces* the value of every admission control (the attacker is now off the policy path).
- Escape is rare enough that *any signal of it* is high-fidelity (low false-positive rate).
- The consequences are catastrophic (worker → cluster admin).

Most of the Falco default ruleset, most of Tetragon's example policies, and most of the kube-hunter findings target escape. That is intentional.

---

## 3. Defense in Depth: The Full Stack

Visualizing the chapter map:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        DEFENSE-IN-DEPTH STACK                            │
│                                                                          │
│   (left = earliest in lifecycle; right = latest)                         │
│                                                                          │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│   │  Build   │  │ Registry │  │Admission │  │ Runtime  │  │ Forensics│  │
│   └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │
│                                                                          │
│   SBOM           cosign        PSA            seccomp        audit log   │
│   scan           signed        Kyverno        AppArmor       Falco logs  │
│   SAST           SBOM stored   Gatekeeper     SELinux        Tetragon    │
│   dep audit      provenance    VAP            NetworkPolicy  Hubble      │
│   (ch 27)        (ch 27)       (this ch §4-  workload-id    SIEM         │
│                                 §15)          (ch 07)        (this §24)  │
│                                                                          │
│                                                Falco runtime             │
│                                                Tetragon enforce          │
│                                                Tracee correlate          │
│                                                sandbox runtime           │
│                                                (ch 29)                   │
│                                                                          │
│                                  Detect ─────► Contain ─────► Recover    │
│                                                  (this ch §27)           │
└──────────────────────────────────────────────────────────────────────────┘
```

The mental model: each layer is a *filter*. A threat that gets past one is handed to the next. The job of a staff engineer is to make sure no single layer is load-bearing — losing any one layer should not result in cluster compromise.

| Layer | Catches | Misses | Chapter |
|-------|---------|--------|---------|
| Image signing | Unsigned/malicious images | Bugs in signed images | 27 |
| Admission policy | Misconfigured Pods | Lateral once admitted | 06 + here |
| Workload identity | Stolen long-lived tokens | Stolen short-lived in the moment | 07 |
| NetworkPolicy | Lateral connections | Allowed connections that are abused | 20 |
| Runtime detection | Anomalous syscalls/network | Detection blind spots | here §16-§19 |
| Sandbox runtime | Kernel-level escape | App-level vulns | 29 |
| Audit + SIEM | Post-incident reconstruction | Real-time prevention | here §24 |

This chapter spends most of its pages on the **admission policy** layer (§4–§15) and the **runtime detection** layer (§16–§19), because those two layers are where the most code is written and the most decisions made. The other layers each have their own chapter.

---

## 4. Pod Security Admission (PSA)

Pod Security Admission is the built-in admission controller that replaced PodSecurityPolicy (PSP) when PSP was removed in 1.25. It is in-tree, has no CRDs, and is configured by *namespace labels*. It is the simplest viable policy floor and there is no excuse not to have it turned on.

### 4.1 Where PSA lives in the apiserver

PSA is implemented at `kubernetes/kubernetes/staging/src/k8s.io/pod-security-admission/admission/admission.go`. It is registered as an admission plugin named `PodSecurity` and is *enabled by default* since 1.23. It is a validating admission plugin (it never mutates) and runs in the validating phase, after mutating webhooks (chapter 06).

```
                      apiserver admission chain (simplified)

  HTTP POST /api/v1/namespaces/foo/pods
        │
        ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  Authentication                                                 │
  │  Authorization                                                  │
  │  Mutating admission (webhooks + builtin)                        │
  │      ─ Defaulter (PodTemplate defaults)                         │
  │      ─ ServiceAccount injection                                 │
  │      ─ MutatingWebhook(s)  (Kyverno mutate, Istio sidecar...)   │
  │  Schema validation (OpenAPI)                                    │
  │  Validating admission (webhooks + builtin)                      │
  │      ─ ResourceQuota                                            │
  │      ─ LimitRanger                                              │
  │      ─ PodSecurity   ◄──────── PSA evaluates here               │
  │      ─ ValidatingWebhook(s)   (Kyverno validate, Gatekeeper...) │
  │      ─ ValidatingAdmissionPolicy  (in-process CEL)              │
  │  Persistence to etcd                                            │
  └─────────────────────────────────────────────────────────────────┘
```

### 4.2 The three modes and three profiles

PSA is a 3×3 matrix:

```
                        ┌──────────────┬──────────────┬──────────────┐
                        │  privileged  │   baseline   │  restricted  │
        ┌───────────────┼──────────────┼──────────────┼──────────────┤
        │   enforce     │   no-op      │  reject Pod  │  reject Pod  │
        ├───────────────┼──────────────┼──────────────┼──────────────┤
        │    warn       │   no-op      │ kubectl warn │ kubectl warn │
        ├───────────────┼──────────────┼──────────────┼──────────────┤
        │    audit      │   no-op      │  audit event │  audit event │
        └───────────────┴──────────────┴──────────────┴──────────────┘
```

You set this per namespace with labels:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: payments
  labels:
    # Enforce the restricted profile. Pods that violate it are rejected.
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.29

    # Warn the user (kubectl shows the warning) for violations of baseline.
    pod-security.kubernetes.io/warn: baseline
    pod-security.kubernetes.io/warn-version: v1.29

    # Audit-log violations of restricted (so you can dashboard them).
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/audit-version: v1.29
```

The `-version` pin matters: PSA evolves between K8s versions. A new K8s release may tighten a profile. Pinning the version means the *behavior is reproducible* until you explicitly bump it. Without a pin you get the *latest* version on every apiserver, which is great for security and terrible for "this Pod started failing after the cluster upgrade."

### 4.3 Cluster-wide defaults

You can also configure cluster-wide PSA defaults via an `AdmissionConfiguration` file passed to the apiserver:

```yaml
# /etc/kubernetes/admission-config.yaml
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
      namespaces: [kube-system, kube-public, kube-node-lease]
```

Then the apiserver flag:

```
--admission-control-config-file=/etc/kubernetes/admission-config.yaml
```

The cluster-wide default is the floor; per-namespace labels can only *tighten* it in well-run clusters (you control which namespaces exist). The exemption list is dangerous — anything in there bypasses PSA entirely. `kube-system` is exempt by default because the kubelet and CNI pods need privileged. Treat the exemption list like sudoers.

---

## 5. PSA Profiles in Detail: privileged / baseline / restricted

Each profile is defined in code at `kubernetes/kubernetes/staging/src/k8s.io/pod-security-admission/policy/`. Let us go through the fields each one cares about, because "the restricted profile is enforced" is not a useful sentence if you cannot tell a developer *which field they need to fix*.

### 5.1 privileged

Effectively the off switch. No restrictions. Reserved for trusted system workloads. The `kube-system` namespace runs as privileged because the kubelet itself needs `hostNetwork`, CSI drivers need `privileged: true`, etc.

### 5.2 baseline — the "don't shoot yourself in the foot" profile

Baseline blocks the most dangerous fields. Anything an attacker would actively use for escape is blocked here. Anything that is merely *suboptimal* (running as root, no resource limits) is *not* blocked.

| Field | Allowed values for baseline |
|-------|----------------------------|
| `spec.hostNetwork` | must be unset or `false` |
| `spec.hostPID` | must be unset or `false` |
| `spec.hostIPC` | must be unset or `false` |
| `spec.containers[*].ports[*].hostPort` | must be unset or `0` |
| `spec.containers[*].securityContext.privileged` | must be unset or `false` |
| `spec.containers[*].securityContext.capabilities.add` | only from the allowed set (see below) |
| `spec.volumes[*].hostPath` | forbidden |
| `spec.containers[*].securityContext.procMount` | must be `Default` (i.e., not `Unmasked`) |
| `spec.containers[*].securityContext.allowPrivilegeEscalation` | any (NOT restricted by baseline) |
| `spec.securityContext.seccompProfile.type` | any (NOT restricted by baseline) |
| AppArmor annotation | only `runtime/default`, `localhost/*`, or `unconfined` (with unconfined gated) |
| SELinux | type must be empty / container_t / container_init_t / container_kvm_t |
| `spec.containers[*].securityContext.capabilities.add` allowed set | `AUDIT_WRITE`, `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `FSETID`, `KILL`, `MKNOD`, `NET_BIND_SERVICE`, `SETFCAP`, `SETGID`, `SETPCAP`, `SETUID`, `SYS_CHROOT` |
| `spec.hostUsers` | must be `true` or unset (1.30+) |

Notably *not* blocked by baseline: running as root (uid 0), no seccomp profile, no resource limits, no `runAsNonRoot`. These are restricted-profile concerns.

### 5.3 restricted — the actually-hardened profile

Restricted is the profile you want for tenant workloads. It is strict enough that most off-the-shelf Helm charts will fail to deploy without modification, which is a *feature* — the Helm chart is being told it cannot run as root or with no resource limits.

| Field | Required value for restricted |
|-------|------------------------------|
| All of baseline | unchanged |
| `spec.volumes[*]` | only `configMap`, `csi`, `downwardAPI`, `emptyDir`, `ephemeral`, `image` (1.31+), `persistentVolumeClaim`, `projected`, `secret` |
| `spec.containers[*].securityContext.allowPrivilegeEscalation` | must be `false` |
| `spec.containers[*].securityContext.capabilities.drop` | must contain `ALL` |
| `spec.containers[*].securityContext.capabilities.add` | only `NET_BIND_SERVICE` |
| `spec.containers[*].securityContext.runAsNonRoot` | must be `true` (at pod or container level) |
| `spec.containers[*].securityContext.runAsUser` | must NOT be `0` if set |
| `spec.containers[*].securityContext.seccompProfile.type` | must be `RuntimeDefault` or `Localhost` |
| `spec.securityContext.seccompProfile.type` | (alt location) must be `RuntimeDefault` or `Localhost` |

A minimal Pod that passes `restricted`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: hardened
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 10000
    runAsGroup: 10000
    fsGroup: 10000
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: app
    image: registry.corp/app:1.2.3
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
      readOnlyRootFilesystem: true
    resources:
      requests: { cpu: 100m, memory: 128Mi }
      limits:   { cpu: 500m, memory: 256Mi }
```

`readOnlyRootFilesystem` is *not* required by PSA restricted. It is good practice, but PSA does not enforce it. Use Kyverno/Gatekeeper for that.

### 5.4 The PSA evaluation order

When a Pod admission request lands, PSA:

1. Resolves the namespace's `enforce`, `warn`, `audit` labels (or cluster default).
2. Resolves the version pin (or `latest`).
3. Evaluates the Pod spec against the resolved profile at that version.
4. If `enforce` fails → reject with HTTP 400 and a human-readable list of violations.
5. If `warn` fails → attach a warning to the response (`kubectl` prints it).
6. If `audit` fails → annotate the audit event.

The rejection response is structured. Example:

```
Error from server (Forbidden): error when creating "pod.yaml":
pods "hardened" is forbidden:
violates PodSecurity "restricted:v1.29":
  allowPrivilegeEscalation != false (container "app" must set
    securityContext.allowPrivilegeEscalation=false),
  unrestricted capabilities (container "app" must set
    securityContext.capabilities.drop=["ALL"]),
  runAsNonRoot != true (pod or container "app" must set
    securityContext.runAsNonRoot=true)
```

Multi-violation responses help developers fix everything in one round trip, rather than fix-resubmit-fix-resubmit.

---

## 6. PSA Limitations and When You Outgrow It

PSA is great as a floor. It is not enough for any cluster with more than one team. Reasons:

1. **Namespace-wide.** You cannot say "this Pod in the `payments` namespace is exempt." The unit of granularity is the namespace. If one workload needs a CAP_NET_ADMIN, the entire namespace drops to baseline.
2. **No expression language.** PSA cannot say "image must be from `registry.corp/`." It only checks the K8s securityContext fields hard-coded into the policy.
3. **Pod admission only.** PSA fires when a Pod is created. It does NOT fire when the spec is updated by replacing a Deployment's PodTemplate. The new Pods get checked when they are created, but if your update changes the *image* the running Pods don't get re-checked. PSA also does not re-evaluate existing Pods when you tighten the namespace label — old Pods keep running.
4. **No mutation.** PSA never sets a default. If a developer forgets `seccompProfile`, PSA rejects them. A policy engine could inject it for them.
5. **No cross-resource view.** PSA cannot say "this Pod's referenced ServiceAccount must have `automountServiceAccountToken: false`."

These are the points at which you reach for Kyverno, Gatekeeper, or VAP. PSA is the moat at the gate; the policy engine is the courtyard guard with a list and a flashlight.

---

## 7. OPA Gatekeeper Deep Look

Gatekeeper is the Kubernetes integration of Open Policy Agent (OPA). The OPA project is at `open-policy-agent/opa`; the K8s integration is at `open-policy-agent/gatekeeper`. Gatekeeper has been the de-facto policy engine for K8s for years and remains a strong choice when you need Rego's expressiveness.

### 7.1 Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│   gatekeeper-system  namespace                                       │
│                                                                      │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │  Deployment: gatekeeper-controller-manager (3 replicas)    │    │
│   │  ┌──────────────────────────────────────────────────────┐  │    │
│   │  │  ValidatingWebhookConfiguration  ─►  pod:8443       │  │    │
│   │  │  MutatingWebhookConfiguration    ─►  pod:8443       │  │    │
│   │  │  Embedded OPA evaluator (Rego)                       │  │    │
│   │  │  Constraint/ConstraintTemplate controller            │  │    │
│   │  │  Sync controller (replicate K8s objs to OPA cache)   │  │    │
│   │  └──────────────────────────────────────────────────────┘  │    │
│   └────────────────────────────────────────────────────────────┘    │
│                                                                      │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │  Deployment: gatekeeper-audit (1 replica, singleton)       │    │
│   │  Periodically lists all resources in cluster and           │    │
│   │  evaluates constraints; writes violations into             │    │
│   │  Constraint.status.violations                              │    │
│   └────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘

         ▲                                              ▲
         │ admission requests                           │ list objects
         │                                              │
   ┌─────┴───────────┐                          ┌───────┴────────┐
   │   kube-apiserver │                          │  kube-apiserver │
   └─────────────────┘                          └────────────────┘
```

The two Deployments do different things:

- **gatekeeper-controller-manager**: the admission webhook. Synchronously evaluates constraints on every CREATE/UPDATE for the kinds it cares about. Latency-critical; HA via multiple replicas.
- **gatekeeper-audit**: the audit controller. Singleton. Periodically (default every 60s) walks every resource of every kind a constraint matches, evaluates the constraint, and writes the violations into the Constraint's status. This is how you find pre-existing violations that were created before the policy was installed.

The webhook can be configured with `failurePolicy: Ignore` (the default in old versions) or `failurePolicy: Fail`. `Ignore` means a Gatekeeper outage does not block your cluster — and silently lets policy violations through. `Fail` is safer but ties your cluster's availability to Gatekeeper's. Pick deliberately; the safe production answer is `Fail` with multiple replicas and a robust deployment.

### 7.2 ConstraintTemplate and Constraint

Gatekeeper splits the policy into two CRDs:

1. **ConstraintTemplate**: defines a *kind* of constraint, including its Rego logic and the CRD schema for parameters. Cluster-scoped. Installed by a platform team.
2. **Constraint** (an instance of the ConstraintTemplate's generated CRD): an *instance* with match criteria and parameters. Cluster-scoped. Installed by either platform or app teams.

Example ConstraintTemplate:

```yaml
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata:
  name: k8srequiredlabels
spec:
  crd:
    spec:
      names:
        kind: K8sRequiredLabels
      validation:
        openAPIV3Schema:
          type: object
          properties:
            labels:
              type: array
              items:
                type: string
  targets:
  - target: admission.k8s.gatekeeper.sh
    rego: |
      package k8srequiredlabels

      violation[{"msg": msg, "details": {"missing_labels": missing}}] {
        provided := {label | input.review.object.metadata.labels[label]}
        required := {label | label := input.parameters.labels[_]}
        missing := required - provided
        count(missing) > 0
        msg := sprintf("you must provide labels: %v", [missing])
      }
```

This creates a CRD called `K8sRequiredLabels`. Now you can create instances:

```yaml
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sRequiredLabels
metadata:
  name: ns-must-have-owner
spec:
  match:
    kinds:
    - apiGroups: [""]
      kinds: ["Namespace"]
  parameters:
    labels: ["owner", "cost-center"]
```

The match block is rich:

```yaml
match:
  kinds:
  - apiGroups: ["apps"]
    kinds: ["Deployment", "StatefulSet"]
  namespaces: ["payments", "checkout"]
  excludedNamespaces: ["kube-system"]
  labelSelector:
    matchLabels: { team: platform }
  scope: Namespaced     # or Cluster
  name: "my-*"          # glob match on resource name
```

### 7.3 The sync controller and OPA's cache

Some policies need to look at *other* resources. "This Pod must use an Image already approved in this ConfigMap." "This Service must not have the same selector as an existing Service." Rego cannot make K8s API calls from inside its evaluator — those evaluations need to be fast and deterministic. So Gatekeeper has a *sync controller* that mirrors selected K8s resources into OPA's in-memory cache, accessible from Rego as `data.inventory`.

You configure what to sync via a `Config` resource:

```yaml
apiVersion: config.gatekeeper.sh/v1alpha1
kind: Config
metadata:
  name: config
  namespace: gatekeeper-system
spec:
  sync:
    syncOnly:
    - group: ""
      version: "v1"
      kind: "Namespace"
    - group: ""
      version: "v1"
      kind: "ConfigMap"
    - group: "apps"
      version: "v1"
      kind: "Deployment"
```

In Rego, those are now visible as:

```rego
data.inventory.cluster.v1.Namespace[name]
data.inventory.namespace[ns_name].v1.ConfigMap[cm_name]
```

The pitfall: if a constraint references a kind that is NOT in the sync config, Rego silently sees an empty set and your policy *fails open* — it thinks there are no violations because it cannot see the data. Gatekeeper 3.10+ has stricter sync validation; older versions require manual checks. Always test policies in audit mode first.

### 7.4 The audit controller in detail

```
   gatekeeper-audit Pod (Deployment, 1 replica)
   ┌──────────────────────────────────────────────────────┐
   │  loop forever:                                       │
   │    every audit-interval (default 60s):               │
   │      for each ConstraintKind installed:              │
   │        for each Constraint of that kind:             │
   │          list all objects matching its `match`:      │
   │            evaluate Rego                              │
   │            if violation: record to ring buffer        │
   │      write status.violations on each Constraint       │
   │      emit audit metrics                              │
   └──────────────────────────────────────────────────────┘
```

You can configure:

- `--audit-interval=60s`
- `--constraint-violations-limit=20` — how many violations to record per Constraint.status (caps memory)
- `--audit-from-cache=true` — read from OPA's cache (fast) vs from the apiserver (slow but fresh)

The audit controller is how you handle the "we just installed this policy, what's already violating it?" question. Without the audit controller, the only way to know is to wait for an admission event, which won't happen for already-running Pods.

### 7.5 enforcementAction

A Constraint can be `deny` (reject the admission), `dryrun` (audit only, never reject), or `warn` (annotate the response with a warning but admit):

```yaml
spec:
  enforcementAction: dryrun
```

The standard rollout pattern is dryrun → warn → deny, with at least a week in each phase to gather audit data.

---

## 8. A Rego Primer (Just Enough)

Rego is a logic-style language used by OPA. Gatekeeper's constraint templates embed Rego. Just enough to read the examples in this chapter:

### 8.1 Packages and rules

```rego
package mypolicy

# A rule is a logical assertion that "x is true if these conditions hold."
violation[{"msg": msg}] {
  some i
  input.review.object.spec.containers[i].securityContext.privileged == true
  msg := sprintf("container %v is privileged", [input.review.object.spec.containers[i].name])
}
```

Gatekeeper convention: the rule is named `violation`, returns a set of violations. The body is a list of conditions ANDed together. If all conditions hold, the violation is emitted.

### 8.2 input and parameters

- `input.review.object` — the object being admitted.
- `input.review.oldObject` — the previous version (on UPDATE).
- `input.review.operation` — `CREATE`, `UPDATE`, `DELETE`.
- `input.parameters` — values from the Constraint instance.
- `data.inventory` — synced K8s state.

### 8.3 Common patterns

```rego
# "Some container has X"
violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  container.image == "nginx:latest"
  msg := "no :latest"
}

# "All containers must have X"
violation[{"msg": msg}] {
  container := input.review.object.spec.containers[_]
  not container.resources.limits.memory
  msg := sprintf("container %v missing memory limit", [container.name])
}

# Cross-reference to another resource
violation[{"msg": msg}] {
  ns := input.review.object.metadata.namespace
  not data.inventory.cluster.v1.Namespace[ns].metadata.labels.owner
  msg := sprintf("namespace %v has no owner label", [ns])
}
```

The `_` in `containers[_]` means "any index" — equivalent to a `for` loop. `not` negates. `some` declares a variable.

Rego is more expressive than most YAML-based engines but also more error-prone. A common bug: forgetting that an empty set is "no violation" rather than "policy bypass." This is why audit mode is essential.

---

## 9. Gatekeeper Mutation

Gatekeeper added mutation in 3.6 (2021). It is opt-in via the `mutation` feature flag and uses four CRDs:

| CRD | What it does |
|-----|--------------|
| `Assign` | Set a field to a value. |
| `AssignMetadata` | Set a metadata field (label/annotation). |
| `ModifySet` | Add/remove items from a list. |
| `AssignImage` | Replace image reference (registry, repo, tag, digest). |

Example: inject default seccomp profile:

```yaml
apiVersion: mutations.gatekeeper.sh/v1
kind: Assign
metadata:
  name: pod-seccomp-default
spec:
  applyTo:
  - groups: [""]
    versions: ["v1"]
    kinds: ["Pod"]
  match:
    scope: Namespaced
    kinds:
    - apiGroups: [""]
      kinds: ["Pod"]
  location: "spec.securityContext.seccompProfile.type"
  parameters:
    assign:
      value: "RuntimeDefault"
    pathTests:
    - subPath: "spec.securityContext.seccompProfile.type"
      condition: MustNotExist
```

The `pathTests` block makes this *conditional*: only inject if the type is not already set, so you do not overwrite a user-provided value.

Mutation in Gatekeeper is solid but less ergonomic than Kyverno's. The Gatekeeper team has explicitly said Gatekeeper is "validation-first," and many users keep mutation in Kyverno and validation in Gatekeeper. That said, mixing two engines doubles your operational surface.

---

## 10. Kyverno Deep Look

Kyverno started in 2019 with a thesis: most policy logic does not need a language as expressive as Rego, and YAML-native policy is easier for K8s users. It has since grown to support image signing verification, generation, cleanup, and ImageVerification policies that are nearly impossible to express in Gatekeeper. Source is at `kyverno/kyverno`.

### 10.1 Architecture

Kyverno splits its responsibilities into four controllers, each shipped as a Deployment in the `kyverno` namespace:

```
┌─────────────────────────────────────────────────────────────────────┐
│   kyverno namespace                                                 │
│                                                                     │
│   ┌─────────────────────────┐   admission webhook                  │
│   │  kyverno-admission-     │ ◄────────── kube-apiserver           │
│   │  controller (3 replicas)│   (mutate + validate + imageVerify)  │
│   └─────────────────────────┘                                       │
│                                                                     │
│   ┌─────────────────────────┐   reports                            │
│   │  kyverno-reports-       │ ─────────► PolicyReport CRDs         │
│   │  controller (1)         │                                       │
│   └─────────────────────────┘                                       │
│                                                                     │
│   ┌─────────────────────────┐   background scan                    │
│   │  kyverno-background-    │ ─────────► reconciles existing       │
│   │  controller (1)         │            resources vs policy        │
│   └─────────────────────────┘                                       │
│                                                                     │
│   ┌─────────────────────────┐   cleanup jobs                       │
│   │  kyverno-cleanup-       │ ─────────► CronJobs that delete      │
│   │  controller (1)         │            old resources              │
│   └─────────────────────────┘                                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

The split exists because each role has different scaling, criticality, and operational characteristics:

- **admission-controller**: synchronous, latency-critical, HA.
- **reports-controller**: writes PolicyReport CRs; can lag.
- **background-controller**: handles generate/mutate-existing/cleanup reconciliation.
- **cleanup-controller**: special-purpose for `CleanupPolicy`.

### 10.2 ClusterPolicy and Policy

Two CRDs:

- **ClusterPolicy**: cluster-scoped. Applies across all namespaces by default; can be scoped via `match`.
- **Policy**: namespace-scoped. Useful for tenant-local policies.

Most policies are ClusterPolicy. Policy is for letting tenants own their own policies.

### 10.3 Policy structure

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-resources
spec:
  validationFailureAction: Enforce   # Enforce or Audit
  background: true                   # also evaluate against existing resources
  rules:
  - name: validate-resources
    match:
      any:
      - resources:
          kinds: ["Pod"]
    exclude:
      any:
      - resources:
          namespaces: ["kube-system", "kyverno"]
    validate:
      message: "Resource limits required"
      pattern:
        spec:
          containers:
          - name: "*"
            resources:
              limits:
                memory: "?*"
                cpu: "?*"
```

The `?*` pattern means "any non-empty value." Patterns in Kyverno use a JSON-like shape with wildcards: `*` (any value), `?*` (any non-empty), `!value` (anything but this), `>10` (numeric).

### 10.4 match / exclude

Both `match` and `exclude` use the `any`/`all` selector pattern.

```yaml
match:
  any:                      # match if any condition holds
  - resources:
      kinds: ["Pod"]
      namespaces: ["prod-*"]
  - resources:
      kinds: ["Deployment"]
      selector:
        matchLabels:
          team: payments
  all:                      # additionally require all of these
  - resources:
      operations: ["CREATE", "UPDATE"]
```

### 10.5 autogen — the magic

If you write a policy that matches Pod, Kyverno *automatically* generates equivalent rules for Deployment, ReplicaSet, DaemonSet, StatefulSet, Job, CronJob. So this:

```yaml
rules:
- name: no-latest
  match:
    any: [{ resources: { kinds: ["Pod"] } }]
  validate:
    message: "no :latest"
    pattern:
      spec:
        containers:
        - image: "!*:latest"
```

…also applies to Deployment etc, even though you never wrote the deployment match. The reason: many policies are about Pod-shaped resources. Without autogen you would write the same rule six times.

You can opt out via the annotation `pod-policies.kyverno.io/autogen-controllers: none` or restrict to specific controllers.

### 10.6 failureAction: Audit vs Enforce

`validationFailureAction` (now `failureAction` on the rule in newer versions) decides whether a failed validation blocks admission or just generates a report.

```yaml
spec:
  rules:
  - name: warn-only
    failureAction: Audit
    ...
  - name: must-block
    failureAction: Enforce
    ...
```

Same rollout pattern as Gatekeeper: ship a policy in Audit, watch PolicyReport for a week, fix the existing violations, flip to Enforce.

---

## 11. Kyverno Rule Types: validate, mutate, generate, cleanup, verifyImages

### 11.1 validate

Already shown. Two evaluation styles:

- `validate.pattern` — JSON pattern match.
- `validate.deny` — boolean expressions (`conditions: any/all`).
- `validate.anyPattern` — pass if *any* of several patterns match.
- `validate.foreach` — iterate over a list and apply the same validation.

`validate.deny`:

```yaml
validate:
  message: "deny if hostNetwork"
  deny:
    conditions:
      any:
      - key: "{{ request.object.spec.hostNetwork }}"
        operator: Equals
        value: true
```

### 11.2 mutate

```yaml
rules:
- name: add-default-resources
  match:
    any: [{ resources: { kinds: ["Pod"] } }]
  mutate:
    patchStrategicMerge:
      spec:
        containers:
        - (name): "*"
          resources:
            limits:
              +(cpu): "500m"
              +(memory): "256Mi"
```

The `(name): "*"` selects all containers; `+(cpu)` means "add only if not present" (conditional anchor). Strategic merge is the same algorithm K8s itself uses for `kubectl apply`.

For arbitrary JSON Patch:

```yaml
mutate:
  patchesJson6902: |-
    - path: "/spec/template/spec/automountServiceAccountToken"
      op: add
      value: false
```

`mutate.foreach` lets you iterate over a list (e.g., apply patch to each container).

### 11.3 generate

Kyverno can *create* resources in response to other resources. The classic use case: when a Namespace is created, generate a default NetworkPolicy.

```yaml
rules:
- name: default-deny-netpol
  match:
    any: [{ resources: { kinds: ["Namespace"] } }]
  generate:
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    name: default-deny
    namespace: "{{ request.object.metadata.name }}"
    synchronize: true
    data:
      spec:
        podSelector: {}
        policyTypes: ["Ingress", "Egress"]
```

`synchronize: true` means Kyverno will keep the generated resource in sync with the policy. If someone deletes the generated NetworkPolicy, Kyverno recreates it. If you change the policy, Kyverno updates all generated resources.

### 11.4 cleanup

```yaml
apiVersion: kyverno.io/v2beta1
kind: ClusterCleanupPolicy
metadata:
  name: clean-old-jobs
spec:
  match:
    any:
    - resources:
        kinds: ["Job"]
  conditions:
    all:
    - key: "{{ time_since('', '{{ target.status.completionTime }}', '') }}"
      operator: GreaterThan
      value: "168h"
  schedule: "0 * * * *"
```

CleanupPolicy uses a CronJob under the hood. Useful for GC of completed Jobs, old PolicyReports, expired ResourceClaims, etc.

### 11.5 verifyImages

The one Kyverno feature with no real Gatekeeper equivalent. Verifies cosign or notary signatures and attestations at admission. Tightly integrated with chapter 27.

```yaml
rules:
- name: verify-signature
  match:
    any: [{ resources: { kinds: ["Pod"] } }]
  verifyImages:
  - imageReferences:
    - "registry.corp/*"
    attestors:
    - entries:
      - keys:
          publicKeys: |-
            -----BEGIN PUBLIC KEY-----
            MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
            -----END PUBLIC KEY-----
    mutateDigest: true       # replace tag with digest in the Pod spec
    verifyDigest: true
    required: true
```

`mutateDigest: true` is the killer feature: Kyverno *rewrites the image reference* to the digest after verifying the signature, eliminating TOCTOU between admission and pull.

`verifyImages` also supports keyless cosign (`certificates`, `keyless`), SLSA provenance (`attestations`), and policy on the in-toto attestation body. See chapter 27 for the broader supply-chain context.

---

## 12. ValidatingAdmissionPolicy and CEL (In-Process Admission)

`ValidatingAdmissionPolicy` (VAP) and `ValidatingAdmissionPolicyBinding` are an in-tree admission mechanism that uses CEL (Common Expression Language) instead of webhooks. GA in 1.30. Source at `kubernetes/kubernetes/staging/src/k8s.io/apiserver/pkg/admission/plugin/policy/validating/`.

The pitch: most admission policies are one-liner expressions. Running an out-of-process webhook (with mTLS, deployment, certs, replicas) for a one-liner is overkill. VAP evaluates CEL *inside the apiserver process*, with no network hop, no webhook to deploy, and the CEL compiler verifies the expression's cost upper bound so it cannot DoS the apiserver.

### 12.1 Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                       kube-apiserver                               │
│                                                                    │
│   admission chain                                                  │
│       ...                                                          │
│       ValidatingWebhook ─► [network] ─► webhook pod ─► [network]   │
│       ValidatingAdmissionPolicy ─► CEL evaluator (in-process)      │
│                                       (~µs, no network)            │
│       ...                                                          │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 12.2 VAP + VAPBinding

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: no-latest-tag
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
      object.spec.containers.all(c, !c.image.endsWith(":latest"))
    message: "no :latest tag allowed"
    reason: Invalid
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: no-latest-tag-binding
spec:
  policyName: no-latest-tag
  validationActions: [Deny]
  matchResources:
    namespaceSelector:
      matchExpressions:
      - key: pod-security.kubernetes.io/enforce
        operator: NotIn
        values: ["privileged"]
```

The split between policy and binding is similar to Gatekeeper's template-and-instance pattern. One policy can have many bindings, each with different match criteria.

### 12.3 CEL: the language

CEL was originally developed by Google for Borg and has since become the standard expression language across the K8s API (CRD validation rules, CEL admission policies, authorization rules, even ConfigurationVariant). Worth learning. Spec is at `google/cel-spec`.

Key features:

- Strongly typed, no nulls (Optional types).
- No I/O, no loops (only macros like `all`, `exists`, `filter`, `map`).
- Cost-bounded by the compiler.
- ~µs evaluation time per expression.

Common K8s patterns:

```cel
# All containers must drop ALL capabilities
object.spec.containers.all(c,
  has(c.securityContext) &&
  has(c.securityContext.capabilities) &&
  has(c.securityContext.capabilities.drop) &&
  c.securityContext.capabilities.drop.exists(d, d == "ALL"))

# Image from approved registry
object.spec.containers.all(c, c.image.startsWith("registry.corp/"))

# Resources required
object.spec.containers.all(c,
  has(c.resources) &&
  has(c.resources.limits) &&
  has(c.resources.limits.memory) &&
  has(c.resources.limits.cpu))

# At least one of two labels
"team" in object.metadata.labels || "service" in object.metadata.labels

# Cross-version comparison (on UPDATE)
object.spec.replicas <= oldObject.spec.replicas + 5
```

### 12.4 Variables, matchConditions, auditAnnotations

```yaml
spec:
  matchConditions:
  - name: 'exclude-leases'
    expression: '!(request.resource.group == "coordination.k8s.io" && request.resource.resource == "leases")'

  variables:
  - name: containers
    expression: 'object.spec.containers + object.spec.initContainers'
  - name: approvedRegistry
    expression: '"registry.corp/"'

  validations:
  - expression: |
      variables.containers.all(c, c.image.startsWith(variables.approvedRegistry))
    messageExpression: |
      "image %s not from approved registry %s".format([
        variables.containers.filter(c, !c.image.startsWith(variables.approvedRegistry))[0].image,
        variables.approvedRegistry
      ])

  auditAnnotations:
  - key: violation-summary
    valueExpression: |
      "image-policy violations: " + string(variables.containers.filter(c, !c.image.startsWith(variables.approvedRegistry)).size())
```

- `matchConditions`: cheap filter before the full validations evaluate. Skip-conditions to reduce evaluation cost.
- `variables`: factor out repeated subexpressions; computed once, used in many validations.
- `messageExpression`: dynamic error message with field values.
- `auditAnnotations`: emitted into the apiserver audit log on every evaluation, even if it passes. Useful for SIEM correlation.

### 12.5 paramRef and external data

A VAP can reference a *parameter resource* — typically a ConfigMap or CRD — for parameter values. This is roughly equivalent to Gatekeeper's Constraint parameters.

```yaml
spec:
  paramKind:
    apiVersion: v1
    kind: ConfigMap
  validations:
  - expression: |
      object.spec.replicas <= int(params.data.maxReplicas)
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
spec:
  policyName: replica-cap
  paramRef:
    name: replica-limits
    namespace: kube-system
```

You change the ConfigMap, all bindings see the new value. No restart.

### 12.6 Cost limits and verifier

The CEL compiler has a cost estimator. Each expression has a "cost budget"; if the compiler computes that the expression can exceed the budget for any input (worst-case), it rejects the policy at install time. This is what makes VAP safe to run in the apiserver — there is no equivalent guarantee for a webhook.

You will occasionally hit a cost limit error like:

```
ValidationError(spec.validations[0].expression): estimated cost 10000001 exceeds 10000000
```

Solutions: factor out a variable, use `matchConditions` to short-circuit, reduce the depth of `all`/`exists` nesting.

---

## 13. MutatingAdmissionPolicy

`MutatingAdmissionPolicy` (alpha in 1.32, beta target 1.34) is the CEL-based mutation counterpart. The pattern:

```yaml
apiVersion: admissionregistration.k8s.io/v1alpha1
kind: MutatingAdmissionPolicy
metadata:
  name: default-seccomp
spec:
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE"]
      resources: ["pods"]
  reinvocationPolicy: IfNeeded
  failurePolicy: Fail
  mutations:
  - patchType: ApplyConfiguration
    applyConfiguration:
      expression: |
        Object{
          spec: Object.spec{
            securityContext: Object.spec.securityContext{
              seccompProfile: Object.spec.securityContext.seccompProfile{
                type: "RuntimeDefault"
              }
            }
          }
        }
```

The `Object{...}` syntax is server-side-apply-style: it expresses the *fields we want to set*; other fields are left untouched.

You can also use JSON Patch:

```yaml
mutations:
- patchType: JSONPatch
  jsonPatch:
    expression: |
      [
        JSONPatch{op: "add", path: "/metadata/labels/managed-by", value: "platform"}
      ]
```

---

## 14. VAP vs Kyverno vs Gatekeeper: The Decision Matrix

| Capability | VAP/MAP | Kyverno | Gatekeeper |
|------------|---------|---------|------------|
| Language | CEL | YAML + JMESPath + CEL (1.10+) | Rego |
| In-process | yes | no (webhook pod) | no (webhook pod) |
| Validate | yes | yes | yes |
| Mutate | MAP (alpha) | yes | yes (3.6+) |
| Generate (create dep resources) | no | yes | no |
| Cleanup | no | yes | no |
| Verify image signatures | no | yes | only via Rego call-out |
| External data (sync controller) | paramRef (ConfigMap/CR) | API call from policy | sync controller |
| Audit existing | apiserver audit log | PolicyReport + background scan | gatekeeper-audit |
| Cost-bounded | yes (CEL verifier) | no (webhook can do anything) | no |
| Failure if engine down | configurable; built-in apiserver | configurable; webhook | configurable; webhook |
| Latency overhead | µs | network hop (~1-5 ms) | network hop (~1-5 ms) |
| K8s-native | yes (in-tree) | CRDs | CRDs |
| Operational footprint | none | 3 Deployments | 2 Deployments |
| Maturity | GA in 1.30 (1.32 for MAP alpha) | GA, prod-tested | GA, prod-tested |

**Practical recommendation tree:**

```
                  ┌─────────────────────────────┐
                  │ Is policy one CEL expr,     │
                  │ no image-signing, no        │
                  │ cross-resource, no mutation? │
                  └────────────┬────────────────┘
                               │
                  ┌────────────┴───────────┐
                  │ Yes                    │ No
                  ▼                        ▼
              Use VAP            ┌──────────────────────┐
                                 │ Need image signature │
                                 │ verification?        │
                                 └───────┬──────────────┘
                                         │
                              ┌──────────┴───────────┐
                              │ Yes                  │ No
                              ▼                      ▼
                          Use Kyverno     ┌────────────────────┐
                                          │ Already have OPA   │
                                          │ skills, complex    │
                                          │ Rego rules?        │
                                          └─────┬──────────────┘
                                                │
                                       ┌────────┴────────┐
                                       │ Yes             │ No
                                       ▼                 ▼
                                   Gatekeeper        Use Kyverno
```

In greenfield clusters in 2025–2026, **Kyverno + VAP is the most common pairing**: VAP for simple rules, Kyverno for everything mutation/generate/verifyImages-related. Gatekeeper still has a strong installed base, especially where the team has invested in Rego across other domains (Conftest, Terraform OPA, etc.).

---

## 15. Policy Library: The Same Nine Policies in Three Engines

The most useful way to internalize the three engines is to see them side by side. We will write nine policies. Real ones, the kind every cluster needs.

### 15.1 Disallow `:latest` tag

**Kyverno:**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-latest-tag
spec:
  validationFailureAction: Enforce
  rules:
  - name: require-tag
    match: { any: [ { resources: { kinds: ["Pod"] } } ] }
    validate:
      message: "image must specify a tag other than :latest"
      pattern:
        spec:
          containers:
          - image: "!*:latest | *@sha256:*"
```

**Gatekeeper:**

```yaml
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata:
  name: k8sdisallowlatesttag
spec:
  crd:
    spec:
      names: { kind: K8sDisallowLatestTag }
  targets:
  - target: admission.k8s.gatekeeper.sh
    rego: |
      package k8sdisallowlatesttag
      violation[{"msg": msg}] {
        c := input.review.object.spec.containers[_]
        endswith(c.image, ":latest")
        msg := sprintf("container %v uses :latest", [c.name])
      }
      violation[{"msg": msg}] {
        c := input.review.object.spec.containers[_]
        not contains(c.image, ":")
        not contains(c.image, "@")
        msg := sprintf("container %v has no tag (implicit :latest)", [c.name])
      }
---
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sDisallowLatestTag
metadata: { name: no-latest }
spec:
  match: { kinds: [{ apiGroups: [""], kinds: ["Pod"] }] }
```

**VAP:**

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata: { name: no-latest }
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE","UPDATE"]
      resources: ["pods"]
  validations:
  - expression: |
      object.spec.containers.all(c,
        !c.image.endsWith(":latest") &&
        (c.image.contains(":") || c.image.contains("@")))
    message: "image must specify a non-:latest tag or digest"
```

### 15.2 Require resource limits

**Kyverno:**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: { name: require-resources }
spec:
  validationFailureAction: Enforce
  rules:
  - name: require-limits
    match: { any: [{ resources: { kinds: ["Pod"] } }] }
    validate:
      message: "containers must declare cpu and memory limits"
      pattern:
        spec:
          containers:
          - resources:
              limits:
                memory: "?*"
                cpu: "?*"
```

**Gatekeeper:**

```rego
package k8srequireresources
violation[{"msg": msg}] {
  c := input.review.object.spec.containers[_]
  not c.resources.limits.cpu
  msg := sprintf("container %v missing cpu limit", [c.name])
}
violation[{"msg": msg}] {
  c := input.review.object.spec.containers[_]
  not c.resources.limits.memory
  msg := sprintf("container %v missing memory limit", [c.name])
}
```

**VAP:**

```yaml
validations:
- expression: |
    object.spec.containers.all(c,
      has(c.resources) && has(c.resources.limits) &&
      has(c.resources.limits.memory) &&
      has(c.resources.limits.cpu))
  message: "all containers require cpu and memory limits"
```

### 15.3 Disallow hostPath

**Kyverno:**

```yaml
rules:
- name: no-hostpath
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  validate:
    message: "hostPath volumes forbidden"
    pattern:
      spec:
        =(volumes):
        - X(hostPath): "null"
```

The `X(hostPath): "null"` says: for each volume, the hostPath field must not exist.

**Gatekeeper:**

```rego
violation[{"msg": "hostPath forbidden"}] {
  input.review.object.spec.volumes[_].hostPath
}
```

**VAP:**

```yaml
- expression: |
    !has(object.spec.volumes) ||
    object.spec.volumes.all(v, !has(v.hostPath))
  message: "hostPath volumes forbidden"
```

### 15.4 Require non-root user

**Kyverno:**

```yaml
rules:
- name: run-as-non-root
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  validate:
    message: "must runAsNonRoot"
    anyPattern:
    - spec:
        securityContext:
          runAsNonRoot: true
    - spec:
        containers:
        - securityContext:
            runAsNonRoot: true
```

**VAP:**

```yaml
- expression: |
    (has(object.spec.securityContext) &&
     has(object.spec.securityContext.runAsNonRoot) &&
     object.spec.securityContext.runAsNonRoot == true) ||
    object.spec.containers.all(c,
      has(c.securityContext) &&
      has(c.securityContext.runAsNonRoot) &&
      c.securityContext.runAsNonRoot == true)
  message: "Pod or all containers must set runAsNonRoot=true"
```

### 15.5 Require image from approved registries

**Kyverno:**

```yaml
rules:
- name: approved-registry
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  validate:
    message: "image must come from registry.corp/ or registry.corp-mirror/"
    pattern:
      spec:
        containers:
        - image: "registry.corp/* | registry.corp-mirror/*"
```

**Gatekeeper:**

```rego
violation[{"msg": msg}] {
  c := input.review.object.spec.containers[_]
  not startswith(c.image, "registry.corp/")
  not startswith(c.image, "registry.corp-mirror/")
  msg := sprintf("image %v not from approved registry", [c.image])
}
```

**VAP:** (with param)

```yaml
spec:
  paramKind:
    apiVersion: v1
    kind: ConfigMap
  variables:
  - name: registries
    expression: 'params.data.allowed.split(",")'
  validations:
  - expression: |
      object.spec.containers.all(c,
        variables.registries.exists(r, c.image.startsWith(r)))
    message: "image must come from approved registry"
```

### 15.6 Propagate label from namespace to pod

This is a *mutation* policy — copy a label from the Namespace to every Pod in it.

**Kyverno:**

```yaml
rules:
- name: copy-cost-center
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  context:
  - name: nslabels
    apiCall:
      urlPath: "/api/v1/namespaces/{{ request.namespace }}"
      jmesPath: "metadata.labels"
  mutate:
    patchStrategicMerge:
      metadata:
        labels:
          cost-center: "{{ nslabels.\"cost-center\" }}"
```

**Gatekeeper:** (AssignMetadata)

```yaml
apiVersion: mutations.gatekeeper.sh/v1
kind: AssignMetadata
metadata: { name: copy-cost-center }
spec:
  match:
    scope: Namespaced
    kinds: [{ apiGroups: [""], kinds: ["Pod"] }]
  location: "metadata.labels.cost-center"
  parameters:
    assign:
      externalData:
        provider: "namespace-labels"
        dataSource: ValueAtLocation
```

(Gatekeeper's external-data integration is more involved; for cross-resource the sync controller is usually used with a Rego rule that *validates* rather than mutates.)

**VAP/MAP:**

MutatingAdmissionPolicy supports paramRef but not full cross-resource read. As of 1.32 alpha, the typical workaround is to keep the value in a ConfigMap and reference via paramRef. For true Namespace → Pod label propagation, Kyverno remains the cleanest tool.

### 15.7 Auto-inject sidecar

A mutation: every Pod gets a logging sidecar injected.

**Kyverno:**

```yaml
rules:
- name: inject-logger
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  exclude:
    any:
    - resources: { namespaces: ["kube-system", "kyverno"] }
    - resources: { selector: { matchLabels: { logger: skip } } }
  mutate:
    patchStrategicMerge:
      spec:
        containers:
        - name: log-sidecar
          image: registry.corp/logger:1.4
          resources:
            requests: { cpu: 50m, memory: 64Mi }
            limits:   { cpu: 100m, memory: 128Mi }
          volumeMounts:
          - name: applogs
            mountPath: /var/log/app
        +(volumes):
        - name: applogs
          emptyDir: {}
```

Note `+(containers)` is implicit because strategic merge appends to lists by default. Sidecar injection is what Istio does (chapter 17) — same pattern.

**Gatekeeper:** Mutation can add fields but appending to a list is awkward without a `ModifySet` per field; not recommended.

**MAP:**

```yaml
mutations:
- patchType: JSONPatch
  jsonPatch:
    expression: |
      [
        JSONPatch{
          op: "add",
          path: "/spec/containers/-",
          value: Object{
            name: "log-sidecar",
            image: "registry.corp/logger:1.4"
          }
        }
      ]
```

### 15.8 Deny privileged containers

**Kyverno:**

```yaml
rules:
- name: no-privileged
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  validate:
    message: "privileged containers forbidden"
    pattern:
      spec:
        =(containers):
        - =(securityContext):
            =(privileged): "false"
```

**Gatekeeper:**

```rego
violation[{"msg": "privileged container"}] {
  input.review.object.spec.containers[_].securityContext.privileged
}
```

**VAP:**

```yaml
- expression: |
    object.spec.containers.all(c,
      !has(c.securityContext) ||
      !has(c.securityContext.privileged) ||
      c.securityContext.privileged != true)
  message: "privileged containers forbidden"
```

### 15.9 Require NetworkPolicy in every namespace

This is a *cross-resource* validation: when a Namespace is created (or updated), check that a NetworkPolicy exists in it.

**Kyverno:** Use `generate` to create one if it does not exist (better than validation, because it solves the problem rather than blocking):

```yaml
rules:
- name: ensure-default-deny
  match: { any: [{ resources: { kinds: ["Namespace"] } }] }
  generate:
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    name: default-deny
    namespace: "{{ request.object.metadata.name }}"
    synchronize: true
    data:
      spec:
        podSelector: {}
        policyTypes: [Ingress, Egress]
```

**Gatekeeper:** Requires sync controller to track NetworkPolicies; rule reads from `data.inventory`:

```rego
violation[{"msg": msg}] {
  ns := input.review.object.metadata.name
  not data.inventory.namespace[ns]["networking.k8s.io/v1"].NetworkPolicy
  msg := sprintf("namespace %v has no NetworkPolicy", [ns])
}
```

**VAP:** Cannot do this directly. VAP cannot query other resources. This is the killer use case where you reach for Kyverno generate or Gatekeeper.

---

## 16. Falco: Architecture and Rules

Falco was open-sourced by Sysdig in 2016 and donated to CNCF in 2018. Source at `falcosecurity/falco`. It graduated to CNCF Incubating, then GA. It is the most widely deployed runtime detection tool in K8s and remains the reference design.

### 16.1 The data path

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Node (Linux kernel)                            │
│                                                                      │
│   Container A         Container B         Container C                │
│   ┌─────────┐         ┌─────────┐         ┌─────────┐                │
│   │  app    │         │  app    │         │  shell  │ ◄ attacker     │
│   └────┬────┘         └────┬────┘         └────┬────┘                │
│        │                   │                   │                     │
│        │ syscalls          │                   │                     │
│        ▼                   ▼                   ▼                     │
│   ╔═══════════════════════════════════════════════════════════════╗  │
│   ║                  Linux kernel                                 ║  │
│   ║                                                               ║  │
│   ║   tracepoint:sys_enter / sys_exit (or kprobe)                ║  │
│   ║          │                                                    ║  │
│   ║          ▼                                                    ║  │
│   ║   ┌────────────────────────────┐                              ║  │
│   ║   │  Falco eBPF program        │   (or legacy kmod)           ║  │
│   ║   │  - filter syscalls         │                              ║  │
│   ║   │  - enrich with cgroup,     │                              ║  │
│   ║   │    container, ns           │                              ║  │
│   ║   │  - push to ring buffer     │                              ║  │
│   ║   └────────┬───────────────────┘                              ║  │
│   ║            │                                                  ║  │
│   ║   ┌────────▼───────────────────┐                              ║  │
│   ║   │  perf/ring buffer (BPF map)│                              ║  │
│   ║   └────────┬───────────────────┘                              ║  │
│   ╚════════════│══════════════════════════════════════════════════╝  │
│                │   userspace ▼                                       │
│   ┌────────────▼──────────────────────┐                              │
│   │  falco userspace daemon           │                              │
│   │  - read events from ring          │                              │
│   │  - evaluate rules                  │                              │
│   │  - emit outputs (stdout/file/grpc) │                              │
│   └────────────┬──────────────────────┘                              │
│                │                                                     │
│                ▼                                                     │
│           falcosidekick → Slack / SIEM / PagerDuty / S3              │
└──────────────────────────────────────────────────────────────────────┘
```

The kernel-side driver hooks syscall entry/exit (`tracepoint:syscalls:sys_enter` etc.). For each event it captures arguments, looks up cgroup/container, drops uninteresting events early, and pushes the rest to a ring buffer. The userspace daemon reads, parses, evaluates the rule set, and emits to the configured output sinks.

### 16.2 Rules format

Falco rules live in YAML. Default rules are in `falcosecurity/rules` (split from main repo in 2022). A rule:

```yaml
- rule: Terminal shell in container
  desc: A shell was used as the entrypoint or executed inside a container.
  condition: >
    spawned_process and container
    and shell_procs
    and proc.tty != 0
    and container_entrypoint
    and not user_expected_terminal_shell_in_container_conditions
  output: >
    A shell was spawned in a container with terminal attached
    (user=%user.name user_loginuid=%user.loginuid
     %container.info shell=%proc.name parent=%proc.pname
     cmdline=%proc.cmdline pid=%proc.pid terminal=%proc.tty
     container_id=%container.id)
  priority: NOTICE
  tags: [container, shell, mitre_execution, T1059]
```

The `condition` uses sysdig filter syntax — basically `field operator value` joined with `and/or/not`. Common fields:

| Field | Meaning |
|-------|---------|
| `spawned_process` | event is a process exec |
| `container` | event is in a container |
| `proc.name` | binary name |
| `proc.pname` | parent process name |
| `proc.cmdline` | full command line |
| `proc.exe` | executable path |
| `evt.type` | syscall name (open, execve, connect, ...) |
| `fd.name` | filename for fd operations |
| `fd.type` | file/socket/pipe |
| `container.image.repository` | image name |
| `k8s.ns.name` | namespace (if k8s_audit enabled) |
| `k8s.pod.name` | pod name |
| `user.name` | username inside container |

Macros (`shell_procs`, `container_entrypoint`) are reusable expressions defined elsewhere in the rules file. Lists (`shell_binaries: [bash, sh, zsh, ...]`) are reusable value sets.

### 16.3 Common default rules

```
- Write below /etc          - alarms when something writes to /etc inside container
- Write below root           - writes to / outside expected paths
- Read sensitive file        - /etc/shadow, /etc/sudoers, /etc/passwd reads
- Mkdir binary dirs          - new dirs in /usr/bin etc
- Modify binary dirs         - writes to /usr/bin etc
- Run shell untrusted        - shell binary in an unusual container
- Privileged container       - a privileged container started (catches policy bypass)
- Launch sensitive mount     - mount with hostPath
- Change thread namespace    - setns() syscall — often used in escape
- Detect crypto miners       - known mining binaries / process names
- Outbound to suspicious     - connect() to known C2 IPs (with IP-list)
```

The `Write below /etc` rule is the canonical "shell-in-container" detector. An attacker who has shell in a container almost always writes to `/etc/cron*`, `/etc/profile.d`, or similar paths for persistence — and Falco catches it.

### 16.4 Outputs and falcosidekick

Falco itself only writes to stdout/file/syslog/gRPC. To get useful alerts you run `falcosidekick`, which subscribes to Falco's gRPC stream and forwards to:

- Slack / Teams / Discord
- PagerDuty / Opsgenie
- AlertManager (Prometheus)
- Elasticsearch / Splunk / Loki
- S3 / GCS for archival
- Webhook for anything bespoke

Falcosidekick also has a UI (`falcosidekick-ui`) that gives you a basic live event feed without setting up a SIEM.

### 16.5 Cost

Falco's per-syscall overhead is the elephant in the room. Every syscall on every container on the node is hooked. Numbers from production reports:

- ~1-3% CPU overhead on typical workloads.
- Up to 10-20% on syscall-heavy workloads (Go runtimes with lots of `epoll_wait`, databases).
- Memory: 200-500 MB per node for the daemon.
- Ring buffer pressure under bursty workloads → dropped events (visible in `falco_events_dropped_total`).

Mitigations:

- Use the modern eBPF driver (lower overhead than legacy kmod).
- Reduce ruleset to only the rules you act on.
- Increase ring buffer size in `falco.yaml` (`syscall_buf_size_preset`).
- Drop syscalls you do not care about via `base_syscalls` config.

---

## 17. Falco eBPF Driver vs Kernel Module

Historically Falco shipped a *kernel module* (`falco.ko`) compiled per-kernel-version. It was the most performant but required matching kernel headers, signed kernel modules in secure-boot environments, and rebooting after updates. The kmod is now deprecated for most distros.

The **legacy eBPF driver** (Falco 0.18+) replaced the kmod with a CO-RE-incompatible BPF program loaded via `bpf()` syscall. Works on most modern kernels but requires `CAP_BPF` + `CAP_PERFMON` + kernel ≥ 4.14.

The **modern eBPF driver** (Falco 0.34+) is the recommended choice in 2025. It uses CO-RE (Compile Once, Run Everywhere), so a single BPF object works across kernels. Faster startup, less memory.

```
┌────────────────────────────────────────────────────────────────────┐
│   Driver matrix                                                    │
│                                                                    │
│   kmod (legacy)        ─ kernel ≥ 2.6   ─ compile per kernel       │
│                          highest perf   ─ deprecated since 0.37    │
│                                                                    │
│   eBPF legacy          ─ kernel ≥ 4.14  ─ no CO-RE, build per krn  │
│                          good perf      ─ deprecated since 0.37    │
│                                                                    │
│   eBPF modern (CO-RE)  ─ kernel ≥ 5.8   ─ single binary all krns   │
│                          best ergonomics, recommended              │
└────────────────────────────────────────────────────────────────────┘
```

You can also run Falco in `--userspace` mode using its `plugins` framework — it can subscribe to non-syscall sources like the K8s audit log, AWS CloudTrail, Okta, GitHub. That mode does not need a kernel driver at all and is useful for "cloud audit" rules sitting next to syscall rules.

---

## 18. Tetragon: eBPF Observability and Enforcement

Tetragon is part of the Cilium project (chapter 16) and lives at `cilium/tetragon`. It is younger than Falco (open-sourced 2022) but takes a fundamentally different design: instead of a fixed-syscall hook set with userspace rule evaluation, Tetragon expresses *policy* as eBPF programs that run in the kernel. The kernel program decides what to capture, what to enrich, and — critically — whether to *kill* the offending process inline.

### 18.1 Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                      Node (Linux kernel)                             │
│                                                                      │
│   ╔══════════════════════════════════════════════════════════════╗   │
│   ║  kernel hooks: kprobe, uprobe, tracepoint                    ║   │
│   ║          │                                                   ║   │
│   ║          ▼                                                   ║   │
│   ║  ┌─────────────────────────────────────────────┐             ║   │
│   ║  │  Tetragon BPF program (one per kprobe)      │             ║   │
│   ║  │  - match argument patterns                  │             ║   │
│   ║  │  - emit event to perf buffer                │             ║   │
│   ║  │  - optionally bpf_send_signal(SIGKILL)      │             ║   │
│   ║  │  - optionally bpf_override_return()         │             ║   │
│   ║  └────────┬────────────────────────────────────┘             ║   │
│   ║           │                                                  ║   │
│   ║   ┌───────▼─────────────────────┐                            ║   │
│   ║   │  perf ring + BPF maps       │                            ║   │
│   ║   │  (process tree, cgroup map) │                            ║   │
│   ║   └───────┬─────────────────────┘                            ║   │
│   ╚═══════════│══════════════════════════════════════════════════╝   │
│               │                                                       │
│   ┌───────────▼───────────────────────┐                              │
│   │  tetragon-agent (userspace DS)    │                              │
│   │  - install TracingPolicy as BPF   │                              │
│   │  - read events                    │                              │
│   │  - enrich with pod/container      │                              │
│   │  - export via gRPC / JSON         │                              │
│   └───────────┬───────────────────────┘                              │
│               │                                                       │
│               ▼                                                       │
│        Hubble UI / SIEM / JSON log                                    │
└──────────────────────────────────────────────────────────────────────┘
```

### 18.2 TracingPolicy CRD

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: write-block-etc
spec:
  kprobes:
  - call: "fd_install"
    syscall: false
    args:
    - index: 0
      type: "int"
    - index: 1
      type: "file"
    selectors:
    - matchArgs:
      - index: 1
        operator: "Prefix"
        values: ["/etc/"]
      matchActions:
      - action: Sigkill
```

This installs a kprobe on `fd_install` (called when a file descriptor is associated with a struct file) and, if the file path is under `/etc/`, sends SIGKILL to the calling process — **from inside the kernel, before the write completes**. The compromised process never gets a chance to finish writing.

`matchActions` supports:

- `Post` — emit an event (default; observation only)
- `Sigkill` — kill the process
- `Override` — make the syscall return a chosen errno (`bpf_override_return`)
- `FollowFD` / `UnfollowFD` — track fd lifecycle so downstream events can match by fd
- `NoPost` — suppress event emission (useful with Override-only enforcement)

### 18.3 Process-tree enrichment

Tetragon's killer feature is that every event is enriched with the *full process tree* — Pod → container → process → parent → grandparent — automatically. This is because the BPF programs maintain a process tree in BPF maps and walk it on each event. The enrichment is therefore *free* (already done in kernel).

Compare to Falco, where the userspace daemon does its own enrichment by maintaining its own process table. Tetragon's approach scales better because the enrichment happens at event time, in kernel, with O(1) cache lookups.

### 18.4 Tetragon vs Falco

| | Falco | Tetragon |
|---|---|---|
| Hook | tracepoints (sys_enter/exit) | kprobes + tracepoints, configurable |
| Rule engine | userspace daemon | BPF program (compiled from policy) |
| Enforcement | none (detect) | SIGKILL, override return |
| Process tree | userspace, lazy | BPF map, eager |
| Default rules | yes, extensive | curated examples |
| Latency to detection | µs → ms (ring buffer drain) | ns (in-kernel) |
| Latency to enforcement | n/a | ns (BPF) |
| Operator surface | falco rules YAML | TracingPolicy CRD |
| Maturity | very mature | mature, growing |
| Best for | detection across many syscalls | targeted prevention |

The current best practice in many shops is to **run both**: Falco for breadth (full default ruleset, low-cost detection) and Tetragon for targeted enforcement (a small set of kill-on-detect rules for the highest-value attacks).

### 18.5 Inline enforcement: caveats

Killing a process from a BPF program is powerful and dangerous. Watch out for:

- **Killing the wrong thing.** If your TracingPolicy matches too broadly, you can SIGKILL kubelet or critical system processes and brick the node. *Always test in observe-only mode first.*
- **Race with execve.** `bpf_send_signal` after `execve` may deliver to the new image — sometimes the right thing, sometimes not.
- **Kernel version constraints.** `bpf_send_signal` requires kernel ≥ 5.3. `bpf_override_return` requires `CONFIG_BPF_KPROBE_OVERRIDE=y`, which is *not* set by default on Ubuntu — you can detect but not override.
- **No userspace audit trail by default.** Enforcement happens in kernel; the event still goes through perf ring for logging, but if the ring is full (high load) you can drop the alert.

---

## 19. Tracee: CO-RE and Image+Runtime Correlation

Tracee is Aqua Security's open-source runtime detector at `aquasecurity/tracee`. It overlaps with Falco and Tetragon in scope but has a few distinguishing features:

1. **CO-RE-first**: written entirely with CO-RE eBPF, no per-kernel build.
2. **Detection signatures** in Rego (`signatures/`) — same Rego you might already use in Gatekeeper.
3. **Image + runtime correlation**: Tracee integrates with `trivy` (also Aqua) so a runtime event can be enriched with "this image had CVE-X" or "this process is running a known-malicious binary detected during image scan."
4. **Process enrichment**: like Tetragon, maintains process tree in BPF maps.

Architecture is similar to Falco's: kernel BPF program emits events, userspace daemon evaluates signatures and emits findings. Tracee's positioning is "runtime forensics with deep signature library" — its signature set is more attack-focused (catching specific TTPs) than Falco's, which is more general.

A Tracee rule (signature) example:

```rego
package tracee.TRC_2

__rego_metadoc__ := {
  "id": "TRC-2",
  "version": "0.1.0",
  "name": "Anti-Debugging",
  "description": "Process trying to detect debugger via ptrace",
  "tags": ["linux", "container"],
  "properties": {
    "Severity": 3,
    "MITRE ATT&CK": "Defense Evasion: Execution Guardrails",
  },
}

eventSelectors := [{ "source": "tracee", "name": "ptrace" }]

tracee_match {
  input.eventName == "ptrace"
  arg := input.args[_]
  arg.name == "request"
  arg.value == "PTRACE_TRACEME"
}
```

Tracee has the most "out-of-the-box detection signature library" of the three; Falco has the most "general runtime hook framework"; Tetragon has the strongest enforcement story. Pick based on your team's expertise and use case.

---

## 20. Other Runtime Tools: bcc, bpftrace, auditd

### 20.1 bcc and bpftrace

`bcc` (BPF Compiler Collection, `iovisor/bcc`) is a library of Python+C eBPF tools. `bpftrace` (`iovisor/bpftrace`) is the awk-of-eBPF — a high-level DSL for one-liners. They are not "K8s runtime security tools" but they are the swiss army knives you reach for during an investigation.

```bash
# Show every execve happening on this node, with cgroup info
bpftrace -e '
  tracepoint:syscalls:sys_enter_execve {
    printf("%d %s %s\n", pid, comm, str(args->filename));
  }
'

# Show every connect() syscall, with destination
bpftrace -e '
  tracepoint:syscalls:sys_enter_connect /comm != "node_exporter"/ {
    printf("%d %s connect\n", pid, comm);
  }
'

# Count syscalls by container (using cgroup id)
bcc-tools/syscount -c
```

bcc's `execsnoop`, `opensnoop`, `tcpconnect`, `oomkill`, `runqlat` are all great IR tools. They are not policy enforcers; they are the diagnostic side of "I just got a Falco alert, what is actually happening?"

### 20.2 auditd

Auditd is the Linux audit subsystem. Predates eBPF by 20 years. Still in use because:

- Required by PCI / DISA STIG / FedRAMP for certain audit events.
- Stable, no kernel module changes.
- Userspace tooling (`ausearch`, `aureport`) is mature.

Disadvantages: very high overhead at scale, output is hard to correlate with containers, no in-kernel filtering. In K8s clusters you typically *minimize* auditd use (just enough to satisfy compliance) and lean on eBPF tools for the bulk of detection.

```bash
# audit rule: log every execve of /bin/sh
auditctl -a always,exit -F arch=b64 -S execve -F path=/bin/sh -k shell_exec

# audit rule: log all writes to /etc
auditctl -w /etc -p wa -k etc_changes
```

You can pipe auditd output to a SIEM via `aushape` or `auditbeat`. For Falco users, Falco's plugin framework includes a `k8saudit` plugin that subscribes to the K8s audit log directly — different from Linux auditd but related.

---

## 21. Detection vs Prevention: The Spectrum

There is no clean line between "detect" and "prevent." Picture it as a spectrum:

```
   pure detect                                              pure prevent
   ───────────────────────────────────────────────────────────────────►

   ┌────────┐  ┌────────────┐  ┌─────────────┐  ┌────────────────────┐
   │ audit  │  │ Falco      │  │ Tetragon    │  │ gVisor / Kata      │
   │ log    │  │ alert      │  │ SIGKILL     │  │ syscall jail        │
   └────────┘  └────────────┘  └─────────────┘  └────────────────────┘
   
   t = post     t = real-time   t = real-time    t = always
   harm   :    high             low              none
   ops risk:   low              medium           high (perf, compat)
```

- **audit log**: zero risk to operations, no prevention, post-incident only.
- **Falco alert**: real-time detection, no enforcement; harm has already happened by the time the human reads the alert. Pages an on-call.
- **Tetragon SIGKILL**: real-time prevention; some harm may have happened in the µs before the kill. Risk: false positive can SIGKILL legitimate processes.
- **Sandbox runtime (gVisor, Kata, chapter 29)**: prevents kernel attack surface entirely by intermediating syscalls. Compatibility cost (some syscalls unsupported) and performance cost (10-30%).

A mature posture combines: sandbox the highest-risk workloads, Tetragon-enforce on the highest-fidelity rules, Falco-detect everything else, audit-log everything for forensics. There is no single "right" point on the spectrum; the right point depends on your tolerance for false positives versus your tolerance for missed detection.

---

## 22. Seccomp Profiles

Seccomp ("secure computing mode") is a Linux kernel feature that filters syscalls. In its modern form (seccomp-bpf, 2014) you supply a BPF program at process start that decides for each syscall whether to allow, kill, return errno, or trap.

### 22.1 The three K8s modes

In `Pod.spec.securityContext.seccompProfile` or container-level:

```yaml
seccompProfile:
  type: RuntimeDefault      # use the container runtime's default
# OR
  type: Localhost
  localhostProfile: "profiles/audit.json"
# OR
  type: Unconfined          # no profile — typically forbidden
```

- **RuntimeDefault**: the kubelet asks the container runtime (containerd, CRI-O) for its default profile. For containerd this is typically a denylist of ~50 dangerous syscalls (mostly `mount`, `reboot`, `ptrace`, `swapon`, etc). Restricted profile of PSA requires this or Localhost.
- **Localhost**: a custom JSON profile in `/var/lib/kubelet/seccomp/` on the node. Path is relative to the kubelet's `--seccomp-default` directory. Ships in the node image or via a DaemonSet.
- **Unconfined**: no profile. Forbidden by PSA restricted. Used by system pods that need full syscall access.

### 22.2 Generating a profile

The Security Profiles Operator (`kubernetes-sigs/security-profiles-operator`) automates profile generation. It runs a Pod in *record mode*, observes every syscall, and emits a Localhost profile listing only those syscalls.

```yaml
apiVersion: security-profiles-operator.x-k8s.io/v1alpha1
kind: ProfileRecording
metadata: { name: record-app }
spec:
  kind: SeccompProfile
  recorder: bpf
  podSelector:
    matchLabels:
      app: payments
```

After your test suite runs against the workload, SPO generates a `SeccompProfile` CR with the observed syscall list. You apply that profile, switch the Pod to `type: Localhost`, and the workload is now restricted to syscalls it actually uses.

You can also generate manually with `strace` (low fidelity) or with `bpftrace`:

```bash
bpftrace -e '
  tracepoint:raw_syscalls:sys_enter /cgroup == cgroupid("/sys/fs/cgroup/...")/ {
    @[args->id] = count();
  }
'
```

Then map syscall IDs to names. Tedious but works.

### 22.3 Profile JSON format

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "defaultErrnoRet": 1,
  "architectures": ["SCMP_ARCH_X86_64"],
  "syscalls": [
    {
      "names": ["accept", "accept4", "access", "arch_prctl", "bind",
                "brk", "clock_gettime", "close", "connect", "epoll_ctl",
                "epoll_pwait", "epoll_wait", "execve", "exit", "exit_group",
                "fcntl", "fstat", "futex", "getpid", "getrandom",
                "ioctl", "listen", "lseek", "madvise", "mmap", "mprotect",
                "munmap", "openat", "pipe2", "poll", "read", "recvfrom",
                "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sched_yield",
                "sendto", "set_robust_list", "set_tid_address", "setsockopt",
                "sigaltstack", "socket", "stat", "tgkill", "write"],
      "action": "SCMP_ACT_ALLOW"
    }
  ]
}
```

`SCMP_ACT_ERRNO` returns EPERM. `SCMP_ACT_KILL_PROCESS` kills the process. `SCMP_ACT_LOG` allows but logs. The PSA `restricted` profile expects either `RuntimeDefault` or `Localhost`; what the Localhost profile *does* is on you.

### 22.4 Cluster-wide seccomp default

Kubernetes 1.25+ has `--seccomp-default=true` on the kubelet, which applies RuntimeDefault to every Pod that does not specify one. This is the single biggest single-flag hardening win in modern Kubernetes. Turn it on.

---

## 23. AppArmor and SELinux on Kubernetes

Seccomp filters syscalls. AppArmor and SELinux filter *resources* — files, capabilities, network endpoints. They complement seccomp.

### 23.1 AppArmor

Path-based MAC. Profiles live in `/etc/apparmor.d/`. To use in K8s (1.30+ GA):

```yaml
spec:
  securityContext:
    appArmorProfile:
      type: Localhost
      localhostProfile: k8s-apparmor-example-deny-write
  containers:
  - name: app
    image: nginx
```

The profile must be loaded on the node (via DaemonSet or node image baking). Example profile:

```
#include <tunables/global>

profile k8s-apparmor-example-deny-write flags=(attach_disconnected) {
  #include <abstractions/base>

  file,

  # deny writes to / (everywhere; specific paths can override)
  deny /** w,

  # allow logs
  /var/log/app/** w,

  # standard caps
  capability net_bind_service,
}
```

Before 1.30, AppArmor was set via an annotation (`container.apparmor.security.beta.kubernetes.io/<container>`). The Pod-spec field is the GA replacement.

### 23.2 SELinux

Type-based MAC. Set via:

```yaml
spec:
  securityContext:
    seLinuxOptions:
      level: "s0:c123,c456"
      type: "container_t"
```

PSA `restricted` requires `type` to be empty (let runtime default) or one of `container_t`, `container_init_t`, `container_kvm_t`. The container runtime applies the appropriate context automatically on a SELinux-enabled node.

In practice, custom SELinux policy in K8s is rare outside RHEL/OpenShift. OpenShift's MCS (multi-category security) does the heavy lifting; on other distros teams usually rely on runtime defaults.

### 23.3 The combined picture

```
       process syscall
            │
            ▼
       seccomp filter  ── deny? ──► EPERM / kill
            │ allow
            ▼
       capability check  ── lack? ──► EPERM
            │ pass
            ▼
       AppArmor / SELinux  ── deny? ──► EPERM
            │ allow
            ▼
       kernel function
```

Three independent layers. A well-hardened pod has all three configured. A real-world minimum: seccomp `RuntimeDefault`, AppArmor `runtime/default` (the runtime ships a default profile too), SELinux `container_t`.

---

## 24. Apiserver Audit Log Analysis

The K8s apiserver emits an audit log of every API call. It is the most important source of K8s-side intrusion data. CIS Benchmark 1.x.x mandates audit logging configured.

### 24.1 Configuration

```yaml
# /etc/kubernetes/audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: ["RequestReceived"]
rules:
- level: None
  users: ["system:kube-proxy"]
  verbs: ["watch"]
  resources:
  - group: ""
    resources: ["endpoints", "services"]

- level: Metadata
  resources:
  - group: ""
    resources: ["secrets", "configmaps", "tokenreviews"]
  - group: authentication.k8s.io
    resources: ["*"]

- level: RequestResponse
  verbs: ["create", "update", "patch", "delete"]
  resources:
  - group: ""
  - group: rbac.authorization.k8s.io

- level: Metadata
  omitStages: ["RequestReceived"]
```

Four levels: `None` (don't log), `Metadata` (who, what, when), `Request` (+ request body), `RequestResponse` (+ response body). Secrets bodies are *never* logged at Metadata level by design.

Pass to apiserver:

```
--audit-policy-file=/etc/kubernetes/audit-policy.yaml
--audit-log-path=/var/log/kube-audit.log
--audit-log-maxage=30
--audit-log-maxbackup=10
--audit-log-maxsize=100
```

Or stream to a webhook (`--audit-webhook-config-file`). EKS / GKE / AKS each have their own audit log forwarding (CloudWatch, Cloud Audit Logs, Azure Monitor).

### 24.2 Common queries

| Question | Field path |
|----------|-----------|
| Who deleted resource X? | `verb=delete AND objectRef.name=X` |
| Who got cluster-admin? | `objectRef.resource=rolebindings AND requestObject.roleRef.name=cluster-admin` |
| Anonymous requests? | `user.username=system:anonymous` |
| Failed authentication? | `verb=create AND objectRef.resource=tokenreviews AND responseStatus.code != 200` |
| Exec into pod? | `verb=create AND objectRef.subresource=exec` |
| Created a Pod with privileged? | `verb=create AND objectRef.resource=pods AND requestObject.spec.containers[*].securityContext.privileged=true` (needs Request level) |
| Listed all secrets? | `verb=list AND objectRef.resource=secrets AND objectRef.namespace=""` |

### 24.3 SIEM integration

Pipe the audit log to your SIEM (Splunk, Elastic, Datadog, Sumo, Chronicle). Build dashboards for the above queries. Set up alerts for:

- More than 0 anonymous requests (typo or attack).
- More than N RBAC modifications per hour (someone's grinding RBAC).
- Any cluster-admin grant.
- Any secret list across all namespaces from a service account.
- Any pod exec by a user (vs system controller).
- Audit log gap (no events in last 1 minute → exfiltration risk).

The "audit log gap" alert is underrated. Attackers turn off audit logging as a defense-evasion step; if the gap is more than a few seconds and not explainable by apiserver restart, treat it as a P1.

---

## 25. Continuous Benchmark Scanning: kube-bench, kubescape, trivy k8s, kube-hunter

Manually checking that 200 CIS controls are configured correctly is a fool's errand. Scan continuously.

### 25.1 kube-bench (`aquasecurity/kube-bench`)

Runs the CIS Kubernetes Benchmark. Reads kubelet flags, apiserver flags, controller-manager flags, etc, and reports PASS/FAIL per control:

```
[INFO] 1.1 Master Node Configuration Files
[PASS] 1.1.1 Ensure that the API server pod specification file permissions are set to 600 or more restrictive
[FAIL] 1.1.2 Ensure that the API server pod specification file ownership is set to root:root
[WARN] 1.2.1 Ensure that the --anonymous-auth argument is set to false (Manual)
...
== Summary ==
72 checks PASS
12 checks FAIL
8 checks WARN
0 checks INFO
```

Run as a Job, schedule daily. Output to SIEM. Fail your CD pipeline on any new FAIL.

### 25.2 kubescape (`kubescape/kubescape`)

Broader scope: NSA Hardening Guidance, CIS Benchmark, MITRE ATT&CK. Scans live cluster and Helm/YAML manifests. Has its own framework definitions and integrates with ArmoSec for SaaS reporting.

```bash
kubescape scan --enable-host-scan --format json
```

### 25.3 trivy k8s (`aquasecurity/trivy`)

Combined CVE scanning, misconfig scanning, and exposed-secret scanning. Trivy is most known for image CVE scanning (chapter 27); `trivy k8s` extends it to cluster-level resources.

```bash
trivy k8s cluster --report summary --severity HIGH,CRITICAL
```

### 25.4 kube-hunter (`aquasecurity/kube-hunter`)

Offensive scanner. Actively probes the cluster for exploitable misconfigurations (open kubelet read-only port, unauthenticated dashboard, anonymous apiserver, etc). Two modes: passive (read-only) and active (attempt exploitation). Run *active* mode only in non-prod.

```bash
kube-hunter --remote 10.0.0.0/24 --active
```

### 25.5 The continuous-scan loop

```
   git push → CI manifest scan (trivy/kubescape) → ─┐
                                                    ▼
                              Block merge if FAIL
                                                    │
   merge → kustomize/helm apply → cluster ──────────┘
                                                    │
                                                    ▼
   nightly job: kube-bench → SIEM
   nightly job: kubescape --enable-host-scan → SIEM
   weekly: kube-hunter --remote → SIEM
   real-time: Falco → SIEM
                                                    │
                                                    ▼
                                  alerting + dashboards
```

---

## 26. MITRE ATT&CK for Containers

MITRE ATT&CK has a "Containers" matrix since 2021. Memorizing the IDs is wasted effort; *tagging your alerts with them* is high leverage because it lets your IR team correlate K8s alerts with the same matrix they use for endpoint and cloud.

Key technique IDs:

| ID | Name | K8s example |
|----|------|-------------|
| T1610 | Deploy Container | Attacker creates a Pod with their image |
| T1611 | Escape to Host | hostPath, privileged + mount, runc CVE |
| T1612 | Build Image on Host | docker build on a compromised node |
| T1613 | Container and Resource Discovery | kubectl get pods |
| T1525 | Implant Internal Image | Push to internal registry with backdoor |
| T1496 | Resource Hijacking | Cryptominer Pod |
| T1059 | Command and Scripting Interpreter | Shell in container |
| T1078.004 | Cloud Accounts | Stolen IRSA / Workload Identity |
| T1552.007 | Container API | Steal SA token from `/var/run/secrets/kubernetes.io/` |
| T1078.001 | Default Accounts | Default SA with unnecessary RBAC |

Falco rules ship with `tags: [mitre_execution, T1059]`. Tetragon and Tracee similarly tag. Your alert payload sent to SIEM should include the technique ID so the IR team's playbook (which is keyed on technique) can fire.

---

## 27. Incident Response Playbook

You have a Falco alert. Now what. Below is a runbook good enough to staple to the wall.

### 27.1 Timeline

```
  T+0s     Falco fires "Write below /etc inside container"
            │
            ▼  falcosidekick → PagerDuty
  T+30s    On-call paged
            │
            ▼
  T+2m     On-call opens alert, sees pod=payments-7f9..., node=ip-10-0-3-11
            │
            ▼ kubectl describe pod payments-7f9...
  T+3m     Triage: known false-positive? compare against baseline of
            recent alerts on this image/pod
            │
            ▼ no — escalate to security
  T+5m     CONTAIN:
            1. kubectl cordon ip-10-0-3-11
            2. NetworkPolicy default-deny in the namespace
            3. kubectl delete pod payments-7f9... --grace-period=0
            │   (or: kubectl debug + freeze with cgroup freezer first)
            ▼
  T+10m    FORENSICS:
            1. ephemeral debug container to a copy of the pod
               kubectl debug -it payments-7f9 --image=alpine --target=app
            2. copy /proc/$PID/maps, /proc/$PID/exe, /proc/$PID/cwd
            3. dump container fs: ctr -n=k8s.io tasks pause $CID
                                  ctr -n=k8s.io snapshots view fs-snap $CID
            4. preserve falco event JSON
            5. take node disk snapshot via cloud provider
            │
            ▼
  T+30m    SCOPE:
            1. Hubble: any other pods talking to the C2 destination?
            2. SIEM: any other audit-log activity from the same SA token?
            3. Image: scan with trivy — known compromise?
            │
            ▼
  T+1h     ERADICATE:
            1. Rotate the SA token (delete SA secret if classic, restart pod if BTSA)
            2. Rotate any secrets the pod had access to
            3. Patch the vulnerable image; rebuild from a known-good base
            4. Replace the node (terminate + new instance)
            │
            ▼
  T+2h     RECOVER:
            1. Redeploy with the new image
            2. Uncordon node (or new node joins)
            3. Resume traffic
            │
            ▼
  T+24h    POSTMORTEM:
            1. Timeline + root cause
            2. Why didn't admission catch this?
            3. Why didn't NetworkPolicy stop egress?
            4. Update Falco rule + Kyverno policy + on-call runbook
```

### 27.2 The "ephemeral debug container" trick

K8s 1.25+ has `kubectl debug` with `--target`:

```bash
kubectl debug payments-7f9... -it \
    --image=alpine:3.18 \
    --target=app \
    --share-processes \
    -- sh
```

This creates a new container in the same Pod, in the same PID namespace, with shared filesystem visibility. You can `ps -ef`, see the suspect process, inspect `/proc/PID/`, capture artifacts. The original container keeps running (or is paused via cgroup freezer — see below).

### 27.3 Pausing a container for forensics

The naive `kubectl delete pod` destroys evidence. Better: freeze the cgroup.

```bash
# Find the container ID via crictl
crictl ps | grep payments-7f9

# Find its cgroup
CG=$(crictl inspect $CID | jq -r '.info.runtimeSpec.linux.cgroupsPath')

# Freeze
echo 1 > /sys/fs/cgroup/$CG/cgroup.freeze
```

The process is now frozen but RAM is preserved. You can `gcore -o app.core $PID`, copy /proc, etc. Then unfreeze or kill.

### 27.4 Mapping container ID to host process

```bash
# From inside container: getpid → some PID inside namespace
# From host: find the host PID
crictl inspect $CID | jq -r '.info.pid'
# Or:
ps -eo pid,cmd --pid $(pgrep -f $CID)
```

The host PID is what you operate on for `/proc`, `gcore`, ptrace, etc.

### 27.5 Recovery checklist

- [ ] Suspect pod deleted or contained
- [ ] Affected node cordoned and (ideally) terminated
- [ ] SA token rotated
- [ ] Image rebuilt and re-signed (chapter 27)
- [ ] Secrets the pod could access rotated
- [ ] NetworkPolicy reviewed; was egress allowed?
- [ ] Audit log preserved
- [ ] Falco event JSON preserved
- [ ] Disk snapshot preserved
- [ ] Postmortem scheduled within 5 business days

---

## 28. The Compliance Stack: SOC2, ISO27001, PCI, HIPAA

Compliance is not security, but compliance auditors are how the security budget gets approved. A staff engineer needs to know how K8s controls map.

| Control area | SOC2 | ISO27001 | PCI-DSS | HIPAA | K8s implementation |
|--------------|------|----------|---------|-------|--------------------|
| Access control | CC6.1 | A.9 | 7.1 | 164.312(a) | RBAC (ch 07), OIDC |
| Encryption at rest | CC6.7 | A.10.1 | 3.4 | 164.312(a)(2)(iv) | etcd encryption (ch 04), CSI encryption (ch 19) |
| Encryption in transit | CC6.7 | A.13.1 | 4.1 | 164.312(e)(1) | mTLS (mesh, ch 17), WireGuard |
| Logging and monitoring | CC7.2 | A.12.4 | 10.x | 164.312(b) | Audit log + SIEM (this §24) |
| Vulnerability management | CC7.1 | A.12.6 | 6.2 | 164.308(a)(1) | Image scan (ch 27), kube-bench (this §25) |
| Change management | CC8.1 | A.12.1 | 6.4 | 164.308(a)(7) | Policy as code (this §32), GitOps |
| Incident response | CC7.3 | A.16 | 12.10 | 164.308(a)(6) | IR runbook (this §27) |
| Segmentation | CC6.1 | A.13.1 | 1.2 | 164.308(a)(4) | NetworkPolicy (ch 20) |
| Secrets management | CC6.1 | A.9 | 3.5 | 164.312(a)(2)(iii) | KMS-backed secrets (ch 07) |

The compliance stack:

```
   PSA enforce: restricted        ─►  hardening evidence
   Kyverno policies in Git        ─►  change-control evidence
   audit log → SIEM 90 days       ─►  monitoring evidence
   etcd KMS encryption            ─►  encryption-at-rest evidence
   NetworkPolicy default-deny     ─►  segmentation evidence
   Falco + IR runbook             ─►  detection evidence
   image signing + cosign verify  ─►  software-integrity evidence
   kube-bench daily scan          ─►  configuration-management evidence
```

Most auditors are not K8s experts. The evidence package is a stack of screenshots, policy YAML, audit-log queries, SIEM dashboards, and IR runbook PDFs. Build the package once, reuse across audits.

---

## 29. Honeytokens

A honeytoken is a credential or resource designed to look real, that triggers an alert on use because no legitimate process should ever touch it. Classic IT has done this for decades; in K8s it is underused.

Patterns:

- **Fake ServiceAccount token** baked into an image. RBAC bound to nothing (or to a "trap" RBAC). Audit log alerts on any TokenReview.
- **Fake Secret named `db-master-password`** in a sensitive namespace. List/get by anyone other than the owning controller fires an alert via audit log.
- **Decoy Pod with an open SSH-like port** on a separate network namespace. Any TCP SYN to it is suspicious.
- **Canary file** at `/etc/secret.json` inside a Pod. Falco rule: read of `/etc/secret.json` → page.

Tools:

- **Thinkst Canaries** — commercial, dead simple, emits a webhook on touch. Has K8s-native canaries (fake Secret CRs).
- **kubehound** (`madhuakula/kubehound`, distinct from the offensive `Datadog/KubeHound`) — has K8s-tailored decoy patterns.
- **DIY** — write a Kyverno generate rule that adds a decoy SA + RBAC + Secret to every namespace.

The honeytoken pattern is one of the highest signal-to-noise security controls available. The cost of deploying them is low; the signal of attacker-in-cluster is unambiguous. The main risk is *deploying decoys that are too obvious* (every namespace has a Secret named `honeypot-please-touch`); experienced attackers notice and avoid them.

---

## 30. Egress Monitoring with Hubble

Hubble is Cilium's observability plane (chapter 16). It gives you flow-level visibility across the cluster, including L7 (HTTP, gRPC, DNS, Kafka). For runtime security, the questions Hubble answers:

- Which Pods are talking to which?
- What is the HTTP path / DNS query?
- Are there denied flows (from NetworkPolicy)?
- Is there an unusual egress destination?

```bash
# Show recent denied flows (e.g., from NetworkPolicy block)
hubble observe --verdict DROPPED

# Show DNS queries from a pod
hubble observe --pod payments/* --protocol DNS

# Show egress to a specific destination
hubble observe --to-fqdn malicious-domain.example
```

The egress detection patterns:

- **DNS to known C2 domains** — match the FQDN list from threat intelligence.
- **DNS anomaly** — a pod that always queries 3 hostnames suddenly queries 200.
- **HTTP to unexpected IP/path** — outbound to AWS S3 from a Pod that should not touch S3.
- **TLS SNI to suspicious domain** — even with TLS, the SNI is in cleartext.

You ship Hubble flows to your SIEM (Hubble → relay → exporter → Splunk/Elastic) and build dashboards on top. Combined with Falco's syscall-level alerts and the audit log, you have visibility from kernel to network.

---

## 31. Mutating Policy as a Hardening Lever

The "platform team makes the cluster safer without dev effort" pattern: instead of validating that developers do the right thing, *mutate their manifests to make them safe automatically*. The developer never sees the policy; their Pod just becomes more secure on admission.

Example mutate rules every platform should ship:

```yaml
# Drop ALL capabilities by default
- name: drop-all-caps
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  mutate:
    patchStrategicMerge:
      spec:
        containers:
        - (name): "*"
          securityContext:
            +(capabilities):
              +(drop): ["ALL"]

# Set seccomp RuntimeDefault
- name: default-seccomp
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  mutate:
    patchStrategicMerge:
      spec:
        +(securityContext):
          +(seccompProfile):
            +(type): "RuntimeDefault"

# Disallow service account token automount unless requested
- name: disable-automount
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  exclude:
    any:
    - resources:
        selector:
          matchLabels: { needs-sa-token: "true" }
  mutate:
    patchStrategicMerge:
      spec:
        +(automountServiceAccountToken): false

# Set default resource requests if missing
- name: default-resources
  match: { any: [{ resources: { kinds: ["Pod"] } }] }
  mutate:
    patchStrategicMerge:
      spec:
        containers:
        - (name): "*"
          resources:
            +(requests):
              +(cpu): "10m"
              +(memory): "32Mi"
            +(limits):
              +(memory): "256Mi"
```

The `+(...)` conditional anchors only inject the field if it does not exist. Developer-set values are preserved; defaults fill the gap.

Pair these mutate rules with validate-Enforce rules that *also* enforce the same constraints. Then:

- Old workloads: the mutate fixes them silently.
- New workloads: the validate teaches developers the policy.

Over a quarter, the validate rejections drop to near-zero because the mutate has trained everyone.

---

## 32. Policy as Code: Git, Tests, Rollout

Policy is code. Same workflow as Terraform or app code:

1. **Source of truth in Git.** One repo per policy domain or one mono-repo per cluster.
2. **PR-reviewed.** Same code-owners as any infra change. Security team is a reviewer.
3. **Tested.** Run policies against fixtures *before* merging:
   - Kyverno: `kyverno test` runs a directory of `kyverno-test.yaml` files containing input + expected output.
   - Gatekeeper: `gator test` runs a similar harness for Constraints.
   - VAP: harder; can use `kubectl alpha validate` + fixtures.
   - Conftest: generic OPA-based testing.
4. **Linted.** `kyverno-lint`, `regal` (for Rego), `kube-linter` for the cluster.
5. **Rolled out staged.** Audit on day one, warn on day three, enforce on day ten. Each stage emits PolicyReport data; you check it before flipping.
6. **Observability built in.** Dashboard for policy fail rate; alerts on Enforce-mode failures.

A typical Kyverno test:

```yaml
# kyverno-test.yaml
name: require-resources
policies:
- ../policies/require-resources.yaml
resources:
- pods-good.yaml
- pods-bad.yaml
results:
- policy: require-resources
  rule: require-limits
  resource: good-pod
  kind: Pod
  result: pass
- policy: require-resources
  rule: require-limits
  resource: bad-pod
  kind: Pod
  result: fail
```

Run with `kyverno test .`. Wire into CI.

---

## 33. Policy Fatigue

A real failure mode of the runtime security stack: too many alerts. Engineers stop reading them. New alerts go unnoticed. Real incidents missed.

The fatigue cycle:

```
   Install Falco default ruleset
           │
           ▼
   Alerts: ~100/day per cluster (most low-priority)
           │
           ▼
   On-call gets paged, mostly false-positive
           │
           ▼
   On-call ignores Falco channel
           │
           ▼
   Real incident's alert lost in noise
```

Antidotes:

1. **Tune the ruleset.** Disable rules that don't apply (e.g., "Run shell" if you use distroless and shell-in-container is impossible; "Modify container entrypoint" if you don't use entrypoint mutation).
2. **Suppress known patterns.** Falco supports per-rule exception lists. Maintain them in Git, reviewed.
3. **Baseline the cluster.** Run Falco in audit-only for two weeks. Build a list of expected alerts. Add exceptions. Then turn on paging.
4. **Tiered severity.** Page only on CRITICAL. NOTICE goes to a Slack channel. INFO goes to the SIEM with no notification.
5. **Aggregation.** If the same rule fires 100 times on the same pod, that is one incident, not 100. Use falcosidekick's dedup or SIEM aggregation.
6. **MITRE-tagged routing.** Page CRITICAL detections under T1611 (escape) immediately; deprioritize less-critical techniques.

The metric to watch: **% of alerts a human actually reads**. If it is below 80%, you have fatigue. Tune until it's back.

---

## 34. Observability of the Policy Stack Itself

Your policy engines are themselves software. They can fail, leak, slow down, lie. Instrument them like any other production system.

### 34.1 Metrics to scrape

**Kyverno**:

```
kyverno_admission_requests_total{action,rule,policy,resource_kind}
kyverno_admission_review_duration_seconds{...}
kyverno_policy_results_total{policy,rule,policy_type,rule_result}
kyverno_policy_execution_duration_seconds{...}
controller_runtime_reconcile_total{...}
```

**Gatekeeper**:

```
gatekeeper_violations{enforcement_action,...}
gatekeeper_constraints{enforcement_action,status}
gatekeeper_constraint_templates
gatekeeper_request_count{...}
gatekeeper_request_duration_seconds{...}
gatekeeper_audit_duration_seconds
gatekeeper_audit_last_run_time
```

**apiserver** (covers all engines):

```
apiserver_admission_step_admission_duration_seconds{type,operation,resource}
apiserver_admission_webhook_admission_duration_seconds{name,operation,type}
apiserver_admission_controller_admission_duration_seconds{name,operation,type}
apiserver_admission_webhook_request_total{name,code}
apiserver_audit_event_total
apiserver_audit_requests_rejected_total
```

### 34.2 Alerts to set

- **Any failed admission in Enforce mode**: `kyverno_admission_review_duration_seconds_count{action="error"} > 0` for 5m.
- **Webhook latency p99 > 500ms**: `apiserver_admission_webhook_admission_duration_seconds:p99 > 0.5`.
- **Gatekeeper audit drift**: `time() - gatekeeper_audit_last_run_time > 600` (no audit in 10 min).
- **Any policy bypass attempt**: `apiserver_admission_step_admission_duration_seconds_count{type="validate",operation="CREATE",rejected="true"}` — track baseline, alert on spike.
- **Policy engine pod restarts > 0** in last hour.

The "audit drift" alert is the most important. A Gatekeeper audit controller that has not run in 10 minutes means you are *blind to existing violations*. Equivalent for Kyverno: `controller_runtime_reconcile_total` flat for reports controller.

### 34.3 The policy-bypass scenario

A particularly subtle failure: an admission webhook with `failurePolicy: Ignore` silently fails open during a Kyverno restart. Pods are admitted without policy evaluation. The metric you watch:

```
kyverno_admission_review_duration_seconds_count{action="error"}
```

If this is climbing while admissions are still happening, you are *bypassing* policy without rejecting requests. Alert immediately and consider switching to `failurePolicy: Fail`.

---

## 35. Pitfalls

A staff engineer's list of footguns. None of these are theoretical; all of them have shipped to production somewhere.

1. **PSA labels not set on a namespace.** No label → no enforcement → effectively `privileged`. Set a cluster-wide default and audit which namespaces are exempt.
2. **Using `warn`/`audit` forever without flipping to `enforce`.** Audit data alone does not prevent bad pods. Set a sunset date.
3. **PSA version pin set to `latest`.** Cluster upgrade tightens the profile silently; previously-admitted workloads suddenly break on update.
4. **Kyverno webhook `failurePolicy: Ignore`.** Silent bypass during Kyverno restart or evict. Use `Fail` with multiple replicas.
5. **Gatekeeper sync controller missing kinds.** Cross-resource Rego fails open because the data is not in OPA's cache. Add the missing kind to Config or change the rule.
6. **Rego policy bug allowing privileged escalation.** Common: forgetting to check both `containers` and `initContainers`. Always lint with `regal`; always include adversarial test cases.
7. **VAP CEL expression with DivByZero / missing-key.** A CEL expression that throws errors fails closed (rejects) under `failurePolicy: Fail`. Use `has()` guards before field access.
8. **Falco rule with too-broad condition.** "Write below /etc" without exceptions fires on every legitimate container startup that touches `/etc/resolv.conf`. Tune.
9. **Tetragon SIGKILL on a system process.** A TracingPolicy matching too broadly kills kubelet → node falls out. Always test in observe-only first; never deploy enforce-mode without a canary node.
10. **Seccomp `Unconfined` by default for system pods.** `--seccomp-default=true` is not on by default on older kubelet versions. Audit; turn it on.
11. **AppArmor profile referenced by Pod but not loaded on the node.** Pod stays pending or fails to start. Use a DaemonSet to load profiles before workload schedules.
12. **Audit log access not itself audited.** Attackers tamper with audit and you don't notice. Audit reads of the audit log path.
13. **SIEM ingestion lag.** Falco events take 10 minutes to reach SIEM; by then the attacker is gone. Aim for sub-minute end-to-end; use streaming, not batch.
14. **Compliance scan run in prod once a year.** Continuous scanning is the only useful posture; annual scans tell you about regressions that have been latent for 11 months.
15. **Runtime detection without an IR runbook.** Falco fires, on-call has no idea what to do. Write the runbook; rehearse with tabletop exercises.
16. **Image scanning blocking on transient registry outage.** verifyImages with `required: true` plus a slow registry blocks all admissions. Use timeout + fallback.
17. **Honeytokens that look real but are too obvious.** A Secret named `fake-credentials-do-not-touch` is comedy. Make decoys realistic — `db-prod-master`, `aws-deploy-key`.
18. **Ignoring `kube-system` in policy.** System pods are exempt for a reason but also are a huge attack surface. Audit (not enforce) policies on `kube-system`.
19. **Mutate webhooks running too late in admission ordering.** Kyverno's mutate runs before validate, but if you have multiple mutating webhooks the order is alphabetical by name. Istio adds a sidecar; your `drop-all-caps` policy doesn't apply to it because Istio is named after Kyverno alphabetically. Be aware of admission ordering.
20. **Assuming PSA covers everything.** It doesn't cover image registries, resource limits, network policy, etc. Layer Kyverno/Gatekeeper/VAP on top.
21. **`failureAction: Enforce` on a generate rule.** Generate rules should usually be advisory; if generation fails (e.g., RBAC), Enforce blocks the parent resource creation. Use Audit.
22. **Not testing policies in CI before merge.** Policy bugs deploy straight to prod; cluster goes read-only for hours. `kyverno test` / `gator test` in every PR.
23. **Falco running without falcosidekick.** Events go to stdout only; never reach the SIEM. Always pair Falco with falcosidekick + Pager / SIEM target.
24. **Same RBAC rolebinding for human admin and CI bot.** When CI bot is compromised, admin alerts trigger but they look like CI activity. Separate identities, separate RBAC.
25. **Mounting the docker socket / containerd socket inside a Pod.** Instant container escape. PSA does not block this directly; needs Kyverno/Gatekeeper.
26. **Forgetting `runAsNonRoot: true` at the Pod level when only one container has it.** PSA restricted requires it at pod or all-container. Some Helm charts set it on one container and inherit the rest.
27. **Tetragon enforce on `execve` of `/bin/sh`** without exceptions for `kubectl debug`. Operators can no longer debug; incident response broken. Always allow-list essential ops paths.
28. **PolicyReport CRDs filling etcd.** Kyverno emits a PolicyReport per resource by default. In large clusters, etcd fills. Configure `--maxReports` and cleanup CronJobs.

---

## 36. Cross-References

This chapter sits in the middle of the K8s security stack. The full map:

- Chapter 06 — Admission control deep dive. Where webhooks, ValidatingAdmissionPolicy, and the admission chain live. Read first if you have not yet.
- Chapter 07 — Authentication and authorization. Workload identity, ServiceAccounts, RBAC, OIDC. The control that decides *who*, before policy decides *what*.
- Chapter 16 — Cilium and eBPF deep dive. Where Tetragon lives natively. Hubble for L7 visibility.
- Chapter 17 — Ingress, gateway, service mesh. Where mTLS lives; supplementary identity layer.
- Chapter 20 — NetworkPolicy and segmentation. The L3/L4 boundary.
- Chapter 27 — Supply chain security. Image signing, SBOM, in-toto, cosign. The build/registry side of the same threat model.
- Chapter 29 — Sandbox runtimes. gVisor, Kata, runtime classes. The "prevent kernel access entirely" layer.
- Chapter 04 — etcd internals. Where the audit log and secret encryption-at-rest live.
- Chapter 25 — Multi-tenancy. Where namespace isolation and PSA combine.

The chapters together describe a defense-in-depth stack from build (27) to admission (06, 28) to runtime (28, 16, 29) to forensics (28). No single chapter is sufficient; the security posture is the *product* of all of them.

The closing point: runtime security in Kubernetes is an exercise in observability married to policy. The kernel is the source of truth — every syscall, every connection, every credential use is observable. The policy engine is the *interpretation* of that truth — what is normal, what is suspicious, what to block, what to alert. The staff engineer's job is to keep the observability lossless, the interpretation accurate, and the response runbook short enough to actually run at 03:00.
