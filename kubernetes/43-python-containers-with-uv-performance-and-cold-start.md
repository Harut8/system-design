# Python Containers with `uv`/`uvx`: High-Performance Images, Minimal Memory, Minimal Start Time

Python is the worst language to containerize well and the most common language to containerize badly. The defaults are user-hostile: `python:3.12` is 1 GB before you write a line of code; `pip install` is single-threaded and re-resolves the world; `requirements.txt` is unhashed; native wheels disagree with Alpine's musl; `import pandas` parses ~2,000 files before main() runs. Every layer of the stack — interpreter, packager, image, runtime — has a default that prioritizes "works for everybody" over "small, fast, secure."

This chapter is about flipping every one of those defaults. The thesis: **with `uv` as the package manager, distroless or `python:3.12-slim` as the base, multi-stage builds, AOT-compiled `.pyc`, and tuned interpreter flags, a Python service can be packaged into a 50–100 MB image that cold-starts in 200–400 ms and uses 30–40% less RSS than the naive equivalent.** Every dial along the way matters; most teams turn none of them.

If you read chapter 39 (Dockerfile best practices), this chapter is the language-specific deep dive that ties it to Python's particular pathologies. It assumes you're already comfortable with multi-stage builds, BuildKit cache mounts, and distroless bases — we'll go past those, into Python-specific territory.

---

## Table of Contents

