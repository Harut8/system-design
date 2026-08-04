# Kubernetes Secrets & ConfigMaps: Configuration Management, etcd Encryption, Kubelet Caching, and External Secret Stores

How dynamic configuration and sensitive payload state land inside pods, how the control plane stores and protects them, and how node-level runtime engines propagate or isolate them. This chapter is the configuration counterpart of chapter 11 (Pod Internals), chapter 04 (etcd Internals), chapter 07 (Authentication & Authorization), and chapter 19 (Storage CSI).

The goal: by the end, you will understand the exact mechanics of `ConfigMap` and `Secret` API objects—from etcd storage serialization and envelope encryption to kubelet cache propagation strategies, atomic symlink tree swaps, `subPath` bind-mount pitfalls, `immutable: true` watch reduction, Secret Store CSI Driver / External Secrets Operator integrations, and enterprise secret rotation patterns.

---

## Table of Contents

1. [The Configuration & Secrets Object Model](#1-the-configuration--secrets-object-model)
2. [etcd Storage Mechanics & Encryption at Rest](#2-etcd-storage-mechanics--encryption-at-rest)
3. [Consumption Mechanism 1: Environment Variables (`env` & `envFrom`)](#3-consumption-mechanism-1-environment-variables-env--envfrom)
4. [Consumption Mechanism 2: Volume Mounts & Atomic Symlink Swaps](#4-consumption-mechanism-2-volume-mounts--atomic-symlink-swaps)
5. [The Kubelet Volume Manager & Propagation Engine](#5-the-kubelet-volume-manager--propagation-engine)
6. [The `subPath` Static Bind-Mount Trap](#6-the-subpath-static-bind-mount-trap)
7. [Control-Plane Scalability: `immutable: true`](#7-control-plane-scalability-immutable-true)
8. [External Secret Management & Enterprise Integrations](#8-external-secret-management--enterprise-integrations)
9. [Secret Rotation Strategies & Signal Handlers](#9-secret-rotation-strategies--signal-handlers)
10. [RBAC Scoping & Security Boundaries](#10-rbac-scoping--security-boundaries)
11. [Staff-Level Pitfalls & Anti-Patterns](#11-staff-level-pitfalls--anti-patterns)
12. [TL;DR Reference Card](#12-tldr-reference-card)

---

## 1. The Configuration & Secrets Object Model

Kubernetes separates *application logic* (container images) from *application parameters* (`ConfigMap`) and *sensitive payload credentials* (`Secret`). Both are first-class API objects stored in etcd, but they have distinct specs, encoding contracts, and intended lifecycle constraints.

### 1.1 Object Specification & Schemas

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: prod
data:
  game.properties: |
    enemies=aliens
    lives=3
    allowed=true
  LOG_LEVEL: "debug"
binaryData:
  favicon.ico: iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9h... # Base64 encoded raw bytes
---
apiVersion: v1
kind: Secret
metadata:
  name: app-db-credentials
  namespace: prod
type: Opaque
stringData:
  DB_PASSWORD: "super-secret-pass" # Write-only convenience field (converted to base64 in data by apiserver)
data:
  DB_USER: cG9zdGdyZXM= # Base64 encoded "postgres"
```

#### Field Mechanics

* **`data`**: Key-value map. For `ConfigMap`, values must be valid UTF-8 strings. For `Secret`, values are Base64-encoded byte arrays.
* **`stringData` (Secret only)**: Write-only field provided for human convenience. Upon `POST`/`PUT`/`PATCH`, the `kube-apiserver` encodes `stringData` values into Base64, populates the `data` field, and clears `stringData`. It is never returned on `GET`/`LIST`.
* **`binaryData` (ConfigMap only)**: Used to store unencoded binary data (e.g., gzip tarballs, raw certificates, image icons). Stored internally as Base64-encoded strings but distinct from `data` for OpenAPI schema validation.


### 1.2 Secret Types Matrix

Kubernetes uses the `type` field to enforce structural validation and semantic expectations for built-in controllers.

| Secret Type | Required Data Keys | Purpose / Primary Consumer |
|---|---|---|
| `Opaque` | Arbitrary user keys | Default type for user application credentials. |
| `kubernetes.io/service-account-token` | `token`, `ca.crt`, `namespace` | Auto-generated service account tokens (legacy long-lived; projected tokens preferred). |
| `kubernetes.io/dockercfg` | `.dockercfg` | Legacy Docker v1 authentication format. |
| `kubernetes.io/dockerconfigjson` | `.dockerconfigjson` | Docker v2 JSON auth config used by kubelet for `imagePullSecrets`. |
| `kubernetes.io/basic-auth` | `username`, `password` | HTTP Basic Authentication credentials. |
| `kubernetes.io/ssh-auth` | `ssh-privatekey` | SSH private keys (e.g., git repo access). |
| `kubernetes.io/tls` | `tls.crt`, `tls.key` | X.509 TLS certificate and private key pairs (used by Ingress, Istio, cert-manager). |
| `bootstrap.kubernetes.io/token` | `token-id`, `token-secret` | Temporary tokens used during `kubeadm join` node bootstrapping. |

---

## 2. etcd Storage Mechanics & Encryption at Rest

A common operational misconception is that standard Kubernetes `Secrets` are encrypted by default. **Base64 is an encoding format, not an encryption algorithm.** Without explicit configuration, Secrets are stored in etcd as unencrypted, plaintext Base64 strings. Anyone with read access to etcd (or etcd backups) can read every secret in the cluster.

### 2.1 The etcd Storage Pipeline

```text
           kubectl apply / REST Client
                       │
                       ▼
              ┌─────────────────┐
              │ kube-apiserver  │
              └────────┬────────┘
                       │ Authentication & Authorization (RBAC)
                       ▼
              ┌─────────────────┐
              │ Mutating Webhook│
              └────────┬────────┘
                       │
                       ▼
      ┌──────────────────────────────────┐
      │ Storage Transformer Layer         │
      │                                  │
      │ ┌──────────────────────────────┐ │
      │ │ Encryption Provider Chain    │ │
      │ │ (Identity / AES / KMS v2)    │ │
      │ └──────────────┬───────────────┘ │
      └────────────────┼─────────────────┘
                       │ Serialized protobuf / encrypted envelope
                       ▼
              ┌─────────────────┐
              │   etcd Storage  │  Key: /registry/secrets/prod/app-db-credentials
              └─────────────────┘  Value: k8s:enc:kms:v2:provider-aws:base64bytes...
```


### 2.2 Encryption at Rest Architecture

To encrypt secrets in etcd, `kube-apiserver` must be launched with the flag `--encryption-provider-config=/etc/kubernetes/enc/encryption-config.yaml`.

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources:
      - secrets
      - configmaps # Optional: encrypt sensitive ConfigMaps if required by compliance
    providers:
      - kms:
          apiVersion: v2
          name: aws-kms-provider
          endpoint: unix:///var/run/kms-plugin/socket.sock
          timeout: 3s
      - aesgcm:
          keys:
            - name: key1
              secret: c2VjcmV0IGlzIGEgc2VjcmV0IGlzIGEgc2VjcmV0IQ==
      - identity: {} # Fallback: allows reading unencrypted legacy secrets
```

#### Provider Hierarchy & Security Properties:
1. **`identity`**: Default provider. Plaintext storage (no encryption).
2. **`secretbox`**: XSalsa20 + Poly1305 symmetric encryption. Strong cipher, manual key management.
3. **`aescbc` / `aesgcm`**: AES-CBC / AES-GCM with PKCS#7 padding. Requires manual key rotation in configuration files.
4. **`kms` (v2)**: **Production Staff Standard.** Envelope encryption backed by an external Hardware Security Module (HSM) or Cloud Key Management Service (AWS KMS, GCP KMS, Azure Key Vault, HashiCorp Vault).

### 2.3 KMS v2 Envelope Encryption Architecture

KMS v2 eliminates control-plane performance bottlenecks by using local Data Encryption Keys (DEKs) encrypted by a remote Key Encryption Key (KEK).

```
                      ┌───────────────────────────────────────────────┐
                      │                kube-apiserver                 │
                      │                                               │
                      │ 1. Generate local DEK (random AES-256 key)    │
                      │ 2. Encrypt Secret payload locally with DEK    │
                      └───────┬───────────────────────────────┬───────┘
                              │                               │
       3. Send raw DEK over   │                               │ 5. Store Encrypted DEK
          Unix Domain Socket  │                               │    + Encrypted Payload
                              ▼                               ▼
                      ┌───────────────┐               ┌───────────────┐
                      │ KMS v2 Plugin │               │     etcd      │
                      └───────┬───────┘               └───────────────┘
                              │ 4. Encrypt DEK with KEK
                              ▼
                      ┌───────────────┐
                      │ External KMS  │ (AWS KMS / Vault / GCP KMS)
                      │  (Holds KEK)  │
                      └───────────────┘
```

* **Write Path**: `apiserver` generates a unique DEK locally, encrypts the Secret payload with the DEK, sends the raw DEK to the KMS plugin over gRPC (Unix socket), receives the encrypted DEK (encrypted by KEK), and writes `[Encrypted DEK + Encrypted Payload]` into etcd.
* **Read Path**: `apiserver` reads the blob from etcd, sends the Encrypted DEK to KMS plugin for decryption, receives the raw DEK, and decrypts the Secret payload in memory.
* **DEK Caching**: KMS v2 caches decrypted DEKs in memory using key IDs, avoiding per-read network round-trips to external Cloud KMS systems.

---

## 3. Consumption Mechanism 1: Environment Variables (`env` & `envFrom`)

Environment variables are the simplest way to consume `ConfigMap` and `Secret` data, but they carry severe operational and security trade-offs.

### 3.1 Spec Declarations

```yaml
spec:
  containers:
  - name: app
    image: myapp:1.0
    env:
      - name: DB_HOST
        valueFrom:
          configMapKeyRef:
            name: app-config
            key: DB_HOST
      - name: DB_PASSWORD
        valueFrom:
          secretKeyRef:
            name: app-db-credentials
            key: DB_PASSWORD
            optional: false # Pod fails to start if key/secret is missing
    envFrom:
      - configMapRef:
          name: app-config
      - secretRef:
          name: app-db-credentials
```

### 3.2 Runtime Behavior & Frozen State

> **Critical Rule:** Environment variables are resolved **once** during container creation by the container runtime (`containerd` / `CRI-O`). **They are permanently frozen for the lifetime of the process.**

If a user modifies a `ConfigMap` or `Secret` in the API server:
1. Existing running pods **do not** receive the update.
2. The environment variables inside `/proc/<pid>/environ` remain unchanged.
3. Applications must be restarted (e.g., via a rolling Deployment restart) to see updated environment variable values.

### 3.3 Security & Operational Exposure Risks

Env vars are widely considered an **anti-pattern for sensitive secrets** due to OS-level leakage vectors:

1. **Process Listing Exposure (`ps aux`)**: In many legacy Linux environments or debugging containers, child processes or monitoring tools can view environment variables of running processes via `/proc/<pid>/environ`.
2. **Crash Dumps & Application Logs**: Application frameworks (Django, Node.js, Spring Boot) often print the full environment map (`process.env` / `os.environ`) to stderr during unhandled exceptions or boot logs.
3. **Child Process Inheritance**: Any sub-process spawned via `fork()`/`exec()` inherits the full parent process environment, exposing secrets to unauthorized scripts or third-party binaries.
4. **No Granular Scoping**: `envFrom` imports every key in a `ConfigMap`/`Secret` into the environment, creating accidental collisions with existing environment variables (`PATH`, `HOST`, `PORT`).

---

## 4. Consumption Mechanism 2: Volume Mounts & Atomic Symlink Swaps

Volume mounts provide **dynamic, live updates** of `ConfigMap` and `Secret` data inside containers without restarting pods.

### 4.1 Volume Specification

```yaml
spec:
  containers:
  - name: app
    image: myapp:1.0
    volumeMounts:
    - name: config-volume
      mountPath: /etc/config
      readOnly: true
    - name: secret-volume
      mountPath: /etc/secrets
      readOnly: true
  volumes:
  - name: config-volume
    configMap:
      name: app-config
      defaultMode: 0640
  - name: secret-volume
    secret:
      secretName: app-db-credentials
      defaultMode: 0400
```

### 4.2 The Atomic Symlink Tree Engine

When the kubelet mounts a `ConfigMap` or `Secret` volume into a container, it does **not** write flat files directly into the directory. Instead, it constructs an atomic symlink tree to allow instant, non-disruptive rotation across all mounted keys.

#### Directory Layout Inside Container Mount Path (`/etc/config`):

```
/etc/config/
├── ..data -> ..2026_08_04_07_30_00.123456789 (symlink to active timestamp dir)
├── ..2026_08_04_07_30_00.123456789/           (real directory containing files)
│   ├── game.properties
│   └── LOG_LEVEL
├── game.properties -> ..data/game.properties   (symlink pointing via ..data)
└── LOG_LEVEL -> ..data/LOG_LEVEL              (symlink pointing via ..data)
```

#### Atomic Rotation Sequence when `ConfigMap` is Updated:
1. Kubelet creates a **new** timestamped directory: `/etc/config/..2026_08_04_07_35_42.987654321`.
2. Kubelet writes the updated key files into this new directory.
3. Kubelet creates a new temporary symlink: `/etc/config/..data_tmp -> ..2026_08_04_07_35_42.987654321`.
4. Kubelet calls **`renameat(2)`** to atomically swap `/etc/config/..data_tmp` to `/etc/config/..data`.
5. Kubelet asynchronously deletes the old timestamped directory (`..2026_08_04_07_30_00.123456789`).

```
BEFORE UPDATE:
/etc/config/game.properties ────────► ..data/game.properties ────────► ..2026_08_04_07_30_00/game.properties

AFTER ATOMIC RENAME (renameat):
/etc/config/game.properties ────────► ..data/game.properties ────────► ..2026_08_04_07_35_42/game.properties
```

#### Why Atomic Symlinks Matter:
Applications opening `/etc/config/game.properties` will **never** read a partially written file or an empty buffer during a config update. The read operation either lands on the old directory or the new directory instantly.

---

## 5. The Kubelet Volume Manager & Propagation Engine

How does the kubelet detect that a `ConfigMap` or `Secret` has changed in etcd, and how long does it take for changes to reflect on disk?

### 5.1 Kubelet Change Detection Strategies

The kubelet's sync behavior is governed by the `--configMapAndSecretChangeDetectionStrategy` flag on `kubelet`.

```
                    ┌──────────────────────────────────────────────┐
                    │                kube-apiserver                │
                    └───────▲──────────────────────▲───────────────┘
                            │                      │
                  Watch     │                      │ Get / List
             (Event stream) │                      │ (Polling)
                            │                      │
               ┌────────────┴─────────────┐  ┌─────┴────────────────────┐
               │ Strategy 1: Watch (Def) │  │ Strategy 3: Get           │
               └────────────┬─────────────┘  └─────┬────────────────────┘
                            │                      │
                            ▼                      ▼
                    ┌──────────────────────────────────────────────┐
                    │             Kubelet Volume Manager           │
                    │   - Local Cache                              │
                    │   - Sync Loop (default: 1 minute)            │
                    └──────────────────────┬───────────────────────┘
                                           │
                                           ▼ Atomic Symlink Swap
                    ┌──────────────────────────────────────────────┐
                    │ Container Filesystem Mount (/etc/config)     │
                    └──────────────────────────────────────────────┘
```

1. **`Watch` (Default)**: Kubelet establishes a continuous API `WATCH` connection for all `ConfigMap` and `Secret` objects referenced by active pods on its node. When etcd changes, apiserver pushes an update event immediately to kubelet.
2. **`Cache`**: Kubelet caches objects in an internal TTL cache (`--sync-frequency`, default 1m). Updates are picked up on cache expiration.
3. **`Get`**: Kubelet issues a direct HTTP `GET` request to `kube-apiserver` every time the pod sync loop executes. High API server load; anti-pattern for large clusters.

### 5.2 End-to-End Propagation Latency Math

The total delay between running `kubectl apply -f configmap.yaml` and seeing the new content inside the container file is:

$$\text{Total Latency} = T_{\text{apiserver watch push}} + T_{\text{kubelet sync loop}} + T_{\text{kernel VFS cache flushing}}$$

* **Watch Push**: $\approx 10-100 \text{ ms}$
* **Kubelet Sync Period**: Controlled by `syncFrequency` (default 1 minute).
* **TTL Cache Buffer**: Controlled by `configMapAndSecretChangeDetectionStrategy` cache parameters.
* **Total Expected Delay**: **$0 \text{ to } 60 \text{ seconds}$**.

---

## 6. The `subPath` Static Bind-Mount Trap

A widespread configuration mistake occurs when mounting a single key from a `ConfigMap` or `Secret` into a directory using `subPath`.

### 6.1 The Broken `subPath` Spec

```yaml
spec:
  containers:
  - name: nginx
    image: nginx:1.27
    volumeMounts:
    - name: config-volume
      mountPath: /etc/nginx/nginx.conf # Intended to override ONLY nginx.conf
      subPath: nginx.conf              # <--- THE TRAP!
  volumes:
  - name: config-volume
    configMap:
      name: nginx-config
```

### 6.2 Why `subPath` Breaks Live Propagation

When `subPath` is specified, the Linux kernel performs a **direct file-to-file bind mount** (`mount --bind /tmp/proc/..data/nginx.conf /etc/nginx/nginx.conf`).

```
Standard Mount (Dynamic Symlink):
/etc/nginx/ -> symlink -> ..data -> ..2026_08_04_07_35_42/ -> [Target File] (SWAPPABLE)

subPath Mount (Static Inode Lock):
/etc/nginx/nginx.conf -> Direct Bind-Mount to Inode #1049281 (FROZEN TO SPECIFIC INODE)
```

1. When the `ConfigMap` is updated, the kubelet creates a new timestamp directory (`..2026_08_04_08_00_00`) with a new inode for `nginx.conf`.
2. The kubelet updates the `..data` symlink to point to the new directory.
3. **However, the container's `subPath` mount is directly pinned to the OLD inode (#1049281).**
4. The container will **NEVER** see the updated content. It remains stuck on the version of the file that existed when the container was created.

> **Staff Engineer Rule:** Never use `subPath` if you require dynamic configuration updates. If you must mount a single file into an existing directory containing other files, use a dedicated sub-directory mount or a sidecar reloader pattern.

---

## 7. Control-Plane Scalability: `immutable: true`

In large-scale Kubernetes clusters (e.g., 5,000+ nodes, 100,000+ pods), holding active `WATCH` connections for tens of thousands of static `ConfigMap` and `Secret` objects imposes severe memory and CPU overhead on `kube-apiserver` and `kubelet`.

### 7.1 Immutable Resources (`immutable: true`)

Kubernetes 1.19+ supports marking `ConfigMaps` and `Secrets` as immutable.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: static-app-config
  namespace: prod
immutable: true # <--- Disables API server and Kubelet watches
data:
  database.json: |
    {"max_connections": 500}
```

### 7.2 Scalability Benefits & Internal Behavior

```
STANDARD CONFIGMAP (Watch Active):
etcd ◄──► apiserver ◄────── WATCH Stream ──────► Kubelet (Holds open TCP socket)

IMMUTABLE CONFIGMAP (Watch Dropped):
etcd ◄──► apiserver    (No Watch Established)    Kubelet (Zero Watch Overhead)
```

1. **Watch Termination**: Once the kubelet mounts an `immutable` `ConfigMap` or `Secret`, it immediately closes its `WATCH` connection to `kube-apiserver` for that object.
2. **etcd & API Server Relief**: Reduces memory footprint (`watchCache`), context switches, and network TCP socket allocations on control-plane nodes by up to 60% in large clusters.
3. **Modification Protection**: Any attempt to issue a `PATCH` or `PUT` to an immutable object is rejected by `kube-apiserver` with `422 Unprocessable Entity`.
4. **Rotation Workflow**: To change an immutable config, you must create a new object with a new name (e.g., `app-config-v2`) and perform a rolling deployment update.

---

## 8. External Secret Management & Enterprise Integrations

Native Kubernetes Secrets have two structural shortcomings in enterprise environments:
1. Storing Secrets in Git repository manifests (GitOps) leads to plaintext credential leaks.
2. Native Secrets lack automatic rotation, fine-grained auditing, and central governance provided by enterprise vaults (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault).

### 8.1 Architecture: Secret Store CSI Driver vs External Secrets Operator (ESO)

```
                       ENTERPRISE SECRET STORES
            ┌─────────────────────────────────────────────┐
            │  HashiCorp Vault / AWS Secrets Manager /    │
            │  GCP Secret Manager / Azure Key Vault       │
            └──────────────▲───────────────▲──────────────┘
                           │               │
             gRPC Protocol │               │ REST API (HTTPS)
                           │               │
┌──────────────────────────┴────┐  ┌───────┴───────────────────────────┐
│ Secret Store CSI Driver       │  │ External Secrets Operator (ESO)   │
│ (In-flight Ephemeral Mount)   │  │ (Syncs Vault -> Native K8s Secret)│
│                               │  │                                   │
│  - Mounts directly as volume  │  │  - Creates real K8s Secret object │
│  - No native K8s Secret created│ │  - Works with env vars & standard │
│  - Stored in tmpfs (RAM disk) │  │    volume mounts                  │
└──────────────┬────────────────┘  └───────────────┬───────────────────┘
               │                                   │
               ▼                                   ▼
┌───────────────────────────────┐  ┌───────────────────────────────────┐
│ Pod Mount: /var/run/secrets/  │  │ Native Secret Object:             │
│ (ephemeral tmpfs)             │  │ apiVersion: v1 / Kind: Secret     │
└───────────────────────────────┘  └───────────────────────────────────┘
```

### 8.2 Architectural Trade-Off Analysis

| Feature / Dimension | Secret Store CSI Driver | External Secrets Operator (ESO) | Sealed Secrets (Bitnami) |
|---|---|---|---|
| **Storage in etcd?** | **No** (Bypasses etcd completely; mounts directly from Vault to container `tmpfs`). | **Yes** (Fetches from Vault and creates a native K8s `Secret`). | **Yes** (Decrypts `SealedSecret` CRD into a native K8s `Secret`). |
| **GitOps Safety** | Excellent (Manifest references external Vault path). | Excellent (Manifest contains `ExternalSecret` CRD referencing Vault key). | Excellent (Asymmetrically encrypted ciphertext checked into Git). |
| **Env Var Support** | Requires `SecretProviderClass` secret synchronization opt-in. | **Native** (Produces normal K8s Secret consumed via `env`). | **Native** (Produces normal K8s Secret consumed via `env`). |
| **Rotation Support** | Automatic autorotation on volume mount (`enableAutoRotation: true`). | Polling sync interval (`refreshInterval: 1h`). | Manual re-encryption required upon master key rotation. |
| **Blast Radius** | Smallest (Secret exists only in pod memory/tmpfs). | Medium (Secret present in etcd & namespace). | Medium (Secret present in etcd & namespace). |

---

## 9. Secret Rotation Strategies & Signal Handlers

Updating a `ConfigMap` or `Secret` volume on disk is only half the battle. **The application running inside the container must be informed that the file has changed.**

### 9.1 The Four Production Rotation Patterns

```
                                 CONFIGMAP/SECRET UPDATED
                                            │
         ┌──────────────────────────────────┼──────────────────────────────────┐
         │                                  │                                  │
         ▼                                  ▼                                  ▼
┌─────────────────┐                ┌─────────────────┐                ┌─────────────────┐
│ Pattern 1:      │                │ Pattern 2:      │                │ Pattern 3:      │
│ In-Process      │                │ Sidecar / Signal│                │ Reloader        │
│ File Watcher    │                │ (SIGHUP Engine) │                │ Controller      │
└────────┬────────┘                └────────┬────────┘                └────────┬────────┘
         │                                  │                                  │
         ▼                                  ▼                                  ▼
Application reloads              Sidecar detects file               Controller updates Pod
config dynamically in            change & sends SIGHUP              spec hash, triggering
memory (inotify / fsnotify)      to main process                    Rolling Deployment Update
```

#### Pattern 1: In-Process Dynamic Reload (`fsnotify` / `inotify`)
Applications (e.g., NGINX, Envoy, Prometheus, Go services) use OS filesystem notifications (`inotify` on Linux) to watch `/etc/config/..data`. When the symlink rename occurs, the watcher triggers an in-memory config reload.

#### Pattern 2: Sidecar Signal Generator (`SIGHUP`)
For legacy applications that cannot watch files but support reload signals (e.g., `kill -HUP <pid>`):
A sidecar container shares the process namespace (`shareProcessNamespace: true`) or volume mount, watches the config file, and sends `SIGHUP` to the main application process.

#### Pattern 3: Deployment Immutable Hash / Reloader Controller
If applications read config only at startup, use an automated controller like **Reloader** (or Kustomize `configMapGenerator`).
Reloader watches `ConfigMap`/`Secret` changes and automatically injects an annotation containing the content hash into the `Deployment` template:

```yaml
kind: Deployment
metadata:
  annotations:
    reloader.stakater.com/auto: "true"
spec:
  template:
    metadata:
      annotations:
        config.k8s.io/hash: "a8f9c3d2e1b4..." # Triggers rolling update of Pods
```

#### Pattern 4: Immutable ConfigMap Name Versioning (Staff Best Practice)
Append a version suffix or content hash to the `ConfigMap` name (`app-config-v1` $\rightarrow$ `app-config-v2`). Update the `Deployment` spec to point to `app-config-v2`. This triggers a standard Kubernetes rolling update with full canary, rollback, and readiness gate protection.

---

## 10. RBAC Scoping & Security Boundaries

`Secrets` are primary targets for privilege escalation in Kubernetes clusters. Improper RBAC permissions can allow an unprivileged user or compromised pod to steal cluster-admin credentials.

### 10.1 The `LIST`/`WATCH` Privilege Escalation Vector

> **Security Rule:** Never grant `list` or `watch` permissions on `secrets` globally or across namespaces unless strictly required by security operators.

```yaml
# DANGEROUS ROLE: Grants access to ALL secrets in namespace
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: secret-reader
  namespace: prod
rules:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "list", "watch"] # <--- "list" allows dumping every secret at once!
```

* **Why `list` is Dangerous**: A service account with `get` on a specific secret name can only fetch that single secret. A service account with `list` can retrieve **all secrets** in the namespace in a single HTTP request (including TLS private keys, service account tokens, and database passwords).
* **Resource Names Restricting**: Always scope RBAC `get` access to specific secret instances using `resourceNames`:

```yaml
# SECURE ROLE: Scoped to specific Secret instance
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: app-secret-reader
  namespace: prod
rules:
- apiGroups: [""]
  resources: ["secrets"]
  resourceNames: ["app-db-credentials"] # Restricts access to THIS secret only
  verbs: ["get"]
```

---

## 11. Staff-Level Pitfalls & Anti-Patterns

### 11.1 The 1MB etcd Object Limit Crash
`ConfigMaps` and `Secrets` are stored as single keys in etcd. etcd enforces a hard maximum payload limit of **1MB per object** (`--max-request-bytes`).
* **Symptom**: `kubectl apply` fails with `Error from server (RequestEntityTooLarge): limit is 1048576 bytes`.
* **Fix**: Do not embed large binary assets, fat Java JARs, or multi-megabyte dataset files into `ConfigMaps`. Use Persistent Volume Claims (PVCs), S3/GCS object storage, or OCI artifact registries instead.

### 11.2 The `stringData` Overwrite Footgun
When updating a `Secret` via `kubectl apply`, developers often forget that `stringData` is write-only. If a manifest specifies `stringData` alongside an existing `data` field, `stringData` will overwrite the corresponding keys in `data` silently upon apply.

### 11.3 Symlink Traversal Breakage in Custom Scripts
Custom shell scripts running inside containers that read mounted config files using fixed symlink dereferencing (e.g., `cp /etc/config/file.txt /tmp/`) will copy the static content at the time of copy, losing all future dynamic updates.

### 11.4 `tmpfs` Memory Overhead for Huge Secrets
Secrets mounted as volumes are backed by node memory (`tmpfs`). Mounting 500MB of secret files into a container consumes 500MB of node RAM and counts against the container's memory limits, potentially triggering an **Out-Of-Memory (OOM) Kill**.

---

## 12. TL;DR Reference Card

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        KUBERNETES SECRETS & CONFIGMAPS CHEAT SHEET                     │
├──────────────────────┬──────────────────────────────────┬──────────────────────────────┤
│ Concern              │ ConfigMap                        │ Secret                       │
├──────────────────────┼──────────────────────────────────┼──────────────────────────────┤
│ Primary Purpose      │ Non-sensitive application config │ Sensitive credentials/keys   │
│ etcd Encoding        │ Plaintext UTF-8 / Base64 binary │ Base64 data (Requires KMS)   │
│ Default Node Mount   │ Standard filesystem              │ RAM-backed tmpfs (No swap)   │
│ Max Size Limit       │ 1 MB (etcd hard limit)           │ 1 MB (etcd hard limit)       │
├──────────────────────┴──────────────────────────────────┴──────────────────────────────┤
│ CONSUMPTION PATTERNS                                                                   │
│ 1. Environment Vars  │ STATIC / FROZEN at boot. No live updates. Exposed in /proc.    │
│ 2. Volume Mounts     │ DYNAMIC. Updated via atomic symlink swap (..data -> ..time).   │
│ 3. subPath Mounts    │ FROZEN. Static inode bind-mount; BREAKS dynamic updates.       │
│ 4. immutable: true   │ DROPS Kubelet watches. Reduces apiserver & etcd load by ~60%.│
├────────────────────────────────────────────────────────────────────────────────────────┤
│ ENTERPRISE BEST PRACTICES                                                              │
│ • Production Security : Enable KMS v2 envelope encryption in kube-apiserver.           │
│ • GitOps & Rotation   : Use Secret Store CSI Driver or External Secrets Operator (ESO).│
│ • RBAC Safety         : Never grant "list/watch" on secrets globally; scope to names. │
│ • App Propagation     : Use immutable CM names + rolling deploys, or fsnotify watchers.│
└────────────────────────────────────────────────────────────────────────────────────────┘
```
