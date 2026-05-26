# Docker Anti-Patterns and Bad Configurations: What Breaks at Scale and Why

Chapter 39 covered how to build images well. This chapter is the inverse: a tour of the **specific configurations and Dockerfile patterns that work in tutorials, break in production, and ruin Sundays**. Every pattern here is something I have personally seen page an on-call engineer, blow a security audit, leak a credential, or generate a six-figure cloud bill. The goal is not to enumerate every bad practice — it is to teach the *reasoning* that lets you recognize a new one when it shows up in a code review.

Anti-patterns cluster around five themes:

1. **Treating containers like VMs.** Long-lived, mutable, in-place upgraded.
2. **Treating Dockerfiles like shell scripts.** Ignoring the cache, leaking secrets, ignoring layers.
3. **Treating images like fungible blobs.** No pinning, no provenance, "latest" everywhere.
4. **Treating runtime config like build-time data.** Secrets baked in, env hardcoded, "rebuild for prod."
5. **Treating Docker as the orchestrator.** Restart loops, `--restart=always`, hand-rolled HA on top of plain Docker.

Each theme has a long tail of consequences. We'll go through them with examples, blast radius, and the fix.

---

## Table of Contents

1. [The "Treat Containers Like VMs" Family](#1-the-treat-containers-like-vms-family)
2. [The Cache-Hostile Dockerfile](#2-the-cache-hostile-dockerfile)
3. [Secret Leaks: ARG, ENV, COPY, and Git](#3-secret-leaks-arg-env-copy-and-git)
4. [Running as Root and Other Privilege Sins](#4-running-as-root-and-other-privilege-sins)
5. [`latest` Tag and the Mutable Image Problem](#5-latest-tag-and-the-mutable-image-problem)
6. [The Bloat Patterns](#6-the-bloat-patterns)
7. [Init, Signals, and Graceful Shutdown Failures](#7-init-signals-and-graceful-shutdown-failures)
8. [Volume and Permission Disasters](#8-volume-and-permission-disasters)
9. [Networking Mistakes](#9-networking-mistakes)
10. [Logging Configurations That Eat Disks](#10-logging-configurations-that-eat-disks)
11. [Build Context, .dockerignore, and Git Hygiene](#11-build-context-dockerignore-and-git-hygiene)
12. [Healthchecks That Make Things Worse](#12-healthchecks-that-make-things-worse)
13. [Misusing `--restart=always` as HA](#13-misusing---restartalways-as-ha)
14. [Bind-Mounting `/var/run/docker.sock` ("Docker-in-Docker")](#14-bind-mounting-varrundockersock-docker-in-docker)
15. [Building on the Production Host](#15-building-on-the-production-host)
16. [Anti-Scalable Patterns](#16-anti-scalable-patterns)
17. [Security and Compliance Anti-Patterns](#17-security-and-compliance-anti-patterns)
18. [TL;DR Checklist](#18-tldr-checklist)

---

## 1. The "Treat Containers Like VMs" Family

The single biggest cultural failure I see in teams adopting Docker after years of VM-based ops is treating containers as long-lived, mutable systems. The visible symptoms:

- `docker exec -it $container bash` followed by `apt-get install` to "fix something quickly."
- `docker commit` to capture a known-good state.
- A container that's been running for 11 months and nobody knows what version of the app is in it.
- A `Dockerfile` that lays down a base OS, then `systemd`, then a bundle of services, all in one container.
- `supervisord` running 6 different daemons in one image.

**Why it breaks.** Containers are designed to be **immutable, single-process units** with all state externalized. Every patch you apply with `exec` is invisible to your image registry, untracked in git, lost on the next restart. Every multi-process container conflates the lifecycle of services that should restart, scale, and observe independently.

**The fixes:**

- **One process per container.** If a service needs a sidecar (log shipper, proxy), put the sidecar in its own container in the same pod (K8s) or its own service in Compose. Don't `supervisord`.
- **All changes via image rebuild.** `kubectl rollout restart deployment/api` after pushing a new tag. Never `kubectl exec`. If you find yourself wanting to, write down what you wanted to do, fix the image, redeploy.
- **State lives outside the container.** Databases in PVCs, cache in Redis, config in ConfigMaps, secrets in Vault/Sealed Secrets, logs to stdout/stderr.
- **Containers are cattle.** Naming them, treating them as "the API box", is a smell. They should be replaceable in under a second.

The rule of thumb: *if `docker rm $container && docker run ...` would lose work, your container is wrong.*

---

## 2. The Cache-Hostile Dockerfile

The most common Dockerfile bug in the wild:

```dockerfile
# BAD
FROM node:20
WORKDIR /app
COPY . .
RUN npm install
CMD ["node", "server.js"]
```

What's wrong:

- `COPY . .` invalidates the layer cache on every code change. The subsequent `npm install` re-runs every time.
- `npm install` (vs `npm ci`) mutates `package-lock.json` if it disagrees with `package.json`. Non-deterministic builds.
- Image runs as root.
- No `.dockerignore` mentioned, so `node_modules/`, `.git/`, `.env` all end up in the build context — and possibly in the image.
- Single stage means dev dependencies and source code are in the production image.
- `CMD ["node", "server.js"]` runs as PID 1, but if the entrypoint isn't `tini`, signal handling in Node is a known footgun for graceful shutdown.

The right shape was covered in ch 39, but the lesson here is **a cache-hostile Dockerfile is a CI-cost-disaster**. A team running 50 builds/day with a 4-minute uncached install vs 20-second cached install costs ~12 minutes/build × 50 = 10 engineer-hours of CI wait per day. That's the salary of a senior SRE just to wait on builds.

Other cache-hostile patterns:

- **Splitting a logical `RUN` into many.** Each `RUN` is a layer. `apt-get update` in one `RUN` and `apt-get install` in the next means the install can be cached while the indexes are stale. Always chain.
- **`RUN apt-get update` alone.** Cached forever; nothing forces re-run. Combine with install.
- **Using mutable URLs in `ADD`.** `ADD https://example.com/script.sh /` — BuildKit checks ETag/Last-Modified; behavior is inconsistent. Use `RUN curl ... | sha256sum -c -`.
- **Computing the date inside the build.** `RUN echo $(date) > /build_date` busts the cache every single time.
- **`COPY` of generated files** (e.g., `target/` or `dist/`) that are different every build because of timestamps.

---

## 3. Secret Leaks: ARG, ENV, COPY, and Git

Five common ways to leak secrets in a container image:

### 3a. `ARG` followed by `RUN` that uses the secret

```dockerfile
# CATASTROPHIC
ARG NPM_TOKEN
RUN echo "//registry.npmjs.org/:_authToken=${NPM_TOKEN}" > ~/.npmrc \
 && npm ci \
 && rm ~/.npmrc
```

Even after deleting `.npmrc`, the `ARG` value is preserved in image metadata. `docker history --no-trunc <image>` shows it. Anyone with pull access has the token.

**Fix:** BuildKit secret mounts.

```dockerfile
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc npm ci
```

```
docker build --secret id=npmrc,src=$HOME/.npmrc -t app .
```

The secret is mounted only during that `RUN`, never persisted in a layer or in metadata.

### 3b. `ENV` for secrets

```dockerfile
ENV DATABASE_PASSWORD=hunter2
```

Burned into the image. Anyone who pulls it sees it via `docker inspect`. And — equally bad — every container that runs this image has it set, including ones you'd rather not.

**Fix:** Pass via runtime: `-e DATABASE_PASSWORD=$(vault read ...)` or Kubernetes Secret with `envFrom: secretRef:`.

### 3c. `COPY` of a `.env` or credentials file

```dockerfile
COPY .env /app/.env
```

If `.env` is in your build context (and `.dockerignore` does not exclude it), it goes into the image. If your repository has a `.env` file committed (which it should not!), it gets pulled by anyone with image access.

**Fix:** `.dockerignore` excludes `.env*`. Mount config at runtime.

### 3d. Cloning a private repo without SSH forwarding

```dockerfile
COPY ~/.ssh/id_rsa /root/.ssh/id_rsa
RUN git clone git@github.com:company/private.git
RUN rm /root/.ssh/id_rsa
```

The key is in a layer. `rm` in a later layer does not delete the bytes.

**Fix:** `RUN --mount=type=ssh git clone ...`.

### 3e. Building from a context with `.git`

```
docker build -t app .
```

If `.dockerignore` does not contain `.git`, the entire `.git` directory is in the build context. `COPY . .` then copies it into the image. The git history likely contains old credentials.

**Fix:** `.dockerignore` includes `.git`.

### Detection

```
docker history --no-trunc <image>
docker inspect <image> | jq '.[0].Config.Env'
trivy image <image>    # finds secrets in layers
syft <image>           # produces SBOM, can flag credentials
```

The minute you suspect a leak, **invalidate the credential** (rotate the token, deauthorize the deploy key). Removing the image from the registry is not enough; anyone who pulled it once still has the bytes.

---

## 4. Running as Root and Other Privilege Sins

Default container behavior is to run as UID 0 (root) **inside the container's user namespace**. Without user namespaces enabled at the runtime level, that root is the host's root (constrained by capabilities, seccomp, etc., but still root). Bad ideas in this neighborhood:

### 4a. No `USER` directive

```dockerfile
FROM debian:bookworm
# ... no USER ...
CMD ["./app"]
```

Now CVE-2024-XXXX in your app leads to RCE as root in the container. Combined with a misconfigured pod (no seccomp, no AppArmor) or a kernel CVE, this is your container escape path.

**Fix:**

```dockerfile
RUN useradd -r -u 10001 -g nogroup app
USER 10001:65534
```

### 4b. `--privileged` in production

```yaml
# docker-compose.yml — production
services:
  app:
    privileged: true
```

`--privileged` is "full host access." Drops all isolation. Used to be required for Docker-in-Docker; with rootless and modern runtimes, almost never necessary. If a vendor docs say "needs `--privileged`", treat that as a procurement red flag and ask why.

**Fix:** Identify the *specific* capability needed. Add only that with `cap_add: [NET_ADMIN]` and `security_opt`/`devices` for the specific privilege required. If you can't justify the cap, the workload doesn't belong in the cluster.

### 4c. Capabilities not dropped

A pod spec without `securityContext.capabilities.drop: ["ALL"]` runs with the default container capability set, which includes `NET_BIND_SERVICE`, `CHOWN`, `SETUID`, `SETGID`, etc. — useful sometimes, attack surface always.

**Fix:**

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
```

### 4d. Writable root filesystem

Default Docker behavior is a writable rootfs. An attacker can drop a webshell, modify binaries, or scratch swap to disk.

**Fix:** `read_only: true` in Compose, `readOnlyRootFilesystem: true` in K8s, plus explicit writable volumes for `/tmp`, `/var/cache`, etc. (`emptyDir` in K8s, `tmpfs` in Compose).

---

## 5. `latest` Tag and the Mutable Image Problem

```yaml
# docker-compose.yml
services:
  api:
    image: myorg/api:latest
```

What happens:

- Today you pull `latest`, which currently resolves to v1.4.2.
- Tomorrow someone pushes v1.5.0 to `latest`. Your next deploy gets v1.5.0 without you noticing.
- Last week's container was v1.4.0. There is no record of *which* `latest` you were running.
- Rolling back means knowing which sha256 was previously running. It's not in the YAML.

**Fix:**

- Tag images with explicit, immutable versions: `myorg/api:1.5.0`, ideally with the git SHA appended: `myorg/api:1.5.0-a3f8c1d`.
- In Kubernetes, pin by digest at deploy time: `image: myorg/api@sha256:abc123...`. This is the only way to be sure that a rolling update doesn't pull a different image than the one your CI built.
- Configure your registry to **make tags immutable**. AWS ECR, GCR/Artifact Registry, Harbor, and Docker Hub (paid) all support this. Once a tag is pushed, it can't be moved.

`latest` is fine for examples and quickstarts. It is forbidden in production.

---

## 6. The Bloat Patterns

A 4 GB Python image. A 9 GB ML image. A 3 GB Node image. They all have the same shape.

### 6a. Build tools in the runtime image

`gcc`, `make`, `python3-dev`, `libpq-dev` are needed to compile wheels. Once the wheels are built, they're dead weight.

**Fix:** Multi-stage. Build wheels in a fat builder, install from wheels in a slim runtime.

### 6b. Untrimmed package indexes

`/var/lib/apt/lists/` after `apt-get update` is 50-200 MB. Most Dockerfiles forget to delete it.

**Fix:**

```dockerfile
RUN apt-get update \
 && apt-get install -y --no-install-recommends pkg \
 && rm -rf /var/lib/apt/lists/*
```

Or use cache mounts (ch 39 §8) so the indexes never enter the layer.

### 6c. `--no-install-recommends` missing

Without it, `apt-get install postfix` pulls 60 MB of "you might want this" packages. Default Debian behavior. Always set `--no-install-recommends`.

### 6d. Symbols and debug info in production binaries

A Go binary without `-ldflags="-s -w"` is 2-3× larger than the stripped version. Same for C/C++/Rust.

**Fix:** Strip during build. Keep a separate symbol-server upload for debugging.

### 6e. `node_modules` with dev dependencies

`npm install` pulls dev dependencies (TypeScript, ESLint, Jest, build tools — easily 500 MB). They are not needed at runtime.

**Fix:** `npm ci --omit=dev` in production stage, or multi-stage: build in dev, copy `dist/` and `node_modules` from a `--omit=dev` install into runtime.

### 6f. CUDA images at full size

NVIDIA's `cuda:12.4-devel-ubuntu22.04` is 3.5 GB; the `runtime` variant is 1.7 GB; the `base` variant (just the driver libs) is 200 MB. For inference, you usually need `runtime` or even `base` + the specific libs.

### 6g. Caching dynamic data

`COPY ./node_modules ./node_modules`. The local `node_modules` from your laptop ends up in the image. Possibly with different binaries (macOS vs Linux). And the local dir mid-edit might have stale state. Always install inside the build, never copy from host.

---

## 7. Init, Signals, and Graceful Shutdown Failures

You see this in metrics: pods that take exactly `terminationGracePeriodSeconds` to terminate (default 30s). That's the kubelet sending `SIGTERM`, getting no response, waiting, then sending `SIGKILL`.

Three common causes:

### 7a. Shell-form ENTRYPOINT

```dockerfile
ENTRYPOINT /app
```

becomes `/bin/sh -c "/app"`. Now `sh` is PID 1, your app is PID 2. SIGTERM goes to `sh`; sh does nothing; app keeps running.

**Fix:** Exec form.

```dockerfile
ENTRYPOINT ["/app"]
```

### 7b. Apps that don't handle SIGTERM

Java apps with `-Xss` defaults work fine. Many Node apps catch `SIGINT` but not `SIGTERM` (or vice versa). Python's `gunicorn` *needs* explicit graceful-shutdown wiring.

**Fix:** In your app, handle SIGTERM, stop accepting new requests, wait for in-flight, exit 0. Test it: `docker run app & sleep 5; docker kill --signal=SIGTERM $!` — should exit within seconds, not 30.

### 7c. Subprocesses without an init

Your app spawns workers; the parent dies; the workers reparent to PID 1 (your app, if you're lucky; sh if you're not); zombies accumulate.

**Fix:** In Kubernetes, the `pause` container handles this at the pod level. In Compose, set `init: true`. In a one-off `docker run`, use `--init`.

---

## 8. Volume and Permission Disasters

### 8a. Anonymous volumes accumulating

```yaml
services:
  db:
    image: postgres:16
    volumes:
      - /var/lib/postgresql/data
```

That's an *anonymous* volume. Every `docker compose down && docker compose up -d` (or a host reboot under some conditions) leaves the volume behind, and a new one is created. Over months, you have hundreds of orphaned 50 GB Postgres volumes.

**Fix:** Named volumes:

```yaml
services:
  db:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata: {}
```

Then `docker volume prune` is safe to run; only orphaned ones get cleaned.

### 8b. Bind-mount permission mismatches

```yaml
services:
  app:
    user: 10001:10001
    volumes:
      - ./data:/data
```

`./data` on the host is owned by your laptop user (UID 501 on macOS, UID 1000 on Linux dev box). Inside the container, UID 10001 cannot write. Logs are full of permission errors.

**Fix:** Either chown the host directory to a UID the container uses, or set the container's UID to match the host (`user: "${UID}:${GID}"` in Compose). In Kubernetes, this is mostly automatic via `fsGroup`.

### 8c. Mounting `/etc` or `/var/lib` from host

```yaml
volumes:
  - /etc:/etc
```

This is "I want to share my host's config." It is "I have welded the host into the container, defeating containerization." If `/etc` matters, copy what you need into the image at build time or use ConfigMaps.

### 8d. Treating bind mounts as backup

A bind mount of `/data` to a host directory feels like a backup — the data is on the host disk. It is not a backup. The host disk can fail, the host VM can be reaped (in cloud), the bind mount can be unmounted accidentally. **Real backups are off-host, versioned, and tested via restores.**

---

## 9. Networking Mistakes

### 9a. `network_mode: host` in Compose

Disables Docker's bridge networking. The container uses the host's network stack directly. Used for performance-sensitive workloads, but:

- Port collisions with the host or other containers become silent.
- `EXPOSE` does nothing.
- The container's `localhost` is the host's `localhost` — a misconfigured app may bind to a port reachable from the public internet.

**Fix:** Use bridge networking with explicit `ports:` mappings. Use `host` only when you have measured the bridge overhead and decided you need to remove it.

### 9b. Exposing the docker bridge to the public internet

`docker run -p 0.0.0.0:5432:5432 postgres` on a host with a public IP: Postgres is reachable from the public internet on port 5432, regardless of any host firewall (Docker manipulates iptables and may bypass `ufw`/`firewalld`).

This is **the** classic "I lost my database" incident. Attackers scan port 5432 across IPv4, find your default-password Postgres, and ransom your data.

**Fixes:**

- Bind to localhost: `-p 127.0.0.1:5432:5432`.
- Use a private network: `-p $PRIVATE_IP:5432:5432`.
- Configure Docker to respect host firewall (`DOCKER_OPTS="--iptables=false"`, then write iptables rules by hand — expert mode).
- Better: don't put databases on hosts with public IPs.

### 9c. Container DNS pointing at default Docker DNS forever

In Compose, services on the same network can resolve each other by service name. Containers outside that network can't. Easy to confuse during multi-Compose-file setups.

### 9d. Port published with the default protocol mistake

`ports: ["53:53"]` only publishes TCP/53. DNS is mostly UDP. Need `["53:53/udp", "53:53/tcp"]`.

### 9e. MTU mismatches with overlay networks

In Swarm or with overlay networks, the default Docker MTU is 1450 (1500 minus VXLAN overhead). If you `docker run` your own bridge with MTU 1500 and connect it to an overlay, you get fragmentation, mysterious connection hangs, and TLS handshake timeouts at exactly 1450 bytes.

---

## 10. Logging Configurations That Eat Disks

Default Docker logging driver is `json-file`, which writes container stdout/stderr to `/var/lib/docker/containers/$CID/$CID-json.log`. By default, with **no rotation**. Your noisy web server logs 100 lines per request; after 3 weeks, you discover the host's `/` is full and the kubelet is evicting pods.

**Fix:** Configure log rotation globally in `/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "5"
  }
}
```

Better, in production: use a log driver that ships logs off-host (`fluentd`, `gelf`, `awslogs`, `journald`). In Kubernetes you generally don't touch this — log shipping is via DaemonSets that read the log files — but if you have plain Docker hosts, rotation is mandatory.

Other logging anti-patterns:

- **Logging to files inside the container.** Now `kubectl logs` shows nothing useful, and the log file is on the container's ephemeral filesystem. Always log to stdout/stderr.
- **Logging structured data as multiple `json-file` lines.** Most parsers expect one JSON per line. A multi-line Java stack trace becomes 30 separate "log records" in your aggregator.
- **Logging secrets.** Trivial to do, hard to redact later. Audit your log shipper for PII/secrets.

---

## 11. Build Context, .dockerignore, and Git Hygiene

A 2 GB build context for a 50-line Go program because `node_modules` and `target/` are in the directory:

```
[+] Building 0.1s (3/3) FINISHED
 => transferring context: 1.97GB
 => => transferring 28147 files
```

This adds seconds to every build, congests the daemon, and risks copying junk into the image.

**Fix:** A thorough `.dockerignore`:

```
.git
.github
.gitignore
.gitattributes
.idea
.vscode
*.iml
.DS_Store
Thumbs.db

# Dependencies installed locally
node_modules
**/node_modules
vendor
.venv
.env*
__pycache__
*.pyc
*.pyo
.pytest_cache
.mypy_cache
.tox

# Build outputs
dist
build
out
target
*.egg-info

# Docker files (not needed inside the image)
Dockerfile*
docker-compose*.yml
.dockerignore

# Tests and CI
coverage
.coverage
.nyc_output
test-results

# Local dev
*.log
*.bak
*.tmp
local/
secrets/
```

`docker build` reports context size. If it's >100 MB and you're not packaging an ML model, you have an ignore problem.

---

## 12. Healthchecks That Make Things Worse

### 12a. Healthcheck depends on a downstream service

```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8080/healthz"]
```

If `/healthz` returns 500 when the database is down, your container is marked unhealthy. The orchestrator restarts it. The new container also can't reach the database. Restart loop. CPU on the node is wasted on continuous restarts. Cascading failure: every service that depends on the database goes into restart loops, the node load skyrockets, other services on the node degrade.

**Fix:** Separate **liveness** (am I wedged?) from **readiness** (can I serve right now?). A wedged process means restart; a sad-but-functioning process should just be marked not-ready and traffic should route elsewhere.

In Kubernetes, this distinction is built in: `livenessProbe` vs `readinessProbe`. In Compose, `healthcheck` is conceptually a readiness check — used in `depends_on: condition: service_healthy`. Don't tie liveness to dependencies.

### 12b. Healthcheck so slow it starves the app

```yaml
healthcheck:
  test: ["CMD", "/usr/bin/run-full-smoke-test.sh"]
  interval: 5s
  timeout: 30s
```

Now you spawn a heavyweight test every 5 seconds. CPU stolen from the application. Memory spiking. Logs flooded.

**Fix:** Healthcheck is a trivial liveness ping. The smoke test belongs in CI, not in production runtime.

### 12c. Curl in distroless

```dockerfile
HEALTHCHECK CMD curl -f http://localhost:8080/healthz
```

with `FROM gcr.io/distroless/static`. No `curl` in distroless. Healthcheck always errors. Either marked permanently unhealthy or, if `start_period` is long, you only learn at production. Compile a small health probe binary into the image.

### 12d. Healthcheck on the wrong port

Common typo when the app changed ports. Tests pass (container starts), production fails (orchestrator marks unhealthy).

---

## 13. Misusing `--restart=always` as HA

```
docker run --restart=always --name api -p 8080:8080 myapp
```

This is what people do when they don't want to deploy Kubernetes. It looks like HA: if the container crashes, Docker restarts it.

Reality:

- The host dies. Your "HA" is dead with it.
- The disk fills. Container restarts but immediately exits because it can't write. CrashLoopBackOff forever, no alerting.
- The container's startup probe was wrong. It restarts every 5s, each restart taking 30s, leaking memory in the meantime.
- A bad code push panics on start. Now every node is restarting the same broken container. No automatic rollback. No canary.

**Fix:** If you need HA, you need an orchestrator: Compose with replicas behind a load balancer (Swarm, ECS), or Kubernetes. `--restart=always` is fine for single-host dev environments. It is not production HA.

The deeper lesson: **Docker is a runtime; orchestration is a different concern.** Don't conflate them.

---

## 14. Bind-Mounting `/var/run/docker.sock` ("Docker-in-Docker")

```yaml
services:
  ci:
    image: ci-runner
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

Now the container can drive the host's Docker daemon. Which means:

- It can launch privileged containers.
- It can mount any host directory (`docker run -v /:/host`).
- It is, effectively, **root on the host**.

If anything compromises the container — a bad CI job, a malicious dependency, a CVE in the image — the attacker has full host access.

**Why people do it:** "I want to build Docker images inside a container" (CI). "I want a Docker UI." "I want to run agents that orchestrate other containers."

**Fixes:**

- **Build images without the socket.** Use `buildah` or `kaniko` (rootless, daemonless builders). They run as normal users, write to the registry directly, no socket needed.
- **Use rootless Docker** if you must keep a daemon: the socket is unprivileged and breaks fewer security models.
- **Use Sysbox or Kata Containers** — runtimes that allow nested containers without exposing the host socket.
- **In Kubernetes, use Kaniko or BuildKit pods** for image builds. Privileged BuildKit pods are a separate evaluation — see ch 27 supply chain.

---

## 15. Building on the Production Host

Symptom: `docker build` runs on the same host that runs production containers. Each build chews CPU, IO, and memory, occasionally OOM-killing production workloads.

**Fix:** Builds go to a build farm (GitHub Actions, BuildKit cluster, Depot, Earthly Cloud). Production hosts pull pre-built images. CI/CD belongs in CI/CD; runtime belongs on production hosts.

A related sin: **building images in the same Compose stack as the running service.** `docker compose up --build` rebuilds and restarts the service. Convenient for dev. Terrible for prod: every reload is a fresh build with potentially different inputs, deployments are not reproducible, rollbacks require rebuilds, and your prod host now needs the source code. Build elsewhere; pull immutable images.

---

## 16. Anti-Scalable Patterns

What ceases to work as you scale containers from "a few" to "thousands":

### 16a. State inside containers

Sticky sessions held in container memory. Local file caches without coherence. A "leader" container chosen by hand. The first time you add a second replica, half the requests fail because the cache isn't shared.

**Fix:** Externalize state. Use Redis, Postgres, S3 for state; design for stateless replicas.

### 16b. Sidecar logging coupled to container lifecycle

A sidecar that buffers logs in memory and dies with the main container loses buffered logs on crash.

**Fix:** Log shipper reads from a host-level log file or via a persistent shared volume that survives the main container.

### 16c. Per-container database connections

If each container opens 100 DB connections, and you scale to 200 containers, that's 20,000 DB connections — most databases collapse. Use a connection pooler (PgBouncer, ProxySQL).

### 16d. No graceful shutdown leading to error rate spikes during deploys

If your service doesn't drain in-flight connections on SIGTERM, every rolling deploy produces a brief error rate spike. At low scale, unnoticed. At high scale, every deploy is a small outage.

### 16e. CPU/memory limits set to "infinity"

No `resources.limits` means a runaway pod can starve the node. The kubelet evicts other pods. Cascading failures.

**Fix:** Always set `requests` (for scheduling) and `limits` (for safety). Profile your app's real usage before setting them — pulling numbers out of thin air leads to either OOM kills (limits too low) or wasted capacity (requests too high).

### 16f. Health checks scale linearly with replicas

100 replicas × probe every 5s × 3 probes = 60 health requests/second. If your healthcheck is non-trivial, that's real load.

**Fix:** Keep healthchecks cheap. Cache results within the app. Use `startupProbe` to gate `livenessProbe` so noisy slow-start services don't get killed.

### 16g. One giant image, many services

"We have a monorepo, we build one image with everything in it, and CMD chooses the service." Convenient at small scale; terrible at scale because every service deploy pulls a 2 GB image even if only one binary changed.

**Fix:** One image per deployable. Multi-stage Dockerfile per service. Use BuildKit's parallel stage execution to build them concurrently.

---

## 17. Security and Compliance Anti-Patterns

A condensed checklist of patterns that fail a security review:

- **No image scanning.** No `trivy` / `grype` / `snyk` in CI. CVEs ship with every build.
- **No SBOM.** When the next supply-chain attack happens (SolarWinds, log4shell), you can't quickly answer "are we vulnerable?"
- **No image signing.** Anyone with registry write access can push a backdoored `nginx:1.25` and your nodes will happily pull it.
- **No admission controls.** Image policy at deploy time would catch unsigned/unattested/CVE-laden images before they run.
- **Public-facing images on Docker Hub** without a private mirror — rate limits will bite you, and Docker Hub's image pulls have been compromised before.
- **`docker-compose up` with a `.env` file containing secrets** committed to git. Search GitHub for "DATABASE_PASSWORD=" — you'll find tens of thousands.
- **Pulling random `bitnami/`, `linuxserver/`, `library/`** images without vetting the maintainer or pinning a digest. The supply chain extends to your image source.
- **Allowing privileged pods in production namespaces.** Should be gated by Pod Security Standards (`restricted`) or admission policy.
- **Logging credentials.** Especially in HTTP middleware that logs request bodies or headers.

---

## 18. TL;DR Checklist

If your team adopts only these rules, you'll dodge most of the chapter:

**Build:**
- BuildKit on. `# syntax=docker/dockerfile:1.7`.
- Multi-stage always; runtime image is distroless or scratch where possible.
- `.dockerignore` excludes `.git`, `node_modules`, `target`, `dist`, `.env*`, `*.log`.
- Dependencies copied before source. Cache mounts for tool caches.
- No `ARG` for secrets. Use `--mount=type=secret`.
- No `ADD` with URLs. Use `RUN curl ... | sha256sum -c -`.
- Tags pinned. Base images pinned by digest in CI.

**Image content:**
- `USER` set, non-zero numeric.
- `ENTRYPOINT` exec form. No shell wrapping.
- No `latest`. No build artifacts. No `node_modules` from the host.
- Strip binaries; clean package caches in the same `RUN`.

**Runtime:**
- No `--privileged`. Capabilities dropped to `ALL`, add only what's needed.
- `readOnlyRootFilesystem: true` plus `tmpfs` for `/tmp`.
- No `:latest` in compose/k8s manifests; use immutable tags or digests.
- Ports bound to specific interfaces, not `0.0.0.0` on public hosts.
- Log rotation configured at daemon level (or use a real log shipper).
- Named volumes, not anonymous. Volumes are not backups.

**Health and lifecycle:**
- Liveness ≠ readiness. Liveness is wedge detection; readiness is dependency awareness.
- Healthchecks cheap, fast, and not in distroless without a probe binary.
- Apps handle SIGTERM and drain gracefully.
- Compose: `init: true`. K8s: rely on `pause`.

**Pipeline:**
- Builds happen in CI, not on production hosts.
- Images scanned for CVEs and secrets before being tagged for production.
- Images signed with cosign; admission policy verifies signatures.
- SBOM and provenance attestations attached.
- Tags immutable in the registry.

**Orchestration:**
- `--restart=always` is not HA.
- One process per container.
- State outside the container.
- Resource requests/limits always set, based on measurement.
- Don't bind-mount the Docker socket into untrusted containers.

The general principle: **a container should be immutable, single-purpose, non-root, externally-stateful, signed, scanned, gracefully-shutdownable, and replaceable in one second.** Every anti-pattern in this chapter violates at least one of those properties.
