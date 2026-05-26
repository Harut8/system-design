# Docker Compose Deep Dive: From `up` to Production-Adjacent Workloads

Compose is the **dev-loop orchestrator** for the container ecosystem. The brief is: take a YAML file describing services, networks, volumes, secrets, and configs, and bring them up consistently on a single host (or a small set of hosts via remote contexts). It is the lowest-effort orchestrator that still feels like an orchestrator, and that fact makes it both popular and routinely misused.

This chapter is a staff-level walkthrough of what Compose actually does, what its YAML keys mean at the API level, where its boundaries are, and how to build Compose stacks that aren't just toys — for dev environments, CI test fixtures, integration tests, single-host deployments, and as the on-ramp to Kubernetes.

The thesis: **Compose is excellent at one host, fine for ephemeral test environments, marginal at small production, and wrong for anything that needs true HA or rolling updates with traffic management.** Knowing where the line is saves teams from running Compose past its useful range.

---

## Table of Contents

1. [What Compose Is (and Isn't)](#1-what-compose-is-and-isnt)
2. [Compose V1 vs V2 vs Compose Spec](#2-compose-v1-vs-v2-vs-compose-spec)
3. [The Project Model: Naming, Labels, and Identity](#3-the-project-model-naming-labels-and-identity)
4. [The Compose File: Sections and Semantics](#4-the-compose-file-sections-and-semantics)
5. [Networks: Bridges, Aliases, IPAM, External](#5-networks-bridges-aliases-ipam-external)
6. [Volumes: Named, Bind, Anonymous, tmpfs](#6-volumes-named-bind-anonymous-tmpfs)
7. [Secrets and Configs](#7-secrets-and-configs)
8. [Environment Variables and `.env` Files](#8-environment-variables-and-env-files)
9. [Variable Interpolation and Default Values](#9-variable-interpolation-and-default-values)
10. [`depends_on`, healthchecks, and Startup Ordering](#10-depends_on-healthchecks-and-startup-ordering)
11. [Profiles: Conditional Services](#11-profiles-conditional-services)
12. [Compose Overrides and Multiple Files](#12-compose-overrides-and-multiple-files)
13. [`extends` and Reusable Service Templates](#13-extends-and-reusable-service-templates)
14. [Build Configuration: Contexts, Targets, Args](#14-build-configuration-contexts-targets-args)
15. [Watch Mode and Compose for Dev Loops](#15-watch-mode-and-compose-for-dev-loops)
16. [Scaling, Replicas, and the Lies They Tell](#16-scaling-replicas-and-the-lies-they-tell)
17. [Resource Limits, ulimits, and sysctls](#17-resource-limits-ulimits-and-sysctls)
18. [Compose for Integration Tests](#18-compose-for-integration-tests)
19. [Compose in CI: The testcontainers Pattern](#19-compose-in-ci-the-testcontainers-pattern)
20. [Compose-to-Kubernetes: Kompose, Helm, and the Migration Path](#20-compose-to-kubernetes-kompose-helm-and-the-migration-path)
21. [Production Use of Compose: Where the Line Is](#21-production-use-of-compose-where-the-line-is)
22. [TL;DR](#22-tldr)

---

## 1. What Compose Is (and Isn't)

Compose is a CLI (`docker compose`, written in Go) that translates a declarative YAML file into a series of Docker Engine API calls. There's no Compose daemon, no Compose cluster state, no Compose distributed coordinator. It is, fundamentally, a **client-side glorified bash script** that calls `POST /containers/create`, `POST /networks/create`, `POST /volumes/create`, etc., in the right order.

Concretely, when you run `docker compose up`:

1. Compose reads `docker-compose.yml` (and any overrides), interpolates variables from `.env` and the shell environment, validates against the Compose Spec schema.
2. Computes a *project name* (derived from the directory, or `-p` flag) and labels every resource it creates with `com.docker.compose.project=<name>` so it can find them again.
3. Computes a dependency graph from `depends_on`, `links`, `volumes_from`, etc.
4. Creates networks (`docker network create --label ...`).
5. Creates volumes.
6. For each service, in dependency order:
   - Pulls or builds the image.
   - Creates and starts the container with the right network, volumes, env, healthchecks, restart policy.
   - Optionally waits for the healthcheck to pass before starting dependents.
7. Streams logs (with `--no-detach=false` not set) or returns (with `-d`).

Compose **does not**: schedule containers across hosts (that's Swarm or K8s), reconcile drift (it's not a controller — once `up` exits, it stops watching), perform rolling updates with traffic control (it does a "stop, start" or "rolling restart" with no traffic awareness), handle multi-host overlay networking on its own (that's Swarm mode), expose a REST API for external automation (Compose is a CLI; you script it).

That list defines its boundary. Everything past it is Kubernetes territory.

---

## 2. Compose V1 vs V2 vs Compose Spec

There have been three Composes:

- **Compose V1**: Python implementation, `docker-compose` binary, separate install. Deprecated since 2023.
- **Compose V2**: Go implementation, `docker compose` (note the space), shipped as a Docker CLI plugin. Default since Docker 20.10.x.
- **Compose Spec**: A *file-format specification* maintained at [compose-spec.io](https://compose-spec.io), independent of any implementation. Implemented by Compose V2, partially by Swarm, partially by Kubernetes via Kompose, partially by Podman Compose, fully by Nerdctl Compose, etc.

When you see articles referencing `version: "3.8"` at the top of a Compose file, that is **Compose V1's file format version**. The Compose Spec dropped `version:` — modern Compose files start directly with `services:`. Both still work in V2, but the `version:` key is ignored (it doesn't pick a parser).

Tooling reality check:

```bash
docker compose version     # V2 (CLI plugin)
docker-compose --version   # V1 (deprecated; only if installed separately)
```

When following an example online: if it uses `docker-compose` with a hyphen, it's V1; with a space, V2. The YAML is mostly compatible across the two, but a small set of fields (`pull_policy`, `develop`, `include`) are V2-only.

---

## 3. The Project Model: Naming, Labels, and Identity

Every Compose run has a **project name**, derived in order of precedence:

1. `--project-name <name>` / `-p <name>` flag.
2. `COMPOSE_PROJECT_NAME` env var.
3. The `name:` top-level field in the Compose file (added in the Compose Spec).
4. The basename of the current directory, lowercased, with non-alphanumeric stripped.

Every Docker resource Compose creates is labeled:

```
com.docker.compose.project=<projectname>
com.docker.compose.service=<servicename>
com.docker.compose.container-number=<index>
com.docker.compose.oneoff=False
com.docker.compose.config-hash=<sha256>
```

And every container is named `<project>-<service>-<index>` (e.g., `myapp-api-1`).

This is how `docker compose down` knows what to remove: it lists all containers/networks/volumes with the matching project label. **Two Compose stacks in two different directories with the same basename will collide.** Set `name:` or use `-p` explicitly.

The labels also enable inspection: `docker ps --filter "label=com.docker.compose.project=myapp"` shows everything Compose created for the project, regardless of which Compose file launched it.

`config-hash` is interesting: Compose computes a hash of the *effective* service config (after interpolation and overrides) and stores it. When you `compose up`, Compose compares the hash of the current config to the running container's hash. If they differ, Compose recreates the container. If they match, Compose leaves it alone. This is the core of "idempotent up" — and the reason `docker compose up` on an unchanged stack is fast.

---

## 4. The Compose File: Sections and Semantics

Top-level keys (Compose Spec):

```yaml
name: my-project          # project name (preferred over directory inference)
include:                  # include other Compose files (Spec 2024+)
  - path: ./compose.db.yml
services:                 # the heart — see below
  api: { ... }
networks:                 # named networks
  default: { ... }
volumes:                  # named volumes
  pgdata: {}
secrets:                  # secret definitions
  db_password: { file: ./db_password.txt }
configs:                  # config definitions
  nginx_conf: { file: ./nginx.conf }
```

Each service entry is rich. Here is an annotated example:

```yaml
services:
  api:
    image: myorg/api:1.5.0          # OR build:
    build:                          # Build instructions (mutually used with image: for "build-and-tag")
      context: ./api
      dockerfile: Dockerfile
      target: production            # multi-stage target
      args:
        VERSION: 1.5.0
      cache_from:
        - myorg/api:cache
      secrets:                      # build-time secrets (BuildKit)
        - npmrc
    container_name: api             # OPTIONAL — usually let Compose generate names
    restart: unless-stopped         # no | on-failure | always | unless-stopped
    pull_policy: missing            # always | missing | never | build (V2)
    init: true                      # tini as PID 1
    user: "10001:10001"
    working_dir: /app
    command: ["/app/server", "--port", "8080"]   # overrides CMD
    entrypoint: ["/app/server"]                  # overrides ENTRYPOINT
    environment:
      LOG_LEVEL: info
      DATABASE_URL: postgres://db:5432/app
    env_file:
      - ./api.env
    secrets:                        # mount runtime secrets
      - db_password
    configs:
      - source: nginx_conf
        target: /etc/nginx/nginx.conf
    ports:
      - "8080:8080"                 # host:container
      - "127.0.0.1:8443:8443"       # explicit host interface
    expose:
      - "9090"                      # internal only, for other services in the network
    networks:
      backend:
        aliases: [api, api.internal]
        priority: 100
    volumes:
      - type: bind
        source: ./api/src
        target: /app/src
        read_only: true
      - type: volume
        source: api_logs
        target: /var/log/app
      - type: tmpfs
        target: /tmp
        tmpfs:
          size: 100m
    depends_on:
      db:
        condition: service_healthy
        restart: true
      cache:
        condition: service_started
    healthcheck:
      test: ["CMD", "/app/healthcheck"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 30s
      start_interval: 1s          # V2+
    deploy:
      resources:
        limits:    { cpus: "2.0", memory: 1G }
        reservations: { cpus: "0.5", memory: 256M }
      replicas: 3                  # only honored by Swarm; see §16
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    cap_drop: ["ALL"]
    cap_add: ["NET_BIND_SERVICE"]
    security_opt:
      - no-new-privileges:true
      - seccomp=./seccomp.json
    read_only: true
    tmpfs:
      - /tmp:size=100m,mode=1777
    sysctls:
      net.core.somaxconn: 1024
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    stop_grace_period: 30s
    stop_signal: SIGTERM
    labels:
      app.team: platform
      app.version: "1.5.0"

networks:
  backend:
    driver: bridge
    driver_opts:
      com.docker.network.bridge.name: br_backend
    ipam:
      config:
        - subnet: 10.5.0.0/24
    internal: false
    attachable: true
    labels:
      app.network: backend

volumes:
  pgdata:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /srv/data/postgres

secrets:
  db_password:
    file: ./secrets/db_password.txt
    # or:
    # environment: DB_PASSWORD    # V2+ — reads from env var
    # external: true              # references a Swarm/external secret

configs:
  nginx_conf:
    file: ./nginx.conf
```

Most of this should look like a YAML version of `docker run` flags. The difference is that the *relationships* (depends_on, networks shared, secrets referenced) are first-class.

Notes on a few non-obvious keys:

- `expose:` does **not** publish ports to the host. It declares that the service listens on a port, which Compose uses to set up the network and which other services in the same network can reach by service name. `ports:` is the one that publishes externally.
- `init: true` runs tini as PID 1 in the container, which reaps zombies and forwards signals. Important for anything that spawns subprocesses.
- `stop_signal: SIGTERM` + `stop_grace_period: 30s`: Compose sends SIGTERM, waits 30s, then SIGKILL. Adjust grace period to match your app's drain time.
- `security_opt: no-new-privileges:true` prevents setuid binaries from gaining privileges. Cheap, valuable hardening.

---

## 5. Networks: Bridges, Aliases, IPAM, External

By default, Compose creates a single network named `<project>_default` and connects every service to it. Services on the same network can resolve each other by **service name** (the YAML key, not `container_name`) via Docker's embedded DNS.

When you need more structure:

```yaml
services:
  api:
    networks: [backend, frontend]
  db:
    networks: [backend]
  proxy:
    networks: [frontend]
networks:
  backend: { driver: bridge, internal: true }   # no host or external access
  frontend: { driver: bridge }
```

Now:

- `api` can reach `db` (both on `backend`) and is reachable from `proxy` (both on `frontend`).
- `db` is on an `internal` network — no NAT to the outside, no published ports possible.
- `proxy` cannot reach `db` directly. Defense in depth at the network layer.

**Aliases.** Useful for renaming a service in a specific network:

```yaml
services:
  postgres:
    networks:
      backend:
        aliases: [db, primary, db.local]
```

Now `db`, `primary`, and `db.local` all resolve to the postgres container on `backend`. Helpful when migrating from a different naming convention or when multiple apps expect different hostnames.

**External networks.** If you have a Docker network created outside of Compose (perhaps by another stack or manually), reference it as `external: true`:

```yaml
networks:
  shared:
    external: true
    name: cross-stack-net
```

Compose won't create or destroy this network; it just attaches services to it. Useful for sharing networks across multiple Compose stacks (e.g., shared observability stack reachable from multiple project stacks).

**Bridge driver options.** For performance or naming:

```yaml
networks:
  fast:
    driver: bridge
    driver_opts:
      com.docker.network.bridge.name: br_fast    # name visible in `ip link`
      com.docker.network.driver.mtu: "9000"      # jumbo frames if host supports
```

**Pitfalls:**

- **Network DNS caches.** Docker's embedded resolver has a TTL. When you `compose up --force-recreate api`, the new container has a new IP. Other containers should re-resolve. Some apps cache DNS forever (Java default before JVM tuning); they'll keep using the old IP and connection will refuse. Set `-Dsun.net.inetaddr.ttl=30` or similar.
- **Default network on `docker network ls` accumulates.** After many `compose up && compose down` cycles, you sometimes see orphan networks. `docker network prune` cleans them.
- **Cross-stack communication.** Two Compose stacks in different directories can't reach each other unless they share an external network.

---

## 6. Volumes: Named, Bind, Anonymous, tmpfs

Compose handles four kinds of mounts. The differences matter.

### 6a. Named volumes

```yaml
services:
  db:
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata: {}
```

Managed by Docker, lives in `/var/lib/docker/volumes/<name>/_data` on the host. Lifecycle independent of containers: removing the container does not delete the volume. `docker compose down -v` deletes named volumes explicitly. **Use these for service state** (databases, queues, caches).

### 6b. Bind mounts

```yaml
volumes:
  - ./src:/app/src
  - /etc/timezone:/etc/timezone:ro
```

Direct mapping of a host path into the container. Great for development (live-edit your source from the host, see changes inside the container). Terrible for portability — the host path may not exist on another machine — and dangerous for production (host filesystem is on the critical path).

Read-only flag (`:ro`) is your friend for configs you don't want the container modifying.

### 6c. Anonymous volumes

```yaml
volumes:
  - /var/lib/postgresql/data
```

Same as named, but Compose generates a random name. Almost always a mistake — orphaned anonymous volumes accumulate forever. Make it explicit:

```yaml
volumes:
  - pgdata:/var/lib/postgresql/data
volumes:
  pgdata: {}
```

### 6d. tmpfs

```yaml
tmpfs:
  - /tmp:size=100m,mode=1777
```

In-memory, ephemeral. Useful when paired with `read_only: true` on the container to give it a writable scratch space that doesn't persist.

### 6e. Long-form syntax (recommended for clarity)

```yaml
volumes:
  - type: bind
    source: ./logs
    target: /var/log/app
    read_only: true
  - type: volume
    source: data
    target: /data
    volume:
      nocopy: true   # don't copy container's existing files into the volume
```

`nocopy: true` is important: by default, when you mount a fresh named volume into a container directory that already has files in the image, Docker copies those files into the volume. That's why your Postgres data dir gets initialized correctly on first run. But for some scenarios (overlay caches), you don't want that copy.

---

## 7. Secrets and Configs

```yaml
services:
  api:
    secrets:
      - db_password
      - source: api_key
        target: /etc/api/key
        mode: 0400
secrets:
  db_password:
    file: ./secrets/db_password.txt
  api_key:
    environment: API_KEY
```

The container sees `/run/secrets/db_password` (a file containing the secret contents). Many Docker Hub images already understand this pattern: `POSTGRES_PASSWORD_FILE=/run/secrets/db_password` instead of `POSTGRES_PASSWORD=...`.

Compose's secret mounting is **a tmpfs file inside the container**, not an env variable. That's important:

- It doesn't appear in `docker inspect` env.
- It doesn't appear in `ps -ef` (env vars do, on some systems).
- It's not in image layers.

Secrets in Compose are *not* encrypted at rest (file is a plain file on the host). They are simply isolated from image and from `docker inspect`. For real secret management (rotation, audit, encryption), use Vault or AWS Secrets Manager and read at startup.

`configs:` work the same way for non-secret configuration files. The difference is intent — configs are non-sensitive, secrets are. Compose treats them identically; the distinction matters for Swarm (which has separate APIs and encrypts secrets at rest).

---

## 8. Environment Variables and `.env` Files

There are *three* environments at play in Compose. Beginners conflate them and create bizarre bugs.

1. **The host shell environment.** Variables exported in your shell.
2. **The `.env` file.** A file in the project directory, loaded by Compose at parse time.
3. **The service's `environment:` and `env_file:`.** Variables that go *into the container*.

### 8a. Host + `.env` are used for *file interpolation*

When Compose reads `docker-compose.yml`, any `${VAR}` is substituted. The lookup order:

1. Shell environment.
2. `.env` file in the project directory (or directory pointed to by `--project-directory`).
3. Default value in the file: `${VAR:-default}`.

So:

```yaml
# docker-compose.yml
services:
  api:
    image: myorg/api:${VERSION:-latest}
```

```
# .env
VERSION=1.5.0
```

After interpolation, the file effectively says `image: myorg/api:1.5.0`. The shell can override: `VERSION=1.6.0 docker compose up`.

**The `.env` file is not loaded into the container.** It's loaded into Compose itself. Beginners often write secrets in `.env` thinking they'll appear in the container — they won't, unless explicitly listed in `environment:` or `env_file:`.

### 8b. `env_file:` puts file contents into the container

```yaml
services:
  api:
    env_file:
      - ./api.env
```

The contents of `api.env` become env vars in the container. They are *not* used for interpolation in the Compose file.

The shape of these files is sensitive: each line is `KEY=value`. Quoting is **literal in V2** — `KEY="value"` makes the value `"value"` (with quotes), unlike shell. This bites people coming from `source .env`.

### 8c. `environment:` is the runtime env

```yaml
environment:
  LOG_LEVEL: info               # plain
  DATABASE_URL: ${DB_URL}       # interpolated from .env / shell
  SECRET_KEY: ${SECRET_KEY:?}   # error if unset
```

`${VAR:?}` errors at parse time if `VAR` is unset. `${VAR:-default}` provides a default. `${VAR:+x}` is the "if-set" form.

### 8d. Multiple `.env` files

Compose Spec 2024 supports `--env-file path/to/another.env` to use a non-default file, and `env_file:` accepts a list of files (later wins). Useful for layering:

```yaml
env_file:
  - ./defaults.env
  - ./local.env       # overrides defaults
```

### 8e. Common gotcha

```
docker compose up
```

vs

```
docker compose --env-file ./prod.env up
```

The second loads `prod.env` for interpolation *only* (not into containers). To put `prod.env` *into* the container too, list it under the service's `env_file:`.

---

## 9. Variable Interpolation and Default Values

The full set of interpolation operators (similar to shell parameter expansion):

| Syntax | Meaning |
|---|---|
| `${VAR}` | Required. Empty string if unset (warning logged). |
| `${VAR:-default}` | Use `default` if `VAR` is unset or empty. |
| `${VAR-default}` | Use `default` if `VAR` is unset (empty is fine). |
| `${VAR:?error}` | Fail at parse time with `error` if `VAR` is unset or empty. |
| `${VAR?error}` | Same, but empty is fine. |
| `${VAR:+x}` | `x` if `VAR` is set and non-empty, else empty. |
| `${VAR+x}` | `x` if `VAR` is set, else empty. |

Heredoc YAML, escape with `$$`:

```yaml
command: ["sh", "-c", "echo $$HOSTNAME"]
```

`$$HOSTNAME` becomes `$HOSTNAME` in the container's shell — without escaping, Compose tries to interpolate `HOSTNAME` from the host environment.

---

## 10. `depends_on`, healthchecks, and Startup Ordering

The simple form just says "start B after A":

```yaml
services:
  api:
    depends_on:
      - db
```

But "started" means "the container has been created and is running", not "the app inside is ready." Postgres might be running but still initializing for 5-10 seconds. API starts, fails to connect, exits, restarts in a loop.

The conditional form (Compose Spec):

```yaml
services:
  api:
    depends_on:
      db:
        condition: service_healthy   # wait for db's healthcheck to pass
        restart: true                # if db restarts, restart api too (V2)
      cache:
        condition: service_started   # default
      migrator:
        condition: service_completed_successfully  # wait for a one-shot
```

The three conditions:

- `service_started`: the container is running. Default.
- `service_healthy`: the container's healthcheck has succeeded at least once.
- `service_completed_successfully`: the container has exited with code 0. Used for one-shot init/migration containers.

This is the right shape. Define healthchecks on every dependency, depend on `service_healthy`. Startup is now properly ordered.

```yaml
services:
  db:
    image: postgres:16
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 10
      start_period: 30s

  migrator:
    image: myorg/migrator:1.0
    depends_on:
      db: { condition: service_healthy }
    command: ["migrate", "up"]
    restart: "no"

  api:
    image: myorg/api:1.5
    depends_on:
      db: { condition: service_healthy }
      migrator: { condition: service_completed_successfully }
```

**Important caveat:** `depends_on` is *only* used at startup. If `db` later becomes unhealthy at runtime, Compose does **not** restart `api`. Compose is not a controller; it doesn't reconcile drift. If the app needs runtime resilience, it must reconnect (which it should anyway — the network is unreliable).

`restart: true` (V2 only) does add some runtime behavior: if `db` is restarted by Compose, dependent services are restarted too. Useful for dev, dangerous for prod (cascading restarts).

---

## 11. Profiles: Conditional Services

```yaml
services:
  api: { ... }
  db: { ... }
  pgadmin:
    image: dpage/pgadmin4
    profiles: ["debug"]
  loadtest:
    image: myorg/k6
    profiles: ["test"]
```

`compose up` starts `api` and `db`. `compose --profile debug up` adds `pgadmin`. `compose --profile test --profile debug up` adds both.

Use cases:

- **Optional dev tools.** pgAdmin, Redis Commander, Mailhog — only useful sometimes.
- **Test/Load services.** Bring up k6 only when running load tests.
- **Production-only vs dev-only services.** A dev-only telemetry mock; a prod-only proxy.

A service can belong to multiple profiles (`profiles: ["debug", "test"]`). A service with no profile is always started.

Without profiles, the alternative is multiple Compose files and explicit `-f` flags, which works but proliferates files. Profiles keep one source of truth.

---

## 12. Compose Overrides and Multiple Files

When you run `docker compose up`, Compose looks for these files in order:

1. `compose.yaml` or `docker-compose.yml`
2. `compose.override.yaml` or `docker-compose.override.yml`

If both exist, Compose merges them, with the override file's values *overriding* (for scalars) or *extending* (for lists, in some keys).

Convention:

- `docker-compose.yml` — production-ready or canonical config.
- `docker-compose.override.yml` — dev tweaks (volume mounts of source, exposed debug ports, looser limits).
- `docker-compose.prod.yml` — production overrides explicitly invoked with `-f`.

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml up
```

This loads the base and applies prod overrides, without loading `override.yml` (whose presence is ignored when `-f` is used).

The merge rules are subtle:

- **Scalars** (image, restart, user, etc.): later file wins.
- **Lists** that have no key (`command:`, `entrypoint:`): later file replaces.
- **Lists with keys** (`ports:`, `volumes:`, `env_file:`): merged. To remove an entry, you can't — you must restructure.
- **Maps** (`environment:`, `labels:`): merged key by key.

When in doubt:

```
docker compose -f a.yml -f b.yml config
```

prints the *effective* config after merge and interpolation. Use it to debug surprising overrides.

The Compose Spec also adds `include:`:

```yaml
include:
  - path: ./compose.db.yml
  - path: ./compose.cache.yml
    env_file: cache.env
```

Modular Compose files for monorepos. Different from `-f` because `include` is hierarchical (each included file can have its own `include`).

---

## 13. `extends` and Reusable Service Templates

```yaml
# common.yml
services:
  base-api:
    image: myorg/api:1.5
    environment:
      LOG_LEVEL: info
    healthcheck:
      test: ["CMD", "/healthcheck"]
      interval: 10s

# docker-compose.yml
services:
  api:
    extends:
      file: common.yml
      service: base-api
    ports:
      - "8080:8080"
  api-worker:
    extends:
      file: common.yml
      service: base-api
    command: ["worker"]
```

`extends` is "inherit fields from another service." Less powerful than YAML anchors (which work too), but cleaner across files. Note: `depends_on`, `volumes_from`, `links` are *not* inherited (they'd create graph cycles).

YAML anchors are the alternative when staying in one file:

```yaml
x-defaults: &defaults
  restart: unless-stopped
  logging:
    driver: json-file
    options: { max-size: "10m" }

services:
  api:
    <<: *defaults
    image: myorg/api
  worker:
    <<: *defaults
    image: myorg/worker
```

Both patterns are fine. `extends` for cross-file, anchors for intra-file.

---

## 14. Build Configuration: Contexts, Targets, Args

```yaml
services:
  api:
    build:
      context: ./api
      dockerfile: Dockerfile
      target: production            # multi-stage target
      args:
        VERSION: ${VERSION}
        BUILDKIT_INLINE_CACHE: "1"
      cache_from:
        - myorg/api:cache
        - type=registry,ref=myorg/api:cache
      cache_to:
        - type=registry,ref=myorg/api:cache,mode=max
      secrets:
        - source: npmrc
          target: /root/.npmrc
      platforms:
        - linux/amd64
        - linux/arm64
      labels:
        org.opencontainers.image.source: https://github.com/myorg/api
        org.opencontainers.image.version: ${VERSION}
      ssh:
        - default
    image: myorg/api:${VERSION}    # tag the built image
```

`docker compose build api` builds the image and tags it as `myorg/api:${VERSION}`. `docker compose up --build` does the same, then starts.

Notes:

- BuildKit features (`cache_to`, `secrets`, `platforms`) require BuildKit (default in V2).
- `target:` lets you build a specific multi-stage target — e.g., a `dev` target with extra tools vs `prod`.
- For dev environments, prefer `image: myorg/api:dev` + `build:` so Compose builds and tags; for production, prefer `image:` referring to a pre-built tag and remove `build:`.

The dev/prod tension:

```yaml
# docker-compose.yml (works for both)
services:
  api:
    image: myorg/api:${VERSION:-latest}
    build:
      context: ./api
      target: ${BUILD_TARGET:-production}
```

- Locally: `BUILD_TARGET=dev docker compose up --build`.
- In prod: `VERSION=1.5.0 docker compose pull && docker compose up -d` — no `--build`, just pull pre-built images.

---

## 15. Watch Mode and Compose for Dev Loops

Compose V2 added `watch`, a file-change-aware reloader:

```yaml
services:
  api:
    image: myorg/api:dev
    build: ./api
    develop:
      watch:
        - action: sync          # copy changed files into container
          path: ./api/src
          target: /app/src
          ignore: [node_modules]
        - action: rebuild       # rebuild image + restart container
          path: ./api/package.json
        - action: sync+restart  # sync + restart (no rebuild)
          path: ./api/config
          target: /app/config
```

```
docker compose watch
```

Compose watches the host filesystem; on file changes, it syncs/rebuilds/restarts according to the rule. This is "hot reload for any container" — vastly better than mounting `./src` as a bind mount with the container running `nodemon`, because:

- The container's filesystem matches production (no host-leak via bind mount).
- File permissions stay clean.
- Different file paths can have different actions (package.json triggers a real rebuild; src triggers a fast sync).

This is the right way to do dev with Compose in 2025.

---

## 16. Scaling, Replicas, and the Lies They Tell

```yaml
services:
  worker:
    image: myorg/worker
    deploy:
      replicas: 3
```

If you `docker compose up`, this **does not** start 3 workers. The `deploy:` key is honored only by Swarm. Plain Compose ignores it.

To scale in plain Compose:

```
docker compose up -d --scale worker=3
```

or, post-up:

```
docker compose scale worker=3
```

Compose will start three `myapp-worker-1`, `-2`, `-3` containers on the same host. They share the network and DNS — `worker` resolves to all three (round-robin from the resolver).

**This is not real scaling.** It's local replication on one host. There's no load balancer (the Docker embedded DNS does a degree of round-robin, but it's stateless). There's no rolling update. There's no anti-affinity. There's no auto-restart on host failure (because the host is the failure boundary).

Where this is useful: a CPU-bound worker pulling from a queue, where you want N instances per host to use available cores. Where this falls down: anything that needs HA, traffic management, or cross-host placement.

Don't use Compose's `scale` to fake an orchestrator. If you need replicas with ordering, drainage, or rollout control, you've outgrown Compose.

---

## 17. Resource Limits, ulimits, and sysctls

```yaml
services:
  api:
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 1G
        reservations:
          cpus: "0.5"
          memory: 256M
    cpus: 2.0               # alternative for non-Swarm Compose
    mem_limit: 1g
    memswap_limit: 1g       # disable swap usage by setting equal to mem_limit
    pids_limit: 1024
    ulimits:
      nofile: { soft: 65536, hard: 65536 }
      nproc: 4096
    sysctls:
      net.core.somaxconn: 1024
      net.ipv4.ip_local_port_range: "1024 65000"
```

Two namespaces of resource control exist:

- `deploy.resources.*` — Compose Spec / Swarm-style. Newer.
- `cpus`, `mem_limit`, etc. at the top level — older, but still works in plain Compose.

In Compose V2 without Swarm, `deploy.resources.limits` is honored (translated to docker `--cpus` and `--memory`). `deploy.resources.reservations` is honored as `--cpus`/`--memory-reservation` (soft limits). `deploy.replicas` is *not* honored (use `--scale`).

`pids_limit` and `ulimits` are real OS-level controls. `nofile` (open file descriptors) is the one that bites in production — default is often 1024, and a busy server hits it easily. Set it to 65536.

`sysctls:` only works for the *container's* network namespace, not host. `net.core.somaxconn` is the listen backlog — most apps want 1024 or higher, default is 128 on many distros.

---

## 18. Compose for Integration Tests

The killer Compose use case: bring up the integration test fixture in CI.

```yaml
# compose.test.yml
services:
  api:
    build: .
    depends_on:
      db: { condition: service_healthy }
      kafka: { condition: service_healthy }
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: test
    tmpfs: ["/var/lib/postgresql/data"]    # ephemeral, fast
    healthcheck:
      test: ["CMD-SHELL", "pg_isready"]
      interval: 1s
      retries: 30
  kafka:
    image: confluentinc/cp-kafka:7.5.0
    environment: { ... }
    healthcheck:
      test: ["CMD", "kafka-broker-api-versions", "--bootstrap-server", "localhost:9092"]
      interval: 2s
      retries: 30
  tests:
    build:
      context: .
      target: test
    depends_on:
      api: { condition: service_healthy }
    command: ["pytest", "tests/integration"]
```

```
docker compose -f compose.test.yml up --abort-on-container-exit --exit-code-from tests
```

`--exit-code-from tests` propagates the test container's exit code as the compose exit code. `--abort-on-container-exit` brings the whole stack down when any container exits. This is the recipe for "run integration tests against a real stack of dependencies."

`tmpfs:` on Postgres makes it fast and ephemeral — your tests don't accidentally inherit state from a previous run. Healthchecks gate the tests on dependencies being ready. No flaky tests-pass-before-db-is-ready.

---

## 19. Compose in CI: The testcontainers Pattern

`testcontainers` (Java, Python, Go, etc.) is a library that programmatically spins up Compose-like environments from within tests, then tears them down. It uses the Docker socket directly — Compose under the hood for the multi-container scenarios.

The pattern:

1. Test starts.
2. testcontainers brings up a Postgres container, waits for healthy.
3. Test runs against `localhost:<ephemeral-port>`.
4. Test ends, container is removed.

Pros:

- No external state (each test gets a fresh DB).
- Parallelizable (each test gets its own port).
- Same Postgres binary as production (no SQLite-in-tests-Postgres-in-prod skew).

Cons:

- Requires Docker socket access (back to §14 of ch 40 — be careful).
- Slow startup if not pooled (Postgres takes ~3s to start; that's per-test if naive).

Compose's role here is conceptual — you use Compose locally to set up the same environment, then testcontainers in CI to script it dynamically. The container images and configurations are shared.

---

## 20. Compose-to-Kubernetes: Kompose, Helm, and the Migration Path

Inevitably, a successful Compose stack outgrows one host. The migration path:

**Option A: Kompose** — a tool that translates Compose files to Kubernetes manifests.

```
kompose convert -f docker-compose.yml
```

Produces a directory of `Deployment`, `Service`, `PersistentVolumeClaim`, `Ingress` YAMLs. Useful as a starting point but rarely correct out of the box:

- `depends_on` becomes an annotation, not a real dependency (K8s uses readiness gates differently).
- `healthcheck` becomes `livenessProbe` and `readinessProbe` — same problem of conflation as in Compose.
- `volumes` become PVCs with `ReadWriteOnce` — works on one node, fails as soon as you scale.
- `network_mode: host` doesn't translate.

Use Kompose to skeleton out manifests, then hand-edit substantially.

**Option B: Re-author in Helm.** Take the Compose file as a *spec* and write Helm charts to match. More work, much better outcome. The Compose file becomes a reference document for "what does this stack contain."

**Option C: Run Compose on Swarm-managed nodes** (transitional). Compose files mostly work on Swarm with `docker stack deploy -c compose.yml`. Gets you HA on multiple hosts without going to K8s. Then later migrate to K8s.

**Option D: Use Docker Desktop Kubernetes** or `kind` to develop directly on K8s, with Compose only for the simplest local dev. Honestly the right answer for new teams in 2025.

The translation gaps to be aware of:

| Compose | K8s equivalent | Notes |
|---|---|---|
| `service.networks` | All pods in a namespace share DNS; NetworkPolicy for segmentation | Different model — no per-service networks |
| `secrets` (file-mounted) | `Secret` + `volumeMounts` | Same shape |
| `secrets` (env) | `secretRef` in `envFrom` | |
| `configs` | `ConfigMap` + `volumeMounts` | |
| `volumes` (named) | `PersistentVolumeClaim` | RWO vs RWX matters; named volumes are RWO equivalent |
| `volumes` (bind) | `hostPath` (anti-pattern) or PVC with NFS | Bind mounts don't translate cleanly |
| `depends_on` | InitContainers + readiness gates | Different mechanism |
| `restart: unless-stopped` | Pod restart policy (always default) | |
| `deploy.replicas` | `replicas` in Deployment | |
| `healthcheck` | `livenessProbe`, `readinessProbe`, `startupProbe` | Separate concerns, finally |
| `ports` | `Service` (ClusterIP/NodePort/LoadBalancer) + `Ingress` | Different model |
| `network_mode: host` | `hostNetwork: true` | Same idea |
| `init: true` | `pause` container handles it | Built in |

---

## 21. Production Use of Compose: Where the Line Is

Can you run production on Compose? Yes — for narrow definitions of production. Here's the decision matrix:

**Compose is fine for production when:**

- Single host, no HA requirement, well-understood failure domain (one VPS, one bare metal box).
- Internal tooling, dev tools, small SaaS, hobby projects, very-low-traffic services.
- You have a process for host-level redundancy (cloud-managed instance autoscaling group of 1; if host dies, a new one comes up with the same Compose).
- State is in managed services (RDS, ElastiCache), not on the host.
- Deploys are infrequent and downtime during deploy is acceptable.

**Compose is the wrong tool when:**

- You need HA across hosts.
- You need rolling updates with traffic management.
- You need autoscaling.
- You need multi-region.
- You have more than a few hundred containers across more than 3-5 hosts.
- You need to enforce policies (admission, image signing) across many teams.
- You need observability into per-service capacity, latency, error rate at scale.

For "small production on Compose" the practical tooling:

- Compose on a host managed by Ansible/Terraform.
- Watchtower or similar to pull updated images automatically (with caution!).
- Caddy or Traefik as a Compose service for reverse proxy + TLS termination.
- Loki/Promtail for log shipping; Prometheus + node-exporter for metrics.
- Restic or borg for backups of named volumes.
- A second host with a Compose file ready to deploy on failover (manual or via DNS swap).

This is a respectable single-host stack. It's also a non-trivial amount of glue. At some point that glue becomes a worse Kubernetes than just running Kubernetes. The threshold is usually around "3+ hosts" or "you start adding orchestration logic on top of Compose by hand" — both signs that Compose is being asked to do more than it should.

---

## 22. TL;DR

- **Compose is a client-side CLI** that translates YAML into Docker Engine API calls. No daemon, no state, no controller.
- The **Compose Spec** is the file format; **Compose V2** is the modern implementation. Use `docker compose` (space), not `docker-compose` (hyphen).
- **Project name** identifies a stack via labels on every resource. Set it explicitly with `name:` or `-p`.
- Services on the same network resolve each other by **service name**. Default network is `<project>_default`.
- **Named volumes for state, bind mounts for dev**, anonymous volumes never, `tmpfs` for ephemeral scratch.
- **Secrets are tmpfs files**, not env vars. Encrypted at rest only on Swarm.
- **`.env` is for Compose interpolation**, not for the container. Use `env_file:` to put a file into the container.
- **`depends_on: condition: service_healthy`** with proper healthchecks is the right way to order startup. Compose does not enforce dependencies at runtime.
- **Profiles** for optional services; **overrides** for environment-specific tweaks; **`extends`** for service templates.
- **`develop.watch`** is the modern way to do hot reload — better than bind mounts.
- **`deploy.replicas` is ignored without Swarm.** Use `--scale` for local replication on one host. It is not HA.
- **Resource limits, ulimits, sysctls** are honored — set `nofile` high if you have a busy server.
- **Compose is the right tool for dev environments, integration test fixtures, and small single-host deployments.** Past that, Kubernetes.
- **The migration path** to K8s involves substantial hand-editing — Compose's mental model maps imperfectly onto pods, services, and PVCs. Plan for re-authoring, not auto-translation.