1. [Why Python Defaults Are Hostile to Containers](#1-why-python-defaults-are-hostile-to-containers)
2. [`uv` and `uvx`: What They Are and Why They Belong in Your Dockerfile](#2-uv-and-uvx-what-they-are-and-why-they-belong-in-your-dockerfile)
3. [The `uv` Mental Model: Lockfile, Resolver, Cache](#3-the-uv-mental-model-lockfile-resolver-cache)
4. [Base Image Choice for Python: A Reality Check](#4-base-image-choice-for-python-a-reality-check)
5. [The Canonical Multi-Stage `uv` Dockerfile](#5-the-canonical-multi-stage-uv-dockerfile)
6. [Cache Mounts for `uv`: The Real Win](#6-cache-mounts-for-uv-the-real-win)
7. [`uv sync` vs `uv pip install`: Project Mode vs Pip Compat](#7-uv-sync-vs-uv-pip-install-project-mode-vs-pip-compat)
8. [Bytecode Compilation: `--compile-bytecode` and Why It Matters](#8-bytecode-compilation---compile-bytecode-and-why-it-matters)
9. [Image Size: Where the Bytes Actually Go in Python Images](#9-image-size-where-the-bytes-actually-go-in-python-images)
10. [Distroless Python: The Production Default](#10-distroless-python-the-production-default)
11. [`uvx` Inside the Build: Tools Without Polluting the Image](#11-uvx-inside-the-build-tools-without-polluting-the-image)
12. [Native Dependencies: `psycopg`, `cryptography`, `numpy`, `lxml`](#12-native-dependencies-psycopg-cryptography-numpy-lxml)
13. [Memory: Interpreter Tuning, Allocator Tuning, Per-Worker Sizing](#13-memory-interpreter-tuning-allocator-tuning-per-worker-sizing)
14. [Cold Start: The Import-Time Tax and How to Pay Less](#14-cold-start-the-import-time-tax-and-how-to-pay-less)
15. [ASGI/WSGI Server Choice: uvicorn, gunicorn, granian, hypercorn](#15-asgiwsgi-server-choice-uvicorn-gunicorn-granian-hypercorn)
16. [`__pycache__` Strategies and Read-Only Filesystems](#16-__pycache__-strategies-and-read-only-filesystems)
17. [Reproducibility with `uv lock`](#17-reproducibility-with-uv-lock)
18. [CI/CD Patterns for `uv` Builds](#18-cicd-patterns-for-uv-builds)
19. [The Gold-Standard Dockerfile, Fully Annotated](#19-the-gold-standard-dockerfile-fully-annotated)
20. [Measuring: How to Tell If You Actually Improved Anything](#20-measuring-how-to-tell-if-you-actually-improved-anything)
21. [TL;DR](#21-tldr)

---

## 1. Why Python Defaults Are Hostile to Containers

Run through the data:

- `python:3.12` (Debian-based, full image): ~1.0 GB compressed, ~1.05 GB uncompressed.
- `python:3.12-slim`: ~45 MB compressed, ~125 MB uncompressed. Drops dev headers, docs, locales.
- `python:3.12-alpine`: ~20 MB compressed, ~55 MB uncompressed. **musl libc.**
- `gcr.io/distroless/python3-debian12`: ~25 MB compressed, ~70 MB uncompressed. No shell, no package manager.

Then your dependencies pile on. `pip install fastapi sqlalchemy psycopg2-binary` adds another ~80 MB (FastAPI alone is small; `psycopg2-binary` ships a libpq inside the wheel; SQLAlchemy ships ~12 MB). Add `pandas` and you're +200 MB. Add `tensorflow` and you're at +2 GB. Add `torch` with CUDA and you're at +4 GB.

The problems compound:

- **Slow `pip install`.** pip's resolver is correct but single-threaded and SAT-solver-slow. On a 50-dep project, a clean resolve can take 20–60 seconds.
- **No content-addressed cache by default.** pip's wheel cache is per-user, not content-addressed; reinstalling the same package may or may not hit the cache.
- **`__pycache__` written at first import.** Cold start pays the `.py` → `.pyc` compilation tax every time, unless you precompile.
- **Import time dominated by `site-packages` scanning.** `import pandas` parses 1,800+ files before returning control. `import django` reads ~700.
- **No native standard for lockfiles.** `pip freeze`, `requirements.txt`, `Pipfile.lock`, `poetry.lock`, `pdm.lock`, `pip-tools` — six conventions, none built in.
- **musl vs glibc wheels split.** Many wheels are `manylinux2014` only; on Alpine, pip falls back to source builds. Now you need `gcc`, `python3-dev`, library headers — image triples in size.
- **Two-step builds without tooling support.** Out of the box, building a wheel-only image (no compilers in production) requires hand-rolled stages.

`uv` and a tight Dockerfile fix every one of these.

---

## 2. `uv` and `uvx`: What They Are and Why They Belong in Your Dockerfile

`uv` is a Python package manager and resolver, written in Rust by Astral (the makers of Ruff). In one binary, it replaces:

- `pip` (installing).
- `pip-tools` (locking).
- `virtualenv` / `venv` (env creation).
- `pipx` (running Python CLIs in isolated envs — that's `uvx`).
- Parts of `poetry` / `pdm` (project management with `pyproject.toml` + `uv.lock`).
- `pyenv` (managing multiple Python versions).

The performance claims are not marketing — `uv` is **10–100× faster than pip** on real workloads. On a fresh resolve of FastAPI + SQLAlchemy + Pydantic + uvicorn + ~20 deps, pip takes ~25 seconds; `uv` takes ~0.8 seconds. On warm cache, `uv` reuses already-extracted wheels via hardlinks and `uv sync` finishes a 50-dep install in well under a second.

Why this matters in a container:

- **CI builds drop 30–90 seconds.** At 50 builds/day, that's a meaningful CI cost.
- **Cache mounts get utilized better.** `uv` has a sane, content-addressed global cache (typically `/root/.cache/uv`) that BuildKit cache mounts handle cleanly.
- **Reproducibility is built-in.** `uv.lock` is hashed, deterministic, and resolved with a real SAT-style resolver.
- **Bytecode compilation is one flag.** `uv sync --compile-bytecode` or `uv pip install --compile-bytecode` precompiles `.pyc` at install time.
- **No interpreter required to bootstrap `uv`.** `uv` is a static Rust binary; you don't need a Python in the image to install Python. (`uv python install 3.12` will fetch a CPython if you don't have one.)

`uvx` is the run-a-Python-CLI-without-installing-it command:

```
uvx ruff check .
uvx black --check .
uvx pytest tests/
```

In a Dockerfile build stage, `uvx` lets you run code-formatters, linters, test-runners, and migration tools **without polluting the image** — the tool runs in an ephemeral venv that lives in `uv`'s cache, never in the final image. Compare to the bad pattern of `pip install pytest && pytest && pip uninstall pytest` (which leaves layer cruft regardless of the uninstall).

---

## 3. The `uv` Mental Model: Lockfile, Resolver, Cache

Three concepts:

- **`pyproject.toml`**: declares dependencies, Python version constraints, and optional dependency groups.
- **`uv.lock`**: the resolved, hashed lockfile. Committed to git. Cross-platform: contains entries for all platforms you care about.
- **Cache** (`~/.cache/uv` or `$UV_CACHE_DIR`): a content-addressed store of downloaded wheels, extracted archives, built source distributions, and resolved version info. Reused across projects.

Workflow:

```
uv init                          # bootstrap pyproject.toml + .venv
uv add fastapi sqlalchemy        # add deps; updates pyproject.toml + uv.lock
uv sync                          # install everything in uv.lock into the venv
uv lock                          # re-resolve and write uv.lock without installing
uv lock --upgrade-package fastapi  # bump a single package
uv run python -m app             # run inside the venv without `source`
```

For Docker, the key insight: `uv sync` is **fully deterministic** given `pyproject.toml` + `uv.lock` + a Python version. Two identical `uv sync` invocations on the same lockfile produce identical site-packages directories, byte for byte (modulo `.pyc` mtimes, which we'll fix in §8 and §17).

**`uv` and `--locked`:** in CI, always use `uv sync --locked` (or `--frozen`). `--locked` errors if `uv.lock` would have to change to satisfy `pyproject.toml`; `--frozen` skips re-resolving entirely. Either guarantees you're installing what the lockfile says.

```dockerfile
RUN uv sync --locked --no-dev --no-install-project
```

Three flags worth knowing:

- `--no-dev`: skip the `dev` dependency group. Production image doesn't need `pytest`, `mypy`, `ruff`.
- `--no-install-project`: don't install the project itself (yet). We'll copy source and install in a later step to get cache layering right.
- `--locked`: fail if lockfile is stale.

---

## 4. Base Image Choice for Python: A Reality Check

The honest comparison for production:

| Base | Size | Wheels | Shell | Verdict |
|---|---|---|---|---|
| `python:3.12` | ~1 GB | Both | Yes | Never for runtime. Sometimes for build stage. |
| `python:3.12-slim-bookworm` | ~45 MB | glibc | Yes | **Default for build stage.** Reasonable for runtime if you need a shell. |
| `python:3.12-alpine` | ~20 MB | musl (often missing) | Yes | Avoid unless you've audited every wheel. |
| `gcr.io/distroless/python3-debian12` | ~25 MB | glibc | No | **Production runtime default.** |
| `cgr.dev/chainguard/python:latest` | ~30 MB | glibc | No | Chainguard's distroless equivalent. Aggressively CVE-tracked. |
| `python:3.12-slim` + `uv` (no python in final) | varies | n/a | yes | If you're shipping a script. |
| `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` | ~85 MB | glibc | Yes | Bundles `uv` + Python; convenient for build stages. |

The Alpine warnings are not theoretical. Specific failures you'll encounter:

- **`psycopg2-binary` has no musl wheel.** Falls back to source build → needs `libpq-dev`, `gcc`.
- **`numpy`, `pandas`, `scipy`, `scikit-learn`** ship `manylinux` wheels first; `musllinux` wheels are newer and sometimes incomplete for older versions.
- **`cryptography`** has musl wheels but requires `rustc` to build older versions.
- **DNS** behaves differently (single-label, parallel queries — see ch 39 §3).
- **Threading** stack sizes are smaller; deep recursion in Python (e.g., parsing nested JSON) can crash.

The two-line rule: **build in `python:3.12-slim-bookworm` (or the `astral-sh/uv` image); ship in `gcr.io/distroless/python3-debian12:nonroot`.** Everything else is a deviation that should be justified.

When *not* to use distroless:

- Your app shells out to subprocesses (calls `subprocess.run(['ffmpeg', ...])`). Distroless has no ffmpeg, no shell. Either bundle the binary you need into the image (copy it from a separate stage) or use `python:3.12-slim` for runtime.
- You need to `kubectl exec` for ad-hoc debugging. Use `kubectl debug` with an ephemeral debug container instead; it's the right tool.
- Your app uses `os.system` or any `shell=True` subprocess call. Refactor that — but if you can't, you need a shell in the image.

---

## 5. The Canonical Multi-Stage `uv` Dockerfile

The starting point. We'll improve on this in later sections, but get this in your head as the baseline:

```dockerfile
# syntax=docker/dockerfile:1.7

# ----- builder ----------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (cache-friendly)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Now copy the project and install it
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ----- runtime ----------------------------------------------------------
FROM gcr.io/distroless/python3-debian12:nonroot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Copy the built venv from the builder
COPY --from=builder --chown=nonroot:nonroot /app/.venv /app/.venv
COPY --from=builder --chown=nonroot:nonroot /app/src /app/src

USER nonroot
EXPOSE 8000
ENTRYPOINT ["python", "-m", "src.main"]
```

What's happening, line by line:

- `# syntax=docker/dockerfile:1.7`: pin BuildKit frontend.
- `FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder`: starts from Astral's image which has both `uv` and Python 3.12 already, on Debian slim. Saves an `apt install` step.
- `UV_COMPILE_BYTECODE=1`: tells `uv` to compile `.py` → `.pyc` at install time. (Equivalent to passing `--compile-bytecode`.)
- `UV_LINK_MODE=copy`: forces `uv` to *copy* wheel contents into the venv rather than hardlink. Hardlinks are faster but break when the cache mount is on a different filesystem than the project — which it always is in BuildKit. Without this, you sometimes see `OSError: [Errno 18] Invalid cross-device link`.
- `UV_PYTHON_DOWNLOADS=never`: `uv` won't try to download a Python interpreter; it uses the one in the image. Saves bytes and avoids surprises.
- `PYTHONDONTWRITEBYTECODE=1`: don't write `.pyc` at *runtime* (we already did it at build time).
- `PYTHONUNBUFFERED=1`: flush stdout/stderr after every write. Critical for container logging: without this, your logs are buffered until the process exits or fills the buffer.
- `COPY pyproject.toml uv.lock` before source: cache layer for dependencies.
- `--mount=type=cache,target=/root/.cache/uv`: BuildKit cache mount on `uv`'s global cache. Massive speedup across builds.
- `uv sync --locked --no-dev --no-install-project`: install dependencies but not the project itself yet.
- `COPY src` then second `uv sync`: now the project is installed (in editable or non-editable mode, depending on pyproject config).
- Final stage: distroless Python, copies the prebuilt `.venv` and source. No `uv`, no `pip`, no shell in the runtime image.
- `USER nonroot`: distroless ships a `nonroot` user at UID 65532. Use it.

Resulting image: ~75–90 MB for a typical FastAPI/SQLAlchemy service. Compare to ~400 MB for a `python:3.12 + pip install` build of the same code.

---

## 6. Cache Mounts for `uv`: The Real Win

`uv`'s cache directory is the secret sauce. By default, on Linux it's `~/.cache/uv` (or `$UV_CACHE_DIR`). It contains:

- **Wheel cache:** downloaded `.whl` files, content-addressed.
- **Extracted wheel cache:** pre-extracted wheel contents (so install is just copy/hardlink).
- **Source distribution cache:** `.tar.gz` sdists.
- **Built wheels:** if `uv` had to build a wheel from an sdist (because no binary wheel was available), it caches the built wheel.
- **Resolver cache:** cached metadata for already-queried packages.

A BuildKit cache mount keeps this directory across builds:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
```

Cache hit rates in practice:

- **Same project, same lockfile, different code change**: ~100% hit. The `uv sync` line completes in <1 second.
- **Same project, dep version bump**: ~95% hit (only the changed package is re-downloaded).
- **Different project, overlapping deps**: significant hit (this is the global-cache magic — `numpy==1.26.4` is in the cache regardless of which project pulled it).

Two important details:

**1. `UV_LINK_MODE=copy` is required for cache mounts to work reliably.**

`uv`'s default link mode is `hardlink` on Linux: when installing a wheel into a venv, it creates hardlinks from the cache to the venv. Hardlinks are fast and disk-efficient — but **they require the source and destination to be on the same filesystem.** In BuildKit, the cache mount is a separate filesystem from the build's working directory. Hardlinking fails with `EXDEV: Invalid cross-device link`.

Set `UV_LINK_MODE=copy` (or pass `--link-mode=copy`) to force copy. Loses the hardlink speedup but gains correctness. Net result: still much faster than pip.

**2. Cache mount sharing across stages.**

If you have multiple stages doing `uv sync` (e.g., one for dev deps to run tests, another for production deps), they share the cache mount. No duplicate downloads:

```dockerfile
FROM ... AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked            # includes dev deps
RUN uv run pytest

FROM ... AS prod
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev   # production only
```

Both stages share `/root/.cache/uv`. The test stage downloads `pytest`; the prod stage doesn't install it, but `numpy` (used by both) was downloaded once.

---

## 7. `uv sync` vs `uv pip install`: Project Mode vs Pip Compat

`uv` has two main install commands:

- **`uv sync`**: project mode. Reads `pyproject.toml` and `uv.lock`. Manages a single venv (typically `.venv`). The canonical command for project workflows.
- **`uv pip install`**: pip-compatible mode. Reads `requirements.txt`. Installs into the current Python environment. Useful for legacy projects or when you don't want a `pyproject.toml`.

In a new project, use `uv sync`. In a legacy project with `requirements.txt`, you have two options:

**Option A: migrate to `pyproject.toml` + `uv.lock`.**

```
uv init --no-readme --no-pin-python
# Hand-edit pyproject.toml to add dependencies, or:
uv add $(cat requirements.txt)
uv lock
```

**Option B: keep `requirements.txt`, use `uv pip install`.**

```dockerfile
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-deps -r requirements.txt
```

`--system` installs into the system Python (no venv). `--no-deps` is dangerous unless you trust your `requirements.txt` to be fully resolved (use `uv pip compile requirements.in -o requirements.txt --generate-hashes` to produce a hashed, fully-resolved file).

For new projects, **always use `uv sync` with `pyproject.toml` + `uv.lock`.** Lockfile-driven workflows are the future; `requirements.txt` is legacy.

Bonus tip — `uv pip compile`:

```
uv pip compile requirements.in -o requirements.txt --generate-hashes
```

This is the `pip-tools` replacement. Even if you keep `requirements.txt`, generating it from `requirements.in` with `uv pip compile --generate-hashes` gives you a hashed, fully-pinned, reproducible install file. The Dockerfile can then `uv pip install --require-hashes -r requirements.txt` for hash-verified installs.

---

## 8. Bytecode Compilation: `--compile-bytecode` and Why It Matters

When Python imports a module, it:

1. Looks for a `.pyc` file (compiled bytecode) in `__pycache__/`.
2. If found and the source's mtime/hash matches: load the `.pyc`.
3. If not found or stale: parse the `.py`, compile to bytecode, write `.pyc`, then execute.

On a cold container with a read-only filesystem, step 3 fails to write but still does the parse-and-compile work — every time the process starts. For a service that imports a couple thousand modules at startup (Django + DRF + a few apps; FastAPI + SQLAlchemy + Alembic; anything ML-flavored), compilation can take **300–800 ms** at every boot.

`uv sync --compile-bytecode` (or `UV_COMPILE_BYTECODE=1`) compiles `.pyc` at install time, in the build stage. The `.pyc` ships in the image. Cold start now skips compilation entirely:

```
# Without --compile-bytecode
container start (350ms)
+ import django (220ms parse + 80ms execute)
+ import drf (180ms parse + 50ms execute)
+ ... = ~700ms before first request

# With --compile-bytecode
container start (350ms)
+ import django (45ms load .pyc + 80ms execute)
+ import drf (30ms load .pyc + 50ms execute)
+ ... = ~250ms before first request
```

Real numbers from a FastAPI service: cold start drops from ~1.2 s to ~0.4 s with bytecode compilation. **Just flip the flag.** It costs nothing at build time except a small disk increase (~10–20% more bytes for the `.pyc` files alongside `.py`).

A related trick — `PYTHONDONTWRITEBYTECODE=1` at runtime — prevents Python from writing *new* `.pyc` files at runtime. Combined with read-only root filesystems (`readOnlyRootFilesystem: true` in K8s), this means:

- Build stage compiles `.pyc`s into the image.
- Runtime has all the `.pyc`s available.
- Runtime can't write new `.pyc`s (no need to, no permission to).
- No write attempts to a read-only FS that would fail loudly.

This is the right configuration. Both flags. Always.

---

## 9. Image Size: Where the Bytes Actually Go in Python Images

Run `dive` on a typical Python service image. The breakdown:

| Component | Size | Reducible? |
|---|---|---|
| Base OS (distroless or slim) | 25–45 MB | Choose smaller base |
| Python interpreter | 25–30 MB | Already minimal |
| `site-packages` (dependencies) | 50–200 MB | Yes (next sections) |
| Your code | 0.5–5 MB | Already minimal |
| `__pycache__` | 10–30 MB | Keep for cold-start; alternative: strip `.py` |

The dependency layer is where the savings are. Specific patterns:

**Strip tests and docs from wheels.**

Many wheels ship their `tests/` directory and docs. Examples: `numpy` ships a `tests/` directory (~30 MB), `scipy` (~50 MB), `pandas` (~15 MB). These are useless in production. Strip them in the build stage:

```dockerfile
RUN find /app/.venv -type d -name tests -prune -exec rm -rf {} + \
 && find /app/.venv -type d -name __pycache__ -prune \
        -path '*/tests/*' -exec rm -rf {} + \
 && find /app/.venv -type d -name "*.dist-info" -exec sh -c \
        'rm -rf "$1/RECORD" "$1/INSTALLER" "$1/REQUESTED"' _ {} \;
```

That `find` typically saves 30–100 MB on data-science-flavored images.

**Strip the C extension `.so` debug info.**

Many wheels ship native extensions (`.so` files) with debug symbols. `strip` them:

```dockerfile
RUN find /app/.venv -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true
```

Saves 20–50 MB for ML-heavy images. The `|| true` is because some `.so`s can't be stripped further; we don't want the find to fail the build.

**Remove `__pycache__` for unused Python versions.**

If wheels ship `.pyc` files for multiple Python versions in their `__pycache__/` (look for `*.cpython-39.pyc`, `*.cpython-310.pyc`, `*.cpython-311.pyc`, etc.), strip those that don't match your runtime:

```dockerfile
# Keep only 3.12 bytecode
RUN find /app/.venv -name "__pycache__" -type d -exec sh -c \
    'find "$1" -name "*.cpython-*.pyc" ! -name "*.cpython-312.pyc" -delete' _ {} \;
```

Not common (most wheels only ship `.py`, with `.pyc` compiled at install) but worth knowing.

**Drop locale data.**

`locale/` directories under `site-packages` are huge for libraries with translations (Django, Babel, polib). If your app is English-only:

```dockerfile
RUN find /app/.venv -path "*/locale/*" \( -name "*.mo" -o -name "*.po" \) -delete
```

This is aggressive — your error messages won't be localized — but it's a 10–40 MB save for some apps.

**Beware of `pip install --no-deps` followed by manual deps.**

A common "minimize" pattern is to install only what you import. It works, but: dependency graphs are deep, and you'll get confusing import errors at runtime instead of build time. Use `--no-deps` only when you have a fully-resolved hashed requirements file.

---

## 10. Distroless Python: The Production Default

`gcr.io/distroless/python3-debian12` includes:

- The CPython interpreter (3.12 currently).
- The CPython standard library.
- glibc, ca-certificates, tzdata, /etc/passwd with a `nonroot` user.

It does NOT include:

- pip, setuptools, wheel (none of them are needed at runtime).
- A shell. No `sh`, no `bash`, no `busybox`.
- Coreutils, curl, wget.
- `apt`, `dpkg`.

For a Python service, this is fine — you're running `python -m yourapp` (or your venv's `python`). No shell needed.

Variant tags:

- `:latest` — current version, root user.
- `:nonroot` — current version, runs as UID 65532.
- `:debug` — adds a busybox shell. Useful for ad-hoc debugging in CI; **do not ship to production.**
- `:debug-nonroot` — both.

**Use `:nonroot` in production.** It maps to `runAsUser: 65532` in your pod spec.

Healthchecks in distroless: there's no `curl`. Two options:

**1. Use a Python-based healthcheck.**

```yaml
livenessProbe:
  exec:
    command: ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
  periodSeconds: 10
```

Adds ~50–80 ms per probe (interpreter startup). Fine for liveness; might be slow for readiness if you probe aggressively.

**2. Bundle a tiny health probe binary.**

Compile a 5-line Go binary that does an HTTP GET and exits 0/1. Copy it into the image. Distroless still works, probe takes <5 ms.

```dockerfile
COPY --from=build /healthcheck /healthcheck
```

```yaml
livenessProbe:
  exec:
    command: ["/healthcheck", "http://127.0.0.1:8000/healthz"]
```

`grpc_health_probe` does this for gRPC.

---

## 11. `uvx` Inside the Build: Tools Without Polluting the Image

`uvx` (alias for `uv tool run`) executes a Python CLI in an ephemeral venv, similar to `pipx run`. Critically, the tool's installation does **not** affect the project's venv — it lives in `uv`'s cache.

Use cases inside a Dockerfile:

```dockerfile
# Run formatter / linter as a build-time check
RUN --mount=type=cache,target=/root/.cache/uv \
    uvx ruff check src/

# Run tests
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uvx pytest -q tests/

# Generate type stubs
RUN --mount=type=cache,target=/root/.cache/uv \
    uvx mypy --install-types --non-interactive src/

# Pin tool version explicitly
RUN --mount=type=cache,target=/root/.cache/uv \
    uvx --from "ruff==0.6.4" ruff check src/
```

None of these tools end up in the final image because:

1. `uvx` installs into a separate venv (cache-mounted, ephemeral).
2. The final stage copies only `/app/.venv` and `/app/src`, not the `uvx` cache.

This is much cleaner than the legacy:

```dockerfile
# Bad: pollutes the venv
RUN pip install ruff && ruff check src/ && pip uninstall -y ruff
```

The `pip uninstall` doesn't remove the disk bytes from the layer (image is a stack of immutable layers). `uvx` sidesteps this entirely.

**`uv run` is the project equivalent.**

For tools that are dev dependencies of your project (in the `dev` group of `pyproject.toml`):

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked    # installs dev deps too
RUN uv run pytest
```

But in a production-image-only build, you don't want dev deps installed. Use `uvx` for one-off tool runs in test stages.

---

## 12. Native Dependencies: `psycopg`, `cryptography`, `numpy`, `lxml`

Python wheels often include C extensions. The wheel format includes prebuilt binaries for specific (Python version, platform, libc) tuples. If a wheel matching your environment exists, install is fast and stable. If not, pip/uv falls back to building from source, which needs compilers.

Common offenders and their fixes:

### `psycopg2-binary` vs `psycopg2` vs `psycopg`

- `psycopg2-binary` ships libpq inside the wheel. Easy install, but a duplicated libpq per container (security hygiene problem — when libpq has a CVE, you wait for `psycopg2-binary` to release a new wheel).
- `psycopg2` (no `-binary`) needs system libpq at install time and runtime. Requires `apt-get install libpq-dev` in build stage and `libpq5` in runtime stage. Smaller image, system-managed libpq.
- `psycopg` (psycopg3) is the modern replacement; `psycopg[binary]` is the easy version. New code should use psycopg3.

For most projects: `psycopg[binary]` is fine. For regulated environments: `psycopg` linked against system libpq.

### `cryptography`

Native wheels exist for glibc and musl on amd64 and arm64. Should "just work" with `uv` on modern bases.

If you see source builds: you've probably pinned an old version. `cryptography>=41` has comprehensive wheels.

### `numpy`, `pandas`, `scipy`

`manylinux` wheels are reliable on `python:3.12-slim`. On Alpine, you need musl wheels (sometimes available) or a source build (slow, needs OpenBLAS/LAPACK dev libraries). **Don't put data-science workloads on Alpine.**

For ML images, consider:

- Use `python:3.12-slim` with prebuilt wheels.
- Skip MKL unless you need it (MKL adds ~500 MB and rarely matters for inference).
- For GPU: use NVIDIA's CUDA base images, install Python + uv on top.

### `lxml`

Has glibc and musl wheels for current versions. Older Alpine + older lxml = source build needing libxml2-dev, libxslt-dev, gcc.

### Generic pattern for unavoidable source builds

If you must build from source, use a multi-stage:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libpq-dev libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Runtime: no compilers, only runtime libs
FROM gcr.io/distroless/python3-debian12:nonroot
# Copy runtime shared libraries
COPY --from=builder /usr/lib/x86_64-linux-gnu/libpq.so.5 /usr/lib/x86_64-linux-gnu/
COPY --from=builder /usr/lib/x86_64-linux-gnu/libxml2.so.2 /usr/lib/x86_64-linux-gnu/
COPY --from=builder /app/.venv /app/.venv
COPY src /app/src
WORKDIR /app
USER nonroot
ENTRYPOINT ["python", "-m", "src.main"]
```

The runtime image gets only the `.so` files it needs, not the dev packages. Use `ldd /app/.venv/lib/python3.12/site-packages/somepackage/_internals.cpython-312-x86_64-linux-gnu.so` to discover which libs each native extension needs.

---

## 13. Memory: Interpreter Tuning, Allocator Tuning, Per-Worker Sizing

Python's memory characteristics in containers:

- **Resident set size (RSS)** is what the kernel counts toward cgroup limits.
- Python rarely returns memory to the OS, so RSS grows toward the high-water mark and stays there.
- The default allocator (`pymalloc`) has per-thread arenas; multi-threaded apps consume more memory than single-threaded equivalents.

### Sizing: how much memory does a Python process need?

Rough order-of-magnitude:

- Bare Python 3.12 interpreter: 10–15 MB RSS.
- Plus Django + DRF + a few apps: 50–80 MB.
- Plus FastAPI + SQLAlchemy + uvicorn: 60–90 MB.
- Plus `pandas` and `numpy` imported: 120–180 MB.
- Plus a 10 MB ORM cache warmed up: + actual data size.

**Set Kubernetes `requests.memory` near the post-warmup RSS and `limits.memory` ~2× that to allow growth.** Too-tight limits cause OOMKill on transient spikes (large request bodies, ORM queries returning lots of rows, periodic batch jobs).

### `PYTHONMALLOC` and the allocator

Python has multiple memory allocators. The default `pymalloc` is fast for small allocations. For diagnostic builds, you can use `PYTHONMALLOC=malloc` to bypass pymalloc and route everything to libc malloc — useful for tracking down memory issues with tools that hook libc malloc (valgrind, address sanitizer). In production, leave it default.

### `MALLOC_ARENA_MAX` for multi-threaded workloads

glibc's `malloc` allocates per-thread arenas. By default, it can use up to **8 × num_cpus** arenas. On a 16-core machine, that's 128 arenas per process. Each arena reserves virtual address space and over time can hold significant unused memory.

For containerized Python workloads (especially uvicorn with multiple workers, or any process with many threads):

```dockerfile
ENV MALLOC_ARENA_MAX=2
```

This caps glibc arenas at 2. RSS savings can be 20–40% on multi-threaded apps. Tradeoff: slightly more lock contention on malloc, rarely measurable for normal Python workloads (the GIL serializes most allocation anyway).

### `PYTHONHASHSEED` and security

`PYTHONHASHSEED=random` (the default in Python 3) randomizes hash seeds per process. Important for security (prevents algorithmic complexity attacks against dicts).

`PYTHONHASHSEED=0` disables this. **Don't.** The performance gain is nil; the security cost is real.

### `PYTHONUNBUFFERED=1` and logging

Without this, stdout/stderr are line-buffered when attached to a TTY, block-buffered when not (pipes, files, container log streams). In a container, your stdout is piped to the container runtime → block-buffered → logs disappear until the buffer is full.

Always set `PYTHONUNBUFFERED=1`. Costs nothing.

### `PYTHONOPTIMIZE`

- `PYTHONOPTIMIZE=1`: removes `assert` statements and sets `__debug__` to False. Small startup speedup, smaller `.pyc` files.
- `PYTHONOPTIMIZE=2`: also removes docstrings. ~5–10% memory savings on doc-heavy libraries.

If you rely on assertions in production code, don't use this. If you only use asserts in tests, `PYTHONOPTIMIZE=1` is a nice tweak.

```dockerfile
ENV PYTHONOPTIMIZE=1
```

### Forking and copy-on-write with `gunicorn --preload`

If you use gunicorn with `--preload`, the master process imports all your code once, then forks workers. The workers share memory via copy-on-write (COW) — until they touch a page, which then gets duplicated. For Django/Flask apps, this can save **30–60% RSS** across all workers.

```
gunicorn --workers 4 --preload --worker-class uvicorn.workers.UvicornWorker app:application
```

The catch: any code that opens connections, file descriptors, or starts threads at import time will share them across workers, which is broken. Defer such initialization to a `post_fork` hook or worker startup.

---

## 14. Cold Start: The Import-Time Tax and How to Pay Less

Cold start is dominated by **imports**. Profile your imports:

```
PYTHONPROFILEIMPORTTIME=1 python -X importtime -m yourapp 2> import.log
```

The log shows hierarchical time spent in each import. Read it; you'll be surprised what's expensive.

Common offenders (Python 3.12 measurements on a modern Linux box):

- `import numpy`: ~80 ms (imports linalg, fft, polynomial, ma, ctypeslib, etc.).
- `import pandas`: ~250 ms (imports numpy + a hundred submodules).
- `import django`: ~150 ms (imports settings, ORM, template engine).
- `import requests`: ~50 ms (imports urllib3, charset_normalizer, idna, certifi).
- `import boto3`: ~300 ms (loads service descriptors for ~300 AWS services).
- `import google.cloud.storage`: ~400 ms (similar story).

### Reduce imports at startup

**Lazy import frequent vs rare paths.** If only 1% of requests need `boto3.client('s3')`, defer the import:

```python
def upload_to_s3(blob: bytes, key: str) -> None:
    import boto3
    s3 = boto3.client("s3")
    s3.put_object(...)
```

Now `boto3` is imported once per process (on first call), not at startup. Cold start drops by 300 ms.

**Use `__getattr__` at the module level (PEP 562) for optional features.**

```python
# package/__init__.py
def __getattr__(name):
    if name == "HeavyClass":
        from .heavy_module import HeavyClass
        return HeavyClass
    raise AttributeError(name)
```

`from package import HeavyClass` works as expected, but `import package` doesn't pull in `heavy_module`.

**Skip pkg_resources.**

`pkg_resources` (from setuptools) is notoriously slow to import (~200 ms). Any library that does `import pkg_resources` at module load pays this. The modern replacement is `importlib.metadata` (stdlib, fast). Audit your deps.

### Precompiled `.pyc` (revisited)

Covered in §8 — `UV_COMPILE_BYTECODE=1` is a free 300–500 ms cold-start win on import-heavy apps. Always do it.

### `PYTHONDONTWRITEBYTECODE=1` and the read-only filesystem

Once you have precompiled `.pyc`s in the image, you don't want runtime to write more. `PYTHONDONTWRITEBYTECODE=1` + `readOnlyRootFilesystem: true` is the right combo. Bonus: prevents an attacker from writing executable bytecode into the container.

### `python -S` (skip site)

Python's `site.py` runs at startup, scanning `site-packages` and processing `.pth` files. For ultra-lean services, `python -S -m yourapp` skips this. You then have to manually adjust `sys.path` (`uv` venvs do this via the activation scripts). Saves ~30 ms. Use it if you've optimized everything else and want the last drop.

### Threading initialization

`import threading` doesn't start threads, but `import concurrent.futures` doesn't either; however, libraries like `urllib3` initialize connection pools (with locks) at import. Largely unavoidable; just be aware.

### "Warm up" the process before declaring it ready

If your app has expensive first-request paths (loading an ML model, opening DB connection pools, JIT compiling a regex), do them at startup, not at first request. Mark readiness only after warm-up completes. Otherwise the first request after pod start is slow, which gets routed to a probe-failing pod, which marks as not-ready, which causes oscillation under autoscaling.

```python
async def startup():
    await db.connect()
    await load_model()
    app.state.ready = True

@app.get("/healthz/ready")
def ready():
    return Response(status_code=200 if app.state.ready else 503)
```

Set Kubernetes `readinessProbe` to hit `/healthz/ready`. Pod doesn't get traffic until warm.

### Cold start budget

For a typical FastAPI service: target **< 500 ms** from `docker run` to "first request served." Hitting that:

- ~100 ms image pull (if cached on node; otherwise much more).
- ~100 ms container setup.
- ~200 ms Python interpreter + imports (with precompiled .pyc).
- ~50 ms app startup (DB connection, warm-up).

Above 500 ms, you're losing autoscaling responsiveness. Above 2 seconds, you're losing user experience during scale events.

---

## 15. ASGI/WSGI Server Choice: uvicorn, gunicorn, granian, hypercorn

For HTTP/web services in containers, the server choice affects memory, CPU, and concurrency model.

| Server | Sync/Async | Multi-process | Use case |
|---|---|---|---|
| `gunicorn` | WSGI (sync) | Yes (`--workers`) | Django, Flask, classic WSGI apps |
| `uvicorn` | ASGI (async) | Single process (use `--workers` carefully) | FastAPI, Starlette, async Django |
| `gunicorn + uvicorn.workers.UvicornWorker` | ASGI (async) | Yes (gunicorn manages, uvicorn handles) | FastAPI in prod |
| `granian` | ASGI/WSGI/RSGI (Rust-backed) | Yes | Performance-critical Python web apps |
| `hypercorn` | ASGI | Yes | HTTP/2, HTTP/3 needs |
| `daphne` | ASGI | Yes | Django Channels (WebSockets) |

The right default for FastAPI in 2026: **gunicorn with uvicorn workers**, or **granian**.

```dockerfile
ENTRYPOINT ["gunicorn", \
    "-k", "uvicorn.workers.UvicornWorker", \
    "--workers", "4", \
    "--worker-tmp-dir", "/dev/shm", \
    "--bind", "0.0.0.0:8000", \
    "--access-logfile", "-", \
    "--preload", \
    "app.main:app"]
```

Notes:

- `--worker-tmp-dir /dev/shm`: gunicorn writes worker heartbeat files. By default `/tmp` (disk); `/dev/shm` (tmpfs) is faster and avoids disk I/O. Distroless doesn't have `/tmp` writable by default — mount one or use this.
- `--workers 4`: usually `2 * CPU + 1` but in containers cap based on memory and request profile. Async workers can handle hundreds of concurrent requests each, so don't over-provision workers.
- `--preload`: master imports app, forks workers. COW memory savings (see §13).
- `--access-logfile -`: log to stdout.

For pure performance, **granian** is faster than gunicorn+uvicorn:

```dockerfile
ENTRYPOINT ["granian", "--interface", "asgi", "--workers", "4", "--host", "0.0.0.0", "--port", "8000", "app.main:app"]
```

Granian is Rust-based, handles HTTP at the C/Rust layer, dispatches into Python only for the application code. ~20–40% lower CPU per request on benchmarks.

### Single uvicorn vs gunicorn+uvicorn

In Kubernetes, a common pattern is **one uvicorn per pod, scale by replica count**:

```
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Simpler than gunicorn+uvicorn.
- Each pod is one process; no preload, no master.
- Scale concurrency horizontally (more pods, fewer workers per pod).
- Memory is predictable per pod.
- Plays well with Kubernetes autoscaling.

This is increasingly the default for async services. Use gunicorn+uvicorn when you want process-level isolation within a pod (one bad worker crashes, others survive).

### Sync Django

Django (even with async views in 4+) is mostly sync. Use gunicorn with sync workers:

```
gunicorn --workers 4 --threads 2 --worker-class gthread --preload app.wsgi
```

`gthread` (gunicorn's threaded sync worker) handles I/O-bound DB queries with threads. Pure sync workers block on the DB.

---

## 16. `__pycache__` Strategies and Read-Only Filesystems

Two consistent strategies for `__pycache__`:

**Strategy A: Precompile at build, ban writes at runtime.**

```dockerfile
ENV UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1
```

```yaml
securityContext:
  readOnlyRootFilesystem: true
```

This is the recommended pattern. Bytecode is precompiled, sits in the image, runtime never writes. Read-only FS prevents an attacker from writing a webshell. Cold start is fast.

**Strategy B: Allow writes, write at first import.**

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=0
```

```yaml
volumeMounts:
  - name: pycache
    mountPath: /app/.venv/lib/python3.12/site-packages
volumes:
  - name: pycache
    emptyDir: {}
```

Not recommended in production. The first request pays the compilation cost. Subsequent requests (same pod) are fast. New pods start cold.

Strategy A wins. The "compile at runtime" strategy was a workaround for build pipelines that couldn't precompile; with `uv`, you can.

If you must use Strategy B (e.g., dynamic plugins loaded at runtime), at least don't ship a read-only filesystem.

---

## 17. Reproducibility with `uv lock`

`uv.lock` is hashed and deterministic. Two builds from the same lockfile produce identical `site-packages` directories.

For bit-identical image digests (advanced, see ch 39 §13):

```dockerfile
ENV SOURCE_DATE_EPOCH=1700000000 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Compile bytecode with deterministic mtime
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
RUN find /app/.venv -name "*.pyc" -exec touch -t 202311140000 {} +
```

`SOURCE_DATE_EPOCH` is the SDE convention for reproducible builds; many tools honor it. For Python `.pyc` files, the mtime is part of the file (used to detect stale bytecode). Forcing a consistent mtime makes `.pyc` outputs identical across builds.

Most teams don't need bit-identical images. They need **dependency-identical** images, which `uv.lock` + `--locked` gives you for free.

### Lockfile hygiene

- Commit `uv.lock` to git. **Always.**
- `uv.lock` should be in your `.dockerignore` exclude (you do want it copied into the build).
- Run `uv lock` periodically (e.g., monthly via a renovation bot) to absorb security fixes.
- In CI, fail fast if `uv.lock` is stale:

```yaml
- name: Verify lockfile
  run: uv lock --locked       # fails if pyproject.toml needs uv.lock to change
```

This catches the bug where a contributor added a dependency to `pyproject.toml` but forgot to `uv lock`.

---

## 18. CI/CD Patterns for `uv` Builds

### GitHub Actions

```yaml
name: Build
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:${{ github.sha }}
            ghcr.io/${{ github.repository }}:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
          provenance: true
          sbom: true
          platforms: linux/amd64,linux/arm64
```

`cache-to type=gha,mode=max` exports all stages' caches; `cache-from type=gha` restores them. Combined with `uv`'s own cache mount, builds drop from minutes to ~30 seconds on cached deps.

### Testing in the same Dockerfile

```dockerfile
FROM ... AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
COPY tests ./tests
RUN uv run pytest -q --tb=short

FROM ... AS build
# ... production stages
```

```
docker buildx build --target test .          # CI: tests run
docker buildx build --target build .         # Deploy: tests are skipped (separate target)
```

`--target test` builds up to the test stage and stops. `--target build` skips the test stage entirely. CI runs both: tests first (gate), then build (the artifact).

A more concise variant — tests inside the build stage:

```dockerfile
FROM ... AS builder
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

COPY src ./src
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uv run pytest -q

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev    # remove dev deps for the final venv
```

Tests run as part of the build; failures abort the build. Dev deps are uninstalled afterward.

---

## 19. The Gold-Standard Dockerfile, Fully Annotated

Putting everything together for a typical FastAPI service:

```dockerfile
# syntax=docker/dockerfile:1.7
# ============================================================================
# Stage 1: Builder
#   - Astral's uv-prebuilt image (Debian slim + Python 3.12 + uv).
#   - Installs deps with cache mount, compiles bytecode at install time.
#   - Strips tests, docs, locales from site-packages.
# ============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Project layer
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Slim the venv
RUN find /app/.venv -type d -name tests -prune -exec rm -rf {} + \
 && find /app/.venv -type d -name "*.dist-info" -exec sh -c \
        'for d; do rm -f "$d/RECORD" "$d/INSTALLER" "$d/REQUESTED"; done' _ {} \; \
 && find /app/.venv -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true \
 && find /app/.venv -path "*/locale/*" \( -name "*.mo" -o -name "*.po" \) -delete

# ============================================================================
# Stage 2: Test (optional, run via --target test)
# ============================================================================
FROM builder AS test

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked        # bring in dev deps
COPY tests ./tests
RUN uv run pytest -q tests/

# ============================================================================
# Stage 3: Runtime
#   - Distroless Python, nonroot.
#   - Only the built venv + source.
#   - No shell, no package manager.
# ============================================================================
FROM gcr.io/distroless/python3-debian12:nonroot AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1 \
    PYTHONHASHSEED=random \
    MALLOC_ARENA_MAX=2 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY --from=builder --chown=nonroot:nonroot /app/.venv /app/.venv
COPY --from=builder --chown=nonroot:nonroot /app/src /app/src

USER nonroot
EXPOSE 8000

# Bind to 0.0.0.0 inside the pod; service maps the port.
ENTRYPOINT ["python", "-m", "uvicorn", "src.main:app", \
            "--host", "0.0.0.0", "--port", "8000", \
            "--log-config", "/app/src/log-config.json", \
            "--no-access-log"]
```

Annotations:

- **Cache mounts** on the global `uv` cache mean repeated builds reuse downloaded wheels.
- **`--no-install-project`** in the first sync gives a clean dependency layer; project install in the second sync is fast and re-runs only when source changes.
- **`--locked`** ensures `uv.lock` is the source of truth (build fails if it's stale).
- **Strip tests/dist-info/locales** trims ~30–80 MB.
- **`strip --strip-unneeded`** on `.so` files removes debug info.
- **Distroless nonroot** runtime; no shell, no pip, no curl. Smallest reasonable surface.
- **`MALLOC_ARENA_MAX=2`** for memory hygiene.
- **`PYTHONOPTIMIZE=1`** drops asserts, small startup win.
- **`PYTHONHASHSEED=random`** explicit (defensive, also the default).
- **Single uvicorn process per pod**, scale via replicas.
- **`--no-access-log`** for low CPU overhead; ship access logs from a sidecar if needed.

Expected result for a FastAPI service with ~30 deps:

- Image size: **70–90 MB compressed.**
- Cold start (from `docker run` to first response): **~300–500 ms.**
- RSS under idle: **~80 MB.**
- RSS under load: **~150–200 MB.**

Compare to the "naive" version (`python:3.12` + `pip install`):

- Image size: ~600–800 MB.
- Cold start: ~1.5–2.5 s.
- RSS under idle: ~120 MB.
- RSS under load: ~200–300 MB.

**4–10× smaller image, 3–5× faster cold start, 20–30% lower RSS.** Same code.

---

## 20. Measuring: How to Tell If You Actually Improved Anything

Don't optimize without measurement. The relevant numbers:

### Image size

```
docker images myapp:latest --format "{{.Size}}"
docker inspect myapp:latest | jq '.[0].Size'      # uncompressed
docker image history myapp:latest
dive myapp:latest                                   # layer-by-layer with score
```

For a quick check that you're not adding bloat: `dive` shows "wasted space" — files added and later removed, which still occupy bytes.

### Cold start

```
time docker run --rm -it myapp:latest python -c "from src.main import app"
```

Roughly captures import time. For real cold start, time from container start to first 200 OK from the readiness probe:

```bash
# In a test cluster
START=$(date +%s.%N)
kubectl run test --image=myapp:latest --restart=Never -- python -m uvicorn src.main:app
kubectl wait --for=condition=ready pod/test --timeout=60s
END=$(date +%s.%N)
echo "Cold start: $(echo "$END - $START" | bc) seconds"
```

### Memory

```
# Inside the pod
cat /sys/fs/cgroup/memory.current      # cgroup v2
cat /sys/fs/cgroup/memory/memory.usage_in_bytes   # cgroup v1

# From Python
import resource; resource.getrusage(resource.RUSAGE_SELF).ru_maxrss   # in KB
```

Memory profiling tools: `memray`, `tracemalloc`, `py-spy`. `memray` is the modern choice — accurate, low overhead, produces good flame graphs.

### Import time

```
python -X importtime -c "import src.main" 2> imports.log
# Sort by cumulative time:
awk -F'|' '{print $2, $4}' imports.log | sort -n
```

Anything above 50 ms per import deserves investigation.

### Continuous tracking

In production:

- **Image size** in CI: fail the build if size grows >20% week-over-week.
- **Cold start** in observability: tag pod start events with a histogram bucket, alert on regressions.
- **RSS** as a standard cluster metric: alert when p99 approaches memory limit.

Don't trust "I made it smaller" without numbers. Don't trust "it's faster now" without a probe time histogram.

---

## 21. TL;DR

- **Use `uv`** (not pip, not poetry) for Python dependency management in containers. 10–100× faster, deterministic lockfile, BuildKit-friendly cache.
- **Use `uvx`** for one-shot CLI tools inside the build (ruff, pytest, mypy) — they don't pollute the image.
- **Multi-stage build with `python-bookworm-slim` (or `astral-sh/uv`) as builder, distroless as runtime.** 70–100 MB images are routine.
- **Cache mounts on `/root/.cache/uv`** with `UV_LINK_MODE=copy`. Required for correctness; gives most of the speedup.
- **`UV_COMPILE_BYTECODE=1` + `PYTHONDONTWRITEBYTECODE=1` + `readOnlyRootFilesystem: true`** — precompile at build, ban writes at runtime, ~300–500 ms cold start savings.
- **Strip tests, locales, `.dist-info` cruft, and debug symbols from native `.so`s** — 30–80 MB image savings.
- **Avoid Alpine for Python** unless you've audited every wheel's musl availability. Use `python:3.12-slim-bookworm` build, distroless runtime.
- **`MALLOC_ARENA_MAX=2`** in multi-threaded apps for 20–40% RSS reduction.
- **`PYTHONUNBUFFERED=1`, `PYTHONOPTIMIZE=1`, `PYTHONHASHSEED=random`** — always.
- **Lazy-import heavy modules** (boto3, pandas, ML libs) from request handlers, not at app startup.
- **One uvicorn per pod, scale via replicas** for async services; gunicorn+uvicorn workers with `--preload` for COW memory savings if you want multi-process per pod.
- **Healthchecks via tiny Go probe or `python -c "import urllib.request,sys; urllib.request.urlopen(...)"`** — no curl in distroless.
- **Commit `uv.lock`. Use `--locked` in CI.** Fail builds if lockfile is stale.
- **Measure before claiming improvement.** Image size with `dive`, cold start by timing pod-ready, memory by `memray` or cgroup files.

The Python-in-Docker default in 2026 is `uv` + distroless + multi-stage + bytecode precompilation. Anything else is leaving 50–90% of the performance and 50–80% of the image size on the table.
