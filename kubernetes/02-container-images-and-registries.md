# Container Images and Registries: OCI, Layers, Registries, and the Supply Chain

What a container image actually *is*, byte for byte: a graph of content-addressed blobs that together describe a filesystem and how to execute it. This chapter takes the abstraction `nginx:1.27` and unfolds it into its real form — a manifest pointing to a config and to a stack of gzipped tarballs, all addressed by their sha256 digest, sitting in a registry that speaks a tightly specified HTTP API. Then it walks the wire: how a `docker pull` becomes HEAD/GET requests, how layers are unpacked by an OverlayFS snapshotter, how `kubelet` materializes the rootfs that runc will pivot into. Finally it climbs back up to the modern concern that wraps all of this — supply chain — and explains Sigstore, cosign, SBOMs, and SLSA as a coherent system rather than a list of acronyms.

If chapter 01 explained *how a container runtime executes a container given an image*, this chapter explains *what an image is, where it lives, and how it gets to the node*. Every later chapter that says "pull the image" or "verify the signature" or "wait for image GC" is referring back to the machinery here.

---

## Table of Contents

1. [What an Image Actually Is](#1-what-an-image-actually-is)
2. [The OCI Image Specification](#2-the-oci-image-specification)
3. [Image Index: Multi-Architecture and Manifest Lists](#3-image-index-multi-architecture-and-manifest-lists)
4. [Image Manifest: The Pointer Object](#4-image-manifest-the-pointer-object)
5. [Image Config: rootfs, History, and Runtime Defaults](#5-image-config-rootfs-history-and-runtime-defaults)
6. [Layers as Deduplicated Tarballs](#6-layers-as-deduplicated-tarballs)
7. [Whiteouts, Opaque Directories, and OverlayFS Materialization](#7-whiteouts-opaque-directories-and-overlayfs-materialization)
8. [Content-Addressable Storage: Why Digests Matter](#8-content-addressable-storage-why-digests-matter)
9. [Tags vs Digests: Mutable Names, Immutable Content](#9-tags-vs-digests-mutable-names-immutable-content)
10. [The Image Config and Build History](#10-the-image-config-and-build-history)
11. [The OCI Distribution Spec v2 Registry API](#11-the-oci-distribution-spec-v2-registry-api)
12. [Registry Authentication: The Token Dance](#12-registry-authentication-the-token-dance)
13. [Registry Implementations and Storage Backends](#13-registry-implementations-and-storage-backends)
14. [Authentication Patterns in Kubernetes](#14-authentication-patterns-in-kubernetes)
15. [Pulling: What Actually Happens on the Wire](#15-pulling-what-actually-happens-on-the-wire)
16. [Image Garbage Collection on the Node](#16-image-garbage-collection-on-the-node)
17. [Lazy and Streaming Pulls: estargz, SOCI, zstd:chunked, Nydus](#17-lazy-and-streaming-pulls-estargz-soci-zstdchunked-nydus)
18. [Supply Chain: Sigstore and Image Signing](#18-supply-chain-sigstore-and-image-signing)
19. [Verifying Signatures at Admission](#19-verifying-signatures-at-admission)
20. [Provenance, SBOMs, and SLSA](#20-provenance-sboms-and-slsa)
21. [OCI Artifacts: Storing Non-Image Content](#21-oci-artifacts-storing-non-image-content)
22. [Image Best Practices](#22-image-best-practices)
23. [Pitfalls](#23-pitfalls)
24. [TL;DR](#24-tldr)

---

## 1. What an Image Actually Is

The single sentence: **an OCI image is a directed acyclic graph of content-addressable JSON and tar blobs.** Everything else is a consequence of that.

When you say `nginx:1.27`, three lookups happen.

1. The string `nginx:1.27` is resolved by a registry into a **manifest digest** — a sha256 of a small JSON document.
2. That JSON document (the **manifest** or the **image index**) lists more sha256 digests: one for the **config** (a small JSON describing rootfs assembly and runtime defaults) and N for the **layers** (gzipped tarballs that, when stacked, produce the filesystem).
3. Each digest can be fetched by `GET /v2/library/nginx/blobs/sha256:<hex>` — the registry has no concept of "image" at the storage layer; it just stores **blobs** and **manifests**, both keyed by their sha256.

```
                              registry namespace: library/nginx
                              tag: 1.27
                                │
                                ▼
                  ┌─────────────────────────────┐
                  │  IMAGE INDEX (manifest list) │   sha256:aaaa...
                  │   mediaType: ...index.v1+json│
                  │   manifests:                 │
                  │     - linux/amd64 → sha256:bbbb...
                  │     - linux/arm64 → sha256:cccc...
                  │     - linux/arm/v7 → sha256:dddd...
                  └─────────────┬───────────────┘
                                │  pick by platform
                                ▼
                  ┌─────────────────────────────┐
                  │  IMAGE MANIFEST              │   sha256:bbbb...
                  │   mediaType: ...manifest.v1+json
                  │   config:   → sha256:eeee... │
                  │   layers:                    │
                  │     - sha256:f0f0... 31 MB   │
                  │     - sha256:f1f1... 12 MB   │
                  │     - sha256:f2f2...  4 KB   │
                  └─────┬──────────────┬────────┘
                        │              │
                        ▼              ▼
              ┌──────────────┐    ┌────────────────┐
              │ IMAGE CONFIG │    │  LAYER BLOBS   │
              │ (small JSON) │    │ (tar+gzip)     │
              │  - rootfs    │    │  diff_ids in   │
              │  - history   │    │  config match  │
              │  - env       │    │  these tars'   │
              │  - entrypoint│    │  uncompressed  │
              │  - cmd       │    │  sha256s       │
              │  - labels    │    └────────────────┘
              └──────────────┘
```

Every arrow is a sha256 digest. The shape is a Merkle tree rooted at the index. Changing one byte of any layer changes its digest, which forces a new manifest, which forces a new index, which forces a new tag mapping. Immutability of content is structural, not a policy.

That property is the spine of everything in this chapter:

- **Deduplication.** The same layer used by `python:3.12-slim` and your own `python:3.12-slim-with-numpy` is the same blob, stored exactly once on disk and in every cache along the way.
- **Verification.** A registry can hand you a blob from any backend (S3, GCS, a malicious mirror) and you can verify it didn't lie by re-hashing.
- **Signing.** Cosign signs the manifest digest, not the tag. You sign immutable content, and tag mutability cannot lie its way past a signature check.
- **Caching.** Every CDN, every node, every laptop can cache by digest forever. The hash is the cache key.
- **Reproducibility.** `image@sha256:bbbb...` is a globally unique, eternally valid pointer. `image:latest` is not.

If you internalize "an image is a Merkle DAG of blobs," the rest of the chapter is filling in the wire formats.

---

## 2. The OCI Image Specification

The OCI Image Spec (`opencontainers/image-spec`) is roughly 100 pages of JSON Schema and prose. The essential vocabulary:

| Term | What it is | Where it lives |
|---|---|---|
| **Blob** | An opaque byte sequence, addressed by `sha256:<hex>` | `/v2/<name>/blobs/<digest>` |
| **Manifest** | JSON describing one image (config + layers) for one platform | `/v2/<name>/manifests/<digest|tag>` |
| **Image Index** | JSON listing manifests by platform (multi-arch) | Also `/v2/<name>/manifests/<digest|tag>` |
| **Image Config** | JSON describing rootfs assembly and runtime defaults | Stored as a blob |
| **Layer** | A tar (optionally gzip/zstd) of filesystem changes | Stored as a blob |
| **Descriptor** | A pointer: `{mediaType, digest, size, urls?, annotations?}` | Embedded inside manifests/indexes |
| **Media type** | A MIME-like string that tells you how to parse a blob | A field on every descriptor |

The **descriptor** is the universal pointer. Every reference from one object to another — index to manifest, manifest to config, manifest to layer — is a descriptor. A descriptor is the *only* thing that crosses the registry boundary as a pointer; everything else is content.

```
type Descriptor struct {
    MediaType   string            `json:"mediaType"`
    Digest      string            `json:"digest"`         // "sha256:abc..."
    Size        int64             `json:"size"`
    URLs        []string          `json:"urls,omitempty"`         // foreign-layer URLs
    Annotations map[string]string `json:"annotations,omitempty"`
    Platform    *Platform         `json:"platform,omitempty"`     // only in index
}
```

### 2.1 Canonical Media Types

The media type tells the client how to decode a blob. There are two parallel lineages — Docker and OCI — and they coexist on the wire because no one wants to break old clients.

| MediaType | Role | Lineage |
|---|---|---|
| `application/vnd.docker.distribution.manifest.list.v2+json` | Manifest list (multi-arch index) | Docker |
| `application/vnd.docker.distribution.manifest.v2+json` | Image manifest | Docker |
| `application/vnd.docker.container.image.v1+json` | Image config | Docker |
| `application/vnd.docker.image.rootfs.diff.tar.gzip` | Layer, tar+gzip | Docker |
| `application/vnd.docker.image.rootfs.foreign.diff.tar.gzip` | "Foreign" layer with URLs (Windows base layers) | Docker |
| `application/vnd.oci.image.index.v1+json` | Image index (multi-arch) | OCI |
| `application/vnd.oci.image.manifest.v1+json` | Image manifest | OCI |
| `application/vnd.oci.image.config.v1+json` | Image config | OCI |
| `application/vnd.oci.image.layer.v1.tar` | Layer, uncompressed tar | OCI |
| `application/vnd.oci.image.layer.v1.tar+gzip` | Layer, tar+gzip | OCI |
| `application/vnd.oci.image.layer.v1.tar+zstd` | Layer, tar+zstd | OCI (newer) |

Modern registries (Docker Hub, ECR, GHCR, Harbor, Quay) accept both. Tools like `buildkit` and `crane` produce OCI-flavored types by default since ~2023; older tools still emit Docker types. From the client's point of view, the protocol shape is identical; the parsing branch is on the media-type string.

### 2.2 The On-Wire Shape (a Real Trace)

Walk a real `docker manifest inspect`. Don't trust the docs — read the JSON.

```bash
# 1. Get the image index for nginx:1.27 (registry-1.docker.io)
$ docker manifest inspect --verbose nginx:1.27 | jq '.[] | .Descriptor'
```

A trimmed real-world response looks like this:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:7d4f9a...e1c2",
      "size": 1779,
      "platform": { "architecture": "amd64", "os": "linux" }
    },
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:a912b3...f0aa",
      "size": 1779,
      "platform": { "architecture": "arm64", "os": "linux", "variant": "v8" }
    },
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:0c1d2e...3a4b",
      "size": 567,
      "platform": { "architecture": "unknown", "os": "unknown" },
      "annotations": { "vnd.docker.reference.type": "attestation-manifest",
                       "vnd.docker.reference.digest": "sha256:7d4f9a...e1c2" }
    }
  ]
}
```

What each field means:

- **`schemaVersion: 2`** — Distinguishes the modern v2 image format from the obsolete v1 (which embedded layer JSON inline and is dead outside of legacy registries).
- **`mediaType`** — Identifies *this object* (the index). The Accept header on the request told the registry which flavors the client understood.
- **`manifests[]`** — Descriptors to each per-platform manifest. The registry never stores "an image"; it stores these blobs and resolves the tag to the index digest.
- **`manifests[].platform`** — How the client picks one. `architecture` + `os` + optional `variant` (e.g., `arm/v7`). The client matches against its node's `GOOS`/`GOARCH`. The mysterious `unknown/unknown` entry is a **referrer** attestation manifest (BuildKit provenance, since Docker 23/BuildKit 0.11) — not a runnable image, just metadata attached to the index. We'll come back to this in §20.

Pick the amd64 entry and fetch its manifest by digest:

```bash
$ TOKEN=$(curl -sSL "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/nginx:pull" | jq -r .token)
$ curl -sSL \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.oci.image.manifest.v1+json" \
    https://registry-1.docker.io/v2/library/nginx/manifests/sha256:7d4f9a...e1c2 | jq .
```

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config": {
    "mediaType": "application/vnd.oci.image.config.v1+json",
    "digest": "sha256:6fe1d4...9c30",
    "size": 7682
  },
  "layers": [
    { "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:af107e...0d1a", "size": 31417882 },
    { "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:5e1b5d...c733", "size": 24134 },
    { "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:a72ad8...0e44", "size": 626 },
    { "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:b6f0e1...2a98", "size": 956 }
  ]
}
```

And the config blob:

```bash
$ curl -sSL -H "Authorization: Bearer $TOKEN" \
    https://registry-1.docker.io/v2/library/nginx/blobs/sha256:6fe1d4...9c30 | jq .
```

```json
{
  "architecture": "amd64",
  "os": "linux",
  "created": "2024-09-19T20:35:27Z",
  "config": {
    "User": "",
    "Env": [
      "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
      "NGINX_VERSION=1.27.1",
      "NJS_VERSION=0.8.5"
    ],
    "Cmd": ["nginx", "-g", "daemon off;"],
    "WorkingDir": "",
    "Entrypoint": ["/docker-entrypoint.sh"],
    "Labels": { "maintainer": "NGINX Docker Maintainers <docker-maint@nginx.com>" },
    "StopSignal": "SIGQUIT",
    "ExposedPorts": { "80/tcp": {} }
  },
  "rootfs": {
    "type": "layers",
    "diff_ids": [
      "sha256:9853575bc4f95b... ",
      "sha256:e83f6b8a4...     ",
      "sha256:2c1ff453d...     ",
      "sha256:c33e1d1a8...     "
    ]
  },
  "history": [
    { "created": "2024-09-12T01:24:54Z",
      "created_by": "/bin/sh -c #(nop) ADD file:9c5... in / " },
    { "created": "2024-09-12T01:24:54Z",
      "created_by": "/bin/sh -c #(nop)  CMD [\"bash\"]",
      "empty_layer": true },
    { "created": "2024-09-19T20:35:00Z",
      "created_by": "RUN /bin/sh -c set -x; apt-get update; apt-get install -y nginx... ",
      "comment": "buildkit.dockerfile.v0" }
  ]
}
```

Every field of that config has a specific role, covered in §5 in detail. For now, observe the two-key pairing: `layers[]` in the manifest are *compressed* (`tar+gzip`), addressed by the sha256 of the compressed bytes. `rootfs.diff_ids[]` in the config are *uncompressed* (`tar`), addressed by the sha256 of the unpacked tarball bytes. These are two different hashes of two different byte streams of the same logical content. The duplication is on purpose: layer digests verify what came over the wire; diff_ids verify what got unpacked to disk and feed the image config hash (which is what cosign signs).

### 2.3 The Mental Model

```
       ┌────────────────────────────────────────────────────────────┐
       │  REGISTRY (HTTP server with two REST collections)          │
       │                                                            │
       │   /v2/<name>/manifests/<digest|tag>   (JSON, schema-aware) │
       │   /v2/<name>/blobs/<digest>           (opaque bytes)       │
       │                                                            │
       │   Tags are mutable pointers to manifest digests.           │
       │   Manifests reference other manifests (index→manifest)     │
       │     and blobs (manifest→config, manifest→layer).           │
       │   All content is sha256-addressed.                         │
       └────────────────────────────────────────────────────────────┘
```

Two collections, three relationships, one hash function. The whole image ecosystem is built on this.

---

## 3. Image Index: Multi-Architecture and Manifest Lists

A pod scheduled to a `linux/arm64` node must not pull the `linux/amd64` image. The image **index** (formerly "manifest list" in Docker parlance) is the multiplexer.

### 3.1 What the Index Contains

```
ImageIndex {
  schemaVersion: 2
  mediaType:     application/vnd.oci.image.index.v1+json
  manifests: [
    Descriptor {
      mediaType: application/vnd.oci.image.manifest.v1+json
      digest:    sha256:...
      size:      ...
      platform:  { architecture, os, variant?, os.version?, os.features? }
    },
    ...
  ]
  annotations?: { key: value, ... }
  subject?:     Descriptor     // OCI 1.1: referrer pointer
}
```

The client (containerd, docker, podman, crane) iterates `manifests[]` and selects the descriptor whose `platform` matches its runtime. If none matches, the pull fails with the famous `no matching manifest for linux/<arch> in the manifest list entries` error.

### 3.2 Platform Selection Rules

```
Containerd's default match (simplified):
  1. arch == GOARCH (e.g., "amd64", "arm64")
  2. os   == GOOS   (e.g., "linux")
  3. if arch == "arm", variant must match (v6/v7/v8)
  4. os.version compared if both set (Windows uses build numbers here)
  5. ties broken by index order
```

You can override this with `--platform=linux/arm64/v8` in docker/crane, or via containerd's `PlatformMatcher`. Pulling a non-native image is allowed (often used for cross-arch builds with QEMU user-mode emulation), but the runtime still has to be able to execute it.

### 3.3 Inspecting Multi-Arch with `crane`

`crane` (from `google/go-containerregistry`) is the most pleasant CLI for low-level OCI work. No daemon, just a Go binary.

```bash
$ crane manifest nginx:1.27 | jq '.manifests[] | {arch: .platform.architecture, os: .platform.os, digest, size}'
```

```
{ "arch": "amd64", "os": "linux", "digest": "sha256:7d4f...", "size": 1779 }
{ "arch": "arm64", "os": "linux", "digest": "sha256:a912...", "size": 1779 }
{ "arch": "arm",   "os": "linux", "digest": "sha256:1f2c...", "size": 1779 }
{ "arch": "386",   "os": "linux", "digest": "sha256:5b3a...", "size": 1779 }
{ "arch": "mips64le", "os": "linux", "digest": "sha256:c4d1...", "size": 1779 }
{ "arch": "ppc64le", "os": "linux", "digest": "sha256:d8e2...", "size": 1779 }
{ "arch": "s390x", "os": "linux", "digest": "sha256:9f1a...", "size": 1779 }
{ "arch": "unknown", "os": "unknown", "digest": "sha256:0c1d..." }  ← attestation
```

Six real platforms plus an attestation referrer. The "unknown" entries are not images you can run; they're how BuildKit attaches signed provenance and SBOMs to the index without breaking older clients (which simply skip platforms they can't match).

### 3.4 Building a Multi-Arch Image

The plumbing under `docker buildx`:

```bash
# Bootstrap a multi-arch builder
$ docker buildx create --use --name multibuilder
$ docker buildx inspect --bootstrap

# Build and push the index in one step
$ docker buildx build \
    --platform linux/amd64,linux/arm64,linux/arm/v7 \
    -t ghcr.io/me/myapp:1.0.0 \
    --push \
    .
```

Under the hood, BuildKit:
1. Spawns a build per platform (using QEMU emulation if the builder is single-arch, or a remote builder per arch).
2. Produces one manifest per platform.
3. Composes an index pointing at those manifests.
4. Pushes blobs (config + layers) and manifests, then the index, then atomically updates the tag.

The push order matters: a registry rejects a manifest whose referenced blobs are not yet uploaded, so blobs go first.

---

## 4. Image Manifest: The Pointer Object

A single-platform image manifest is the simplest interesting OCI document. It's a flat list of descriptors with no logic.

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config": {
    "mediaType": "application/vnd.oci.image.config.v1+json",
    "digest": "sha256:6fe1d4...9c30",
    "size": 7682
  },
  "layers": [
    {
      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:af107e...0d1a",
      "size": 31417882
    },
    {
      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:5e1b5d...c733",
      "size": 24134
    }
  ],
  "annotations": {
    "org.opencontainers.image.created": "2024-09-19T20:35:27Z",
    "org.opencontainers.image.source": "https://github.com/nginx/docker-nginx",
    "org.opencontainers.image.revision": "abc123",
    "org.opencontainers.image.version": "1.27.1"
  }
}
```

### 4.1 Field-by-Field

| Field | Required | Meaning |
|---|---|---|
| `schemaVersion` | yes | Always `2` for OCI/Docker v2; rejects v1 |
| `mediaType` | yes (OCI 1.0+) | The media type *of this manifest* |
| `config` | yes | Descriptor to the image config JSON blob |
| `layers[]` | yes | Ordered list of layer descriptors, **bottom-up** |
| `annotations` | no | Free-form key/value, by convention `org.opencontainers.image.*` |
| `subject` | no (OCI 1.1) | Referrer pointer to a "parent" manifest (used for attestations) |

### 4.2 Layer Order Matters

`layers[0]` is the **base** (e.g., the debian rootfs). `layers[N-1]` is the **top** (your final `COPY`). At unpack time the snapshotter applies them in that order. If you swap two layers, you get a different filesystem.

This ordering is also why `docker history` reads bottom-up: the first history entry corresponds to the first non-empty layer, etc.

### 4.3 The Manifest Digest

The manifest is just JSON. Its sha256 is computed over the **exact bytes** as serialized — including whitespace, key ordering, and trailing newlines. Re-serializing the parsed JSON does **not** reproduce the digest. The wire form is canonical; parsers must remember the raw bytes for digest verification.

This is why `crane manifest` prints raw JSON, and why every image tool internally treats the manifest as `(rawBytes, parsedDoc)` rather than a single document object. Treat the bytes as the source of truth.

---

## 5. Image Config: rootfs, History, and Runtime Defaults

The config is the smallest blob and the most important one for understanding the image semantically. It is the only place where the *runtime defaults* (entrypoint, env, exposed ports) live, and the only place where the *build history* lives. It's also what cosign signs by default.

### 5.1 Top-Level Shape

```json
{
  "architecture": "amd64",
  "os": "linux",
  "os.version": "",                       // Windows only
  "os.features": [],                      // rare
  "variant": "",                          // e.g., "v8" for arm64
  "created": "2024-09-19T20:35:27Z",
  "author": "",
  "config":   { ... runtime defaults ... },
  "rootfs":   { "type": "layers", "diff_ids": [ ... ] },
  "history":  [ ... ]
}
```

### 5.2 The Runtime Config Block

```json
"config": {
  "User":         "1000:1000",
  "ExposedPorts": {"8080/tcp": {}},
  "Env":          ["PATH=/usr/bin:/bin", "APP_HOME=/srv"],
  "Entrypoint":   ["/app/bin/server"],
  "Cmd":          ["--port=8080"],
  "Volumes":      {"/data": {}},
  "WorkingDir":   "/srv",
  "Labels":       {"org.opencontainers.image.title": "myapp"},
  "StopSignal":   "SIGTERM",
  "ArgsEscaped":  false,
  "OnBuild":      [],
  "Healthcheck":  { "Test": ["CMD", "curl", "-f", "http://localhost:8080/healthz"],
                    "Interval": 30000000000,    // nanoseconds
                    "Timeout":  5000000000,
                    "Retries":  3,
                    "StartPeriod": 10000000000 }
}
```

The fields you actually care about for Kubernetes:

| Field | Effective behavior in Kubernetes |
|---|---|
| `Entrypoint` | Becomes `pod.spec.containers[].command` if not overridden |
| `Cmd` | Becomes `pod.spec.containers[].args` if not overridden |
| `Env` | Merged with `pod.spec.containers[].env` (pod wins on conflict) |
| `WorkingDir` | Used unless `pod.spec.containers[].workingDir` is set |
| `User` | Default UID/GID **unless** SecurityContext overrides; Pod Security Admission may reject |
| `Labels` | Available as image metadata; do not flow to the container |
| `StopSignal` | Sent on graceful shutdown unless `pod.spec.containers[].lifecycle.preStop` overrides |
| `Healthcheck` | **Ignored by Kubernetes.** Kubernetes uses `livenessProbe`/`readinessProbe` instead |
| `ExposedPorts` | Metadata only; does not open ports. `pod.spec.containers[].ports` is informational too |
| `Volumes` | Largely ignored by Kubernetes; volume mounting is explicit |

The pod spec is the source of truth in Kubernetes. The image config supplies defaults for fields the pod omits. This is a frequent source of "why is the wrong command running" — someone changed the Dockerfile `ENTRYPOINT`, but the pod overrides `command`, so the change has no effect.

### 5.3 The rootfs Block

```json
"rootfs": {
  "type": "layers",
  "diff_ids": [
    "sha256:9853575bc4f95b3a4d83a6a234cf6b56...",
    "sha256:e83f6b8a4cabae18bba35ac5d7f6c1d3...",
    "sha256:2c1ff453dbf01b81bf4a64a2fee8e3...",
    "sha256:c33e1d1a8b8c0f6a18cb1e3e0a..."
  ]
}
```

The `diff_ids` are sha256 of **uncompressed** tar bytes — one per layer, in the same order as `manifest.layers[]`. The relationship between a layer's wire digest and its diff_id is:

```
layer.tar+gzip blob bytes ─sha256─► manifest.layers[i].digest
        │
        │ gunzip
        ▼
layer.tar bytes ──────────sha256─► config.rootfs.diff_ids[i]
```

Two hashes of two byte streams. The wire digest verifies what came over the network. The diff_id is what the image config commits to, and what the snapshotter compares after decompression. They must be derivable from each other or the image is corrupt.

The image's **chain ID** (an internal containerd concept) is computed as:

```
chain_id[0]   = diff_id[0]
chain_id[i]   = sha256("chain_id[i-1] diff_id[i]")
chain_id[N-1] = the "top" chain ID of the image
```

The chain ID is the key into containerd's snapshotter — that's how layer reuse works across images. Two images with identical first three layers share the same `chain_id[2]`, so they share the same on-disk snapshot for that prefix.

### 5.4 The history Block

```json
"history": [
  { "created":    "2024-09-12T01:24:54Z",
    "created_by": "/bin/sh -c #(nop) ADD file:9c5... in /",
    "comment":    "" },
  { "created":    "2024-09-12T01:24:54Z",
    "created_by": "/bin/sh -c #(nop)  CMD [\"bash\"]",
    "empty_layer": true },
  { "created":    "2024-09-19T20:35:00Z",
    "created_by": "RUN /bin/sh -c set -x; apt-get update; apt-get install -y nginx ...",
    "comment":    "buildkit.dockerfile.v0" },
  { "created":    "2024-09-19T20:35:20Z",
    "created_by": "COPY entrypoint.sh /docker-entrypoint.sh # buildkit",
    "empty_layer": false,
    "comment":    "buildkit.dockerfile.v0" }
]
```

One entry per Dockerfile instruction. Entries with `"empty_layer": true` correspond to Dockerfile instructions that don't change the filesystem (`CMD`, `ENV`, `LABEL`, `EXPOSE`, `WORKDIR`, `USER`). They are recorded for history but do not contribute a layer; they only mutate the config.

`docker history nginx:1.27` reconstructs this view:

```
IMAGE          CREATED        CREATED BY                                       SIZE
sha256:6fe1d4  2 weeks ago    COPY entrypoint.sh /docker-entrypoint.sh # b...  956B
<missing>      2 weeks ago    RUN /bin/sh -c set -x; apt-get update; apt-...  31.4MB
<missing>      4 weeks ago    /bin/sh -c #(nop)  CMD ["bash"]                  0B
<missing>      4 weeks ago    /bin/sh -c #(nop) ADD file:9c5... in /            74MB
```

The `<missing>` rows are not bugs; intermediate images aren't stored locally anymore (and don't exist remotely in OCI v2). The history is the only record of how the image was built.

### 5.5 Layer Count Limits

| Implementation | Limit |
|---|---|
| OCI spec | No formal limit |
| Docker v2 | 127 layers (historical AUFS limit) |
| containerd + OverlayFS | 128 lower dirs (`overlay`'s static limit; configurable via kernel patch) |
| containerd + native snapshotter | No practical limit |

Practical guidance: stay under ~25 layers. Excess layers blow up manifest size, slow down pulls (more HTTP requests), and stress the snapshotter. Multi-stage Dockerfiles + `RUN` chaining (`apt-get update && apt-get install && rm -rf /var/lib/apt/lists/*`) keep the count down.

### 5.6 The Config Digest Is What Gets Signed

When cosign signs an image, the canonical thing signed is the **manifest digest**, which in turn commits to the **config digest**, which in turn commits to the **diff_ids** and **history**. The signature transitively covers every byte of every layer plus the runtime defaults plus the build provenance.

Tag mutability cannot lie past a signature: `nginx:1.27` may point to a new manifest tomorrow, but the cosign signature was over `sha256:7d4f9a...e1c2`. If the verifier resolves the tag and gets a different digest, the signature won't match.

This is the core idea behind every supply-chain control in §18–§20. Signatures attach to *content*; tags are *names*. Mutable names cannot launder immutable content.

---

## 6. Layers as Deduplicated Tarballs

A layer is a tarball. Specifically: a tarball of the *filesystem changes* relative to the previous layer in the stack. Three properties define it:

1. **Tar format.** GNU/POSIX `pax` tar by default. Inside the tar are file entries with mode, uid/gid, mtime, size, and contents.
2. **Compression.** Almost always gzip (`tar+gzip`), increasingly zstd (`tar+zstd`). Both are *streaming* compressors; the registry serves the compressed bytes as a single blob.
3. **Content-addressed by sha256.** The manifest stores `sha256(compressed_bytes)`; the config stores `sha256(uncompressed_tar_bytes)` as the diff_id.

### 6.1 What's in a Layer

For an instruction like `RUN apt-get install -y nginx`, the layer's tar contains:

- New files: `/usr/sbin/nginx`, `/usr/share/nginx/...`, package metadata in `/var/lib/dpkg/...`
- Modified files (recorded as a full new copy, not a delta): updated `/etc/passwd` if a user was added
- Whiteouts and opaque-directory markers (see §7) representing deletions and replaced directories

There is no concept of a "patch" inside a layer. Each file appears in its entirety. This is why removing a file does not shrink the image: the lower layer still contains it, and a whiteout entry in the upper layer hides it but adds, not removes, bytes.

### 6.2 Layer Deduplication

Two images can share a layer because the layer is identified by its content hash. On the wire, the registry checks per-blob existence: `HEAD /v2/<name>/blobs/<digest>` returns 200 if the blob exists, and push clients use this to **mount** a known blob into a new repository instead of re-uploading:

```bash
# Cross-repository blob mount
$ curl -X POST -H "Authorization: Bearer $TOKEN" \
    "https://registry-1.docker.io/v2/library/myapp/blobs/uploads/?mount=sha256:af107e...0d1a&from=library/nginx"
# 201 Created  Location: /v2/library/myapp/blobs/sha256:af107e...0d1a
```

The registry only stores the blob once. Disk savings on large registries (Docker Hub, ECR, GHCR) come almost entirely from this property.

On the node, deduplication also happens. Containerd's content store keys by digest:

```
/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/af107e...0d1a   ← one copy
```

Every image whose manifest references that blob shares the file. Pulling 10 derived images of `python:3.12-slim` pulls one copy of the Debian base.

### 6.3 Layer Dedup Math

Suppose:
- 100 microservices, each ~120MB image
- 90MB base (debian + libs)
- 25MB language runtime
- ~5MB unique app code

Naive total: 12GB. Deduplicated: 90 + 25 + 100*5 = 615MB on disk. The ~20× savings is why container images aren't an absurd waste of storage. **This works only if all 100 services use the *exact same* base digest.** A semver drift across teams kills the dedup.

### 6.4 Reproducibility of Layer Hashes

Two builds of the same Dockerfile typically produce **different layer hashes** because tar embeds:

- File modification times (mtime)
- File creation order (affects tar entry order)
- Sometimes uid/gid mapping
- Embedded build timestamps in compiled binaries

Reproducible builds (Bazel rules_docker, ko, BuildKit with `SOURCE_DATE_EPOCH`, nix2container) normalize mtimes to a fixed epoch, sort entries, and pin tooling versions. The output: bit-identical layer blobs across machines and time. This is a prerequisite for SLSA Level 3 (§20) and for deterministic vulnerability scanning.

---

## 7. Whiteouts, Opaque Directories, and OverlayFS Materialization

The trickiest part of the layer model is **deletions**. The tar format has no "delete this file" entry. Instead, OCI borrowed AUFS's convention:

- **Whiteout file**: `.wh.<name>` (zero-byte file) at the path of the deleted entry. When the snapshotter encounters this, it removes the file from the unified view.
- **Opaque directory**: `.wh..wh..opq` (zero-byte file) inside a directory. Tells the snapshotter: "ignore everything from lower layers under this directory; only this layer's contents are visible."

### 7.1 Example: Removing a File

A Dockerfile:

```dockerfile
FROM debian:12
RUN rm /etc/motd
```

The layer's tar contains a single entry:

```
.wh.motd     0 bytes   (inside the etc/ directory)
```

When the snapshotter unpacks, it does **not** actually create `.wh.motd` on disk. Instead it:

- On OverlayFS: creates a "character device 0:0" at `etc/motd` in the upper dir (OverlayFS's native whiteout). Combined with the lower layer's regular `etc/motd`, the unified view shows no `motd`.

```
# In the upper dir of the OverlayFS mount:
$ ls -la /var/lib/containerd/.../upperdir/etc/motd
crw-r--r-- 1 root root 0, 0 ...   etc/motd   ← character device, whiteout marker
```

### 7.2 Example: Replacing a Directory

```dockerfile
FROM debian:12
RUN rm -rf /etc && mkdir /etc && echo "fresh" > /etc/hello
```

The layer needs to say: "ignore everything in /etc from below, here's the new /etc." Tar contents:

```
etc/                       directory
etc/.wh..wh..opq           0 bytes   ← opaque marker
etc/hello                  6 bytes
```

OverlayFS realizes this by setting `trusted.overlay.opaque="y"` as an xattr on `etc/` in the upper dir:

```
$ getfattr -n trusted.overlay.opaque /var/lib/containerd/.../upperdir/etc
trusted.overlay.opaque="y"
```

Now the unified view of `/etc` shows only `hello`, regardless of what was in lower layers.

### 7.3 OverlayFS Materialization

When kubelet asks containerd to start a container, containerd asks the snapshotter to *materialize* the rootfs. With the OverlayFS snapshotter:

```
┌────────────────────────────────────────────────────────────────┐
│  Per layer in image, in order (base first):                    │
│    1. Find content blob in content store                       │
│    2. Decompress (gunzip / zstd)                               │
│    3. Apply diff_apply: walk tar, copy entries into a fresh    │
│       upper dir, translate .wh.* to OverlayFS whiteouts         │
│    4. Commit snapshot with this layer's chain_id as key        │
│  Final step:                                                    │
│    Create an active (read-write) snapshot on top:               │
│      mount -t overlay -o \                                     │
│        lowerdir=<L_N-1>:<L_N-2>:...:<L_0>,                     │
│        upperdir=<active_upper>,                                │
│        workdir=<active_work>                                   │
│      overlay /run/containerd/io.containerd.runtime.v2.task/.../rootfs
└────────────────────────────────────────────────────────────────┘
```

The resulting `rootfs/` is what runc receives as `root.path` in `config.json`. It's a normal directory, but reads transparently traverse the layer stack, and writes go to the active upper dir.

```
[ container view of /etc/hello ]
  │
  ▼
overlay mount
  │
  ├── upperdir/etc/hello       ← container's local writes
  ├── lowerdir[N-1]/etc/hello  ← top layer
  ├── lowerdir[N-2]/etc/...    ← whiteout? opaque?
  ├── ...
  └── lowerdir[0]/etc/...      ← base layer
```

When the container is deleted, only the active upper dir is removed. Lower layers (the committed snapshots) stay, ready to be reused by the next container.

### 7.4 OverlayFS Lower-Dir Limits

OverlayFS in Linux has a static limit of **128 lower directories** in a single mount (was 500 in older kernels, lowered for memory reasons, raised again in some distros). This caps the number of layers in any single image: practical limit ~125.

When this limit is hit:
- `mount: /run/.../rootfs: too many levels of symbolic links` — misleading message; it's the overlay layer count
- containerd will refuse to create the snapshot

Workaround: rebuild the image with fewer layers (squash, multi-stage).

### 7.5 Other Snapshotters

OverlayFS is the default but not the only choice. Containerd's snapshotter plugin interface lets you swap it out:

| Snapshotter | Use case | Cost |
|---|---|---|
| `overlayfs` | Default Linux | Kernel module; fast; layer limit |
| `native` | Fallback (no OverlayFS) | Copies entire rootfs per container; slow |
| `btrfs` | Btrfs-based hosts | Subvolume snapshots; very fast |
| `zfs` | ZFS-based hosts | ZFS clones; fast; needs ZFS |
| `stargz` / `soci` | Lazy pulling | See §17 |
| `nydus` | Lazy + dedup | See §17 |
| `devmapper` | Block-device-based | LVM thin provisioning; advanced |

For 99% of clusters, the answer is OverlayFS. The rest of this chapter assumes it unless stated.

---

## 8. Content-Addressable Storage: Why Digests Matter

The single design choice that makes the OCI ecosystem coherent is **content addressing**: every blob's name is a hash of its bytes.

```
digest = sha256(bytes)
```

This is not a checksum (a verification *after* you know the name). It *is* the name. The hash function is part of the identifier: `sha256:af107e...` — clients that don't recognize `sha256` (or `sha512`, allowed but rarely used) reject the blob.

### 8.1 Properties That Fall Out

| Property | Consequence |
|---|---|
| **Immutability** | Mutating a blob would change its digest. Therefore a digest pins exact bytes forever. |
| **Verifiability** | Anyone holding a digest can verify the bytes they received match. Registry compromise can't substitute bytes silently. |
| **Free deduplication** | Two identical blobs have the same name. Storage backends transparently dedup. |
| **Cacheability** | A CDN, proxy, mirror, or laptop cache can serve any blob it has without coordination — the digest is the cache key. |
| **Signing surface** | Signatures attach to digests. You sign the root of a Merkle DAG and inherit coverage of everything reachable. |
| **No partial writes** | A pulled blob whose hash doesn't match is invalid; you cannot partially trust it. |

### 8.2 The Cost: Garbage Collection Is Harder

Mutable-name systems (LRU caches keyed by URL) can evict by name. Content-addressed systems must track *references* — which manifests still point at a blob — and only evict when refcount hits zero. This is why every registry has a "garbage collect" step (§13.5) and why containerd has a `LeaseManager` (an explicit GC anchor).

### 8.3 The Digest Format in Practice

```
sha256:af107ea1b3aab5b3f8c7c8e9c5e6b1c2d3a8e6c8f9a8b7c6d5e4f3a2b1c0d
└────┘└──────────────────────────────────────────────────────────┘
algo  64 lowercase hex chars (sha256 = 256 bits = 32 bytes = 64 hex)
```

Always lowercase, always hex, no truncation. `crane digest` returns it; `docker pull` accepts it.

```bash
$ crane digest nginx:1.27
sha256:7d4f9aae0c5e69a8c6d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2

$ docker pull nginx@sha256:7d4f9aae0c5e69a8c6d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2
```

The `@sha256:` suffix is the canonical way to refer to an image by content rather than by name.

---

## 9. Tags vs Digests: Mutable Names, Immutable Content

A **tag** is a string label that points at a manifest digest. The registry stores it as a small entry in its tag index:

```
library/nginx
├── tags
│   ├── 1.27         → sha256:7d4f9a...
│   ├── 1.27.1       → sha256:7d4f9a...
│   ├── stable       → sha256:7d4f9a...
│   ├── latest       → sha256:7d4f9a...
│   └── mainline     → sha256:7d4f9a...
└── manifests
    └── sha256:7d4f9a... → bytes
```

Multiple tags can point at the same digest. Tags can be moved (re-pointed) at any time, deleted, or recreated.

### 9.1 What Goes Wrong With Tag-Only References

**Drift.** `nginx:1.27` resolved to `sha256:7d4f9a...` on Monday and `sha256:c812e3...` on Friday because a CVE patch was published. Your CI test ran against the first; production pulled the second. A bug that exists only in production is now indistinguishable from a flaky test.

**Cache poisoning.** Your build cache says "we already have `nginx:1.27`", but the meaning of `nginx:1.27` has changed. The cache hit is silently stale.

**Rollback impossible.** "Roll back to last week's release" — but last week's tag points to today's content. Nothing on your cluster has a record of last week's bytes unless they happen to be cached.

**Signature meaningless.** You signed `nginx:1.27`. The tag moves. The same signature, applied to a new digest, is invalid — but if your verifier checks the tag name and not the digest, it might still let the new image through (depending on implementation).

**Latency for "is it new?".** Without a digest, you must HEAD the manifest every reconciliation to detect change. With a digest, the answer is in the spec.

### 9.2 Pinning by Digest in Production

```yaml
# pod-bad.yaml
spec:
  containers:
  - name: web
    image: nginx:1.27          # ← drifts
    imagePullPolicy: Always    # ← refetches every restart, hides drift

# pod-good.yaml
spec:
  containers:
  - name: web
    image: nginx@sha256:7d4f9a...e1c2    # ← exact bytes
    imagePullPolicy: IfNotPresent        # ← uses cached if present
```

The "good" pod is reproducible. Re-applying the same YAML six months later resolves to identical bytes. CI can record this digest at build time, propagate it through `kustomize edit set image`, and you have a deterministic deployment pipeline.

### 9.3 The Tooling

```bash
# Resolve a tag to a digest once, in CI:
$ DIGEST=$(crane digest nginx:1.27)
$ echo $DIGEST
sha256:7d4f9a...e1c2

# Patch a manifest to use the digest form:
$ kustomize edit set image nginx=nginx@$DIGEST
```

Several admission policies enforce "no tags": Kyverno's `disallow-image-tags` rule, OPA Gatekeeper's `K8sBlockImageTag` template, or a small CEL ValidatingAdmissionPolicy:

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: require-digest-pin
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      resources: ["pods"]
      operations: ["CREATE", "UPDATE"]
  validations:
  - expression: "object.spec.containers.all(c, c.image.contains('@sha256:'))"
    message: "Container image must be pinned by digest (@sha256:...)"
```

Pair with cosign verification (§19) and you have a baseline supply-chain admission gate.

### 9.4 `imagePullPolicy` Semantics

| `imagePullPolicy` | Behavior |
|---|---|
| `Always` | Pull (resolves manifest, downloads missing layers) on every pod start |
| `IfNotPresent` | Pull only if the image is not already on the node |
| `Never` | Never pull; fail if not present |

Default when the image tag is `:latest` or absent: `Always`. Default otherwise: `IfNotPresent`. Default when the image is referenced by digest: `IfNotPresent` (because the bytes can't change).

`Always` with a tag is the most common source of "production looks like staging looks like dev because everyone keeps repulling `latest`" and the corresponding "I redeployed and it changed without me changing the manifest" mystery. Tag drift + `Always` = nondeterminism.

---

## 10. The Image Config and Build History

We covered the config in §5; this section is about reading the **history** as a tool.

### 10.1 `docker history` Walkthrough

```bash
$ docker history nginx:1.27
IMAGE          CREATED        CREATED BY                                          SIZE     COMMENT
6fe1d4...9c30  2 weeks ago    COPY entrypoint.sh /docker-entrypoint.sh # buildkit  956B
<missing>      2 weeks ago    RUN /bin/sh -c set -x; apt-get update; apt-get ...   31.4MB
<missing>      4 weeks ago    /bin/sh -c #(nop)  CMD ["bash"]                       0B
<missing>      4 weeks ago    /bin/sh -c #(nop) ADD file:9c5... in /                74MB
```

What this tells you:
- **Layer count**: 3 layers contribute mass (74 + 31.4 + ~0); 1 layer is empty (CMD).
- **Build provenance**: The `RUN` step is a buildkit instruction. The base was added with `ADD`. You can correlate to the upstream Dockerfile.
- **Storage cost**: Each non-empty entry is a layer. Repeated `RUN` steps accumulate.

### 10.2 Reading History Critically

Things to look for in production images:

- `ADD file:... in /` early in history → that's the rootfs of the base image. Identify the distro.
- `COPY` of large directories → likely cache-busting; should be split out.
- `RUN apt-get install ... && ... && rm -rf /var/lib/apt/lists/*` → good practice, prevents bloat.
- `RUN curl ... | sh` → unsigned shell-piped install; supply-chain risk.
- `COPY --chown=... /tmp/secrets/...` → secrets baked in? (See §23.)
- `RUN groupadd -r app && useradd -r -g app app` followed by `USER app` → image runs non-root by default.

`docker history --no-trunc` shows the full command for each layer.

### 10.3 Computing the Provenance

You can deduce a lot from history but not everything. The `created_by` field is freeform text from the build tool; it's not a structured trace. For real provenance, you want SLSA attestations (§20), where the build system signs a statement about *how* the image was produced (source commit, builder identity, materials). History is the user-facing approximation.

### 10.4 The Relationship Between Dockerfile Instructions and Layers

| Dockerfile instruction | Produces a layer? | Effect on config |
|---|---|---|
| `FROM` | Inherits all base layers | Resets config to base's config |
| `RUN <cmd>` | Yes | None unless `RUN` modifies env/etc via shell wrapping |
| `COPY`, `ADD` | Yes | None |
| `CMD` | No | Sets `Cmd` |
| `ENTRYPOINT` | No | Sets `Entrypoint` |
| `ENV` | No | Adds to `Env` |
| `EXPOSE` | No | Adds to `ExposedPorts` |
| `WORKDIR` | No | Sets `WorkingDir`; creates dir if needed (still no layer in BuildKit) |
| `USER` | No | Sets `User` |
| `LABEL` | No | Adds to `Labels` |
| `VOLUME` | No | Adds to `Volumes` |
| `STOPSIGNAL` | No | Sets `StopSignal` |
| `HEALTHCHECK` | No | Sets `Healthcheck` (Kubernetes ignores) |
| `ARG` | No | Build-time only; not in image |
| `ONBUILD` | No | Adds to `OnBuild` |
| `SHELL` | No | Changes how subsequent `RUN` is parsed |

The Dockerfile is essentially a script that produces (layers, config) pairs. BuildKit doesn't quite work this way internally — it builds a DAG and prunes — but the output conforms to this model.

---

## 11. The OCI Distribution Spec v2 Registry API

A registry is an HTTP server with two collections (blobs and manifests) and a tiny endpoint of metadata. The spec is the OCI Distribution Specification (`opencontainers/distribution-spec`), originally derived from Docker's Registry HTTP API V2.

### 11.1 Endpoint Map

| Method | Path | Purpose |
|---|---|---|
| GET | `/v2/` | API version probe; also where AuthN challenge happens |
| GET | `/v2/_catalog?n=N&last=last` | List repository names (optional, often disabled) |
| GET | `/v2/<name>/tags/list?n=N&last=last` | List tags in a repository |
| HEAD | `/v2/<name>/manifests/<reference>` | Check manifest exists; returns digest in `Docker-Content-Digest` header |
| GET | `/v2/<name>/manifests/<reference>` | Get manifest body |
| PUT | `/v2/<name>/manifests/<reference>` | Upload a manifest (or update a tag) |
| DELETE | `/v2/<name>/manifests/<reference>` | Delete a tag or manifest |
| HEAD | `/v2/<name>/blobs/<digest>` | Check blob exists |
| GET | `/v2/<name>/blobs/<digest>` | Download a blob (supports Range) |
| POST | `/v2/<name>/blobs/uploads/` | Initiate a blob upload (monolithic or chunked) |
| PATCH | `/v2/<name>/blobs/uploads/<uuid>` | Stream chunk |
| PUT | `/v2/<name>/blobs/uploads/<uuid>?digest=...` | Finalize the upload |
| POST | `/v2/<name>/blobs/uploads/?mount=<digest>&from=<repo>` | Cross-repo mount |
| DELETE | `/v2/<name>/blobs/<digest>` | Delete a blob (often disabled in production) |
| GET | `/v2/<name>/referrers/<digest>` | OCI 1.1: list referrers to a manifest (attestations, signatures) |

`<reference>` is either a tag (e.g., `1.27`) or a digest (`sha256:7d4f9a...`). `<name>` is the repository, typically `<namespace>/<repo>` or just `<repo>` (Docker Hub's `library/` prefix is implicit for official images).

### 11.2 API Version Probe

Every interaction starts with a probe to confirm the server speaks v2:

```bash
$ curl -i https://registry-1.docker.io/v2/
HTTP/1.1 401 Unauthorized
Www-Authenticate: Bearer realm="https://auth.docker.io/token",service="registry.docker.io"
Docker-Distribution-Api-Version: registry/2.0
```

Two things to notice:
1. `Docker-Distribution-Api-Version: registry/2.0` confirms v2.
2. The 401 with `Www-Authenticate: Bearer ...` is the **AuthN challenge** — the client now knows where to get a token. This is the token dance (§12).

For an authenticated/public-anonymous server, you'd get:
```
HTTP/1.1 200 OK
Docker-Distribution-Api-Version: registry/2.0
```

### 11.3 Manifest GET (Real Trace)

```bash
$ TOKEN=$(curl -sSL "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/nginx:pull" | jq -r .token)

$ curl -sSL -i \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Accept: application/vnd.oci.image.index.v1+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    https://registry-1.docker.io/v2/library/nginx/manifests/1.27
HTTP/1.1 200 OK
Content-Type: application/vnd.oci.image.index.v1+json
Docker-Content-Digest: sha256:aaaa...
Etag: "sha256:aaaa..."
Content-Length: 2353

{ "schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", ... }
```

The `Accept` header is **load-bearing**. The registry uses it to decide whether to serve you an index (multi-arch) or a single-platform manifest, and which media-type lineage (OCI vs Docker). If you omit the OCI accepts on an OCI-native registry, you may get a 404 because there is no Docker-flavored manifest to return.

`Docker-Content-Digest` is how the client learns the digest of the response without recomputing it. The client *should* still recompute and compare to catch tampering or bugs — but many do not.

### 11.4 Blob GET (Real Trace)

```bash
$ curl -sSL -i -H "Authorization: Bearer $TOKEN" \
    https://registry-1.docker.io/v2/library/nginx/blobs/sha256:6fe1d4...9c30
HTTP/1.1 307 Temporary Redirect
Location: https://production.cloudflare.docker.com/registry-v2/docker/registry/v2/blobs/sha256/6f/6fe1d4.../data?...

# Follow redirect:
HTTP/1.1 200 OK
Content-Type: application/octet-stream
Content-Length: 7682
Docker-Content-Digest: sha256:6fe1d4...9c30

{ ...json bytes... }
```

The 307 to a CDN is typical for large registries. The actual blob bytes come from edge storage, not the registry's API endpoint. ECR, GCR, and ACR use signed URLs to S3/GCS/Azure Blob with short expirations.

Critical: clients must follow redirects but **also re-verify** the digest of the bytes they actually received. The CDN URL is not authenticated as the registry; only the bytes are.

### 11.5 Blob Upload: Monolithic and Chunked

To push a blob, you first POST to obtain an upload UUID:

```bash
$ curl -i -X POST -H "Authorization: Bearer $TOKEN" \
    https://myregistry.io/v2/me/myapp/blobs/uploads/
HTTP/1.1 202 Accepted
Location: /v2/me/myapp/blobs/uploads/abc123-uuid
Range: 0-0
Docker-Upload-Uuid: abc123-uuid
```

Then either:

**Monolithic** (small blobs, single PUT):

```bash
$ curl -X PUT -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @layer.tar.gz \
    "https://myregistry.io/v2/me/myapp/blobs/uploads/abc123-uuid?digest=sha256:af107e..."
HTTP/1.1 201 Created
Location: /v2/me/myapp/blobs/sha256:af107e...
Docker-Content-Digest: sha256:af107e...
```

**Chunked** (large blobs, multiple PATCH then PUT):

```bash
# Send chunk 1 (bytes 0–9999999)
$ curl -X PATCH -H "Authorization: Bearer $TOKEN" \
    -H "Content-Range: 0-9999999" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @chunk1.bin \
    "https://myregistry.io/v2/me/myapp/blobs/uploads/abc123-uuid"
# 202 Accepted, Range: 0-9999999

# Send chunk 2 (bytes 10000000–19999999)
$ curl -X PATCH ... --data-binary @chunk2.bin ...
# 202 Accepted, Range: 0-19999999

# Finalize
$ curl -X PUT -H "Authorization: Bearer $TOKEN" \
    "https://myregistry.io/v2/me/myapp/blobs/uploads/abc123-uuid?digest=sha256:af107e..."
# 201 Created
```

Modern clients (BuildKit, crane) almost always use monolithic with HTTP/2 streaming; chunked exists for HTTP/1.1 proxies that buffer requests.

### 11.6 Manifest PUT

After all blobs are uploaded, push the manifest:

```bash
$ curl -X PUT -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/vnd.oci.image.manifest.v1+json" \
    --data-binary @manifest.json \
    "https://myregistry.io/v2/me/myapp/manifests/1.0.0"
HTTP/1.1 201 Created
Location: /v2/me/myapp/manifests/sha256:bbbb...
Docker-Content-Digest: sha256:bbbb...
```

The registry validates that every referenced blob exists (HEAD checks internally). If any blob is missing, you get a 400 with `MANIFEST_BLOB_UNKNOWN`.

### 11.7 OCI Referrers API

OCI 1.1 added a way to attach metadata to an existing manifest (think: signatures, SBOMs, attestations). A referrer is a manifest with a `subject` field pointing at another manifest:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.dev.sigstore.bundle.v0.3+json",
  "config": { "mediaType": "application/vnd.oci.empty.v1+json", "digest": "sha256:...", "size": 2 },
  "layers": [ { "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json", "digest": "sha256:...", "size": 1234 } ],
  "subject": { "mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:7d4f9a...", "size": 1779 }
}
```

And clients query:

```bash
$ curl -H "Authorization: Bearer $TOKEN" \
    https://myregistry.io/v2/me/myapp/referrers/sha256:7d4f9a...
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    { "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:eeee...",
      "size": 567,
      "artifactType": "application/vnd.dev.sigstore.bundle.v0.3+json" },
    { "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:ffff...",
      "size": 412,
      "artifactType": "application/spdx+json" }
  ]
}
```

This is how cosign signatures, SBOMs, and SLSA attestations attach to images in OCI 1.1+. Older registries (pre-1.1) emulate the same shape with a separate tag scheme (`sha256-XXX.sig`, `sha256-XXX.sbom`), which is what `cosign sign` falls back to.

---

## 12. Registry Authentication: The Token Dance

The OCI spec uses **bearer-token AuthN** with a challenge-response flow that piggybacks on RFC 6750.

### 12.1 The Flow

```
                ┌──────────┐                                     ┌──────────┐
                │  client  │                                     │ registry │
                └────┬─────┘                                     └────┬─────┘
                     │                                                 │
                     │  ── GET /v2/foo/manifests/1.0 (no auth) ─────► │
                     │                                                 │
                     │  ◄── 401 Unauthorized ────────────────────────  │
                     │      Www-Authenticate: Bearer                   │
                     │        realm="https://auth.example.com/token",  │
                     │        service="registry.example.com",          │
                     │        scope="repository:foo:pull"              │
                     │                                                 │
                     │                                                 │
       ┌─────────────▼────────────┐                                    │
       │                          │                                    │
       │  ── GET auth.example.com/token?              ─────────┐       │
       │       service=registry.example.com                    │       │
       │       &scope=repository:foo:pull                      ▼       │
       │       Authorization: Basic <user:pass>          ┌──────────┐  │
       │                                                  │   auth   │  │
       │                                                  │  server  │  │
       │  ◄── 200 OK ──────────────────────────────────── │          │  │
       │      { "token": "eyJh...", "expires_in": 300, … }│          │  │
       │                                                  └──────────┘  │
       │                          │                                    │
       └─────────────┬────────────┘                                    │
                     │                                                 │
                     │  ── GET /v2/foo/manifests/1.0                  │
                     │       Authorization: Bearer eyJh... ─────────►  │
                     │                                                 │
                     │  ◄── 200 OK + manifest body ──────────────────  │
```

1. Anonymous request → 401 with `Www-Authenticate: Bearer realm=... service=... scope=...`
2. Client extracts the realm URL, the service, and the scope.
3. Client POSTs/GETs the realm with Basic auth (or no auth for anonymous tokens) and the requested scope.
4. Auth server returns a JWT (typically) bearer token.
5. Client retries the original request with `Authorization: Bearer <token>`.

The token is **scoped** — it grants exactly the requested operations (`pull`, `push`, `*`) on the requested repository. A token for `repository:foo:pull` cannot push to `foo` or read `bar`.

### 12.2 Scope Strings

```
scope = "repository:" <name> ":" <action_list>
action_list = action ("," action)*
action = "pull" | "push" | "delete" | "*"

# Examples
repository:library/nginx:pull
repository:me/myapp:pull,push
repository:me/myapp:*
```

Multiple scopes may appear (multiple `scope=` query params on the token request) for cross-repo operations like blob mount.

### 12.3 Token Lifetimes

| Registry | Default lifetime |
|---|---|
| Docker Hub | 5 minutes |
| GHCR | 1 hour |
| ECR | 12 hours |
| GCR | 60 minutes |
| Harbor | configurable (default 30m) |

Long-running pulls (large layers, slow networks) can outlive a token. Clients are expected to retry with a fresh token on 401, but some old clients don't, which manifests as truncated layer downloads on slow connections.

### 12.4 Anonymous Access

For public images, the realm accepts no credentials:

```bash
$ curl "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/nginx:pull"
{"token": "eyJh...", "access_token":"eyJh...", "expires_in":300, "issued_at":"..."}
```

The same token endpoint, no Basic auth header, public scope.

### 12.5 Per-Cloud Variants

The bearer-token pattern is universal, but how credentials are obtained varies:

- **Docker Hub**: username + password (or PAT) → Basic auth on token endpoint.
- **GHCR**: GitHub PAT or `GITHUB_TOKEN` (CI) → Basic auth.
- **ECR**: AWS Sigv4-signed call to `ecr:GetAuthorizationToken` returns a base64'd `AWS:<token>` pair, used as Basic auth on `xxxxx.dkr.ecr.<region>.amazonaws.com/v2/`. Token valid 12 hours.
- **GCR/Artifact Registry**: Google OAuth2 access token; `Bearer <oauth_token>` directly (no challenge needed).
- **ACR**: AAD token exchanged at `<registry>.azurecr.io/oauth2/exchange` for a registry-scoped token.

Kubernetes wraps all of this in **kubelet credential providers** (§14).

---

## 13. Registry Implementations and Storage Backends

### 13.1 The Field

| Registry | Owner | Backend | Notable features |
|---|---|---|---|
| **distribution/distribution** | CNCF (formerly Docker) | filesystem, S3, GCS, Azure | The reference implementation; what's inside `registry:2` |
| **Harbor** | CNCF | distribution-based | Replication, scanning (Trivy), signing, RBAC, Helm/OCI |
| **Quay** | Red Hat | Postgres + S3 | Robot accounts, scanning (Clair), repository mirroring |
| **ECR / ECR Public** | AWS | S3 (managed) | IAM integration, replication, pull-through cache |
| **GCR / Artifact Registry** | Google | GCS (managed) | IAM, Vulnerability Scanning, Workload Identity |
| **ACR** | Azure | Blob (managed) | AAD, geo-replication, content trust |
| **GHCR** | GitHub | (managed) | GitHub Actions integration, free for public |
| **Zot** | (community, OCI-conformant) | filesystem, S3 | Lightweight, OCI-only, lazy-pull support |
| **JFrog Artifactory** | JFrog | (proprietary) | Multi-protocol (Docker + npm + Maven + …) |
| **Nexus Repository** | Sonatype | (proprietary) | Same |

For self-hosting, Harbor is the default choice in most enterprise Kubernetes installs; Zot is gaining ground for being smaller and stricter about OCI conformance.

### 13.2 Storage Backends

A registry is mostly a thin API in front of a key-value store keyed by digest. The backend is interchangeable:

```
distribution config (~/distribution.yml)
storage:
  s3:
    region: us-east-1
    bucket: my-registry-bucket
    rootdirectory: /registry
    encrypt: true
    keyid: <kms-key-arn>
    accesskey: ...
    secretkey: ...
  delete:
    enabled: true
  cache:
    blobdescriptor: redis
```

Backends:

- **Filesystem**: A directory layout (`/var/lib/registry/docker/registry/v2/blobs/sha256/...`). Fine for single-node; doesn't scale horizontally.
- **S3 / GCS / Azure Blob**: The standard production choice. Multi-AZ durability built-in; arbitrary scale.
- **OSS / Swift**: Other cloud object stores.
- **inmemory**: For testing.

Throughput is bounded by the object store, not the registry — modern registries are nearly stateless and trivial to horizontally scale behind a load balancer.

### 13.3 Pull-Through Caches

A pull-through cache (PTC) is a registry that proxies to an upstream and caches blobs. Common patterns:

- **In-cluster PTC** for Docker Hub: reduces rate-limit pressure (Docker Hub limits anonymous pulls to 100/6h per IP). Configure containerd's `registry mirrors` to use the in-cluster PTC.
- **ECR pull-through cache** (a managed AWS feature): proxies Docker Hub, ECR Public, quay.io, k8s.gcr.io into a private ECR repo.
- **Harbor proxy cache projects**: same idea, Harbor-native.

Containerd config (`/etc/containerd/config.toml`):

```toml
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"

# /etc/containerd/certs.d/docker.io/hosts.toml:
server = "https://registry-1.docker.io"

[host."https://my-ptc.internal"]
  capabilities = ["pull", "resolve"]
```

### 13.4 Garbage Collection on the Registry

Content-addressed storage requires a sweep to remove unreferenced blobs:

```
1. Set the registry into read-only mode (or accept races)
2. Walk all manifests, build the set of referenced blob digests
3. Walk the blob store, delete any blob not in the set
```

distribution/distribution's GC:

```bash
$ registry garbage-collect /etc/distribution.yml
```

This is **expensive** at scale (millions of blobs, S3 LIST throughput limits) and is typically run nightly or weekly. Production registries (Harbor, ECR) automate it.

GC is also the only way to actually reclaim space after a `DELETE /manifests`. Deleting a manifest only removes the tag/manifest record; the layer blobs remain until the next GC sweep.

### 13.5 Replication and Mirroring

Registries replicate via the same v2 API: a replication agent pulls from one and pushes to another. Harbor and Quay ship replication built-in. ECR has cross-region replication as a managed feature. Network egress costs are usually the practical limit, not the API.

---

## 14. Authentication Patterns in Kubernetes

Kubernetes nodes need to authenticate to registries to pull non-public images. There are four mechanisms, in increasing order of "modern."

### 14.1 The `docker config.json` File

The oldest pattern: a JSON file with base64-encoded credentials, mounted as a Secret.

```json
{
  "auths": {
    "myregistry.io": {
      "auth": "dXNlcjpwYXNz"   // base64("user:pass")
    },
    "ghcr.io": {
      "auth": "..."
    }
  }
}
```

Stored as a Secret of type `kubernetes.io/dockerconfigjson`:

```bash
$ kubectl create secret docker-registry myregcred \
    --docker-server=myregistry.io \
    --docker-username=user \
    --docker-password=pass \
    --docker-email=- \
    -n myteam
```

Then referenced from the pod:

```yaml
spec:
  imagePullSecrets:
  - name: myregcred
  containers:
  - name: app
    image: myregistry.io/myteam/app:1.0.0
```

Or attached to the **ServiceAccount** so every pod using that SA inherits it:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: default
  namespace: myteam
imagePullSecrets:
- name: myregcred
```

**Problems**: secrets are stored in etcd (base64, not encrypted at rest by default — fix with KMS encryption config), they're per-namespace (operational burden at scale), and credentials are long-lived static passwords. Compromised secret = registry compromise.

### 14.2 Node-Level Docker Config

Kubelet also reads `/var/lib/kubelet/config.json` (and historical `~/.docker/config.json` on the host). Credentials placed there are used for any pull on the node, without an `imagePullSecret`. Used heavily by VM image builders. Same drawbacks.

### 14.3 Kubelet Credential Providers (v1 Plugin API)

The modern answer. Instead of static secrets, kubelet invokes an **external binary** at pull time to get a short-lived credential.

```
kubelet                          credential provider binary
   │                                 (e.g., ecr-credential-provider)
   │  exec stdin: CredentialProviderRequest
   │             { kind: ..., apiVersion: ..., image: "..." }
   ├────────────────────────────────────►
   │                                 (talks to cloud metadata API,
   │                                  signs with instance role, etc.)
   │  exec stdout: CredentialProviderResponse
   │             { auth: { username, password }, expirationDuration, ... }
   ◄────────────────────────────────────┤
   │
   │  uses credential to pull
   ▼
```

Configured on the kubelet:

```yaml
# /etc/kubernetes/credential-providers/config.yaml
apiVersion: kubelet.config.k8s.io/v1
kind: CredentialProviderConfig
providers:
- name: ecr-credential-provider
  matchImages:
  - "*.dkr.ecr.*.amazonaws.com"
  - "*.dkr.ecr.*.amazonaws.com.cn"
  defaultCacheDuration: "12h"
  apiVersion: credentialprovider.kubelet.k8s.io/v1
- name: gcr-credential-provider
  matchImages:
  - "gcr.io"
  - "*.gcr.io"
  - "*.pkg.dev"
  defaultCacheDuration: "30m"
  apiVersion: credentialprovider.kubelet.k8s.io/v1
- name: acr-credential-provider
  matchImages:
  - "*.azurecr.io"
  defaultCacheDuration: "1h"
  apiVersion: credentialprovider.kubelet.k8s.io/v1
  args:
  - /etc/kubernetes/azure.json
```

```
# kubelet flags
--image-credential-provider-config=/etc/kubernetes/credential-providers/config.yaml
--image-credential-provider-bin-dir=/etc/kubernetes/credential-providers/bin
```

What changes:

- No long-lived secret in etcd.
- The credential is **node-scoped**, sourced from the node's cloud identity (instance role / managed identity / GKE service account).
- Tokens are short-lived (cached for `defaultCacheDuration`).

### 14.4 Workload Identity (IRSA / GKE WI / Azure WI)

For ECR specifically, the cleanest pattern combines kubelet credential providers (for node-level access) with **IRSA-style workload identity** (for pod-level access to other AWS services). For *image pulls* specifically, you usually want node-level — the kubelet, not the pod, does the pull.

But if you want the pull credential scoped to the pod's identity (rare but useful for shared multi-tenant nodes), you can:

- Configure the credential provider to consult the pod's projected SA token before calling the cloud API.
- Use **kubelet identity federation** via `aws sts assume-role-with-web-identity` for each pod.

In practice, almost all clusters use one node IAM role with `ecr:GetAuthorizationToken`, and that's fine because the registry credential is read-only and short-lived.

### 14.5 Pulling From Multiple Registries

A pod can reference images from N registries:

```yaml
spec:
  containers:
  - name: a
    image: 1234.dkr.ecr.us-east-1.amazonaws.com/team/a:v1     # ECR via cred provider
  - name: b
    image: ghcr.io/myorg/b@sha256:...                          # GHCR via imagePullSecret
  - name: c
    image: docker.io/library/redis:7                           # public Docker Hub
  imagePullSecrets:
  - name: ghcr-cred
```

Kubelet iterates registries: tries cred providers (by `matchImages` glob), then per-pod `imagePullSecrets`, then SA `imagePullSecrets`, then node-level `~/.docker/config.json`. First success wins.

### 14.6 The Operational Pain Points

- **`imagePullSecrets` are namespaced.** You must create the same secret in every namespace. Operators like `imagepullsecret-patcher` automate this; it's a real ergonomic gap.
- **Rotation.** Static passwords expire eventually; rotating across N namespaces × M secrets is painful. Credential providers solve this by being stateless.
- **Anonymous Docker Hub rate limits.** A node behind a NAT with no creds may hit 100 pulls / 6h. Solution: log in with even a free account (4× the rate), or use a PTC mirror.
- **Pull credential vs runtime credential.** Image pull is done by the **kubelet** with the kubelet's identity. Runtime AWS calls from inside the container use the **pod's** IRSA SA. These are two separate things; conflating them is a common bug.

---

## 15. Pulling: What Actually Happens on the Wire

End-to-end, from `kubectl apply` to a container running.

### 15.1 The Sequence

```
[apiserver]   Pod object stored, spec.nodeName=node-2
   │
   ▼  watch event
[kubelet on node-2]
   syncLoop sees pod
   │
   ▼
[image manager]
   For each container.image:
     1. Apply imagePullPolicy:
          if Never:       require local; fail if missing
          if IfNotPresent: check local content store first
          if Always:      always re-resolve manifest
     2. Resolve reference:
          a. Look up registry credentials (cred providers + imagePullSecrets)
          b. Probe /v2/ (handle 401 → token dance)
          c. HEAD /v2/<name>/manifests/<tag-or-digest>
                 ← Docker-Content-Digest header gives the canonical digest
          d. If digest already in content store → DONE (warm cache)
          e. Otherwise: GET /v2/<name>/manifests/<digest>
                 ← parse media type, dispatch:
                     - if image index: pick platform → GET inner manifest
                     - if image manifest: continue
     3. GET config blob:
          GET /v2/<name>/blobs/<config-digest>
          Verify sha256 of bytes matches descriptor.digest
          Store in content store
     4. For each layer not already in content store (in parallel):
          GET /v2/<name>/blobs/<layer-digest>
            (follow CDN 307s)
          Verify sha256 of bytes matches descriptor.digest
          Stream into content store + snapshotter unpack
            (concurrent gzip-decompress + tar-extract + diff-apply)
     5. Snapshotter materializes the chain:
          For each layer L, in order:
            chain_id = sha256(prev_chain_id + diff_id_of_L)
            Commit snapshot keyed by chain_id (idempotent, dedup'd)
          Active snapshot on top: overlay mount with N lowerdirs
     6. Image pulled. PullImage CRI call returns.
   │
   ▼
[runtime] CreateContainer with rootfs path = active snapshot mount
[runtime] StartContainer
```

### 15.2 Critical Optimizations

Several things happen in parallel that look sequential above:

- Layer downloads are parallel (containerd default: up to 3 simultaneously per image, configurable). On NVMe-backed nodes with 10 Gb networks, parallelism is a big win for fat images.
- Decompression + unpack overlap with download (streaming).
- HEAD on manifest is cheap (a few KB); a node restart can verify "do I still have this?" in milliseconds.

### 15.3 Latency Math

A medium image, cold pull:

```
Pod scheduled        T+0 ms
Resolve cred         T+5 ms   (cred provider exec)
Token dance          T+50 ms  (HTTPS RTT × 2 + auth server processing)
HEAD manifest        T+80 ms
GET index            T+105 ms (~3 KB)
GET manifest         T+130 ms (~2 KB)
GET config           T+160 ms (~10 KB)
GET layers (parallel)
  L1 (90 MB, gzip)   downloads at link speed minus protocol overhead
                     @ 1 Gbps link, 90 MB = ~720 ms wire + unpack
  L2 (25 MB)         ~200 ms wire + unpack
  L3 (5 MB)          ~40 ms wire + unpack
  L4 (1 MB)          ~8 ms
  parallel ⇒ ~720 ms total dominated by L1
Snapshot commit      T+900 ms
PullImage returns    T+910 ms
```

Warm pull (image already on node):

```
HEAD manifest        T+80 ms   (or skipped entirely under IfNotPresent + digest reference)
Snapshot already exists → instant
PullImage returns    T+85 ms
```

A pinned-by-digest, IfNotPresent pull skips the network entirely:

```
PullImage returns    T+2 ms    (local content store lookup, snapshot already committed)
```

The order-of-magnitude difference between cold and warm explains why **image locality scoring in the scheduler** (a Filter+Score plugin) materially reduces tail latency: scheduling a pod to a node that already has its image gives ~900ms back per pod.

### 15.4 Failure Modes During Pull

| Symptom | Likely cause |
|---|---|
| `ErrImagePull` then retry | Transient network / 5xx |
| `ImagePullBackOff` (sustained) | 401, 403, 404, or invalid digest |
| `manifest unknown` | Wrong tag/digest, or tag deleted from registry |
| `unauthorized: authentication required` | Missing or expired credential; cred provider misconfigured |
| `no matching manifest for linux/amd64 in the manifest list entries` | Multi-arch index doesn't have your platform |
| `failed to copy: read tcp ... use of closed network connection` | Truncated layer download; token expired mid-download |
| `failed to verify layer ... digest mismatch` | Corruption or registry bug (rare); content-store recovery needed |

`kubectl describe pod` shows the kubelet's pull events; `journalctl -u containerd -f` shows the containerd-side detail; `crictl pull <image>` reproduces a pull on the node, bypassing kubelet logic.

### 15.5 Image Locality and DaemonSet Pre-Pulling

To eliminate cold-pull latency at scale, two patterns:

**Scheduler image locality.** The scheduler's `ImageLocality` plugin scores nodes that already have the pod's images higher. Default-on, fine for batch workloads.

**Pre-pull DaemonSet.** A DaemonSet whose only job is to pull a set of images on every node:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata: { name: image-prepuller }
spec:
  selector: { matchLabels: { app: prepuller } }
  template:
    metadata: { labels: { app: prepuller } }
    spec:
      initContainers:
      - name: prepull-app-v1-2-3
        image: myreg.io/app@sha256:abcd...
        command: ["/bin/true"]
      - name: prepull-sidecar-v0-5
        image: myreg.io/sidecar@sha256:efgh...
        command: ["/bin/true"]
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.9
```

The init containers force a pull of each image (the entrypoint `true` exits immediately); the main `pause` keeps the pod alive for kubelet's bookkeeping. On the next workload pod that needs those images, the pull is warm.

Karpenter and some other provisioners do this automatically.

---

## 16. Image Garbage Collection on the Node

Without GC, every pull would consume disk forever. Kubelet runs an **image GC loop** governed by two thresholds.

### 16.1 Kubelet Image GC Thresholds

```yaml
# /var/lib/kubelet/config.yaml
imageGCHighThresholdPercent: 85     # default
imageGCLowThresholdPercent: 80      # default
imageMinimumGCAge: 2m0s             # default
imageMaximumGCAge: 0s               # default (0 = disabled, never force GC by age alone)
```

| Knob | Meaning |
|---|---|
| `imageGCHighThresholdPercent` | When image-filesystem usage exceeds this, kubelet starts deleting images |
| `imageGCLowThresholdPercent` | Stop deleting once usage drops below this |
| `imageMinimumGCAge` | Don't delete images touched more recently than this |
| `imageMaximumGCAge` | If set, force delete images older than this regardless of pressure (1.32+) |

### 16.2 Eligibility and Order

A pulled image is eligible for GC if:
1. It is **not currently used** by any container (running or not, on this node).
2. Its last-used age exceeds `imageMinimumGCAge`.
3. It is not in the **image pinning** list (see §16.4).

Order of deletion: **least-recently-used first**, by image. Within a single image, all layers are deleted as a group (since the image is the unit of usage tracking, not the layer).

Note: layers shared by other still-resident images are NOT deleted, because the content store ref-counts blobs. Deleting image A only deletes blobs unique to A.

### 16.3 Image-Filesystem Disk Pressure

If disk pressure on the imagefs becomes severe, kubelet also fires **node-pressure eviction** to remove pods, freeing more space. The thresholds (configurable) are:

```yaml
evictionHard:
  imagefs.available: "15%"
  imagefs.inodesFree: "5%"
evictionSoft:
  imagefs.available: "20%"
  imagefs.inodesFree: "10%"
evictionSoftGracePeriod:
  imagefs.available: "2m"
  imagefs.inodesFree: "2m"
```

Pods with `BestEffort` QoS are evicted first; then `Burstable` exceeding requests; then `Guaranteed`. (Details in ch 21.)

### 16.4 Image Pinning

Kubelet can be told to never GC certain images regardless of LRU:

```yaml
# kubelet config
imageGCHighThresholdPercent: 85
# (no first-class "pin" field; pinning is via CRI)
```

CRI-level pinning: containerd supports `pinned` images via labels (`io.cri-containerd.pinned=pinned`), and kubelet sets the pause image and the kubelet's static-pod images as pinned by default. This is why `registry.k8s.io/pause:3.9` is never GC'd.

For your own critical images (e.g., a logging agent everyone depends on), use `crictl pull --pin-image=true` or set the label via containerd's CRI config:

```toml
[plugins."io.containerd.grpc.v1.cri".image_decryption]
  ...
[plugins."io.containerd.grpc.v1.cri"]
  pinned_images = ["registry.k8s.io/pause:3.9", "myreg.io/logger:latest"]
```

### 16.5 Observing Image GC

```bash
# What's on the node:
$ crictl images
IMAGE                          TAG       IMAGE ID       SIZE
nginx                          1.27      6fe1d4...      72MB
registry.k8s.io/pause          3.9       e6f181...      300kB
myreg.io/myapp                 v1.2.3    aabbcc...      130MB
...

# Force GC for testing:
$ crictl rmi --prune        # delete all unused images
$ crictl rmi <image-id>     # delete one

# Watch kubelet's GC:
$ journalctl -u kubelet -f | grep -i 'image gc'
"Image garbage collection succeeded" usagePercent=86 → 79
```

---

## 17. Lazy and Streaming Pulls: estargz, SOCI, zstd:chunked, Nydus

For multi-GB images (think: ML/AI inference containers with 4 GB of CUDA libraries), the cold-pull penalty becomes brutal — 30–90 seconds before the container can start. Lazy pulling techniques amortize this cost.

The core idea: **start the container before the image is fully pulled**, and fetch layer contents on demand as files are accessed.

### 17.1 The Problem

```
Traditional pull (4 GB image, 1 Gbps link):
  Download    [============= 32 seconds ============]
  Decompress              [==== 8 seconds ====]
  Container start                                   [run]
  Total time to first byte of work: ~40 seconds

Lazy pull:
  Container start (using FUSE / overlayfs + on-demand fetch)
                  [run, paging in files as needed ...]
  Total time to first byte of work: ~1 second
```

The trade-off: cold first-touch of any file inside the container blocks on a network fetch. For workloads with locality (read 50 files at startup, ignore the other 50,000) this is a win; for workloads that touch the whole image fast (a backup tool that tars everything), it's a wash or loss.

### 17.2 estargz (extended seekable gzip)

Original lazy-pull format, from Google's `stargz-snapshotter`. An estargz layer is a **regular gzip tar with extra index metadata** that lets a reader seek to any file without scanning the whole archive.

Key properties:
- Backward compatible: an estargz layer is a valid `tar+gzip`, readable by any OCI client.
- Extra: a TOC (table of contents) at the end maps `file path → offset` in the archive.
- The snapshotter sets up a FUSE filesystem; opening a file triggers an HTTP Range request to the registry, fetching just the bytes for that file.

```bash
# Convert an image to estargz with stargz-snapshotter's CLI:
$ ctr-remote image convert --estargz myreg.io/app:v1 myreg.io/app:v1-esgz

# Push:
$ ctr-remote image push myreg.io/app:v1-esgz
```

Containerd with the stargz plugin recognizes the layer media type (`application/vnd.oci.image.layer.v1.tar+gzip+esgz` annotation) and uses the snapshotter:

```toml
# containerd config
[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "stargz"

[proxy_plugins.stargz]
  type = "snapshot"
  address = "/run/containerd-stargz-grpc/containerd-stargz-grpc.sock"
```

### 17.3 SOCI (Seekable OCI)

AWS's variant. Instead of modifying the image layer format, SOCI stores a **separate index artifact** in the registry that points into the original (unmodified) layer:

```
Original layer:    sha256:af107e... (regular tar+gzip)
SOCI index:        sha256:eeee...   (a separate artifact, referrers-linked to the manifest)
```

The advantage: the original image stays bit-identical, signatures still verify. The SOCI index is pulled by SOCI-aware snapshotters; non-SOCI clients pull the layer normally.

```bash
# Build the index:
$ soci create myreg.io/app@sha256:bbbb...

# Push the index alongside the image:
$ soci push --image myreg.io/app@sha256:bbbb...
```

ECR has native SOCI support for select regions, and the `soci-snapshotter` containerd plugin is the client.

### 17.4 zstd:chunked

A newer format pushed by Red Hat for podman/CRI-O. Replaces gzip with **zstd** compression, and structures the layer in **independently decompressible chunks** with an index — same idea as estargz, different codec.

Why zstd:
- 2–4× faster decompression than gzip.
- Better compression ratios at comparable speeds.
- Random-access friendly via "skippable frames" in the zstd format.

Layer media type: `application/vnd.oci.image.layer.v1.tar+zstd` with an annotation indicating chunked structure.

```bash
$ podman push --compression-format zstd:chunked myreg.io/app:v1
```

### 17.5 Nydus

Alibaba's format. More aggressive: a Nydus image is a **rewritten** image with deduplication at the chunk level (sub-file). Multiple images can share chunks of files, not just whole layers.

Architecture:
- A `nydus-image` tool converts standard OCI images to Nydus images.
- The `nydusd` user-space daemon serves the rootfs over FUSE.
- Chunks are fetched on demand from the registry.
- Local cache by chunk digest.

Format components:
- **Bootstrap**: a metadata blob describing the filesystem tree, inode-by-inode.
- **Chunks**: 4 MB blocks containing actual file data, deduplicated across files and layers.

```bash
$ nydusify convert --source myreg.io/app:v1 --target myreg.io/app:v1-nydus
```

Used widely at Alibaba and Ant Group for sub-second cold starts of multi-GB images.

### 17.6 Measured Cold-Start Improvements

Industry benchmarks (representative numbers; depend on workload):

| Image | Standard pull | estargz / SOCI | Nydus |
|---|---|---|---|
| 500 MB Python app, accesses 50 files at start | 12 s | 2 s | 1 s |
| 4 GB CUDA inference container | 50 s | 8 s | 3 s |
| 8 GB Java enterprise app | 90 s | 18 s | 5 s |

10–100× improvements at the extremes. **The catch**: every file access during steady-state may incur a network fetch (mitigated by client-side caching). For latency-sensitive or repeatedly-restarted workloads, lazy pull is a clear win.

### 17.7 When Not to Use Lazy Pull

- **Air-gapped environments** where the registry might be unreachable mid-run. A lazy-pull container that needs to fetch a chunk after the registry has gone away will crash on file access.
- **Workloads that immediately read the entire image**. No benefit, plus indexing overhead.
- **Strong signing requirements** where you signed the whole image and the lazy-pull format requires a separate signed artifact (SOCI mitigates this; estargz needs care).

---

## 18. Supply Chain: Sigstore and Image Signing

A signature over an image digest proves: "this entity, at this time, attested that this image is theirs / safe / approved." Pre-Sigstore, image signing was a swamp of GPG keys (Docker Content Trust / Notary v1), key management was nightmarish, and adoption was near zero.

Sigstore changed three things:
1. **Keyless signing**: short-lived certificates issued from an OIDC identity, not long-lived keypairs.
2. **Transparency log**: every signature is recorded in a public append-only log (Rekor), so issuance fraud is detectable.
3. **Convention**: signatures, attestations, and SBOMs are stored as OCI artifacts in the same registry as the image.

### 18.1 Components

| Component | Role |
|---|---|
| **cosign** | The client: signs and verifies |
| **Fulcio** | A CA that issues short-lived (10-minute) certificates bound to an OIDC identity |
| **Rekor** | An append-only transparency log of every signature, queryable by digest or identity |
| **OIDC provider** | GitHub Actions, Google, your IDP — provides the identity Fulcio binds the cert to |
| **Registry** | Stores the signature artifact alongside the image |

### 18.2 The Keyless Signing Flow

```
                    ┌─────────────────────────────────────────┐
                    │ cosign sign ghcr.io/me/app@sha256:bbbb..│
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ 1. Generate ephemeral key pair (in memory)               │
       │      priv_k, pub_k                                       │
       └──────────────────────────────┬───────────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ 2. Obtain OIDC token from configured provider            │
       │    (GitHub Actions: from $ACTIONS_ID_TOKEN_REQUEST_URL)  │
       │    (local: opens a browser, OAuth dance)                 │
       │    token contains: sub=user@host or workflow ref, aud=…  │
       └──────────────────────────────┬───────────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ 3. POST Fulcio with (CSR signed by priv_k, OIDC token)   │
       │    Fulcio:                                               │
       │      - verifies OIDC token signature                     │
       │      - extracts identity claim                            │
       │      - issues X.509 cert with identity in SAN,           │
       │        valid for ~10 minutes                              │
       │      - returns cert + chain                              │
       └──────────────────────────────┬───────────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ 4. Compute payload = canonical JSON of image digest      │
       │    Sign payload with priv_k → signature                  │
       └──────────────────────────────┬───────────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ 5. POST Rekor with                                       │
       │    { payload, signature, public_key (cert) }             │
       │    Rekor:                                                │
       │      - verifies signature against cert                   │
       │      - appends entry to Merkle log                       │
       │      - returns a Signed Entry Timestamp (SET)            │
       └──────────────────────────────┬───────────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ 6. Bundle = { signature, cert, rekor_set }               │
       │    Push to registry as an OCI artifact attached to       │
       │    the original manifest via referrer or                 │
       │    by tag scheme (sha256-bbbb.sig)                       │
       │    Discard priv_k (never persisted to disk)              │
       └──────────────────────────────────────────────────────────┘
```

The brilliance: the **private key never persists**. It exists for the duration of one signing operation, then is gone. Compromising a developer laptop a week later steals nothing useful, because there's no key sitting around. The transparency log means a fraudulent issuance (Fulcio compromised, attacker gets a cert claiming to be you) is publicly detectable — your monitoring sees a Rekor entry for an image you didn't sign.

### 18.3 cosign in Practice

```bash
# Sign (keyless, OIDC):
$ cosign sign --yes ghcr.io/me/app@sha256:bbbb...
Generating ephemeral keys...
Retrieving signed certificate...
Note that there may be a short delay before the transparency log entry is propagated...
Successfully verified SCT...
Recording log entry...
tlog entry created with index: 12345678
Pushing signature to: ghcr.io/me/app

# Verify (certificate identity-based):
$ cosign verify ghcr.io/me/app@sha256:bbbb... \
    --certificate-identity-regexp '^https://github\.com/myorg/.*' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
Verification for ghcr.io/me/app@sha256:bbbb... --
The following checks were performed on each of these signatures:
  - The cosign claims were validated
  - Existence of the claims in the transparency log was verified offline
  - The code-signing certificate was verified using trusted certificate authority certificates
[{"critical":{"identity":...,"image":{"docker-manifest-digest":"sha256:bbbb..."}, ...}]
```

The verifier specifies *who* should have signed (`--certificate-identity-regexp` matches the OIDC subject) and *which issuer* (must be a trusted Fulcio or a private one). That tuple — identity pattern + issuer — is your trust policy.

### 18.4 The Signature Artifact

A cosign signature is itself a small OCI manifest, attached either via the OCI 1.1 referrer API or via a tag scheme:

```
Original image:   ghcr.io/me/app@sha256:bbbb...
Signature (legacy tag scheme):
                  ghcr.io/me/app:sha256-bbbb.sig
Signature (OCI 1.1 referrer):
                  GET /v2/me/app/referrers/sha256:bbbb
```

The signature artifact's layer is the actual signature payload (signature bytes + cert + Rekor SET). The "config" is empty.

### 18.5 Keyed Signing (When Keyless Isn't Available)

You can still sign with a long-lived key:

```bash
$ cosign generate-key-pair       # writes cosign.key (encrypted) and cosign.pub
$ cosign sign --key cosign.key ghcr.io/me/app@sha256:bbbb...
$ cosign verify --key cosign.pub ghcr.io/me/app@sha256:bbbb...
```

This is the right choice for fully air-gapped environments or when you have a KMS-backed key (`--key awskms://...`). The downside: key management costs return. The Rekor log is still optional (and useful) for keyed signatures too.

---

## 19. Verifying Signatures at Admission

A signature in the registry that no one verifies is theater. The admission boundary is where verification has to happen.

### 19.1 The Architecture

```
┌──────────────────┐
│ kubectl apply    │   pod with image ghcr.io/me/app:v1
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│ apiserver:                                                       │
│   AuthN → AuthZ → MutatingAdmission                              │
│   ValidatingAdmission ──┐                                        │
│                          │                                       │
│                          ▼                                       │
│            ┌─────────────────────────────┐                       │
│            │ policy-controller webhook   │                       │
│            │ (sigstore/policy-controller) │                       │
│            │                             │                       │
│            │ for each image in pod:      │                       │
│            │   resolve tag → digest      │                       │
│            │   cosign.Verify(            │                       │
│            │     image,                  │                       │
│            │     CertIdentity, Issuer)   │                       │
│            │   if fail: deny             │                       │
│            └─────────────────────────────┘                       │
└──────────────────────────────────────────────────────────────────┘
```

### 19.2 sigstore/policy-controller

The simplest in-cluster admission verifier:

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: require-cosign-from-myorg
spec:
  images:
  - glob: "ghcr.io/myorg/**"
  - glob: "**.dkr.ecr.us-east-1.amazonaws.com/myorg/**"
  authorities:
  - keyless:
      url: https://fulcio.sigstore.dev
      identities:
      - issuer: https://token.actions.githubusercontent.com
        subjectRegExp: "^https://github\\.com/myorg/[^/]+/\\.github/workflows/release\\.yml@refs/.*"
      ctlog:
        url: https://rekor.sigstore.dev
```

Any pod referencing an image matching the `images.glob` patterns must have a cosign signature whose certificate identity matches the regex and whose issuer is the trusted one. The webhook also resolves tags to digests and **mutates the pod spec** to pin by digest, so the image that runs is the one that was verified — closing a TOCTOU gap.

### 19.3 Kyverno

Kyverno can do the same thing with more general policy:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-images
spec:
  validationFailureAction: Enforce
  webhookTimeoutSeconds: 30
  rules:
  - name: verify-myorg-images
    match:
      any:
      - resources: { kinds: ["Pod"] }
    verifyImages:
    - imageReferences:
      - "ghcr.io/myorg/*"
      attestors:
      - entries:
        - keyless:
            issuer: "https://token.actions.githubusercontent.com"
            subject: "https://github.com/myorg/*"
            rekor: { url: "https://rekor.sigstore.dev" }
      mutateDigest: true
      required: true
```

### 19.4 Connaisseur

Older, lightweight, focused only on image signing (no general policy). Used in some Notary-era deployments; cosign-compatible.

### 19.5 The Failure Mode That Matters

`failurePolicy: Fail` (deny when the webhook is down) is correct for production: a wedged signature verifier should block deploys, not silently let unsigned images in. But it means **the webhook is now a critical path of every pod create**, so you must:

- Run the webhook with HA (≥3 replicas).
- Set tight `webhookTimeoutSeconds` (10–30s).
- Cache verification results by digest (policy-controller does this, TTL-based).
- Monitor: webhook errors are an outage, not an alert.

For initial rollouts, run in `Warn` mode (Kyverno) or `Audit` mode (policy-controller) to detect violations before enforcing.

### 19.6 The TOCTOU Trap

```
T+0   pod admitted, image tag resolved to digest A, signature for A verified
T+1   pod actually pulled — registry returns digest B (tag moved)
T+2   container of digest B runs, unverified
```

This is real. Mitigations:

- **Mutate the pod to pin by digest** at admission, so kubelet pulls exactly the verified bytes. Both policy-controller and Kyverno do this when `mutateDigest: true`.
- Enforce a separate policy that rejects pods without `@sha256:` digests after the verification webhook has run.

---

## 20. Provenance, SBOMs, and SLSA

Signing an image proves identity. **Attestations** prove statements *about* the image: how it was built, what's inside it, whether known vulnerabilities apply. The umbrella standard is **in-toto**; the umbrella maturity model is **SLSA**.

### 20.1 in-toto Attestations

An in-toto attestation is a signed JSON document:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    { "name": "ghcr.io/me/app", "digest": { "sha256": "bbbb..." } }
  ],
  "predicateType": "https://slsa.dev/provenance/v1",
  "predicate": {
    "buildDefinition": {
      "buildType": "https://actions.github.io/buildtypes/workflow/v1",
      "externalParameters": {
        "workflow": {
          "ref": "refs/heads/main",
          "repository": "https://github.com/myorg/app",
          "path": ".github/workflows/release.yml"
        }
      },
      ...
    },
    "runDetails": {
      "builder": {
        "id": "https://github.com/actions/runner/...",
        "version": { "runner": "2.317.0" }
      },
      "metadata": {
        "invocationId": "12345/abcdef",
        "startedOn": "2024-09-19T20:35:00Z",
        "finishedOn": "2024-09-19T20:36:42Z"
      }
    }
  }
}
```

The `subject` is the image digest. The `predicate` is a structured statement about it. The whole document is wrapped in a **DSSE envelope** (Dead Simple Signing Envelope) and signed (typically with Sigstore keyless).

Attestation is stored in the registry as an OCI artifact, referrer-linked to the image. cosign attaches and verifies:

```bash
# Attach SLSA provenance to an image:
$ cosign attest --predicate provenance.json \
                --type slsaprovenance \
                ghcr.io/me/app@sha256:bbbb...

# Verify it:
$ cosign verify-attestation \
    --type slsaprovenance \
    --certificate-identity-regexp '^https://github\.com/myorg/' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/me/app@sha256:bbbb...
```

### 20.2 SLSA Levels

SLSA (Supply-chain Levels for Software Artifacts) ranks build pipelines from L1 to L4:

| Level | Provenance | Builder | Materials | Reproducible |
|---|---|---|---|---|
| **L1** | Available (any format) | Any | Documented | No requirement |
| **L2** | Authenticated (signed) | Hosted | Tamper-resistant | No requirement |
| **L3** | Non-falsifiable (isolated build) | Isolated, hardened | Per-build ephemeral | Strong recommendation |
| **L4** (deprecated → "Build L3 + Source") | All above + hermetic, two-party review | Same | Same | Required |

In practice today:
- **L1**: include a Dockerfile in your repo. (Trivial.)
- **L2**: sign your image and SLSA provenance with a hosted CI's identity (GitHub Actions, BuildKit). (Easy.)
- **L3**: build in an isolated, ephemeral environment with hardened-runners (GitHub Actions reusable workflows, Tekton with isolation). (Realistic for many orgs.)
- **L4**: hermetic builds, two-party review of every change. (Aspirational outside Google.)

The SLSA spec separates "Build" levels from "Source" levels (the new SLSA v1.0); legacy materials sometimes still cite the old combined L1–L4. Use the v1.0 framing.

### 20.3 SBOMs: SPDX and CycloneDX

A Software Bill of Materials enumerates the components inside an image. Two competing formats:

- **SPDX** (Linux Foundation): broader, more complete schema; ISO/IEC 5962.
- **CycloneDX** (OWASP): leaner, focused on security use cases; richer vulnerability fields.

Both are JSON (or XML, etc.). Both can express:
- All installed packages (debian, alpine, language ecosystems).
- File checksums.
- Licenses.
- Relationships (dependsOn, isContainedIn).

Generation:

```bash
# Syft (Anchore): generates SBOMs from images
$ syft ghcr.io/me/app@sha256:bbbb... -o spdx-json > app.spdx.json
$ syft ghcr.io/me/app@sha256:bbbb... -o cyclonedx-json > app.cdx.json

# Trivy: SBOMs + scanning
$ trivy image --format spdx-json --output app.spdx.json ghcr.io/me/app:v1
```

Attach the SBOM to the image with cosign:

```bash
$ cosign attest --predicate app.spdx.json --type spdx ghcr.io/me/app@sha256:bbbb...
```

Now the image is paired with a signed SBOM in the registry. Vulnerability scanners (Trivy, Grype, Snyk Container, Clair) consume the SBOM and a vulnerability database; consumers can verify the SBOM independently.

### 20.4 VEX: Vulnerability Exploitability eXchange

An SBOM lists components. A vulnerability scanner cross-references components against CVEs. **VEX** is a separate signed document that says, per CVE: "this CVE applies / does not apply / has been mitigated in this image."

```json
{
  "@context": "https://openvex.dev/ns/v0.2.0",
  "@id": "https://example.com/vex/myapp-2024-09-19",
  "author": "security@example.com",
  "timestamp": "2024-09-19T20:35:00Z",
  "statements": [
    {
      "vulnerability": { "name": "CVE-2024-12345" },
      "products": [ { "@id": "pkg:oci/app?repository_url=ghcr.io/me/app&digest=sha256:bbbb..." } ],
      "status": "not_affected",
      "justification": "vulnerable_code_not_in_execute_path"
    }
  ]
}
```

VEX dramatically reduces "vulnerability noise" in mature pipelines: the scanner flags 50 CVEs, VEX explains that 45 are inert, leaving 5 actual decisions.

### 20.5 The End-to-End Picture

```
                            ┌──────────────────────────┐
                            │  GitHub Actions runner   │
                            │  (or Tekton, BuildKit)   │
                            └──────────┬───────────────┘
                                       │
                              build & test
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │  Container image         │
                            │  ghcr.io/me/app:v1       │
                            │  → sha256:bbbb...        │
                            └──────────┬───────────────┘
                                       │
                                       │
                       ┌───────────────┼───────────────┐
                       │               │               │
                       ▼               ▼               ▼
            ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
            │  cosign      │  │  syft        │  │  GH Actions  │
            │  sign        │  │  → SBOM      │  │  → SLSA      │
            │              │  │              │  │  provenance  │
            └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
                   │                  │                  │
                   ▼                  ▼                  ▼
            ┌────────────────────────────────────────────────────┐
            │  Registry: image + signature + SBOM + provenance   │
            │  All linked via OCI referrers to sha256:bbbb...     │
            └────────────────────────┬───────────────────────────┘
                                     │
                                     ▼
                            ┌──────────────────────────┐
                            │  Admission webhook       │
                            │  (policy-controller)     │
                            │                          │
                            │  Verify signature        │
                            │  Verify provenance       │
                            │  Check SBOM against      │
                            │    VEX, vuln DB          │
                            │  Allow / Deny pod        │
                            └──────────────────────────┘
```

Each artifact is independently fetchable, independently verifiable, independently signed. The image stays bit-identical regardless of how many attestations attach.

---

## 21. OCI Artifacts: Storing Non-Image Content

OCI 1.1 generalized the registry from "container images" to **any content addressable artifact**. The format: a manifest with an `artifactType` field and arbitrary layers.

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.cncf.helm.chart.v1.tar+gzip",
  "config": {
    "mediaType": "application/vnd.cncf.helm.config.v1+json",
    "digest": "sha256:...",
    "size": 320
  },
  "layers": [
    {
      "mediaType": "application/vnd.cncf.helm.chart.v1.tar+gzip",
      "digest": "sha256:...",
      "size": 12345
    }
  ]
}
```

This isn't a runnable image; it's a Helm chart. The registry stores it the same way it stores nginx. Tools that understand Helm pull this manifest, fetch the layer, and use it as a chart.

### 21.1 Things Stored as OCI Artifacts

| Content | artifactType | Tool |
|---|---|---|
| Helm chart | `application/vnd.cncf.helm.chart.v1.tar+gzip` | `helm push` |
| OPA bundle | `application/vnd.cncf.openpolicyagent.policy.layer.v1+rego` | `opa` |
| WASM module | `application/vnd.wasm.content.layer.v1+wasm` | `wasm-to-oci`, `spin` |
| SBOM | `application/spdx+json` or `application/vnd.cyclonedx+json` | `cosign attest` |
| Signature | `application/vnd.dev.cosign.simplesigning.v1+json` | `cosign sign` |
| SLSA provenance | `application/vnd.in-toto+json` | `cosign attest` |
| AI model weights | `application/vnd.ollama.image.config.v1+json` (varies) | `ollama push` |
| Backup tarballs | (any) | custom tools, `oras` |

### 21.2 ORAS: The Generic OCI Push/Pull Client

`oras` (OCI Registry As Storage) is the CLI for arbitrary artifacts:

```bash
# Push any files as an OCI artifact:
$ oras push myreg.io/me/mybundle:v1 \
    --artifact-type application/vnd.example.bundle.v1+tar \
    bundle.tar:application/vnd.example.layer.v1+tar

# Pull:
$ oras pull myreg.io/me/mybundle:v1

# Attach an attestation to an existing artifact:
$ oras attach myreg.io/me/app:v1 \
    --artifact-type application/vnd.example.attestation.v1+json \
    attestation.json:application/json
```

This makes the registry a general-purpose artifact distribution channel. Many CI systems use ORAS to ship build outputs without setting up a separate artifact store.

### 21.3 Why This Matters

A single registry, with a single auth surface and a single content-addressed store, can hold your container images **and** your Helm charts **and** your policies **and** your SBOMs **and** your signed provenance. Operational simplicity at scale: one credential, one URL, one set of permissions, one GC policy.

The cost: tools need to know which media types to look for. A Docker client pulling `myreg.io/me/mybundle:v1` will fail in a baffling way because the manifest has no config blob it can interpret.

---

## 22. Image Best Practices

A checklist for production-grade images, with the rationale for each.

### 22.1 Use a Minimal Base

| Base | Size | When to use |
|---|---|---|
| `scratch` | 0 B | Static Go/Rust binaries; nothing else fits |
| `distroless/static` | ~2 MB | Static binaries that need TLS roots, tz data |
| `distroless/base` | ~20 MB | Dynamic binaries needing libc |
| `distroless/python3-debian12` | ~50 MB | Python apps |
| `alpine:3.20` | ~5 MB | Anything; musl-based, has shell |
| `debian:12-slim` | ~75 MB | Full Linux, glibc |
| `ubuntu:24.04` | ~80 MB | Full Linux, popular |

Distroless is the right default for compiled languages: no shell, no package manager, no `/bin/sh`. Smaller attack surface (no `nc`, no `wget`, no `curl` for an attacker to abuse), smaller image size, fewer CVEs in the base.

### 22.2 Use Multi-Stage Builds

```dockerfile
# Stage 1: build
FROM golang:1.23 AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/server ./cmd/server

# Stage 2: runtime
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /out/server /server
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/server"]
```

Properties:
- The build toolchain (Go SDK, ~800 MB) does not end up in the final image.
- The final image is the static binary + minimal rootfs (~5 MB).
- `nonroot` tag pins UID 65532, no shell.

### 22.3 Run as Non-Root

Even with namespaces and seccomp, processes run as root inside the container by default. **Bake in a non-root UID:**

```dockerfile
# Debian-based
RUN groupadd -r app --gid 10001 && useradd -r -g app --uid 10001 app
USER 10001:10001

# Alpine
RUN addgroup -g 10001 app && adduser -D -u 10001 -G app app
USER 10001:10001

# Distroless: already includes nonroot user (65532)
```

Pair with a pod-level SecurityContext that enforces it:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile: { type: RuntimeDefault }
  containers:
  - name: app
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: { drop: ["ALL"] }
```

`runAsNonRoot: true` causes kubelet to refuse the container if the image's User is `0`. Defense in depth.

### 22.4 Minimize Layers (Within Reason)

```dockerfile
# Bad: every RUN creates a layer; the rm at the end doesn't shrink anything
RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y vim
RUN rm -rf /var/lib/apt/lists/*

# Good: one layer, cleanup in same RUN
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl vim \
    && rm -rf /var/lib/apt/lists/*
```

But don't go nuts: collapsing every COPY and RUN into one giant step kills the build cache. Aim for ~5–15 layers, with the most cache-stable steps first (deps before app code).

### 22.5 .dockerignore Religiously

`.dockerignore` is the build-context filter. Without it, `COPY . .` includes `.git/`, `node_modules/`, build outputs, secrets — bloating the image and risking secret leaks.

```
# .dockerignore
.git
.github
node_modules
target
build
dist
*.log
.env
.env.*
secrets/
**/*.tmp
```

The build context is uploaded to the daemon (or BuildKit) — even files that aren't COPYed cost build time and disk to transfer.

### 22.6 Reproducible Builds

For high-assurance pipelines (SLSA L3+), reproducibility matters:

- Pin the base image by digest, not tag.
- Pin every package version (`apt-get install -y nginx=1.27.1-1~debian12u1`).
- Pin language runtimes (`golang:1.23.2-bookworm`).
- Use `SOURCE_DATE_EPOCH` to normalize timestamps.
- Use BuildKit's `--output type=docker,dest=image.tar` to capture exact bytes.

### 22.7 Avoid `:latest`

```dockerfile
FROM debian:latest        # ← bad
FROM debian:12            # ← OK
FROM debian:12-slim       # ← better
FROM debian@sha256:abc... # ← best
```

Same logic applies on the Kubernetes side: a pod referencing `app:latest` triggers `imagePullPolicy: Always` by default and is non-deterministic across restarts.

### 22.8 Label Everything

```dockerfile
LABEL org.opencontainers.image.title="myapp"
LABEL org.opencontainers.image.version="1.2.3"
LABEL org.opencontainers.image.source="https://github.com/myorg/app"
LABEL org.opencontainers.image.revision="abc1234"
LABEL org.opencontainers.image.created="2024-09-19T20:35:00Z"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="team@example.com"
```

These labels surface in registries, SBOM tools, scanners, and the `docker inspect` output. They are also the human-readable connection between an image digest and its source commit.

### 22.9 No Secrets in Layers

Anything `ADD`ed or `COPY`ed into an image is there **forever**, in that layer, even if a later `RUN rm` deletes it. Whiteouts hide; they don't delete. To pass a secret to a build:

```dockerfile
# BuildKit secret mount (does not bake into layer)
# syntax=docker/dockerfile:1.7
FROM alpine
RUN --mount=type=secret,id=npm_token,target=/root/.npmrc \
    npm install
```

```bash
$ DOCKER_BUILDKIT=1 docker build \
    --secret id=npm_token,src=$HOME/.npmrc \
    -t myapp:v1 .
```

The `--mount=type=secret` makes the file available only during that RUN; it does not become part of any layer.

### 22.10 Health and Readiness Concerns

Don't bake `HEALTHCHECK` into the Dockerfile expecting Kubernetes to use it; Kubernetes ignores it. Define probes in the pod spec:

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8080 }
  initialDelaySeconds: 10
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /ready, port: 8080 }
  initialDelaySeconds: 2
  periodSeconds: 5
```

(See ch 11 for the full probes story.)

---

## 23. Pitfalls

A field guide to the bugs you (and everyone) will hit.

### 23.1 Tag Mutability Bites

You pin nothing, you trust Docker Hub. A maintainer pushes a new `library/redis:7` that introduces a regression. Half your pods that pulled before the change are running the old bytes; half pulled after are running the new bytes. Production behaves inconsistently. There is no record on the cluster of which bytes are which.

**Mitigation**: pin by digest. Build CI to record digests at release time. Enforce at admission.

### 23.2 Registry Rate Limits

Docker Hub: anonymous pulls are 100/6h per IP. Authenticated free: 200/6h. NAT-fronted clusters share an IP, so 1000 nodes share 100 pulls/6h.

Symptoms: random `toomanyrequests: You have reached your pull rate limit` errors. Pods stuck in `ImagePullBackOff` until the rate window resets.

**Mitigations**:
- Mirror to ECR, GHCR, or Harbor (all unlimited within their service).
- Use a pull-through cache.
- Authenticate even anonymously useful images (4× the rate).

### 23.3 Missing Platform in Multi-Arch Pull

A pod runs on a `linux/arm64` node. The image is amd64-only. Pull fails with `no matching manifest for linux/arm64 in the manifest list entries`.

**Mitigations**:
- Build multi-arch images with buildx.
- Use `nodeAffinity` to constrain pods to compatible architectures.
- Set explicit `nodeSelector: { kubernetes.io/arch: amd64 }` when an image is single-arch.

### 23.4 Layer Cache Defeated by Big COPY

```dockerfile
COPY . .
RUN go build ./...
```

Every `git commit` invalidates the COPY layer (because some file changed), which invalidates the build layer, which invalidates everything below. Each build downloads and recompiles the world.

**Fix**: copy dependency manifests first, install dependencies, then copy source:

```dockerfile
COPY go.mod go.sum ./
RUN go mod download             # cached unless go.mod/go.sum change
COPY . .
RUN go build ./...
```

Now `go mod download` is cached across every commit that doesn't touch deps. The build is 10× faster on the steady state.

### 23.5 Secrets Baked Into Layers

```dockerfile
COPY .npmrc /root/.npmrc        # contains a token
RUN npm install
RUN rm /root/.npmrc             # ← does NOT remove from the COPY layer
```

The token is in the image, forever, in the COPY layer. Anyone with pull access can `docker save` the image, extract the tarball, and read the token.

**Fix**: use BuildKit secrets (`--mount=type=secret`), or multi-stage with the secret only in the build stage:

```dockerfile
FROM alpine AS deps
COPY .npmrc /root/.npmrc
RUN npm install
FROM alpine
COPY --from=deps /app/node_modules /app/node_modules
# Final image has node_modules but never had .npmrc
```

### 23.6 Image Pull Credential per Namespace Pain

You have 50 namespaces and you need image-pull credentials in every one (because the SA in each ns must reference an `imagePullSecret`). Rotating a registry credential means updating 50 secrets, hoping nothing was deployed in a new namespace yesterday with no secret yet.

**Mitigations**:
- Use kubelet credential providers — node-level, no per-namespace secrets.
- Use a controller like `imagepullsecret-patcher` to replicate a secret across all namespaces.
- For multi-team clusters, give each team a single ServiceAccount in their namespace with the imagePullSecret pre-attached; teams set `serviceAccountName: ours` in their pods.

### 23.7 Tag Mutation Bypassing Signing

You signed `myreg.io/me/app@sha256:bbbb...`. You also tagged it `v1`. Two months later, someone re-tags `v1` to `sha256:cccc...`. Pods deploying with `image: myreg.io/me/app:v1` now pull `cccc`, which is unsigned (or signed by someone else). Your verification webhook either rejects the pod (good) or fails open (bad).

**Mitigation**: admission webhook must resolve tag → digest and verify the *digest*. Both Kyverno and policy-controller do this when configured correctly. Additionally, mutate the pod to pin the resolved digest, so the kubelet pulls exactly the verified bytes.

### 23.8 Lazy Pull + Air Gap

You enabled `stargz-snapshotter`. The cold start is amazing. Then your cluster temporarily loses connectivity to the registry. Containers that were running keep running for a while; as soon as they touch a file that hasn't been paged in yet (a logfile, a localization file, etc.), they hang or crash.

**Mitigation**: use lazy pull only when registry availability matches your container's lifetime. For long-running stateful workloads, prefer traditional pull. For short-lived burst workloads (CI jobs, batch), lazy pull is fine.

### 23.9 Overlay Layer Limit

You build an image with 130 layers (`RUN` and `COPY` everywhere). Containerd refuses: `failed to mount overlay: too many lower directories`.

**Mitigation**: squash. `docker build --squash` or BuildKit `--output type=docker,dest=- | docker load` with intermediate flattening. Or restructure the Dockerfile.

### 23.10 ImagePullPolicy: Always With a Tag

```yaml
image: nginx:1.27
imagePullPolicy: Always
```

Every pod restart re-resolves the tag. The kubelet performs a HEAD request to the registry. If the tag has moved, the pod starts running new bytes. Across a rolling restart, replicas may diverge mid-rollout.

**Mitigation**: pin by digest. Or `imagePullPolicy: IfNotPresent` with strict tag discipline (semver-immutable tags via Harbor policies, for example).

### 23.11 Confusing Image and OCI Artifact

You push a Helm chart with `helm push chart.tgz oci://myreg.io/me`. Someone tries `docker pull myreg.io/me/mychart:v1` and gets a confusing error or a chart binary they can't run. The registry made no distinction; the user's tool did.

**Mitigation**: use distinct repositories for distinct artifact types (`registry/images/...` vs `registry/charts/...`), or rely on the `artifactType` field and tool-specific clients.

### 23.12 Skewed Cosign and Rekor Endpoints

Default cosign points at public Sigstore (`fulcio.sigstore.dev`, `rekor.sigstore.dev`). If you signed against a private Sigstore but verify against the public one, verification fails with a vague "no matching key" error. The reverse — public signing, private verification — fails for the same reason.

**Mitigation**: explicitly configure both signing and verifying clients with the same `--fulcio-url` and `--rekor-url`. Store these in a policy bundle that ships with your admission policy.

### 23.13 Treating SBOM Generation as Signing

Generating an SBOM (`syft`) is *not* signing it. An unsigned SBOM next to an image is just a list of components that anyone could have written. Always pair `syft` (generate) with `cosign attest` (sign and attach).

### 23.14 Garbage Collection Surprise

A team noticed they could only pull old image tags by digest, not by name. Their tag had been deleted; they assumed the bytes were gone too. They were not — until the next registry GC sweep, which then removed them. The lesson: a tag delete is a name change, not a content delete. The content disappears only at GC.

This is also a footgun: a tag-delete-and-recreate to "force a fresh pull" doesn't delete the underlying layers, so a different team can still pull the old bytes by digest. Compliance-wise this can be surprising.

### 23.15 Multi-Arch Build Without QEMU

```bash
$ docker buildx build --platform linux/amd64,linux/arm64 .
ERROR: exec /bin/sh: exec format error
```

You're on amd64 and trying to build arm64 without QEMU user-mode emulation registered. Fix:

```bash
$ docker run --privileged --rm tonistiigi/binfmt --install all
# or
$ docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

Then buildx will transparently use QEMU for cross-arch.

---

## 24. TL;DR

**An image is a Merkle DAG of content-addressed blobs in a registry.** At the root is an image **index** (multi-arch) or **manifest** (single-arch). The manifest descriptor-points to one **config** JSON and N **layer** tarballs, each addressed by `sha256(bytes)`. The config holds runtime defaults (entrypoint, env, user) and the `diff_ids` of the unpacked layers; the layers are gzipped tars whose stacking, with whiteout (`.wh.*`) and opaque (`.wh..wh..opq`) conventions, produces the rootfs that runc pivots into. Layer deduplication is structural: the same digest is stored once per registry and once per node, no matter how many images reference it.

**The registry is two REST collections.** `/v2/<name>/manifests/<ref>` for manifests and indexes; `/v2/<name>/blobs/<digest>` for opaque content. Authentication is the bearer-token dance: anonymous request → 401 with `Www-Authenticate` realm → token endpoint → retry with `Authorization: Bearer`. Cloud registries (ECR, GCR, ACR) wrap the same dance behind their identity systems; Kubernetes integrates via **kubelet credential providers** that exec a binary at pull time to obtain a short-lived credential — strictly better than long-lived secrets in etcd.

**Tags are mutable names; digests are immutable content.** Pin by digest in production. Tags drift; signatures, caches, and rollback semantics all assume content-addressed identity. `imagePullPolicy: Always` with a tag is the most common source of "production drifted overnight."

**The pull on the wire** is HEAD-manifest → GET (index if multi-arch, pick platform) → GET (manifest) → GET (config) → GET (layers, in parallel) → snapshotter unpacks each layer into an OverlayFS lower dir → active snapshot mount with N lowerdirs becomes the rootfs. Cold pull on a fat image is ~30 s; warm pull is ~5 ms; digest-pinned IfNotPresent is local-only. Image locality scoring in the scheduler and pre-pull DaemonSets close the gap.

**Image GC on the node** runs when `imagefs` crosses `imageGCHighThresholdPercent` and removes least-recently-used unused images down to the low threshold. The pause image and pinned images are exempt. Disk pressure on the imagefs also fires pod evictions.

**Lazy pulling** (`estargz`, `SOCI`, `zstd:chunked`, `Nydus`) reduces cold-start from tens of seconds to single-digit seconds by combining indexed layers + FUSE/snapshotter on-demand fetch. Don't enable in air-gapped or eventually-disconnected environments.

**Supply chain starts at the image.** Cosign signs the manifest digest with a Sigstore-issued short-lived cert bound to an OIDC identity (no long-lived keys), logged in the Rekor transparency log. Verify at admission with `policy-controller`, Kyverno, or Connaisseur, and mutate the pod to pin the resolved digest to close TOCTOU. SBOMs (SPDX, CycloneDX) enumerate components; SLSA provenance (in-toto) describes how the build ran; VEX tells you which CVEs actually apply. Everything attaches to the image by digest as an OCI referrer.

**OCI artifacts** generalize the registry beyond images: Helm charts, OPA bundles, WASM modules, SBOMs, signatures, and arbitrary tarballs all live in the same content-addressed store, manageable with `oras`. One registry, one identity, one GC policy.

**Best practices**: minimal base (distroless/scratch), multi-stage builds, non-root UIDs, .dockerignore, multi-arch via `buildx`, no secrets in layers, digest-pinned `FROM`, never `:latest`. The pitfalls are the inverse: tag drift, registry rate limits, layer cache defeats, secrets baked into a layer that you can't delete after the fact, the per-namespace `imagePullSecret` rope.

**The mental model**: every byte that ever runs in a container came from a blob in a registry, addressed by its sha256. Everything else — signing, SBOMs, lazy pulling, image GC, pull credentials — is operational machinery wrapped around that one immutable fact. Once you internalize "image = Merkle DAG of blobs," the rest of this chapter, and the supply-chain world that builds on it, becomes one consistent model rather than a vocabulary of disconnected tools.
