# Dockerfiles at Staff Level: Layering, Caching, Reproducibility, and the BuildKit Frontend

This chapter is about building container images the way a staff engineer thinks about them — not "what flags exist", but **what each line of a Dockerfile does to the resulting Merkle DAG, the build cache, the image size, the CI minutes, the cold-start latency on a node, the attack surface, and the on-call pager**. Chapter 02 explained what an OCI image *is*. This chapter explains how to *produce* one without shooting yourself in the foot.

The thesis: **a Dockerfile is not a shell script. It is a declarative graph of cache-keyed filesystem snapshots, executed by a frontend (BuildKit, Buildah, Kaniko) that decides which steps to re-run, parallelize, mount, or skip.** Most production Dockerfile problems are the result of authors treating it like a bash script — every problem solves itself the moment you start seeing the DAG.

---

## Table of Contents

1. [The Mental Model: Dockerfile as a Cache-Keyed DAG](#1-the-mental-model-dockerfile-as-a-cache-keyed-dag)
2. [Legacy Builder vs BuildKit: Why It Matters](#2-legacy-builder-vs-buildkit-why-it-matters)
3. [Base Image Selection: distroless, alpine, slim, scratch](#3-base-image-selection-distroless-alpine-slim-scratch)
4. [The Layer Cache: What Invalidates What](#4-the-layer-cache-what-invalidates-what)
5. [Ordering Instructions for Cache Hits](#5-ordering-instructions-for-cache-hits)
6. [Multi-Stage Builds: The Single Most Important Pattern](#6-multi-stage-builds-the-single-most-important-pattern)
7. [`COPY` vs `ADD` and `.dockerignore`](#7-copy-vs-add-and-dockerignore)
8. [Cache Mounts, Bind Mounts, and Secret Mounts (BuildKit)](#8-cache-mounts-bind-mounts-and-secret-mounts-buildkit)
9. [`RUN` Discipline: One Shell, Many Sins](#9-run-discipline-one-shell-many-sins)
10. [USER, Capabilities, and the Rootless Default](#10-user-capabilities-and-the-rootless-default)
11. [ENTRYPOINT vs CMD, Signals, and PID 1](#11-entrypoint-vs-cmd-signals-and-pid-1)
12. [Healthchecks: Use in Compose, Don't Bake into Kubernetes Images](#12-healthchecks-use-in-compose-dont-bake-into-kubernetes-images)
13. [Reproducibility: SOURCE_DATE_EPOCH, Pinning, and Determinism](#13-reproducibility-source_date_epoch-pinning-and-determinism)
14. [Multi-Architecture Builds: buildx, QEMU, and Native Nodes](#14-multi-architecture-builds-buildx-qemu-and-native-nodes)
15. [Remote Cache: registry, gha, s3, inline](#15-remote-cache-registry-gha-s3-inline)
16. [SBOM and Provenance Attestations at Build Time](#16-sbom-and-provenance-attestations-at-build-time)
17. [Image Size: Where the Bytes Actually Go](#17-image-size-where-the-bytes-actually-go)
18. [Cold-Start Latency: The Forgotten Cost of Big Images](#18-cold-start-latency-the-forgotten-cost-of-big-images)
19. [Language-Specific Pitfalls](#19-language-specific-pitfalls)
20. [TL;DR](#20-tldr)

---

## 1. The Mental Model: Dockerfile as a Cache-Keyed DAG

Forget the line-by-line view. When BuildKit parses a Dockerfile, it produces a **Low-Level Build (LLB) graph** — a DAG where each node is a filesystem operation (copy these files, run this command, fetch this image) and each edge is a dependency. Each node has a **cache key** computed from:

- The operation's command string (the literal `RUN ...` or `COPY src dst`).
- The digest of every input (parent image, files being copied, cache mounts).
- The platform (`linux/amd64`, `linux/arm64`).
- BuildKit's frontend version, if it affects semantics.

If a node's cache key matches a previously built node, BuildKit **reuses the resulting layer**. If not, the node executes and its output becomes a new cached layer. The downstream nodes then have new inputs and must also be re-run. This is the only model you need; every "why is my cache being busted" question reduces to "what input did you change for this node, or for any ancestor".

```
   FROM golang:1.22 AS build          (node 0: base image digest)
        │
        ▼
   WORKDIR /src                       (node 1: trivial, almost free)
        │
        ▼
   COPY go.mod go.sum ./              (node 2: input = hash of those 2 files)
        │
        ▼
   RUN go mod download                (node 3: input = node 2 fs + cmd)
        │
        ▼
   COPY . .                           (node 4: input = hash of all files)
        │
        ▼
   RUN go build -o /out/app ./cmd/app (node 5: input = node 4 fs + cmd)
        │
        ▼
   FROM gcr.io/distroless/static AS final
   COPY --from=build /out/app /app    (final image: only the binary)
```

The reason `COPY go.mod go.sum` comes before `COPY . .` is not stylistic — it is the entire reason `go mod download` (slow, network-heavy) gets cached across 99% of code changes. The cache key for node 3 only changes when `go.mod` or `go.sum` actually changes. Move `COPY . .` above it and every source edit re-downloads every module.

**Rule.** *Order Dockerfile instructions from least-frequently-changing to most-frequently-changing.* That single sentence captures the heart of build performance.

---

## 2. Legacy Builder vs BuildKit: Why It Matters

There are two builders in the wild and they produce **different images and different cache behavior**.

| Concern | Legacy (`DOCKER_BUILDKIT=0`) | BuildKit (`DOCKER_BUILDKIT=1`, default since 23.0) |
|---|---|---|
| Execution | Sequential, single-threaded | Parallel where the DAG allows |
| Cache | Layer cache, sometimes invalidated by trivia (timestamps in tar headers) | Content-addressed, robust |
| Cache mounts | None | `RUN --mount=type=cache,target=/root/.cache/go-build` |
| Secrets | Passed via `--build-arg` (leaks into image history!) | `RUN --mount=type=secret,id=npm` (never in layer) |
| SSH forwarding | Hacks | `RUN --mount=type=ssh` |
| Multi-stage | Builds all stages even if unused | Builds only stages reachable from the target |
| Frontend | Hardcoded Dockerfile semantics | Pluggable; `# syntax=docker/dockerfile:1.7` pins frontend |
| Output | Only image to local daemon | image, tar, OCI layout, registry, multiple at once |

**Always use BuildKit.** If you see a Dockerfile that starts without `# syntax=` and relies on `ARG` to pass secrets, you're reading code from 2018 that has a credential leak.

```dockerfile
# syntax=docker/dockerfile:1.7
```

This first line is a frontend pragma — BuildKit will pull this exact frontend image to parse the rest of the file. It is how new features (heredocs, named contexts, `--mount=type=secret`) ship without waiting for the engine to upgrade.

---

## 3. Base Image Selection: distroless, alpine, slim, scratch

The base image decision sets the floor for everything downstream: size, attack surface, CVE noise, libc, package manager, and whether `kubectl exec` is going to be useful at 3 AM.

| Base | libc | Shell | Size | When to use |
|---|---|---|---|---|
| `scratch` | none | none | 0 | Statically linked binaries (Go with `CGO_ENABLED=0`, Rust musl). Smallest possible. |
| `gcr.io/distroless/static` | none | none | ~2 MB | Statically linked Go/Rust, with CA certs + tzdata + `/etc/passwd`. |
| `gcr.io/distroless/base` | glibc | none | ~20 MB | Dynamically linked binaries needing glibc, OpenSSL. |
| `gcr.io/distroless/cc` | glibc + libstdc++ | none | ~25 MB | C++ binaries. |
| `alpine` | musl | ash | ~5 MB | Anything tolerating musl. **Beware musl bugs in DNS, threading.** |
| `debian:bookworm-slim` | glibc | bash | ~75 MB | Default safe choice. Real package manager. |
| `ubuntu:24.04` | glibc | bash | ~80 MB | Same as Debian, more drivers, more CVEs. |
| `nvidia/cuda:12.4-runtime-ubuntu22.04` | glibc | bash | ~3 GB | GPU workloads. Pay the cost. |

**The musl trap.** Alpine uses musl, not glibc. Most issues you'll hit:

- **DNS resolution behaves differently.** musl's resolver does not honor `/etc/nsswitch.conf`, parallelizes queries, and historically did not support search domains for single-label hosts. Production Kubernetes services have been broken by switching to alpine and discovering that pods can no longer resolve `database` (single label) but can resolve `database.default.svc.cluster.local`.
- **Threading and stack sizes differ.** Default stack size in musl is 80 KB vs glibc 8 MB. JVMs, Python (deep recursion), and any code with large thread-local arenas can crash.
- **getaddrinfo is not thread-safe in some musl versions.** Crashed Node.js applications under load.
- **Some Python wheels assume glibc** and either don't have musllinux wheels (`manylinux2014` only) — meaning pip will fall back to building from source, dragging in build deps and tripling your image size.

If you do not need to be on Alpine, do not be on Alpine. The size savings are dwarfed by debugging cost the first time a musl bug catches a senior engineer at 2 AM. Use `debian:bookworm-slim` as the build stage and `distroless/base` or `distroless/static` as the final stage.

**Distroless is the right default for production.** It contains your binary, its dynamic libraries, the trust store, and nothing else: no shell, no package manager, no curl, no busybox. That kills an entire class of post-exploitation moves. Yes, you lose `kubectl exec -- sh`. Ship an ephemeral debug container instead (`kubectl debug` since 1.25), or use the `debug` variants of distroless during incident response.

---

## 4. The Layer Cache: What Invalidates What

Every layer's cache key is a function of its **inputs** and **command**. The invalidation rules differ by instruction:

| Instruction | Cache key changes when… |
|---|---|
| `FROM image:tag` | The resolved digest of `image:tag` changes. **A tag is mutable.** Pin to a digest in CI for determinism. |
| `RUN ...` | The literal command string changes. **Not** the output of the command. `RUN apt-get update` will give you the same cached package list from 2 years ago until you change the command. |
| `COPY src dst` | Any byte in any file matched by `src` (after `.dockerignore`) changes, including mtime in some legacy builders. BuildKit hashes content, not mtime. |
| `ARG x` (before `FROM`) | The value of `x` changes — but only affects layers that reference it. |
| `ENV` | The literal value changes. |
| `WORKDIR`, `LABEL`, `USER` | The literal value changes. |

The single most common cache pitfall:

```dockerfile
# BAD
RUN apt-get update && apt-get install -y curl
COPY . /app
```

The `apt-get update` is cached forever (until you edit the line). Six months later a transitive dep ships a CVE fix, and your image is still pulling the same packages. Then someone edits the line and suddenly `apt-get install` fails because the indexed packages have been deleted from the mirror. This is the **"apt-get update cache trap"**.

Fix it three ways, in increasing rigor:

1. **Combine update and install in the same `RUN`** (you already do that — good). Then either pin every package version, or rebuild base images on a schedule.
2. **Pin packages explicitly:** `apt-get install -y curl=7.88.1-10+deb12u5`. Now the cache key is correct and the build is reproducible.
3. **Use a "freeze" registry** (Artifactory, Sonatype) that exposes a point-in-time snapshot of the upstream repo. Inputs become deterministic and you don't have to chase version pins.

---

## 5. Ordering Instructions for Cache Hits

The pattern is the same in every language:

```dockerfile
# 1. Base image
FROM node:20-bookworm-slim AS deps
WORKDIR /app

# 2. Dependency manifests only — change rarely
COPY package.json package-lock.json ./

# 3. Install deps — slow, cached when manifests don't change
RUN --mount=type=cache,target=/root/.npm \
    npm ci --only=production

# 4. Source code — changes frequently
COPY . .

# 5. Build (if needed)
RUN npm run build
```

Same pattern for Go: `go.mod`/`go.sum` → `go mod download` → `COPY . .` → `go build`.
For Python: `pyproject.toml`/`poetry.lock` (or `requirements.txt`) → install deps → `COPY . .`.
For Rust: copy `Cargo.toml`/`Cargo.lock`, `mkdir src && echo "fn main() {}" > src/main.rs`, `cargo build --release` (caches deps), then copy real source and rebuild. The dummy-source trick is the only sane way to get Rust dependency caching in Docker.

**Anti-patterns this kills:**

- `COPY . .` as the first content step. Now every code change re-runs everything.
- Splitting `COPY package.json` and `COPY package-lock.json` into two `COPY` lines. Same number of cache hits but doubles the layer count; tiny but cumulative across a fleet.
- Running `npm install` instead of `npm ci`. `install` mutates the lockfile in some scenarios; `ci` is deterministic.

---

## 6. Multi-Stage Builds: The Single Most Important Pattern

Multi-stage is not "a feature for advanced users." It is the **default**. Without it your image contains the toolchain, source code, package manager indexes, and build cache — at minimum 10× the size of what's needed at runtime, and a much larger attack surface.

The canonical Go example:

```dockerfile
# syntax=docker/dockerfile:1.7

# Stage 1: build
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/app ./cmd/app

# Stage 2: runtime
FROM gcr.io/distroless/static:nonroot
COPY --from=build /out/app /app
USER nonroot:nonroot
ENTRYPOINT ["/app"]
```

What's happening here:

- The final image contains exactly one file (the static binary) plus the distroless base (CA certs, tzdata, `/etc/passwd`). Total: ~10 MB.
- `-trimpath` strips absolute paths from the binary (reproducibility, no leaking `/home/runner/...`).
- `-ldflags="-s -w"` strips the symbol table and DWARF — typically halves the binary size. Lose this if you want core dumps to be debuggable.
- `CGO_ENABLED=0` produces a fully static binary; `distroless/static` has no libc.
- `USER nonroot:nonroot` matches the user distroless ships (UID 65532).

Other multi-stage patterns:

**Test stage that doesn't ship:**

```dockerfile
FROM golang:1.22 AS test
WORKDIR /src
COPY . .
RUN go test ./...

FROM golang:1.22 AS build
# ...
```

Building only the `build` target (`docker build --target build`) skips tests. Building only `test` is a CI-friendly way to run tests in the same environment that builds the image. BuildKit will skip stages that aren't reachable from the target.

**Multiple parallel build stages.** BuildKit will execute independent stages in parallel:

```dockerfile
FROM rust:1.78 AS rust_build
# ...

FROM node:20 AS node_build
# ...

FROM gcr.io/distroless/cc
COPY --from=rust_build /out/server /server
COPY --from=node_build /app/dist /static
```

The Rust and Node builds run concurrently. With legacy builder, they ran sequentially.

**Named build context** (BuildKit 1.4+): `docker build --build-context shared=../shared .` and `COPY --from=shared . /shared` lets you pull files from a sibling directory or another image without committing them to the same monorepo. Useful for monorepo builds without bloating the context.

---

## 7. `COPY` vs `ADD` and `.dockerignore`

**Always use `COPY`.** `ADD` has surprising semantics: it auto-extracts tarballs and can fetch URLs. Both are footguns.

- Tar auto-extraction depends on filename heuristics; a file named `data.tgz` mysteriously becomes a directory.
- URL fetching with `ADD https://...` does not verify TLS, ignores HTTP caching, and creates a layer that is essentially un-pinned. Use `RUN curl -fsSL ... | sha256sum -c -` so the digest of the downloaded file is part of your build's correctness check.

`.dockerignore` is mandatory. Without it, your build context contains every file in the current directory — including `.git`, `node_modules` (if you ran `npm install` locally), local `.env` files, IDE state, build artifacts. Then `COPY . .` puts those in the image.

Minimum `.dockerignore`:

```
.git
.github
.idea
.vscode
node_modules
**/__pycache__
**/*.pyc
**/.venv
**/target
**/dist
**/build
.env
.env.*
*.log
**/.DS_Store
README.md
Dockerfile
docker-compose*.yml
.dockerignore
```

A bad `.dockerignore` is the most common reason for "why is my build context 800 MB?" and "why did I leak credentials into a public image?"

---

## 8. Cache Mounts, Bind Mounts, and Secret Mounts (BuildKit)

This is where modern Dockerfiles diverge most sharply from 2018 ones.

### Cache mounts

A cache mount is a directory that persists *across builds* but does not become a layer of the image. This is for tool caches (`~/.cache/go-build`, `~/.npm`, `~/.cargo/registry`, `~/.cache/pip`, `apt` cache).

```dockerfile
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    go build ./...

# APT cache: don't let apt clean it
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl
```

Cache mounts are local to the BuildKit instance, so in CI you must either:

- Use a persistent BuildKit (`docker buildx create --use --driver docker-container --buildkitd-flags '...'`) backed by a stable volume.
- Use a **remote cache** so the cached layers (not the raw cache mount) are restored. See §15.

**Don't put cache mounts where the build's outputs go.** A cache mount on `/app/dist` means the output is not part of the image.

### Bind mounts (read-only build context)

```dockerfile
RUN --mount=type=bind,source=scripts,target=/scripts \
    /scripts/install.sh
```

Mounts a directory from the build context into the build step without making it a layer. Used when you want a script available during one `RUN` but not in the final image.

### Secret mounts

This is the only correct way to handle build-time secrets.

```dockerfile
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc \
    npm ci
```

Then build with:

```
docker buildx build --secret id=npmrc,src=$HOME/.npmrc -t myapp .
```

The secret file is mounted into the build step's filesystem for the duration of the `RUN`, but is **not** in the layer, **not** in the image history, and **not** in the build cache. Compare to the legacy hack:

```dockerfile
# NEVER do this
ARG NPM_TOKEN
RUN echo "//registry.npmjs.org/:_authToken=${NPM_TOKEN}" > ~/.npmrc && npm ci && rm ~/.npmrc
```

The `ARG` value lives forever in `docker inspect <image> | jq '.[0].Config'` and in the build history. Deleting `.npmrc` in the same `RUN` doesn't help — secret was already in the build cache and ARG metadata.

### SSH mounts

For private git repos:

```dockerfile
RUN --mount=type=ssh \
    git clone git@github.com:myorg/internal-lib.git
```

Build: `docker buildx build --ssh default=$SSH_AUTH_SOCK .`. SSH agent socket is forwarded into the build, no keys land in image.

---

## 9. `RUN` Discipline: One Shell, Many Sins

`RUN` is shelled out to `/bin/sh -c "<command>"` by default. The shell form (`RUN apt-get install ...`) loses you exit codes on pipe failures. The exec form (`RUN ["apt-get", "install", "..."]`) does not start a shell at all and so cannot do `&&`, redirection, expansion.

Two non-negotiable habits:

**Set `SHELL` to bash with pipefail** if you use pipes:

```dockerfile
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
RUN curl -fsSL https://example.com/install.sh | bash
```

Without `pipefail`, the `RUN` succeeds even if `curl` 404s — because the pipe's exit code is `bash`'s, and bash got an empty input which it ran successfully.

**Chain commands and clean up in the same layer**. The classic:

```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg && \
    rm -rf /var/lib/apt/lists/*
```

Three lessons baked into this:

- `--no-install-recommends` cuts hundreds of MB of "maybe useful" packages.
- `rm -rf /var/lib/apt/lists/*` — if you don't delete the apt indexes in the *same* layer, they're now permanent in the image. (With cache mounts you instead don't write them in the first place; pick one strategy and be consistent.)
- One `RUN` = one layer. Splitting into three `RUN` lines means apt indexes are committed to layer 1, then removed in layer 3, but layer 1's bytes are still in the image because images are stacks of immutable layers — deletion in a higher layer is a *whiteout* and doesn't reclaim space.

Heredocs (BuildKit 1.3+) let you write multi-line `RUN` blocks without backslash hell:

```dockerfile
RUN <<EOF
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends curl ca-certificates
rm -rf /var/lib/apt/lists/*
EOF
```

Read like a shell script, run like a shell script, but it's still **one** layer.

---

## 10. USER, Capabilities, and the Rootless Default

**Every image should declare a non-root `USER`.** Period. If a CVE in your application leads to code execution, "is it root inside the container" is the deciding factor between "minor incident" and "container escape via kernel bug followed by node takeover".

```dockerfile
RUN groupadd -r app && useradd -r -g app -u 10001 app
USER 10001:10001
```

Note the UID and GID are numeric — Kubernetes' `runAsNonRoot: true` enforcement compares numerically; a `USER app` with no matching `/etc/passwd` in distroless will fail.

Distroless images already ship a `nonroot` user (UID 65532):

```dockerfile
FROM gcr.io/distroless/static:nonroot
USER nonroot:nonroot
```

**Filesystem ownership matters.** If your binary writes to `/data`, you must `COPY --chown=10001:10001 ... /data`, otherwise the mounted filesystem will reject writes. With PVCs in Kubernetes, ownership is handled by `fsGroup`; with bind mounts in Compose, it is on you.

**Don't drop privileges in ENTRYPOINT.** `gosu` and `su-exec` are runtime privilege drops — historically used to start as root, fix ownership of a mounted volume, then drop. The cost is that until that drop happens, the container is root, and any vulnerability in init scripts has root privileges. The clean approach: build for non-root, demand the right `fsGroup`/UID from the orchestrator, and let your container run as a normal user from PID 1.

**Capabilities.** Containers run by default with a reduced set (`CAP_NET_BIND_SERVICE`, `CAP_CHOWN`, etc.), and Kubernetes pod specs can `drop: ["ALL"]` and `add: ["NET_BIND_SERVICE"]`. The image doesn't set capabilities, but it determines whether your code *needs* them. Two relevant cases:

- **Binding to ports <1024.** Linux requires `CAP_NET_BIND_SERVICE`. Better: bind to 8080 and let a Service map to 80.
- **Setting file capabilities.** `setcap cap_net_bind_service=+ep /app` lets a non-root user bind low ports, but file capabilities **do not survive `COPY --from=`** in many BuildKit versions because they live in xattrs that the tar format may not preserve.

---

## 11. ENTRYPOINT vs CMD, Signals, and PID 1

**Use exec form, always.**

```dockerfile
# Good
ENTRYPOINT ["/app"]
CMD ["--config", "/etc/app/config.yaml"]

# Bad
ENTRYPOINT /app
CMD --config /etc/app/config.yaml
```

The shell form (`ENTRYPOINT /app`) is silently rewritten to `["/bin/sh", "-c", "/app"]`. That has consequences:

- **Signals are sent to `sh`, not to `/app`.** When Kubernetes sends `SIGTERM` to PID 1, `sh` does not forward it. Your app never gets the termination signal and the kubelet eventually `SIGKILL`s it after the grace period.
- **`sh -c` becomes PID 1.** That makes zombie reaping `sh`'s job, which it does not do.

If you must do shell interpolation (env vars in args), use `exec`:

```dockerfile
ENTRYPOINT ["/bin/sh", "-c", "exec /app --port=$PORT"]
```

`exec` replaces the shell with the app, restoring PID 1 to the binary so signals are delivered correctly.

**PID 1 problems.** A real init (`tini`, `dumb-init`, `s6-overlay`) reaps zombies and forwards signals. You need an init when:

- Your app spawns child processes that may outlive their parent (creating zombies).
- Your app has shell-form entrypoint and you can't change it.

Kubernetes pod spec has `shareProcessNamespace: true` and `terminationGracePeriodSeconds`, plus a `pause` container that serves as PID 1 in the pod's PID namespace, so in K8s you generally do **not** need an init in the image. In Compose, you do: `init: true` in the service spec makes Compose run `tini` as PID 1.

**Don't bake `dumb-init` into images destined for Kubernetes** unless you actually spawn subprocesses. It's noise.

---

## 12. Healthchecks: Use in Compose, Don't Bake into Kubernetes Images

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8080/healthz || exit 1
```

Kubernetes **ignores `HEALTHCHECK` from the image**. Probes are defined in the pod spec (`livenessProbe`, `readinessProbe`, `startupProbe`) and run by the kubelet directly, not the runtime. If your image is only ever deployed to Kubernetes, omit `HEALTHCHECK`.

But: Compose and Swarm honor it. If you build images that run in both Compose (dev/local) and Kubernetes (prod), bake the `HEALTHCHECK` in for the Compose case — it will be silently ignored on the K8s side.

Pitfalls:

- `HEALTHCHECK CMD curl ...` assumes `curl` is in the image. Distroless has no curl. Implement healthchecks as a `/healthz` HTTP endpoint and use a tiny static binary like `grpc_health_probe` (or write a 50-line health probe binary in Go).
- A healthcheck that polls every 5 seconds adds up: 100 containers × 1 connection every 5s = 20 healthcheck requests per second. On a constrained app this matters.
- A healthcheck that *fails closed* on dependency failure (db unreachable → unhealthy) creates cascading failures. Liveness should mean "the process is wedged"; readiness means "I can serve traffic." Conflating them is a classic outage pattern.

---

## 13. Reproducibility: SOURCE_DATE_EPOCH, Pinning, and Determinism

Two builds of the same Dockerfile from the same source should produce the same image digest. They almost never do, because:

- `apt-get install` pulls whatever version is current.
- `pip install foo` resolves to the newest matching version.
- `RUN go build` embeds build timestamps and absolute paths.
- Tar headers in `COPY` include file mtimes.

For most teams "reproducible" means "we can rebuild any version forever and get something that works." That's enough — and is achieved by pinning. For some teams (supply-chain-paranoid, regulated) it means "bit-identical."

The pinning ladder:

1. **Pin base images by digest:** `FROM debian@sha256:abc123...` rather than `FROM debian:bookworm`. Bookworm tag mutates as Debian releases point updates; the digest doesn't.
2. **Pin OS packages:** `apt-get install -y curl=7.88.1-10+deb12u5`. Use `apt-cache madison` to find versions.
3. **Pin language deps via lockfiles** (`package-lock.json`, `poetry.lock`, `Cargo.lock`, `go.sum`). For Python, `pip-tools` or `uv pip compile` to produce a hashed lockfile.
4. **Use a snapshot of upstream package indexes** (snapshot.debian.org, Artifactory snapshots, pip with `--index-url=`).
5. **`SOURCE_DATE_EPOCH`** — set in the env, BuildKit honors it for layer timestamps. Some toolchains (Go with `-trimpath`, recent `tar`) honor it too. With this set, two builds at different times produce identical layer digests if all inputs are identical.

Bit-identical reproducibility is the difference between "I trust the build" and "I can prove the build." Sigstore + provenance attestations (§16) build on top.

---

## 14. Multi-Architecture Builds: buildx, QEMU, and Native Nodes

A modern image manifest list is a multi-arch index: one image, N manifests (one per platform). Building this with `buildx`:

```
docker buildx create --use --name multi --driver docker-container
docker buildx build --platform linux/amd64,linux/arm64 -t myorg/app:1.2.3 --push .
```

Two ways to actually execute the foreign-arch build:

1. **QEMU user-mode emulation** (default with `buildx`). Slow — Go builds easily 5× slower under qemu-aarch64. Compile-bound builds become the dominant CI cost.
2. **Native builders** — `buildx` connects to remote BuildKit instances on each architecture. ARM workloads build on ARM hosts; AMD64 on AMD64. Use `docker buildx create --append --node arm-builder --platform linux/arm64 ssh://user@arm-host`.

In practice, GitHub-hosted runners with `buildx` + QEMU works for small images (Python/Node). For Go, Rust, anything compiled, run a self-hosted ARM runner or use a service that provides them (Depot, BuildJet, Namespace).

**Cross-compile vs emulate.** Go's cross-compilation (`GOOS=linux GOARCH=arm64 go build`) is faster than running x86 Go under qemu-aarch64. But the resulting binary must be packaged in an arm64 image. The trick:

```dockerfile
FROM --platform=$BUILDPLATFORM golang:1.22 AS build
ARG TARGETOS TARGETARCH
WORKDIR /src
COPY . .
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH go build -o /out/app ./cmd/app

FROM --platform=$TARGETPLATFORM gcr.io/distroless/static
COPY --from=build /out/app /app
ENTRYPOINT ["/app"]
```

`$BUILDPLATFORM` is the host platform (amd64); `$TARGETPLATFORM` is the requested one. The build stage runs natively on the host; the final stage uses the target arch's base image. Result: full speed builds for all architectures from one host.

---

## 15. Remote Cache: registry, gha, s3, inline

Local cache is useless in CI; each runner is fresh. Remote cache pushes the cache to a shared store.

| Backend | Notes |
|---|---|
| `--cache-to type=inline` | Cache embedded in the pushed image. Simple. Bad for big images. |
| `--cache-to type=registry,ref=myorg/app:cache` | Separate cache image. Better. |
| `--cache-to type=gha` | GitHub Actions cache. Free in GHA, limited to 10 GB and 7-day eviction. |
| `--cache-to type=s3,region=us-east-1,bucket=...` | S3 backend. Best for self-hosted. |
| `--cache-to type=azblob,...` | Azure equivalent. |
| `--cache-to type=local,dest=/tmp/cache` | Filesystem dump. For self-managed CI. |

Typical CI invocation:

```
docker buildx build \
  --cache-from type=registry,ref=myorg/app:cache \
  --cache-to   type=registry,ref=myorg/app:cache,mode=max \
  --push -t myorg/app:${SHA} .
```

`mode=max` exports cache for *all* stages (multi-stage builds), not just the final stage. Cost: larger cache image. Benefit: changes to the build stage are also cached. For most teams, `mode=max` is right.

---

## 16. SBOM and Provenance Attestations at Build Time

BuildKit 0.11+ can attach SBOM (software bill of materials) and SLSA provenance attestations to the image at build time, as separate manifests in the index referencing the image manifest.

```
docker buildx build \
  --sbom=true \
  --provenance=mode=max \
  -t myorg/app:1.2.3 --push .
```

The pushed image is now an index with three children: the actual image manifests (per arch), the SBOM (an OCI artifact with SPDX or CycloneDX content), and the provenance attestation (in-toto SLSA format).

Downstream:

- `docker buildx imagetools inspect myorg/app:1.2.3 --format '{{json .SBOM}}'` reads the SBOM.
- `cosign verify-attestation` can verify the provenance attestation against a signing identity.
- Admission webhooks (Kyverno, OPA Gatekeeper, sigstore-policy-controller) enforce "only images with SBOM and provenance from CI may be deployed". See ch 27 (supply chain security) for the policy side.

This is the cheapest piece of supply chain hygiene — flip two flags and your build is attestable. The expensive part is **enforcing** it at admission.

---

## 17. Image Size: Where the Bytes Actually Go

Image size matters because:

- **Pull time:** large images delay pod start. A 2 GB image takes ~20 seconds to pull on a 1 Gbps node from a same-region registry; from cross-region, minutes.
- **Storage cost:** registries charge for storage and egress.
- **Disk pressure on nodes:** kubelet's image GC starts evicting images when `nodefs` hits 85%. Big images run out of room faster, evicting cache and slowing future pulls.
- **Memory cost of cold start:** every byte read from disk is one more page that didn't go to your app's working set.

Measuring: `dive` is the right tool. `docker images` only shows compressed size; `dive` shows per-layer expansion, wasted space (files added then removed in a later layer), and a "score."

The usual suspects in a bloated image:

1. **Build tools in the runtime image.** Solve with multi-stage.
2. **OS package cache.** `/var/lib/apt/lists`, `/var/cache/apt/archives`. Either delete in the same `RUN` or use cache mounts.
3. **Python wheels with sources.** `pip install --no-cache-dir`. Use `--compile` for `.pyc`s, then strip `.py` if you don't need them (controversial, debug-hostile).
4. **Node `node_modules` with dev deps.** `npm ci --only=production` or `npm prune --production` after build.
5. **`.git` directories** if cloned during build.
6. **Locale data / man pages / docs.** Use `--no-install-recommends` and consider `/etc/dpkg/dpkg.cfg.d/01_nodoc`.
7. **Symbols and debug info in compiled binaries.** Strip in the build stage (`strip /out/app` or `-ldflags="-s -w"` for Go).
8. **`pip` and `npm` install metadata** ending up in image: install with `--no-cache-dir` for pip, `npm ci` + `npm prune` for node.

Concrete win: a real Python service started at 1.4 GB (full `python:3.12` + venv + system deps for `numpy` + dev tools). After multi-stage + slim base + `--no-install-recommends` + cleanup: 220 MB. Pull time went from 14s to 3s; pod p95 startup dropped a similar amount.

---

## 18. Cold-Start Latency: The Forgotten Cost of Big Images

Image size affects more than just pull time. When kubelet calls `containerd` to start the container, containerd has to:

1. **Pull** every layer not already on the node.
2. **Unpack** every layer onto the snapshotter (overlayfs by default). Unpacking is CPU-bound (decompression) and IOPS-bound (writing thousands of small files).
3. **Mount** the resulting overlay.

For autoscaling, especially scale-to-zero on Knative or KEDA, this is your p99 latency. The optimizations:

- **Smaller layers.** Layer extraction is parallelized at the layer granularity; many small layers extract faster than one giant one. But there's overhead per layer, so don't go nuts.
- **Lazy pulling** via `estargz`, `SOCI`, or `Nydus` (see ch 02 §17). The image is structured so that the runtime can start the container before all layers are fully pulled, fetching files on demand. AWS reports 60-70% reductions in cold start for large images using SOCI.
- **Image preloading.** If you know the image is coming, run a DaemonSet that pre-pulls it. `kubectl drain` + `--cordon` after preload gets it onto every node.
- **Image streaming registries.** Google Cloud's Artifact Registry has streaming, AWS ECR has SOCI indexes.

For a regional cluster pulling from a global registry, **the registry's location dominates pull time**, often more than the image's size. Put a pull-through cache registry in each region (Harbor, Sonatype Nexus, AWS ECR pull-through cache).

---

## 19. Language-Specific Pitfalls

**Go.** `CGO_ENABLED=0` for static binaries; `-trimpath` for reproducibility; `-ldflags="-s -w"` for size. Use `go build -mod=readonly` in CI to error if `go.mod` would be modified. Use a workspace cache mount.

**Python.** Use `python:3.12-slim-bookworm`, not `python:3.12`. Two-stage: stage 1 builds wheels into `/wheels`, stage 2 installs from wheels with `--no-index`. Always `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`. Beware: many Python C extensions (`uvloop`, `psycopg2`, `lxml`) have musl wheels missing, so Alpine forces source builds → giant images.

**Node.** `npm ci`, not `npm install`. `--omit=dev` in the production stage. Be aware of `node-gyp` and native modules — they will need the build toolchain in your build stage. `node:20-slim` is the right base. Watch out for postinstall scripts that download binaries (Cypress, Playwright, esbuild) — they may need a network during build that BuildKit blocks unless explicitly allowed.

**Rust.** Use the dummy-`main.rs` trick for caching deps. Strip the binary (`strip target/release/app`). Use musl target (`x86_64-unknown-linux-musl`) for true static binaries. `cargo chef` is a build tool that automates dependency-only builds.

**Java.** Use `eclipse-temurin:21-jre` (or distroless `java21`). JLink to make a custom JRE with only the modules you need (cuts JRE from ~250 MB to ~80 MB). Layered jars (Spring Boot `BP_LAYERS=true`) put your code in a separate layer from dependencies, so a code change re-builds only one tiny layer.

**.NET.** Use the runtime image (`mcr.microsoft.com/dotnet/aspnet:8.0`), not the SDK. Layer your build: copy `.csproj`, restore, then copy source. Trim with `PublishTrimmed=true` if your code permits.

---

## 20. TL;DR

- **A Dockerfile is a cache-keyed DAG, not a shell script.** Order steps from least to most volatile so the cache stays warm for the common-case edit.
- **Always use BuildKit.** Use `# syntax=docker/dockerfile:1.7`. Use cache mounts for tool caches, secret mounts for secrets, bind mounts for build-only scripts. Never `ARG NPM_TOKEN`.
- **Multi-stage is the default.** Build in a fat builder image, copy artifacts into distroless or scratch. Image size drops by 5-50×.
- **Pin everything.** Base image by digest, OS packages by version, language deps via lockfiles. Without pinning you have neither reproducibility nor security hygiene.
- **Non-root by default.** Numeric UID, declared `USER`. Distroless `nonroot` variant for production.
- **Exec form for ENTRYPOINT/CMD.** Shell form swallows signals and breaks graceful shutdown.
- **`HEALTHCHECK` for Compose, K8s probes for Kubernetes.** Don't conflate; don't fail liveness on dependency failure.
- **Multi-arch via `buildx`.** Use `$BUILDPLATFORM`/`$TARGETPLATFORM` to cross-compile rather than emulate when the toolchain supports it.
- **Remote cache + `mode=max`** in CI. Inline cache is fine for hobby projects; nobody else.
- **SBOM and provenance attestations are two flags away** and gate-able at admission. Ship them.
- **Size matters because pull time, disk pressure, and cold-start latency matter.** `dive` your images. Anything over 500 MB without a GPU/ML reason has unused weight.
- **`.dockerignore` is not optional.** Bad ignore = leaked secrets, bloated context, slow builds.
- **Avoid musl/Alpine for production services** unless you've quantified the DNS, threading, and wheel-availability risk and decided it's acceptable.
