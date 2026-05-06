# 42 — Python Observability

> Python is the most-instrumented language on Earth and the most-misinstrumented one. The same ecosystem that gives you `pip install opentelemetry-distro` in 30 seconds also gives you: a GIL that distorts CPU profiling, a `fork()` model that breaks Prometheus client state, four different async frameworks each with a different context propagation contract, four web servers each with a different worker model, and a culture of monkey-patching that makes auto-instrumentation either magic or landmine. This chapter is the staff-level Python-specific guide: what to install, what to configure, what to never do, and what the *correct* defaults look like for production Python services.

This document is a Python-only specialization of doc 03 (instrumentation), doc 04 (collection), doc 06/07/08 (storage), and doc 09 (profiling). It assumes you already understand what a histogram, span, structured log, and tail sampler are. If not, read 00 → 03 first.

The two rules that make 80% of Python observability problems disappear:

1. **Pick a worker model and instrument *for it***. Multi-worker (Gunicorn/uWSGI prefork) is not the same as single-process async (Uvicorn `--workers 1`) is not the same as Celery prefork is not the same as `asyncio` in one process. Each requires a different metrics export mode, a different context propagation strategy, and a different profiling tool. The single largest source of broken Python observability is using the single-process default with a multi-worker server.
2. **Standardize on OpenTelemetry as the SDK for traces and metrics, `structlog` (or `logging` with a JSON formatter) for logs, and `prometheus_client` only for metrics scraped from `/metrics`**. Mixing OTel metrics, `prometheus_client`, `statsd`, and a custom `Counter` class — which is the actual state of most Python codebases — produces overlapping series, divergent attribute names, and a debugging tax that compounds.

If you remember nothing else: **OTel SDK + `structlog` + Prometheus multiprocess + Gunicorn workers ≥ CPU count + py-spy/austin for profiling**. The rest of this chapter explains why and what the pitfalls are.

---

## Table of Contents

1. [Why Python is Uniquely Hard](#1-why-python-is-uniquely-hard)
2. [The Python Observability Stack: Recommended Defaults](#2-the-python-observability-stack-recommended-defaults)
3. [OpenTelemetry SDK in Python: Architecture and Setup](#3-opentelemetry-sdk-in-python)
4. [Logging: structlog, contextvars, and the trace_id Bridge](#4-logging-structlog-contextvars-trace-id)
5. [Metrics: prometheus_client, Multiprocess Mode, and OTel Metrics](#5-metrics-prometheus-multiprocess-otel)
6. [Tracing: Context Propagation Across Sync, Async, Threads, Subprocesses](#6-tracing-context-propagation)
7. [Profiling Python in Production](#7-profiling-python-in-production)
8. [Framework-Specific Instrumentation](#8-framework-specific-instrumentation)
9. [Worker Models: Gunicorn, Uvicorn, uWSGI, Hypercorn](#9-worker-models)
10. [Async, Threads, and the GIL: Implications for Telemetry](#10-async-threads-gil)
11. [Celery, RQ, Dramatiq: Background Workers](#11-celery-rq-dramatiq)
12. [Data and ML Workloads: Pandas, Spark, PyTorch, Notebooks](#12-data-and-ml-workloads)
13. [Performance Overhead and Sampling](#13-performance-overhead-and-sampling)
14. [PII, Redaction, and Secret Hygiene in Python](#14-pii-redaction-secrets)
15. [Testing Observability: Unit, Integration, Pre-flight in CI](#15-testing-observability)
16. [Packaging, Versioning, and Rollout Discipline](#16-packaging-versioning-rollout)
17. [A Complete Production-Shaped Example: FastAPI Service](#17-complete-example)
18. [Process Lifecycle, Races, Restarts, and Crash Edge Cases](#18-lifecycle-races-restarts)
19. [Anti-Patterns: The Python Hall of Shame](#19-anti-patterns)
20. [Pitfalls and Edge Cases](#20-pitfalls-and-edge-cases)
21. [The Staff-Level Standards Checklist](#21-staff-level-standards-checklist)

---

## 1. Why Python is Uniquely Hard

Other languages have observability problems. Python has *Python's* observability problems, on top of those. A staff engineer should know which ones bite and why.

### 1.1 The fork-based worker model breaks naive instrumentation

The classic Python deployment is Gunicorn / uWSGI in **prefork** mode: parent process loads the app, then `fork()`s N workers. Each worker has:

- An independent counter for `prometheus_client.Counter` — scraping `/metrics` from the parent returns one worker's view.
- An independent OpenTelemetry SDK state — span batches that should flush together end up split across workers.
- An independent connection pool — DB pool size in dashboards is per-worker, not per-pod.

This is not a bug. It is the consequence of process isolation. The fix is **multiprocess mode** (§5.2) and **workers receive an init hook** (§9.1). Default code without this fix produces metrics that lie.

### 1.2 The GIL distorts CPU profiling

Python's reference implementation (CPython) runs only one bytecode-executing thread at a time. CPU samplers like `perf` see one Python thread doing work and N-1 threads parked in `take_gil()` — which is *not* the same as those threads being idle from the user's perspective. Tools that don't speak Python natively (e.g., raw `perf`) produce flame graphs dominated by `take_gil` and `pthread_cond_wait` and tell you nothing useful.

The fix is to use Python-aware profilers (py-spy, austin, pyspy, scalene, pyinstrument) that walk the Python frame stack via the eval loop, not the C stack. See §7.

### 1.3 Context propagation in async is a contract

`asyncio` does not propagate thread-local state across `await` points. This means:

- `threading.local()` does not carry a `trace_id` through `await db.fetch()`.
- The naive "stash trace_id on `request.state`" pattern stops working when you span across `asyncio.gather()`.

Python solved this with `contextvars` (PEP 567, Python 3.7+). OpenTelemetry uses `contextvars` underneath; `structlog`'s contextvars binder exists for this reason. **Any logging or tracing library that uses `threading.local()` instead of `contextvars` is broken in async code.** Validate before adopting.

### 1.4 Monkey-patching auto-instrumentation: magic or landmine

`opentelemetry-instrument` swaps in patched versions of `requests.get`, `psycopg.connect`, `redis.Redis.execute_command`, etc. This works beautifully — until:

- A library is imported before the instrumentor (no patches applied).
- A library uses a private internal API the patch didn't account for (silent miss).
- A library version moves a method (`from_url` → `Redis.from_url`) and the patch breaks (raises at import time).

The fix is *deterministic instrumentation order* (§3.5) and *integration tests that assert spans exist* (§15.3).

### 1.5 The "many small libraries" problem

A typical Python service has 80+ direct dependencies. Each can:

- Log at INFO level on every request (drowns logs).
- Re-emit the same metric with a different name (`requests_total` and `request_count`).
- Open its own connection pool (which doesn't show up in your dashboards).
- Use `print()` instead of a logger (bypasses redaction).

The discipline is **fence the boundary**: ingress, egress, DB, queue. Then audit dependency log levels at startup. See §13.

### 1.6 Notebooks and scripts have no instrumentation by default

Half of Python in production is ML training, ETL scripts, and `cron`-launched batch jobs. These have:

- No `/metrics` endpoint (nothing to scrape).
- Often no logger setup beyond `print()`.
- Lifecycles measured in hours; a `OTLPSpanExporter` that flushes on `atexit` is not enough.

The pattern is **push-mode metrics** (Pushgateway or OTLP) and **structured stdout logs** parsed by the log shipper. See §12.

---

## 2. The Python Observability Stack: Recommended Defaults

For a new Python service in 2026, the staff-level recommendation is below. Each line is *opinionated* — if you choose differently, document the reason in your platform's golden-path README.

| Concern | Recommended | Acceptable alternatives | Avoid |
|---|---|---|---|
| **Tracing SDK** | `opentelemetry-sdk` + `opentelemetry-exporter-otlp` | Sentry tracing (if Sentry is the trace store) | `jaeger-client` (deprecated), Zipkin native client, custom |
| **Logging library** | `structlog` ≥ 24.x with `structlog.contextvars` | `logging` + `python-json-logger`, `loguru` | `print()`, raw f-string-formatted `logger.info("user %s did %s", ...)` |
| **Metrics library** | `prometheus_client` for `/metrics`; OTel Metrics SDK if remote-pushing | `statsd` only if forced by infra | Custom counter classes; `metrics.gauge()` from your own utils module |
| **Auto-instrumentation** | `opentelemetry-distro` + `opentelemetry-bootstrap` for selective enablement | Manual instrumentation per library | `opentelemetry-instrument` blanket wrap on a poorly-known dependency tree |
| **Profiling (continuous)** | py-spy with Pyroscope/Parca; austin in CI | pyinstrument (request-scoped); memray for memory | cProfile in production (overhead too high) |
| **Trace exporter** | OTLP/gRPC to a local OTel Collector (sidecar or daemonset) | OTLP/HTTP if gRPC is blocked | Direct vendor SDK from app code (couples to vendor) |
| **Web server** | Gunicorn (sync) or Uvicorn (async) with explicit `--workers N` | Hypercorn for HTTP/3, mod_wsgi for legacy | uWSGI for new services (operational quirks); `python -m http.server` (debug only) |
| **Async framework** | FastAPI (HTTP), gRPC-Python (RPC), Celery (jobs) | Starlette directly, AIOHTTP, Sanic, Quart | Mixing sync DB drivers with async web framework (easy footgun) |
| **DB drivers** | `asyncpg` (Postgres async), `psycopg[c]` 3.x (Postgres sync), `redis-py` async, `motor` (Mongo async) | `psycopg2` if you must | `aiopg` (deprecated), pure `sqlite3` in async code without `aiosqlite` |
| **Test instrumentation** | `pytest-otel`, `OTel InMemorySpanExporter` fixture | Snapshot tests on JSON span output | Asserting on log strings (brittle) |

Two non-obvious notes:

- **`structlog` over `loguru`**. `loguru` is one-import beautiful but its sink architecture is opaque, it doesn't play well with `logging`-based libraries (which is most of them), and its "magic" `{user_id}` interpolation is exactly the kind of unbounded-attribute pattern you want to *avoid* for cardinality. `structlog` integrates cleanly with `logging`, has first-class contextvars support, and renders to JSON for the log shipper without ceremony.
- **`prometheus_client` for `/metrics`, OTel Metrics for OTLP push**. The two are not exclusive. If you scrape, use `prometheus_client` (battle-tested, simple). If you push to a collector (Lambda, Cloud Run, batch jobs that don't live long enough to be scraped), use OTel Metrics SDK. Use both only if you have a reason — and document it.

---

## 3. OpenTelemetry SDK in Python

### 3.1 The SDK module landscape

OpenTelemetry Python ships as ~30 packages. The minimum useful set for a new service:

```
opentelemetry-api                       # The interfaces (always)
opentelemetry-sdk                       # The implementations (always)
opentelemetry-exporter-otlp             # gRPC and HTTP OTLP exporters
opentelemetry-distro                    # Convenience wiring
opentelemetry-instrumentation-fastapi   # Per framework
opentelemetry-instrumentation-requests
opentelemetry-instrumentation-psycopg
opentelemetry-instrumentation-redis
opentelemetry-instrumentation-logging   # Inject trace_id into log records
```

Pin them all to the *same* version. The `api` and `sdk` versions diverging (api 1.27 + sdk 1.25) is one of the more common debugging dead-ends — your spans are silently dropped because the SDK doesn't recognize the API surface.

### 3.2 Programmatic SDK setup (the explicit version)

```python
# observability/setup.py — call once at process start, before importing the app.
import os
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.b3 import B3MultiFormat
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator


def setup_telemetry() -> None:
    resource = Resource.create({
        SERVICE_NAME: os.environ["OTEL_SERVICE_NAME"],
        SERVICE_VERSION: os.environ.get("APP_VERSION", "unknown"),
        "deployment.environment": os.environ.get("ENV", "unknown"),
        "service.instance.id": os.environ.get("HOSTNAME", "unknown"),
    })

    # --- Traces ---
    tracer_provider = TracerProvider(
        resource=resource,
        # Head sampling: keep 100% of root traces if env=staging, else 10%.
        # Tail sampling happens at the collector — see doc 04.
        sampler=ParentBased(root=TraceIdRatioBased(
            float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.10"))
        )),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]),
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        )
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]),
        export_interval_millis=15000,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    # --- Propagators ---
    # W3C Trace Context is the modern default; Baggage carries cross-cutting attrs;
    # B3 is required only if you have older Zipkin-aware services in the call graph.
    set_global_textmap(CompositePropagator([
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
        B3MultiFormat(),  # remove if not needed
    ]))
```

This explicit setup is what you want in a production service. The `opentelemetry-instrument` autoloader is acceptable for prototypes; for a service you own, write the wiring above so you can read what's running.

### 3.3 Resource attributes: what every Python service must set

`SERVICE_NAME` is the bare minimum. For staff-level standards, also set:

- `service.version` — git SHA or semver. Without this, "did the bug start at deploy X?" is unanswerable.
- `deployment.environment` — `prod | staging | dev`. Filtering everything by environment is the single most-used Grafana dashboard variable.
- `service.instance.id` — pod name or hostname. Required to debug "one replica is bad."
- `service.namespace` — team or domain (`payments`, `search`). Enables multi-tenant cost attribution.
- `process.runtime.name = "cpython"`, `process.runtime.version = sys.version`. Useful when the fleet is heterogeneous (Python 3.10 and 3.12 simultaneously) and a bug is version-specific.

OTel's [SemConv resource attributes](https://opentelemetry.io/docs/specs/semconv/resource/) are the contract; do not invent your own (`my_service`, `app_env`, `version_str`). See doc 34 on schema governance.

### 3.4 The selective auto-instrumentation pattern

`opentelemetry-instrument python app.py` blindly wraps everything in your dependency tree. This is fine for a hello-world, dangerous for a service with 80 deps. Prefer the **selective** pattern:

```python
# observability/instrumentation.py
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor


def install_instrumentors(app) -> None:
    # Each line below is a deliberate choice. Audit it on every dep upgrade.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/healthz,/metrics,/readyz")
    PsycopgInstrumentor().instrument(enable_commenter=True, commenter_options={
        "db_driver": True, "opentelemetry_values": True,
    })
    RedisInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    LoggingInstrumentor().instrument(set_logging_format=False)  # we handle format ourselves
```

`enable_commenter=True` on the Postgres instrumentor adds `/* traceparent='00-...' */` to every SQL statement. This is the [SQLCommenter](https://google.github.io/sqlcommenter/) standard and is the *single most useful* tracing-DB integration you can enable: now `pg_stat_statements` shows the trace_id of the slowest query, joining trace store ↔ database in one SQL filter. See doc 23.

`excluded_urls` matters: scraping `/metrics` and `/healthz` produces a span on every probe and floods the trace store. Always exclude.

### 3.5 Instrumentation order

The single most-asked Python OTel question is "why are some spans missing?" The answer is almost always **import order**.

```python
# WRONG — requests is imported before the instrumentor patches it.
import requests
from opentelemetry.instrumentation.requests import RequestsInstrumentor
RequestsInstrumentor().instrument()
# Now requests.get() is NOT instrumented in this module's import scope.

# RIGHT — set up SDK + instrumentors *before* importing app code.
from observability.setup import setup_telemetry
setup_telemetry()
from observability.instrumentation import install_instrumentors

import app  # app imports requests inside; the patched version is what it gets.
install_instrumentors(app.fastapi_app)
```

The discipline: have a single `bootstrap.py` that runs SDK + instrumentors first, then imports the application. Your `gunicorn` `--preload` or `if __name__ == "__main__"` calls bootstrap.

---

## 4. Logging: structlog, contextvars, trace_id

### 4.1 The two patterns to choose between

There are two valid Python logging architectures in 2026:

1. **`structlog` as the front-end + `logging` as the back-end.** `structlog`'s loggers are wrappers around `logging.Logger`; you keep the standard library's handlers, levels, and library compat, but get structured-first ergonomics and contextvars binding. **This is the recommended default.**
2. **`logging` + `python-json-logger` formatter.** Pure stdlib, no new dep. Works fine; you write more boilerplate for context binding (`extra={...}` on every call); fine for small services.

`loguru` is *not* in this list. It's lovely for personal scripts and rejects the `logging` ecosystem in ways that hurt at staff-engineer scale.

### 4.2 Production-shaped `structlog` setup

```python
# observability/logging_setup.py
import logging
import sys
import structlog
from opentelemetry import trace


def add_trace_context(logger, method_name, event_dict):
    """Inject trace_id and span_id from the active OTel context."""
    span = trace.get_current_span()
    if span is None:
        return event_dict
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
        event_dict["trace_flags"] = format(ctx.trace_flags, "02x")
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    pre_chain = [
        structlog.contextvars.merge_contextvars,  # async-safe context binding
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=pre_chain + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging (used by libraries) through structlog's renderer.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=pre_chain,
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)

    # Tame noisy libraries that log at INFO by default.
    for noisy in ("urllib3", "botocore", "aiokafka.consumer.subscription_state"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
```

The two non-obvious parts:

- **`merge_contextvars` first.** This must run before any binding processor so that values bound via `structlog.contextvars.bind_contextvars(...)` show up in every log line in the current async context.
- **`add_trace_context` reads from OTel.** This is the bridge that puts `trace_id` on every log line *without* requiring engineers to remember to pass it. The presence of this single processor is why "logs ↔ traces" join queries work in your Loki/ClickHouse later.

### 4.3 The contextvars binding pattern

Bind once per request, log freely throughout the call stack. Use middleware:

```python
# In a FastAPI middleware:
import structlog
from fastapi import Request

@app.middleware("http")
async def bind_request_context(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request.headers.get("x-request-id", ""),
        route=request.scope.get("route").path if request.scope.get("route") else request.url.path,
        method=request.method,
        # NOT user_id here — bind that *after* auth middleware runs.
    )
    return await call_next(request)
```

After auth, in the auth middleware:

```python
structlog.contextvars.bind_contextvars(user_id=user.id, tenant_id=user.tenant_id)
```

Now every log line in the handler — including library logs that flow through the stdlib bridge — carries `request_id`, `route`, `method`, `user_id`, `tenant_id`, `trace_id`, `span_id`. Zero ceremony at the call site.

### 4.4 What to log, what not to log

| Always log | Never log | Sometimes log |
|---|---|---|
| Request begin/end (or rely on auto-instr) | Full request body | Truncated body on error (with redaction) |
| Auth decisions (success/fail/reason class) | Passwords, tokens, API keys, credit cards | Email at INFO when user-explicit (consent) |
| Boundary-crossing errors with context | `print()` debug statements | Stack traces at ERROR but not at INFO |
| Slow-query / slow-call events with exemplar | Per-row of a 100k-row dataframe | Per-batch of an N-batch job |
| Retry attempts with reason | Health check probes (suppress at access log) | Background-job lifecycle events |
| Circuit-breaker state changes | Library debug logs unless triaging | Feature-flag evaluations |

The rule: **logs scale with volume of business activity, not with code paths**. A loop should not log per-iteration; it should log per-batch with a count.

### 4.5 Levels: a defensible choice

Most teams have wars over log levels. The staff-level resolution:

- **DEBUG** — Off in production. On only via dynamic config flag for one pod, for ≤30 minutes.
- **INFO** — Lifecycle events: server starting, connection pool opened, leadership acquired, batch completed. Roughly: <10 INFO log lines per request budget.
- **WARNING** — Recovered errors, retries, deprecation usage, soft limits hit. Should not fire continuously; if it does, it's either an INFO or an ERROR, not a WARNING.
- **ERROR** — Something the user noticed, or that *should* have caused user-visible failure. Includes stack trace.
- **CRITICAL** — Service-wide failure modes (cannot connect to required dependency, config invalid, license expired). Page-worthy if persistent.

If the production log level is INFO and your service produces 10,000 lines per second, you have a structural problem — fix the volume, don't move to WARNING.

---

## 5. Metrics: prometheus_client, Multiprocess, OTel

### 5.1 Why prometheus_client is still the right default

For services that are **scraped** (i.e., your platform is Prometheus / Mimir / VictoriaMetrics), `prometheus_client` is the right choice over OTel Metrics. Reasons:

- Battle-tested in CPython since 2015.
- Zero dependency on the OTel SDK lifecycle (no `MeterProvider` to get wrong).
- Native exposition format on `/metrics` — no protocol translation.
- Documented multiprocess mode that handles Gunicorn correctly.
- Extensive ecosystem (Django, Flask, Celery exporters all assume `prometheus_client`).

Use OTel Metrics when:

- You're pushing to an OTLP endpoint (Lambda, Cloud Run, FaaS, no scrape).
- You want exemplars on histograms tied to OTel traces (works in `prometheus_client` too, but OTel's API is cleaner here).
- You have a cross-language SDK consistency requirement and OTel reduces that surface area.

The two SDKs are not mutually exclusive in one process, but running both *for the same metric* doubles your series cost. Pick one per metric.

### 5.2 Multiprocess mode: the only Gunicorn metrics setup that doesn't lie

In Gunicorn prefork mode, each worker has its own `prometheus_client` registry. Scraping `/metrics` on one worker shows that worker's counters; the other N-1 workers' counters are invisible. The fix is `prometheus_client.multiprocess`, which writes counters to memory-mapped files in a shared directory; on scrape, the registry aggregates across all workers' files.

```python
# observability/metrics_setup.py
import os
import shutil
from prometheus_client import CollectorRegistry, multiprocess
from prometheus_client import Counter, Gauge, Histogram


# REQUIRED env: PROMETHEUS_MULTIPROC_DIR=/tmp/prom_metrics (or tmpfs in k8s)
# This dir MUST be writable, MUST be wiped on process start (for clean counters),
# and MUST NOT be persisted across pod restarts.

def init_multiprocess_metrics():
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not multiproc_dir:
        return  # single-process mode is fine
    if os.path.isdir(multiproc_dir):
        shutil.rmtree(multiproc_dir)
    os.makedirs(multiproc_dir, exist_ok=True)


def get_registry() -> CollectorRegistry:
    """Use this registry for /metrics in multiprocess mode."""
    registry = CollectorRegistry()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        multiprocess.MultiProcessCollector(registry)
    return registry
```

Gauges in multiprocess mode require an explicit aggregation strategy:

```python
# Per-worker gauge that aggregates as min / max / sum / liveall:
DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "Current size of the DB connection pool",
    multiprocess_mode="livesum",  # sum across alive workers
)
INFLIGHT = Gauge(
    "http_inflight_requests",
    "Inflight HTTP requests",
    multiprocess_mode="livesum",
)
LAST_SUCCESS = Gauge(
    "job_last_success_timestamp",
    "Last successful job timestamp",
    multiprocess_mode="liveall",  # one series per worker; query takes max()
)
```

Wire it into Gunicorn:

```python
# gunicorn.conf.py
import os

def when_ready(server):
    from observability.metrics_setup import init_multiprocess_metrics
    init_multiprocess_metrics()

def child_exit(server, worker):
    # Critical: reclaim the worker's mmap files on graceful exit.
    from prometheus_client import multiprocess
    multiprocess.mark_process_dead(worker.pid)
```

The `child_exit` hook is essential. Without it, dead-worker counters keep showing up in `/metrics` forever (they live in mmap files that are never cleaned up), which causes "phantom" series in dashboards. This is the most common Python multiprocess metrics bug.

### 5.3 What to instrument: the Python service shapes

Refer to doc 03 §2.2 for the universal table. Python-specific additions:

| Pattern | Counter | Histogram | Gauge | Note |
|---|---|---|---|---|
| **Asyncio event loop** | — | `asyncio_event_loop_lag_seconds` | `asyncio_tasks_alive` | Loop lag > 100ms = saturation |
| **GC** | `python_gc_collections_total{generation}` | `python_gc_pause_seconds{generation}` | `python_gc_objects_total{generation}` | gen-2 collections > 1/min suggests memory pressure |
| **Thread pool executor** | `threadpool_jobs_submitted_total` | `threadpool_job_seconds`; `threadpool_queue_wait_seconds` | `threadpool_active_workers`; `threadpool_queue_depth` | Hidden source of latency; instrument any `run_in_executor` |
| **Celery** | `celery_tasks_total{task,result}`; `celery_retries_total{task,reason}` | `celery_task_runtime_seconds{task}`; `celery_task_queue_seconds{task}` | `celery_workers_active`; `celery_queue_depth{queue}` | See §11 |
| **Pandas / numpy hot paths** | (sample) | `pandas_op_seconds{op}` | `pandas_dataframe_memory_bytes` | Don't put column names in labels |

The **event loop lag** metric is the single best Python-async health signal. A periodic task measures `loop.time() - expected_time` and exposes the lag; a P99 lag above 100ms is *always* something — either CPU-bound work on the loop, or a sync call sneaking in.

```python
import asyncio
import time
from prometheus_client import Histogram

LOOP_LAG = Histogram(
    "asyncio_event_loop_lag_seconds",
    "Drift between scheduled tick and actual tick",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

async def loop_lag_probe(interval: float = 0.5):
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        actual = time.perf_counter() - t0
        LOOP_LAG.observe(max(0.0, actual - interval))
```

### 5.4 Histograms with exemplars in Python

`prometheus_client` 0.18+ supports exemplars on histograms. Wire them through OTel context:

```python
from prometheus_client import Histogram
from opentelemetry import trace

H = Histogram(
    "http_request_duration_seconds",
    "Server-side HTTP request duration",
    ["method", "route", "status_class"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

def observe(method: str, route: str, status_class: str, seconds: float) -> None:
    span = trace.get_current_span()
    sc = span.get_span_context() if span else None
    exemplar = None
    if sc and sc.is_valid:
        exemplar = {"trace_id": format(sc.trace_id, "032x")}
    H.labels(method, route, status_class).observe(seconds, exemplar=exemplar)
```

Without this single function, your "click the spike, jump to a trace" workflow doesn't work in Grafana. With it, it does.

### 5.5 Cardinality discipline in Python

Python makes it *easy* to produce a high-cardinality label by accident. Examples that cause real outages:

```python
# WRONG — path includes /users/12345/orders/67890; every URL is a unique series.
REQUESTS.labels(method="GET", route=request.url.path).inc()

# RIGHT — templated route from the framework's router.
REQUESTS.labels(method="GET", route=request.scope["route"].path).inc()  # /users/{id}/orders/{order_id}

# WRONG — error class includes the message, with a UUID in it.
ERRORS.labels(error_class=str(exc)).inc()  # ValueError: invalid uuid 'a3f...'

# RIGHT — error class is the type only.
ERRORS.labels(error_class=type(exc).__name__).inc()  # ValueError

# WRONG — label is a column header from a user-uploaded CSV.
ROWS.labels(column=col_name).inc()  # 50,000 unique columns one day

# RIGHT — bucket the column by class (numeric/string/datetime/unknown).
ROWS.labels(column_kind=classify(col_name)).inc()
```

The pre-flight test in CI:

```python
# tests/test_cardinality.py — runs in CI before merge.
import pytest
from prometheus_client import REGISTRY
from app.main import app  # this imports all metrics

def test_known_metrics_have_bounded_cardinality():
    for collector in REGISTRY._collector_to_names:
        for metric in collector.collect():
            for sample in metric.samples:
                # Assert no label value is plausibly unbounded.
                for k, v in sample.labels.items():
                    if k in ("method", "status_class", "result", "error_class", "route"):
                        continue
                    if k.endswith("_id") or k in ("user", "session", "request_id"):
                        pytest.fail(f"forbidden label {k} on metric {sample.name}")

def test_route_is_templated(client):
    client.get("/users/12345")
    metrics_text = client.get("/metrics").text
    assert "/users/{id}" in metrics_text
    assert "/users/12345" not in metrics_text
```

This test runs in 200ms and catches 90% of cardinality regressions before they ship.

---

## 6. Tracing: Context Propagation

Context propagation is the bridge between processes, threads, and async tasks. In Python it is *not* automatic in every situation, and the failure mode is silent — your spans are orphaned, not erroring.

### 6.1 The four contexts to think about

```
1. Sync within one function     — implicit; OTel uses contextvars; works.
2. Across `await` boundaries     — implicit; contextvars carry; works.
3. Across `threading.Thread`    — NOT automatic; must propagate manually.
4. Across `multiprocessing.Process` — NOT automatic; must serialize headers.
5. Across queue boundaries (Celery, Kafka, SQS) — NOT automatic; must inject/extract.
```

(1) and (2) are the easy cases and OTel handles them. (3), (4), (5) are where your spans go missing.

### 6.2 Threads: capturing context for `run_in_executor`

`asyncio`'s `run_in_executor` runs the function in a thread pool — by default, *outside* the current OTel context. Spans created inside are root spans, not children of your request span.

```python
# WRONG — span inside fn() is detached from the request trace.
result = await loop.run_in_executor(None, fn, arg)

# RIGHT — capture context, restore in the worker thread.
import contextvars
from opentelemetry import context as otel_context

ctx = contextvars.copy_context()
result = await loop.run_in_executor(None, ctx.run, fn, arg)
```

If you can't use `contextvars.copy_context()` (e.g., you're in a sync code path before contextvars are bound), use OTel's explicit attach:

```python
from opentelemetry import context, trace

current = context.get_current()

def wrapper():
    token = context.attach(current)
    try:
        return fn()
    finally:
        context.detach(token)

executor.submit(wrapper)
```

A custom `ContextPreservingExecutor` that wraps `concurrent.futures.ThreadPoolExecutor` and does this automatically is a great platform-team utility to write once and import everywhere.

### 6.3 Subprocesses: inject the traceparent header

For `subprocess.run` or `multiprocessing.Process`, traces propagate only if you explicitly serialize them:

```python
import os
from opentelemetry import trace
from opentelemetry.propagate import inject

def child_env() -> dict:
    env = os.environ.copy()
    inject(env)  # writes traceparent and tracestate keys
    return env

import subprocess
subprocess.run(["python", "child.py"], env=child_env())
```

Inside `child.py`, on startup:

```python
from opentelemetry.propagate import extract
from opentelemetry import context, trace
ctx = extract(os.environ)
context.attach(ctx)
# now spans created here are children of the parent's span
```

This is the same pattern for `fork()`-style multiprocessing — but because fork copies memory, you can usually just continue with the inherited context. The above is for `spawn` start methods (default on macOS / Windows).

### 6.4 Async queue boundaries: Kafka, RabbitMQ, Celery, SQS

For message brokers, the discipline is identical to HTTP: inject on send, extract on receive.

```python
# Producer — Kafka header injection.
from opentelemetry.propagate import inject

def produce(topic, payload):
    headers = {}
    inject(headers)
    kafka_headers = [(k, v.encode()) for k, v in headers.items()]
    producer.send(topic, value=payload, headers=kafka_headers)

# Consumer — extract.
from opentelemetry.propagate import extract
from opentelemetry import trace, context

def consume(msg):
    headers = {k: v.decode() for k, v in (msg.headers or [])}
    parent_ctx = extract(headers)
    with trace.get_tracer(__name__).start_as_current_span(
        f"consume {msg.topic}", context=parent_ctx,
    ):
        process(msg.value)
```

This produces a continuous trace from the producer's span to the consumer's span. Without it, every consume is a new root trace and your "where did this order go after publish?" debugging is impossible.

### 6.5 Span attributes: what to put on every span

OTel's [HTTP semantic conventions](https://opentelemetry.io/docs/specs/semconv/http/http-spans/) define the names. Use them. The most-used attributes:

| Attribute | Type | Required for | Example |
|---|---|---|---|
| `http.request.method` | string | HTTP server / client | `POST` |
| `http.route` | string | HTTP server | `/users/{id}` |
| `url.path`, `url.query` | string | HTTP client | `/v2/users` |
| `http.response.status_code` | int | both | `200` |
| `db.system`, `db.statement`, `db.operation` | string | DB clients | `postgresql`, `SELECT id FROM users WHERE...` |
| `messaging.system`, `messaging.destination.name` | string | Kafka/queue | `kafka`, `orders` |
| `error.type` | string | when status=ERROR | `TimeoutError` |
| `peer.service` | string | client spans | `payments-svc` |

**Custom attributes** — domain attributes — should be namespaced (`app.user.tier`, `app.order.amount_cents`). Never put PII in span attributes; treat them like log fields. Do not put unbounded values (`app.user.id` is fine if your trace store handles it; `app.error.full_traceback` is not).

### 6.6 The `start_as_current_span` decorator (and why to skip it for hot paths)

The convenient form:

```python
@tracer.start_as_current_span("apply_promo")
def apply_promo(cart, code):
    ...
```

For hot paths (≥ 1k calls/sec to one function), the decorator's overhead matters (~5-10µs per call). Profile before reaching for it. For cold paths and request-scoped operations, use it freely.

For hot paths, prefer:

```python
def apply_promo(cart, code):
    span = tracer.start_span("apply_promo")
    try:
        with trace.use_span(span, end_on_exit=True):
            ...
    except Exception as e:
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR))
        raise
```

Or, more often, **don't span every call** — sample upstream at the request level, and let the request span carry the work. Adding spans per-loop-iteration is one of the top sources of overhead.

---

## 7. Profiling Python in Production

CPU and memory profiling in Python in production needs different tools than other languages. The summary table:

| Tool | What it samples | Overhead | Fork-safe | Best for |
|---|---|---|---|---|
| **py-spy** | Python frame stack at N Hz, from outside the process via `/proc/<pid>/mem` | ~1-3% at 100Hz | Yes (separate process) | Continuous production profiling, on-demand `top` view, generating flame graphs without code changes |
| **austin** | Same model as py-spy, with memory mode | ~1-2% | Yes | When py-spy can't attach (locked-down envs); slightly faster |
| **pyinstrument** | Python frame sampler, in-process | 5-10% | No | Per-request profiling on selected requests; CI flame graphs |
| **memray** | Native + Python allocations, deep | 30-200% | Partial | Memory leak hunting; *not for prod* unless attached briefly |
| **cProfile / profile** | Deterministic call-by-call | 50-300% | No | Never in production. CI only, for unit benchmarks |
| **scalene** | CPU + memory + GPU; Python + native | 10-30% | No | Dev / pre-prod hunts; great UI |
| **tracemalloc** (stdlib) | Allocation tracebacks | Variable, can be high | No | When debugging a specific OOM; toggle dynamically |

### 7.1 The continuous profiling pattern: py-spy + Pyroscope

The setup that most teams want:

```yaml
# k8s sidecar pattern
- name: py-spy
  image: pyspy/pyspy:0.3
  args:
    - record
    - --pid
    - "1"  # the main app PID
    - --rate
    - "99"
    - --duration
    - "60"
    - --output
    - /tmp/profile.pb.gz
  securityContext:
    capabilities:
      add: ["SYS_PTRACE"]
```

For production-grade continuous profiling, deploy [Pyroscope](https://pyroscope.io) or [Polar Signals/Parca](https://www.polarsignals.com/) and have py-spy push samples directly:

```bash
PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040 \
PYROSCOPE_APPLICATION_NAME=my-service \
py-spy record --rate 99 --pyspy-format pyroscope --pid 1 --duration 86400
```

This gives you flame graphs over time, diff'able across deploys, with ~1% overhead. See doc 09.

### 7.2 The "profile this request" pattern with pyinstrument

For ad-hoc deep profiling of a specific request flow:

```python
from pyinstrument import Profiler

@app.middleware("http")
async def profile_request(request, call_next):
    if request.headers.get("x-pyinstrument") != os.environ.get("PROFILE_TOKEN"):
        return await call_next(request)
    profiler = Profiler(async_mode="enabled")
    profiler.start()
    response = await call_next(request)
    profiler.stop()
    output = profiler.output_html()
    # Save to S3 or logs for inspection. Don't return inline (CSP / size).
    save_profile(request_id=request.headers["x-request-id"], html=output)
    return response
```

Gated by a header + token; production-safe; run on-demand against one pod.

### 7.3 Memory profiling: memray for the leak

When memory grows without bound, you need allocation tracebacks. memray is the best tool:

```bash
# Attach to a running process via ptrace.
memray attach <pid> --output /tmp/leak.bin
# Stop after some duration.
memray stats /tmp/leak.bin
memray flamegraph /tmp/leak.bin
```

This requires `SYS_PTRACE` and is *not* what you run continuously. Run for 30-60 seconds against the leaking pod; stop; analyze.

For long-running suspicion (e.g., "memory grows 50MB/hour"), instrument with `tracemalloc` toggled by env var:

```python
import tracemalloc

if os.environ.get("PYTHON_TRACEMALLOC"):
    tracemalloc.start(int(os.environ["PYTHON_TRACEMALLOC"]))  # frame depth

# Periodic snapshot:
async def memory_snapshot_loop():
    while True:
        await asyncio.sleep(300)
        if tracemalloc.is_tracing():
            snap = tracemalloc.take_snapshot().filter_traces((
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            ))
            top = snap.statistics("lineno")[:20]
            structlog.get_logger().info("memory.snapshot", top=[str(t) for t in top])
```

### 7.4 GIL contention: the metric you should have

Beyond profiling: a Prometheus counter for GIL pressure exists in CPython 3.12+. For older versions, infer from `asyncio_event_loop_lag_seconds` + `process_cpu_seconds_total` — if loop lag is high but CPU is low, you have GIL contention from C extensions or subinterpreters.

In Python 3.13+ with `--disable-gil`, profiling rules change — the C-stack flame graph becomes meaningful again. Continue using py-spy; it handles both modes.

---

## 8. Framework-Specific Instrumentation

### 8.1 FastAPI

```python
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = FastAPI()
FastAPIInstrumentor.instrument_app(
    app,
    excluded_urls="/healthz,/readyz,/metrics",
    server_request_hook=lambda span, scope: span.set_attribute(
        "app.tenant", scope.get("headers", {}).get("x-tenant-id", "unknown")
    ),
)
```

What you get free: a span per request with `http.method`, `http.route`, `http.status_code`. What you must add: domain attributes via the `server_request_hook`, error semantics on raised exceptions (FastAPI's exception handlers should `span.record_exception()` and `span.set_status(StatusCode.ERROR)`), and request-id propagation if you have a custom header.

### 8.2 Django

```python
# settings.py
from opentelemetry.instrumentation.django import DjangoInstrumentor
DjangoInstrumentor().instrument(
    excluded_urls="/health,/metrics",
    is_sql_commentor_enabled=True,
)

# Add the prometheus middleware:
MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    # ... rest of middleware ...
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]
```

`django-prometheus` provides per-view RED metrics out of the box; combine with OTel for tracing. In Django, watch for the **per-request DB connection** anti-pattern: each request opens and closes a connection unless you use `CONN_MAX_AGE` or pgbouncer. The DB pool gauges (§3.4) are how you see this.

### 8.3 Flask

```python
from flask import Flask
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from prometheus_flask_exporter.multiprocess import GunicornPrometheusMetrics

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app, excluded_urls="/health,/metrics")
metrics = GunicornPrometheusMetrics(app)
```

Flask is the trickiest in multi-worker mode because the default `prometheus_flask_exporter` does not handle Gunicorn. Use `GunicornPrometheusMetrics` and set `PROMETHEUS_MULTIPROC_DIR`.

### 8.4 gRPC (server and client)

```python
from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer, GrpcInstrumentorClient

GrpcInstrumentorServer().instrument()
GrpcInstrumentorClient().instrument()
```

For gRPC, manually set `peer.service` on client spans via interceptors — auto-instrumentation knows the host but not the logical service name your dashboards filter on.

### 8.5 The "instrument everything you import" reality

For libraries without an official instrumentor (most of them), the pattern is:

1. Find the boundary call: `lib.send_email(to, body)`.
2. Wrap it once with a context manager that opens a CLIENT span:

```python
from contextlib import contextmanager
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

@contextmanager
def email_span(to: str, template: str):
    with tracer.start_as_current_span("email.send", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("messaging.system", "sendgrid")
        span.set_attribute("messaging.destination.name", template)
        span.set_attribute("recipient.domain", to.split("@", 1)[1])  # not the local part
        yield span
```

Use `email_span(...)` everywhere `lib.send_email` is called. Three months later, the boundary is consistently instrumented and your traces tell a complete story.

---

## 9. Worker Models

### 9.1 Gunicorn (sync, prefork)

The default for sync Python web apps. The right config:

```python
# gunicorn.conf.py
import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
worker_class = "sync"
threads = 1  # sync workers ignore this
preload_app = True  # load before fork = saves memory; less reload-friendly
timeout = 30
graceful_timeout = 30
keepalive = 5

# Multiprocess metrics setup
def when_ready(server):
    from observability.metrics_setup import init_multiprocess_metrics
    init_multiprocess_metrics()

def post_fork(server, worker):
    # Re-init OTel SDK in each worker (BatchSpanProcessor uses a thread).
    from observability.setup import setup_telemetry
    setup_telemetry()

def child_exit(server, worker):
    from prometheus_client import multiprocess
    multiprocess.mark_process_dead(worker.pid)
```

Three rules:

1. `preload_app = True` AND `post_fork` re-initializes OTel — the BatchSpanProcessor's background thread does not survive fork.
2. `child_exit` calls `mark_process_dead(worker.pid)` — required for clean multiprocess metrics.
3. `workers = (cores * 2) + 1` is the textbook formula, but if your app is async-leaning (uses `httpx`, `asyncpg` via `asgiref.sync_to_async`), reduce to `cores + 1`. If pure sync CPU, `cores`. Profile before assuming.

### 9.2 Uvicorn (async, ASGI)

```bash
uvicorn app:app \
  --host 0.0.0.0 --port 8000 \
  --workers 4 \
  --no-access-log \
  --lifespan on
```

For `--workers > 1`, Uvicorn forks; the same multiprocess discipline applies. For `--workers 1`, you run one event loop and depend on async concurrency only — fine for high-IO services with few threads, dangerous if any handler is CPU-bound.

`--no-access-log` is correct for production: the access log duplicates what your OTel server-span and `http_requests_total` counter already capture, with worse structure. Replace with structlog request-end log if you want one.

### 9.3 Gunicorn + Uvicorn worker class (the hybrid)

For ASGI apps in production, this is the most common deployment:

```bash
gunicorn app:app \
  -w 4 \
  -k uvicorn.workers.UvicornWorker \
  --preload \
  --timeout 60 \
  --graceful-timeout 30
```

Uses Gunicorn's process management (graceful restarts, signal handling, master process) with Uvicorn's ASGI loop per worker. The Gunicorn hooks above (`when_ready`, `post_fork`, `child_exit`) all apply.

### 9.4 uWSGI

uWSGI works but the operational rough edges (Vassal/Emperor configs, custom signal semantics, harder log routing) make it a non-default in 2026. If you must:

- Set `enable-threads = true` if you use any threads (default is off — silent breakage).
- Set `lazy-apps = true` if you want per-worker init (analogous to Gunicorn `--preload = false`).
- Use [`uwsgi-prometheus-exporter`](https://github.com/timonwong/uwsgi_exporter) — uWSGI exposes its own stats socket; that exporter translates them.

### 9.5 The "single worker async" trap

Running Uvicorn with `--workers 1` looks tempting:

- No multiprocess metrics setup.
- No fork/SDK reinit.
- All state in one place.

The trap: one CPU-bound handler stalls the entire event loop for every concurrent request. Defaults to *N=cores* workers; gate this on a measured CPU profile. Never single-worker for an API service with mixed sync/async dependencies.

---

## 10. Async, Threads, and the GIL

### 10.1 The mental model

```
asyncio event loop  ─┐
                     ├── all share ONE python thread (the GIL holder)
async tasks          ─┘
─────────────────────────────────────
threading.Thread     ─── separate Python threads, but ONLY ONE runs Python at a time
─────────────────────────────────────
multiprocessing       ─── separate processes, separate GILs, separate state
─────────────────────────────────────
C extensions w/ no-GIL ─── NumPy, asyncpg's protocol parser, etc. — release GIL while in C
```

What this means for observability:

- A blocking call in an async handler blocks **every** concurrent request on that worker.
- Two threads doing CPU-bound Python work do NOT speed up.
- A `pool.submit` returning a `Future` and then `await asyncio.wrap_future(fut)` is the right way to push CPU-bound work off the loop.

### 10.2 Detecting blocking-in-async

The single best signal: `asyncio_event_loop_lag_seconds` (§5.3). The second best: the OTel `aiomonitor` integration or `loop.set_debug(True)` in non-prod, which logs slow callbacks.

Pre-prod safety net:

```python
import asyncio

if os.environ.get("ASYNCIO_DEBUG") == "1":
    asyncio.get_event_loop().set_debug(True)
    # Log a warning if a callback runs > 100ms (default).
    asyncio.get_event_loop().slow_callback_duration = 0.1
```

Turn this on in staging; surface the warnings as real errors (your structlog setup makes them queryable in Loki).

### 10.3 Thread pool instrumentation

Most Python services have at least one thread pool: FastAPI's default for sync endpoints, ML services for inference, data pipelines for I/O fan-out. Always instrument:

```python
from concurrent.futures import ThreadPoolExecutor
from prometheus_client import Counter, Histogram, Gauge

JOBS_SUBMITTED = Counter("threadpool_jobs_submitted_total", "Jobs submitted", ["pool"])
JOB_RUNTIME = Histogram("threadpool_job_seconds", "Job execution time", ["pool"])
QUEUE_WAIT = Histogram("threadpool_queue_wait_seconds", "Time queued", ["pool"])
ACTIVE = Gauge("threadpool_active_workers", "Active workers", ["pool"])
QUEUE_DEPTH = Gauge("threadpool_queue_depth", "Pending tasks", ["pool"])

class InstrumentedExecutor(ThreadPoolExecutor):
    def __init__(self, *args, name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._name = name

    def submit(self, fn, *args, **kwargs):
        submitted_at = time.perf_counter()
        JOBS_SUBMITTED.labels(self._name).inc()
        QUEUE_DEPTH.labels(self._name).inc()
        def wrapped(*a, **k):
            QUEUE_DEPTH.labels(self._name).dec()
            ACTIVE.labels(self._name).inc()
            QUEUE_WAIT.labels(self._name).observe(time.perf_counter() - submitted_at)
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                ACTIVE.labels(self._name).dec()
                JOB_RUNTIME.labels(self._name).observe(time.perf_counter() - t)
        return super().submit(wrapped, *args, **kwargs)
```

Now `Queue depth > 0 sustained` is a saturation signal — your pool is too small or your jobs are too slow.

---

## 11. Celery, RQ, Dramatiq

### 11.1 Celery: the discipline

Celery is the Python background-job standard and the source of more invisible bugs than any other Python library. The non-obvious instrumentation rules:

**Trace propagation through the broker.** Use `opentelemetry-instrumentation-celery`:

```python
from celery import Celery
from celery.signals import worker_process_init
from opentelemetry.instrumentation.celery import CeleryInstrumentor

celery_app = Celery(...)

@worker_process_init.connect
def _setup(**_):
    from observability.setup import setup_telemetry
    setup_telemetry()
    CeleryInstrumentor().instrument()
```

`worker_process_init` is the celery equivalent of Gunicorn's `post_fork` — without re-initializing in each prefork worker, the OTel BatchSpanProcessor thread is dead.

**Queue depth.** Celery's default monitoring is broken. Use `celery-prometheus-exporter` or roll your own:

```python
from celery import Celery
from prometheus_client import Gauge

QUEUE_DEPTH = Gauge("celery_queue_depth", "Tasks waiting", ["queue"])

def update_queue_depth():
    with celery_app.connection() as conn:
        with conn.channel() as channel:
            for queue in ("default", "high_priority", "slow"):
                _, count, _ = channel.queue_declare(queue=queue, passive=True)
                QUEUE_DEPTH.labels(queue).set(count)
```

Run periodically in a sidecar or via `celery beat`.

**Task latency: queue time + execution time, not just execution.** "Slow tasks" are usually slow because they sat in the queue, not because they took long to run.

```python
from celery.signals import task_prerun, task_postrun

@task_prerun.connect
def on_task_prerun(sender=None, task_id=None, task=None, **_):
    # task.request.timelimit, task.request.retries, etc. all available
    request_received_at = task.request.kwargs.pop("__sent_at", None) if task.request.kwargs else None
    if request_received_at:
        QUEUE_WAIT.labels(task=task.name).observe(time.time() - request_received_at)

@task_postrun.connect
def on_task_postrun(sender=None, state=None, **_):
    TASKS.labels(task=sender.name, result=state).inc()
```

Decorate your tasks to inject `__sent_at` at apply time.

### 11.2 RQ (Redis Queue)

Lighter than Celery, less feature-rich. Use the `rq.contrib.prometheus` exporter and OTel context inject/extract on `enqueue`. The same trace_id-through-broker discipline applies.

### 11.3 Dramatiq

Modern Celery alternative. OTel auto-instrumentation exists; otherwise the patterns are the same. Dramatiq's middleware system makes adding `before_process_message` / `after_process_message` hooks for span management trivial.

---

## 12. Data and ML Workloads

### 12.1 The "no /metrics endpoint" problem

ETL scripts, training jobs, notebooks — these have lifecycles that don't fit the scrape model. Two patterns:

1. **OTLP push** — the OTel MeterProvider with `PeriodicExportingMetricReader` sending to a collector. Works for jobs running ≥ 1 minute.
2. **Pushgateway** — `prometheus_client.push_to_gateway()` at the end of a job. Right for short jobs (cron). Fails badly if you forget to push (last-success metric becomes stale and dashboards lie).

Recommended: OTLP push for anything ≥ 1 minute; Pushgateway with `job_last_success_timestamp` for sub-minute jobs. Always emit a `job_last_success_timestamp` gauge so a stale-job alert fires when an unattended cron stops running.

### 12.2 Pandas / NumPy hot paths

Don't span every column op. Do span the *stages*:

```python
with tracer.start_as_current_span("etl.load"):
    df = pd.read_parquet(input_path)
    span = trace.get_current_span()
    span.set_attribute("rows", len(df))
    span.set_attribute("columns", len(df.columns))
    span.set_attribute("memory_bytes", df.memory_usage(deep=True).sum())

with tracer.start_as_current_span("etl.transform"):
    df = transform(df)

with tracer.start_as_current_span("etl.write"):
    df.to_parquet(output_path)
```

Three spans, six attributes, and the entire ETL lifecycle is observable.

### 12.3 PyTorch / TensorFlow training

For training jobs, the standard signals:

- `train_step_seconds` histogram (per step)
- `train_loss{phase}` gauge (current loss; not in metrics if you have a metrics-style loss tracker like MLflow)
- `gpu_utilization`, `gpu_memory_used_bytes` (from DCGM-exporter — see `gpu-observability` sister folder)
- `dataloader_seconds` histogram (often the bottleneck; instrument explicitly)

For agent and LLM systems, see doc 26.

### 12.4 The notebook problem

Notebooks are the dark matter of production Python. The pattern:

- Pre-import a `notebook_observability` helper that sets up structlog + OTel.
- Wrap critical cells with a `@traced` decorator.
- Make the helper save a "notebook run" record to S3 with run_id and trace_id, so a notebook execution can be replayed and joined to its DB queries.

---

## 13. Performance Overhead and Sampling

### 13.1 The Python observability budget

Realistic overhead for a well-instrumented Python service:

| Layer | Overhead | Notes |
|---|---|---|
| `prometheus_client` Counter.inc / Histogram.observe | ~1µs | Negligible |
| OTel span start_span + end_span (no exporter) | ~5-10µs | Hot path math |
| OTel span + 5 attributes + exporter | ~15-30µs | Hot path math |
| `structlog` log line (3 fields, JSON to stdout) | ~50-200µs | Stdout is the bottleneck |
| Log line at DEBUG that gets dropped | ~1µs (filtered early) | Use lazy formatters |
| Auto-instrumented psycopg query | ~50-100µs | On top of query time |
| Pyinstrument profiling middleware | ~5-10ms / req | Don't run continuously |
| py-spy at 99 Hz | ~1-3% CPU | Continuous OK |

For a 10ms RED-method service, 50µs per request is 0.5% — fine. For a 200µs Redis-cache-hit micro-service, 50µs is 25% — too much; sample down or skip span-per-request.

### 13.2 Sampling by route

Configure the OTel sampler per-route via a custom sampler, not globally:

```python
from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult, Decision, ParentBased

class RouteAwareSampler(Sampler):
    def __init__(self, default_rate: float, route_rates: dict):
        self.default = default_rate
        self.route_rates = route_rates

    def should_sample(self, parent_context, trace_id, name, **kwargs):
        attributes = kwargs.get("attributes", {}) or {}
        rate = self.default
        for route_prefix, r in self.route_rates.items():
            if name.startswith(route_prefix):
                rate = r
                break
        if (trace_id & 0xFFFFFFFF) / 0x100000000 < rate:
            return SamplingResult(Decision.RECORD_AND_SAMPLE)
        return SamplingResult(Decision.DROP)

    def get_description(self):
        return f"RouteAwareSampler(default={self.default})"

sampler = ParentBased(root=RouteAwareSampler(
    default_rate=0.10,
    route_rates={
        "GET /healthz": 0.0,         # never trace
        "GET /static/": 0.0,
        "POST /checkout": 1.0,        # always trace
        "POST /payments/": 1.0,
    },
))
```

Then let the **collector tail-sample** for error/slow keep policy (doc 04). Head-sampling at the Python SDK is for *cost*; tail-sampling at the collector is for *interest*.

### 13.3 The DEBUG-when-traced pattern

A great pattern for adaptive log volume: log at DEBUG only when the trace is sampled.

```python
def smart_log_level(default: int = logging.INFO) -> int:
    span = trace.get_current_span()
    sc = span.get_span_context() if span else None
    if sc and sc.is_valid and sc.trace_flags.sampled:
        return logging.DEBUG
    return default
```

Hook this into a custom processor. Now on the 1% of sampled requests, you get verbose logs; on the 99%, you don't pay for them. Combined with tail sampling that keeps errors, this means *every error has a complete debug log stream* — an enormous debugging upgrade.

---

## 14. PII, Redaction, Secrets

### 14.1 Where to redact

The principle from doc 03 §14: **redact at the source, not at the store**. By the time it's in the log shipper, it's already on the wire.

In Python:

```python
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

def redact_value(v):
    if not isinstance(v, str):
        return v
    v = EMAIL_RE.sub("<EMAIL>", v)
    v = CARD_RE.sub("<CARD>", v)
    return v

def redact_processor(logger, method_name, event_dict):
    for k, v in list(event_dict.items()):
        if k.lower() in {"password", "token", "api_key", "secret", "authorization"}:
            event_dict[k] = "<REDACTED>"
        else:
            event_dict[k] = redact_value(v) if isinstance(v, str) else v
    return event_dict
```

Add `redact_processor` to your structlog processor chain *before* the JSON renderer.

### 14.2 The OTel attribute cleanup hook

OTel exporters can apply a `BatchSpanProcessor` that filters attributes. Implement a custom processor to scrub or drop sensitive attribute keys:

```python
from opentelemetry.sdk.trace import SpanProcessor

SENSITIVE_KEYS = {"http.request.header.authorization", "db.statement.parameters"}

class RedactingProcessor(SpanProcessor):
    def on_end(self, span):
        for key in list(span.attributes.keys()):
            if key in SENSITIVE_KEYS:
                # OTel attributes are immutable post-export, so this is best-effort;
                # the durable fix is a SemConv-aware Collector processor (doc 04).
                pass
```

A more durable redaction policy lives in the **OTel Collector** (`processor: redaction:`) rather than in each Python service. Use the SDK's hook for app-specific values; use the collector for the org-wide policy.

### 14.3 Secrets in environment variables

A common Python footgun: `logger.debug(f"env: {dict(os.environ)}")` at startup. *Never* log the environment. If you must:

```python
SAFE_ENV_KEYS = {"PATH", "HOSTNAME", "PYTHONPATH", "ENV", "APP_VERSION"}

def safe_env():
    return {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS}

logger.info("startup", env=safe_env())
```

Allow-list, not deny-list. The deny-list will miss the next secret your team adds.

---

## 15. Testing Observability

### 15.1 The premise

Telemetry that isn't tested rots. The first time you find out a span is missing is when you need it during an incident — too late.

### 15.2 Span assertions with InMemorySpanExporter

```python
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

@pytest.fixture
def memory_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    exporter.clear()

def test_checkout_emits_expected_spans(memory_exporter, client):
    client.post("/checkout", json={"cart_id": "c1"})
    spans = memory_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "POST /checkout" in names
    assert "db.query.cart_lookup" in names
    assert "external.payment.charge" in names
    # Critical assertion: the trace is connected.
    root = next(s for s in spans if s.parent is None)
    children = [s for s in spans if s.parent and s.parent.span_id == root.context.span_id]
    assert len(children) >= 2
```

### 15.3 Log assertions

```python
def test_login_failure_logs_audit_event(caplog):
    caplog.set_level("INFO")
    client.post("/login", json={"user": "x", "password": "wrong"})
    audit_events = [
        r for r in caplog.records
        if getattr(r, "event", None) == "auth.login.failed"
    ]
    assert len(audit_events) == 1
    assert audit_events[0].user == "x"
    assert "password" not in audit_events[0].__dict__  # redacted
```

### 15.4 Cardinality assertion (the one that prevents prod fires)

See §5.5. The `test_route_is_templated` and `test_known_metrics_have_bounded_cardinality` tests catch the most common pre-prod regressions.

### 15.5 Dashboard-as-code lint

If your Grafana dashboards are JSON in git (they should be), add a CI step that asserts every panel's PromQL query references a metric that the service actually exposes. Grafana provides a CLI for this; alternatively, a small Python script that imports the service and checks the exposed registry against the dashboard JSON.

---

## 16. Packaging, Versioning, Rollout

### 16.1 Dependency pinning

OpenTelemetry-Python's stability is officially "stable" for the core but the contrib instrumentations move faster. Pin all OTel deps to **exact** versions in `requirements.txt` / `pyproject.toml`:

```
opentelemetry-api==1.27.0
opentelemetry-sdk==1.27.0
opentelemetry-exporter-otlp==1.27.0
opentelemetry-instrumentation-fastapi==0.48b0
opentelemetry-instrumentation-psycopg==0.48b0
```

Note the `b0` (beta) suffix on contrib — they version separately from the core. Mismatches between SDK and contrib are the #1 silent breakage.

### 16.2 Canary rollouts

When changing instrumentation:

- Roll out to one pod first; verify spans still arrive at the collector.
- Compare metric series count before and after; alert on > 10% growth.
- Inspect a sample trace end-to-end in Grafana/Tempo before promoting.

A simple smoke test: a synthetic request that exercises the most-traced path, asserts the resulting trace has ≥ N spans matching expected names. Run after every deploy.

### 16.3 Vendoring vs upstream

Some teams vendor the OTel SDK. Don't. The SDK ships security and correctness fixes monthly; pinning gives you reproducibility, vendoring gives you stagnation.

---

## 17. A Complete Production-Shaped Example: FastAPI Service

Putting it all together — what a *good* Python service looks like end to end.

**Directory layout:**

```
src/
├── observability/
│   ├── __init__.py
│   ├── setup.py            # SDK + propagators
│   ├── instrumentation.py  # selective auto-instrumentors
│   ├── logging_setup.py    # structlog
│   ├── metrics_setup.py    # multiprocess + custom metrics
│   └── middleware.py       # request-id, contextvars binding
├── app/
│   └── main.py             # FastAPI app
└── bootstrap.py            # entry point — sets up obs first
gunicorn.conf.py
pyproject.toml
```

**`bootstrap.py`** (entry point — *first thing that runs*):

```python
from observability.setup import setup_telemetry
from observability.logging_setup import setup_logging
from observability.metrics_setup import init_multiprocess_metrics

setup_logging(level="INFO")
init_multiprocess_metrics()
setup_telemetry()

from app.main import app  # only after observability is ready
```

**`app/main.py`**:

```python
import time
from fastapi import FastAPI, Request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import structlog

from observability.metrics_setup import (
    get_registry, REQUESTS, DURATION, INFLIGHT, observe_with_exemplar,
)
from observability.instrumentation import install_instrumentors
from observability.middleware import bind_request_context

logger = structlog.get_logger()

app = FastAPI()
install_instrumentors(app)
app.middleware("http")(bind_request_context)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    route = request.scope.get("route").path if request.scope.get("route") else "<unknown>"
    INFLIGHT.labels(route).inc()
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request.unhandled", route=route)
        REQUESTS.labels(request.method, route, "5xx").inc()
        raise
    finally:
        INFLIGHT.labels(route).dec()
    duration = time.perf_counter() - t0
    sc = f"{response.status_code // 100}xx"
    REQUESTS.labels(request.method, route, sc).inc()
    observe_with_exemplar(DURATION, [request.method, route, sc], duration)
    logger.info("request.end", route=route, status=response.status_code, latency_ms=round(duration * 1000))
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(get_registry()), media_type=CONTENT_TYPE_LATEST)


@app.post("/checkout")
async def checkout(request: Request):
    # request_id, trace_id, user_id are bound on contextvars.
    # Spans are auto-created by FastAPI instrumentor.
    # DB calls are auto-spanned by PsycopgInstrumentor.
    # All logs in here carry trace_id automatically.
    logger.info("checkout.begin")
    ...
    return {"order_id": "..."}
```

**Run it:**

```bash
PROMETHEUS_MULTIPROC_DIR=/tmp/prom \
OTEL_SERVICE_NAME=checkout \
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
OTEL_TRACES_SAMPLER_ARG=0.1 \
APP_VERSION=$(git rev-parse --short HEAD) \
ENV=prod \
gunicorn bootstrap:app -c gunicorn.conf.py
```

What this gets you, working out of the box:

- Every request: a span with `http.method`, `http.route`, `http.status_code`.
- Every log line in the request handler: structured, JSON, carries `trace_id`, `span_id`, `request_id`, `route`, `method`, `user_id`.
- `http_requests_total{method, route, status_class}` counter (multiprocess-safe).
- `http_request_duration_seconds_bucket{method, route, status_class}` histogram with exemplars pointing to traces.
- `http_inflight_requests{route}` gauge (saturation signal).
- Auto-instrumented Postgres queries with SQLCommenter (`/* traceparent */`).
- Auto-instrumented Redis, requests, gRPC.
- Continuous CPU profiling via py-spy sidecar pushing to Pyroscope.
- Pre-flight cardinality test in CI catching label drift.
- Multi-window multi-burn-rate alert tied to `http_request_duration_seconds` for the SLO.

This is the floor for a staff-engineer-quality Python service in 2026, not the ceiling.

---

## 18. Process Lifecycle, Races, Restarts, and Crash Edge Cases

The category of bugs that don't show up in dev, don't show up in staging, don't show up in load tests, and only show up at 03:14 on a Saturday during a deploy. Python's process model — `fork()`, daemon threads, `atexit`, async tasks running until cancellation — produces a long list of telemetry edge cases that a staff engineer should anticipate.

The unifying observation: **observability is a side effect, not a result**. The application's job is to serve the request; emitting the span, log, and metric is opportunistic. A graceless shutdown silently drops opportunistic work, and the engineer never sees the missing data because *the data is missing*. Defending against this requires both code discipline and infrastructure discipline.

### 18.1 The lifecycle map a Python service actually has

```
START                                                       SHUTDOWN
─────                                                       ────────
1. exec python                                              ┌──────────────────────┐
2. import sys, builtins                                     │  Termination signal  │
3. import bootstrap.py                                      │  (SIGTERM, SIGINT,   │
   ├─ setup_logging()              ← logger ready           │   SIGKILL, OOM,      │
   ├─ init_multiprocess_metrics()  ← metric files ready     │   container stop)    │
   └─ setup_telemetry()            ← OTel ready, BSP thread │                      │
4. import app                                               │  Race window opens:  │
5. install_instrumentors(app)                               │  - in-flight spans   │
6. fork() worker N                                          │  - queued logs       │
   ├─ post_fork: re-init OTel SDK in worker                 │  - exporter buffer   │
   ├─ open DB pool, Redis pool, Kafka producer              │  - in-flight reqs    │
   └─ ready                                                 │                      │
7. accept requests / consume from queue                     │  Goal: drain → flush │
8. … ←───── steady state ─────→                             │  → exit              │
                                                            └──────────────────────┘
```

Each *transition* between phases is a race window where telemetry can be lost or corrupted. The hard cases are: cold start (telemetry not yet initialized), `fork()` (state inheritance), shutdown (drain vs flush vs kill), and crash (no chance to drain).

### 18.2 SIGTERM and graceful shutdown: the canonical Python sequence

Kubernetes (and most container runtimes) deliver SIGTERM, wait `terminationGracePeriodSeconds` (default 30s), then SIGKILL. The window between SIGTERM and SIGKILL is when you must:

1. Stop accepting new work (fail readiness probe or stop pulling from the queue).
2. Finish in-flight work (or up to a budget; reject the rest).
3. Flush all telemetry (log buffers, span buffers, metric buffers, profile uploads).
4. Close pools and connections cleanly (so DB doesn't see "client gone").
5. Exit 0.

The skeleton:

```python
import asyncio
import signal
from opentelemetry import trace, metrics
import structlog

logger = structlog.get_logger()
shutdown_event = asyncio.Event()

async def graceful_shutdown(reason: str):
    if shutdown_event.is_set():
        return
    shutdown_event.set()
    logger.info("shutdown.begin", reason=reason)

    # 1. Mark unready — load balancer stops sending new requests.
    set_ready(False)

    # 2. Drain in-flight work (with a budget).
    drain_budget = float(os.environ.get("SHUTDOWN_DRAIN_SECONDS", "20"))
    deadline = asyncio.get_event_loop().time() + drain_budget
    while inflight_count() > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    if inflight_count() > 0:
        logger.warning("shutdown.drain.timeout", remaining=inflight_count())

    # 3. Close upstream connections so peers fail fast (not on idle timeout).
    await close_pools()

    # 4. Flush telemetry — order matters: spans, then metrics, then logs.
    try:
        trace.get_tracer_provider().shutdown()  # flushes BSP
    except Exception:
        logger.exception("shutdown.trace.flush.failed")
    try:
        metrics.get_meter_provider().shutdown()  # flushes metric reader
    except Exception:
        logger.exception("shutdown.metrics.flush.failed")

    # 5. Last log line — synchronous flush of stdout.
    logger.info("shutdown.end")
    for handler in logging.getLogger().handlers:
        handler.flush()
    sys.stdout.flush()
    sys.stderr.flush()


def install_signal_handlers(loop):
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(
            graceful_shutdown(f"signal:{s.name}")
        ))
```

Three rules embedded above:

- **Flush spans before metrics, metrics before logs.** Spans reference `trace_id`s that the metric exemplars also reference; flushing in this order maximizes correlation in the trace store. Logs flush last because they are the most resilient (line-buffered to stdout, picked up by the daemonset shipper, replayed if Kafka is buffering).
- **Drain has a budget**, not "wait forever". The container will be SIGKILL'd at 30s anyway; reserve ≥ 5s for flushing, not 0.
- **`signal.signal()` only works in the main thread.** In `asyncio` apps use `loop.add_signal_handler`. In sync apps use `signal.signal` but only register from the main thread before fork. Workers inherit handlers from the master.

### 18.3 The 30-second cliff (and why your `terminationGracePeriodSeconds` should be longer)

Default Kubernetes `terminationGracePeriodSeconds=30` is hostile to Python services that:

- Have long-running requests (LLM inference, ETL endpoints, large file uploads).
- Have queue consumers that take seconds per message.
- Run BatchSpanProcessor with `schedule_delay_millis=5000` and `max_export_batch_size=512` — flushing 5k queued spans takes 10+ seconds.

Set it explicitly to **drain budget + flush budget + cushion**, e.g., 60 seconds. Set the application's `SHUTDOWN_DRAIN_SECONDS` to `terminationGracePeriodSeconds - 10` so the in-process drain finishes before the kernel kills you.

```yaml
spec:
  terminationGracePeriodSeconds: 60
  containers:
    - env:
        - name: SHUTDOWN_DRAIN_SECONDS
          value: "45"
```

### 18.4 `fork()` after threads — the silent BSP corruption

The most dangerous Python observability race. Sequence:

1. Master process imports app; `setup_telemetry()` starts the BatchSpanProcessor's exporter thread.
2. Master forks worker N. `fork()` copies memory but **only the calling thread** — the BSP exporter thread does NOT exist in the child.
3. Child's `BatchSpanProcessor` queue still has spans buffered in shared memory at fork time (which the child now owns its own copy of).
4. Child processes requests, queues new spans into a queue with no consumer thread.
5. Queue fills; `BatchSpanProcessor.on_end()` blocks or drops; export silently fails forever.

The fix is `post_fork` re-init (§9.1), which constructs a *new* TracerProvider with a *new* BSP and a *new* exporter thread in each child. The Gunicorn `post_fork` hook is the single most important hook for OTel correctness:

```python
def post_fork(server, worker):
    # Discard parent's TracerProvider entirely; build a new one in this worker.
    from observability.setup import setup_telemetry, reset_telemetry
    reset_telemetry()
    setup_telemetry()
```

`reset_telemetry()` should `shutdown()` the inherited provider (best-effort; the inherited BSP is broken but `shutdown()` is idempotent) and clear the global, then setup constructs fresh state.

Equivalent traps live in: Celery (`worker_process_init`), `multiprocessing.Process` start (`spawn` is safe; `fork` requires re-init), gunicorn's `--preload` mode specifically. **Always** put the OTel init in the post-fork hook, never just in module-level code.

### 18.5 SIGKILL, OOM, container-stop: the data you cannot save

You get *no notification* on SIGKILL, OOM-killer, or `docker kill -s KILL`. Whatever was in the BSP queue, the metrics multiproc files since the last scrape, and the structlog buffer is gone.

The defenses are infrastructure, not code:

- **Make exporter intervals short enough that buffer loss is bounded.** `schedule_delay_millis=5000` means up to 5 seconds of trace data is in flight at any moment. For high-traffic services, set 1000-2000ms.
- **Use the OTel Collector as the durability boundary.** The Collector has its own queue, persists to disk if configured, and sits behind a Kafka. The application's job is to deliver to the Collector; the Collector's job is durability.
- **Scrape `/metrics` frequently enough** that pre-OOM metrics are visible. Default 60s; for tier-0 services, scrape every 15s.
- **Suppress async batching for last-resort lifecycle events.** Audit logs (auth.success, auth.fail, payment.completed) should go through a `SimpleSpanProcessor` for traces and a synchronous-flush log path. Slower per-event, but they survive crash.

```python
# Synchronous span processor for audit-grade events.
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
audit_processor = SimpleSpanProcessor(OTLPSpanExporter(endpoint=...))
tracer_provider.add_span_processor(audit_processor)

audit_tracer = trace.get_tracer("audit")
with audit_tracer.start_as_current_span("payment.charged", kind=trace.SpanKind.INTERNAL):
    ...
# Span is exported BEFORE this line returns. Survives crash.
```

### 18.6 Counter resets on restart and what they look like in PromQL

Every counter resets to 0 on process restart. PromQL's `rate()` and `increase()` functions handle this — they detect the reset by seeing `value(t+1) < value(t)` and assume the previous max was just before. **However**:

- `rate()` over a window crossing a restart is interpolated. It will *understate* the rate (the 0-to-restart climb is missing).
- `increase()` over the same window has the same issue — it returns the cumulative since the most recent reset, not since window start.
- **A frequent restart loop produces erratic dashboards.** Pods restarting every 90s with a 5m rate window means the rate is consistently reset.

Defenses:

- **Alert on restart frequency**, not just on metrics. `kube_pod_container_status_restarts_total` rising = a separate alarm.
- **Use exemplars rather than raw counts** for "is this normal?" — a single exemplar trace from the bucket is more informative than a smooth rate that hides a restart loop.
- **Track `process_start_time_seconds`** (auto-emitted by `prometheus_client`) and compute "fleet seconds since last restart" to detect bad-pod hotspots.

### 18.7 The first-scrape-after-restart problem

On Prometheus's first scrape after a process restart:

- Counters are at 0 → no `rate()` data point yet.
- Histograms have empty buckets → `histogram_quantile()` returns NaN.
- Gauges may not have been initialized → missing series.

The dashboard panel briefly shows "No data" for 15-60 seconds. Annoying but expected. The fix is **initialization**: in startup, exercise every metric once (with a 0-value sample where applicable):

```python
# In startup, after setup:
def warm_metrics():
    for method in ("GET", "POST", "PUT", "DELETE"):
        for sc in ("2xx", "4xx", "5xx"):
            REQUESTS.labels(method, "<warm>", sc)  # creates the series
            DURATION.labels(method, "<warm>", sc)
```

Now the series exists with value 0 immediately on first scrape. (`<warm>` is a sentinel route value that's filtered out of dashboards.)

### 18.8 Async tasks not awaited at shutdown

A common pattern:

```python
async def handler(request):
    asyncio.create_task(audit_log("user.action", request.user))  # fire-and-forget
    return JSONResponse({"ok": True})
```

If the server starts shutting down between the `create_task` and the audit_log's await of `OTLPLogExporter.export()`, the task is cancelled. The log line is lost.

Two fixes:

1. **Track background tasks**, await them at shutdown:

```python
class TaskRegistry:
    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def drain(self, timeout: float):
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        if pending:
            logger.warning("background.tasks.timeout", pending=len(pending))
            for t in pending:
                t.cancel()

tasks = TaskRegistry()
tasks.spawn(audit_log("user.action", user))
# at shutdown: await tasks.drain(timeout=10)
```

2. **Don't fire-and-forget telemetry-critical work.** If the audit log matters, `await` it before returning.

### 18.9 The "double atexit" / re-entrant shutdown problem

`atexit.register(some_handler)` is fragile in multi-process Python:

- Master registers handler; forks; child inherits; both call the handler at exit. (Idempotent? Hopefully.)
- Handler raises during interpreter shutdown when half the modules are already cleared. (Crashes; lost flush.)
- `signal.signal(SIGTERM, h1)` then later `signal.signal(SIGTERM, h2)` overwrites — last writer wins. A third-party library installing its own SIGTERM handler silently breaks yours.

Defenses:

- Make every shutdown hook **idempotent** (set a `_shutdown_done` flag).
- Use `loop.add_signal_handler` (which composes) rather than `signal.signal` (which replaces).
- Treat `atexit` as best-effort. The real shutdown logic lives in your signal handler.

### 18.10 Liveness/readiness probes during shutdown — the readiness lie

A subtle race:

```
T+0     SIGTERM received
T+0.1   Liveness probe still passing (response: 200)
T+0.2   Readiness probe set to fail (intentionally)
T+10    LB removes pod from rotation (probe failed 5x)
T+10..30  Drain in-flight requests
T+30    Exit
```

But: the **load balancer** discovers the pod is unready only after N failed probes. During those N probes, **new requests still arrive**. If your handler doesn't check `shutdown_event.is_set()` and reject early, those requests start work that the drain budget might not finish.

Defenses:

- **Sleep before exiting readiness.** Set ready=False, then sleep `2 * probe_interval` before any drain, so the LB has time to stop sending traffic.
- **Reject in handler if shutting down** (return 503 with `Retry-After`).
- **Pre-stop hook with sleep**: `lifecycle.preStop: ["/bin/sleep", "10"]` in k8s — gives the LB time to deregister before SIGTERM hits the app.

### 18.11 Multiprocess metric file races

`prometheus_client.multiprocess` writes to mmap'd `.db` files in `PROMETHEUS_MULTIPROC_DIR`. The races:

- **Worker dies without `mark_process_dead`**: file persists, contributes to scrape forever (phantom series). Mitigation: `child_exit` Gunicorn hook calls `mark_process_dead`. For uvicorn workers under gunicorn, this works through the `UvicornWorker` class.
- **Pod restarts without dir cleanup**: if `PROMETHEUS_MULTIPROC_DIR` is on a persistent volume (shouldn't be), files from previous run persist. Mitigation: tmpfs only; wipe on `when_ready`.
- **Concurrent writes to the same file**: counters use atomic adds (`mmap` + `int64`), safe. Histograms write to per-bucket entries, safe. Gauges with `multiprocess_mode="livesum"` aggregate at scrape time, safe. The race that's NOT safe is *gauge with no mode set* — it raises `ValueError` at definition time, so you find out in dev.
- **Scrape during write**: scrape reads all `.db` files; concurrent writes happen; a read can see an inconsistent state across writers. In practice the inconsistency is per-counter, so a single counter is consistent but two counters for the same series may differ by one increment. Negligible for monotonic counters; matters for "ratio of X to Y" alerts. Mitigation: use a derived series rather than two raw counters in the alert rule.

### 18.12 Connection pools during shutdown

DB / Redis / HTTP client pools have their own lifecycle. A common mistake: closing the pool before all in-flight queries complete. Symptoms: "connection lost" errors at the end of every deploy, false-positive error rate spike, alert fires.

```python
# WRONG — pool closed before in-flight query finishes.
async def shutdown():
    await pool.close()
    await drain_inflight()

# RIGHT — drain first, then close.
async def shutdown():
    await drain_inflight()
    await pool.close(timeout=5.0)  # finite timeout so we don't hang
```

If your pool doesn't support `close(timeout=...)`, wrap in `asyncio.wait_for`:

```python
try:
    await asyncio.wait_for(pool.close(), timeout=5.0)
except asyncio.TimeoutError:
    logger.warning("pool.close.timeout")
```

### 18.13 The "deploy spike" race

During a rolling deploy, both old and new replicas serve traffic simultaneously for ~30 seconds. Telemetry consequences:

- **Span volume doubles transiently.** If your tail-sampling has a global-rate quota, it hits limits and starts dropping interesting spans.
- **Two `service.version`s in dashboards.** Filtering panels by version requires explicit panel queries; teams that filter only by `service.name` see "weird" mid-deploy graphs.
- **Counter rate dips then recovers.** New replicas start at 0; rate calculation includes them as they ramp.
- **Alert rules can flap.** A rule that fires on `histogram_quantile(0.99, sum(rate(...)) by (service)) > 0.5` may fire if the new version is 2x slower for the first minute (cold cache, JIT not warm, etc.).

Defenses:

- **Filter dashboards by `service.version`** as a top variable, not just `service.name`.
- **Use `for: 5m` on slow-burn alerts** to avoid deploy-induced flap.
- **Alert on deploy-aware burn rate**: combine the SLO burn-rate alert with `kube_deployment_status_replicas_unavailable > 0` so the alert is suppressed (or escalated) accordingly.

### 18.14 Crash during span export retry storms

When the OTel Collector is unreachable, the OTLP exporter retries with exponential backoff. Pathological case:

- Collector is down for 10 minutes.
- BSP queue fills (default `max_queue_size=2048`).
- New spans are dropped at the queue.
- When Collector returns, the exporter dumps the queue at full speed → Collector sees 10× normal traffic → Collector OOMs → death spiral.

Defenses:

- **Cap retries.** OTLP exporter's `max_retry_attempts` should be ≤ 3 for traces (lose-some > delay-all).
- **Use the Collector as the durability layer**, not the SDK. The SDK should drop after one retry; the Collector should buffer.
- **Don't make `max_queue_size` huge**. Bigger queue = more loss on crash, not less.
- **Monitor `otel_exporter_dropped_spans_total`** (the SDK exposes this if you wire the meter). Spike = problem.

### 18.15 Logger handler atexit deadlock

If a logger handler holds a lock that another thread is waiting on at shutdown, the atexit handler deadlocks the interpreter. Symptoms: process hangs at exit, `terminationGracePeriodSeconds` exhausted, SIGKILL.

The classic case:

```python
# A handler that calls into a logger that holds the same lock.
class CustomHandler(logging.Handler):
    def emit(self, record):
        logger.info("emit", record=record.msg)  # recursive — deadlock waiting
```

Defenses:

- Never log from within a `logging.Handler.emit()`.
- Use `logging.handlers.QueueHandler` + `QueueListener` if your handlers do non-trivial work (network, disk). The listener thread does the work; the handler thread just enqueues.
- At shutdown, **stop the queue listener before closing the underlying handler.**

```python
import logging.handlers, queue

log_queue = queue.Queue(maxsize=10000)
queue_handler = logging.handlers.QueueHandler(log_queue)
listener = logging.handlers.QueueListener(log_queue, *real_handlers, respect_handler_level=True)
listener.start()
logging.getLogger().addHandler(queue_handler)

# at shutdown:
listener.stop()  # drains queue first, then closes
```

### 18.16 Daemon threads mid-operation

OTel's BSP, prometheus_client's Pushgateway thread, and many libraries spawn `daemon=True` threads. Daemon threads are killed *immediately* on interpreter shutdown — no `finally` blocks, no exit cleanup, mid-write.

For most telemetry this is acceptable (the data is opportunistic). But:

- A daemon thread mid-`os.write()` to a metric file may leave a corrupted entry (rare but observed).
- A daemon thread holding a network connection mid-send leaves the peer with a dangling socket.

Defenses:

- For audit-critical paths, use non-daemon threads with explicit join in shutdown.
- Set `BatchSpanProcessor.export_timeout_millis` low enough that an un-joined exporter doesn't hold up shutdown.

### 18.17 Async tasks crashing silently

`asyncio.create_task(foo())` returns a Task. If `foo()` raises and nobody calls `result()`/`exception()`, the exception is logged at DEBUG level (Python ≤3.11) or via `loop.set_exception_handler` (3.12+). Easy to miss.

```python
def _handle_exception(loop, ctx):
    exc = ctx.get("exception")
    msg = ctx.get("message", "<no message>")
    logger.error("asyncio.unhandled_exception", message=msg, exc_info=exc)
    # Increment a counter so you can alert on it.
    UNHANDLED.inc()

loop.set_exception_handler(_handle_exception)
```

Without this, your "fire and forget" tasks fail silently and the missing telemetry is the only signal.

### 18.18 Idempotency: jobs and consumers across restarts

For Celery / Kafka consumers: a worker restarting mid-message means the same message is re-delivered. Without idempotency, the side effects (charges, emails, DB inserts) duplicate. The telemetry consequence:

- Same `messaging.message.id` produces two consumer spans.
- Both spans are children of the *original* producer span (good — trace shows the duplicate).
- Same business metric counter increments twice.

Defenses:

- **Idempotency keys** on side effects (database unique constraint, Stripe idempotency_key header).
- **Span attribute `messaging.delivery_attempt`** captures the retry count; a span with `attempt=2` is informative.
- **Counter increments inside the idempotent boundary only**, not at message receipt.

### 18.19 Cold starts and first-N-requests instrumentation

Lambda, Cloud Run, scale-from-zero in k8s: the first request after a cold start has:

- OTel SDK initialization in-line (200-500ms).
- DB connection pool empty (first connection time).
- Module imports cold (Python's lazy import).
- JIT-equivalent (CPython has none, but pre-imports of NumPy/Pandas can be 1-2s).

Defenses:

- **Init telemetry at module-load**, not at first-request, so the first request span is clean.
- **Distinguish cold vs warm in spans**: `app.cold_start = true` attribute on first-request span. Saves debugging "why is one request slow".
- **Pre-warm pools** in startup, not lazily.

```python
COLD_START = True

@app.middleware("http")
async def mark_cold_start(request, call_next):
    global COLD_START
    span = trace.get_current_span()
    if COLD_START:
        span.set_attribute("app.cold_start", True)
        COLD_START = False
    return await call_next(request)
```

### 18.20 The trace ID generation race

`opentelemetry.sdk.trace.RandomIdGenerator` uses `os.urandom()` under the hood — thread-safe and fork-safe. **However**:

- A custom `IdGenerator` using `random` (not `secrets`/`urandom`) inherits the parent's PRNG state on fork; multiple workers generate the same sequence. Result: trace_id collisions across workers, broken trace assembly.

Always use the default `RandomIdGenerator`. If you must roll your own (rare), call `os.urandom()` or re-seed in `post_fork`.

### 18.21 Time skew across replicas

Span timestamps are local. A 1-second clock skew between replica A and replica B produces:

- "Negative duration" spans in the trace store (span ends before its parent started, on the wall clock).
- Misleading service-graph latency (if A's clock is fast, the A→B edge appears longer than it is).

Defenses:

- Run an NTP/chrony daemon. Verify clock skew with a metric (`node_timex_offset_seconds` if you have node_exporter).
- Spans use *monotonic* time within a process for duration; only timestamps cross processes — accept some skew.
- Alert on `time.time() - ntp_time > 100ms` per node.

### 18.22 The race between span end and queue overflow

`BatchSpanProcessor.on_end()` enqueues; if the queue is full, it drops. Drops are silent unless you wire `dropped_spans` counters. This race happens during:

- Burst traffic (10× normal for 30s).
- Collector outage (queue drains slowly).
- A handler emitting hundreds of spans per request (per-iteration of a loop — see anti-pattern #13).

Defenses:

- Cap span emission per request. A request emitting > 50 spans is suspicious; > 500 is broken.
- Wire the SDK's diagnostic metrics (`OTEL_PYTHON_SDK_DROPPED_SPANS` or via the SDK's internal callbacks) into your Prometheus output.

### 18.23 Race-condition checklist for the staff engineer

Quick-reference for the lifecycle hardening review:

- [ ] **`post_fork` re-init OTel** in every worker spawn point (Gunicorn, Celery, multiprocessing).
- [ ] **`tracer_provider.shutdown()` and `meter_provider.shutdown()`** wired to SIGTERM handler.
- [ ] **`SHUTDOWN_DRAIN_SECONDS = terminationGracePeriodSeconds - 10`** in container env.
- [ ] **`terminationGracePeriodSeconds ≥ 60`** for services with > 5s P99 or queue consumers.
- [ ] **Pre-stop hook with `sleep 10`** to let LB deregister before SIGTERM.
- [ ] **Readiness probe** flips to false at shutdown start; handler returns 503 if `shutdown_event.is_set()`.
- [ ] **`mark_process_dead(worker.pid)`** in Gunicorn `child_exit`.
- [ ] **`PROMETHEUS_MULTIPROC_DIR` on tmpfs**; wiped on `when_ready`.
- [ ] **`SimpleSpanProcessor` for audit-grade events**; `BatchSpanProcessor` for everything else.
- [ ] **Background task registry** with `await drain(timeout)` at shutdown.
- [ ] **`loop.set_exception_handler`** wired to a counter and logger.
- [ ] **`max_retry_attempts` capped on OTLP exporter** (≤ 3); rely on Collector for durability.
- [ ] **Series warmup at startup** — exercise key label combinations once.
- [ ] **`process_start_time_seconds` watched** in alerts to detect restart loops.
- [ ] **`app.cold_start=true` attribute** on first-request span.
- [ ] **NTP / clock-skew alert** on every node.
- [ ] **Logger uses `QueueHandler`** if any handler does network/disk work.
- [ ] **No logging from within `logging.Handler.emit`** (audit for recursion).
- [ ] **Pool close has finite timeout**; in-flight drained first, pool closed second.
- [ ] **Idempotency keys on side effects** for queue consumers; restart-safe.
- [ ] **`asyncio.create_task` results tracked**; no silent unhandled exceptions.
- [ ] **Default `RandomIdGenerator`** used (or re-seeded in `post_fork`).
- [ ] **Deploy-spike test**: synthetic load test that runs through a rolling restart and verifies no telemetry gap > N seconds.
- [ ] **Crash test in CI**: SIGKILL the process during a request; verify the request that was in flight does not corrupt state and that the next replica picks up cleanly.

The single sentence that summarizes this section: **observability that survives a rolling deploy is engineered; observability that doesn't is implicit and lies under load**. Engineering observability for the lifecycle is what separates a working service from a reliable one.

---

## 19. Anti-Patterns: The Python Hall of Shame

The list of mistakes that show up in real Python codebases. Identify and fix each one *before* it produces an outage.

1. **Using `print()`.** Bypasses every redaction and structuring policy. Replace with `structlog.get_logger().info(...)`. CI lint that fails on `print(` outside tests.
2. **`logger.info(f"user {user_id} did X")`** — string-formatted logs. Lost-cause for queries. Use `logger.info("did X", user_id=user_id)`.
3. **`prometheus_client` without multiprocess setup under Gunicorn.** Metrics lie silently. See §5.2.
4. **`threading.local()` for request context in async code.** `await` doesn't carry it. Use `contextvars`. See §1.3.
5. **`opentelemetry-instrument python app.py` for a real service.** Magic; non-deterministic order; hides what's running. Write explicit setup. See §3.5.
6. **Per-request OTel SDK init.** SDK lifecycle is per-process. One init, in bootstrap. Re-init in `post_fork`.
7. **Histogram buckets unchanged from default.** The default targets 5ms-10s; if your SLO is 50ms you're alerting on the wrong quantile. See §3.2 of doc 03.
8. **`route=request.url.path` in metric labels.** Unbounded cardinality. Use the framework's templated route.
9. **`error_class=str(exc)` in metrics.** Includes UUIDs, IDs, paths. Use `error_class=type(exc).__name__`.
10. **`logger.exception()` in a tight loop.** N stack traces per error per second. Aggregate at the boundary; log the count per batch.
11. **Profiling with `cProfile` in production.** 50-300% overhead. Use py-spy.
12. **No `child_exit` hook in Gunicorn.** Phantom dead-worker series in `/metrics` forever.
13. **OTel span for every line of business code.** Per-iteration spans of a 100k-row loop = OOM in the BatchSpanProcessor queue. Span per stage, not per row.
14. **Mixing sync DB drivers in async handlers via `asgiref.sync_to_async`.** Hidden thread-pool blocking; loop lag spikes.
15. **No `excluded_urls` for `/healthz`.** Spans on every probe; trace store fills with noise.
16. **`logger.debug(f"env: {os.environ}")`.** Logs all secrets. Allow-list only. See §14.3.
17. **`asyncio.create_task(...)` without context propagation.** The fire-and-forget task doesn't share contextvars unless you use `asyncio.create_task(coro, context=current_ctx)`.
18. **One giant `@traced` decorator that spans every method on every class.** Spans for `__getattr__`, `__init__`, etc. Trace storage explodes. Span the *boundary*, not the internals.
19. **Auto-instrumenting libraries that you don't use** (because they were imported transitively). Bloats startup, wraps unused calls, more attack surface in the patches. Selective instrumentation only.
20. **`@functools.cache` on a function with span-creating side effects.** Caches the *first* span's lifetime forever. Caching and instrumentation interact poorly; verify both.
21. **Putting `pyinstrument` middleware on every request in production.** 5-10ms per request. Gate by token, run on demand.
22. **Mixing `prometheus_client.Summary` and `Histogram` for the same SLI.** Summary quantiles can't aggregate across instances. Histogram only. See §5.4 of doc 03.
23. **`tracer.start_span()` without `with use_span(...)`.** The span never becomes the active span; child spans don't attach. Use the context manager.
24. **Failing to call `span.set_status(Status(StatusCode.ERROR))` on caught exceptions.** Trace is "OK" with an error inside. Always set status when you catch.
25. **Pinning `opentelemetry-api` and `opentelemetry-sdk` to different versions.** Silent span drops. Pin both to the same version.
26. **`asyncio.run(main())` followed by orphan threads.** OTel's BatchSpanProcessor exit handler doesn't always fire; spans lost. Use `tracer_provider.shutdown()` explicitly in `finally:`.
27. **No log when a circuit breaker trips.** The single most-debugging-relevant event is invisible. Always log state transitions of resilience primitives.
28. **Log levels that don't match production.** Defaulting to DEBUG in dev and forgetting to change for prod. Set via env var; assert in production startup.
29. **No `OTEL_RESOURCE_ATTRIBUTES` set.** Spans land with no `service.version`; "did this start at deploy X?" unanswerable.
30. **Importing the app module at module-load time of `gunicorn.conf.py`.** Causes double-import and weird OTel state. Defer to hook functions.

---

## 20. Pitfalls and Edge Cases

A scattered set of "lost a day to this" gotchas.

- **`uvloop` and OTel context.** `uvloop` is a drop-in replacement for `asyncio` event loop, written in Cython. It uses Python's contextvars correctly *as of recent versions*, but older versions had quirks. Verify with an integration test.
- **`gevent` / monkey-patching.** Gevent monkey-patches stdlib at import. If `gevent.monkey.patch_all()` runs *after* OTel sets up its socket, OTel's HTTP exporter starts blocking. Patch first, then init OTel.
- **`fork` in middleware.** Some webapps `fork()` for image processing or mail. The forked child inherits OTel state including the BatchSpanProcessor's exporter thread, which is now dead. Re-init in the child.
- **`__del__` methods that log.** `__del__` runs at indeterminate times, sometimes during interpreter shutdown when the logger is partially destroyed. Move cleanup logging to explicit `close()` or context managers.
- **`signal.signal(SIGTERM, ...)`** without OTel shutdown. Container kill → exporter never flushes → last 5 seconds of spans lost. Register a handler that calls `tracer_provider.shutdown()` and `meter_provider.shutdown()`.
- **`pytest` collecting `app.py` triggers OTel setup.** Tests fail with "endpoint refused". Gate setup behind `if __name__ == "__main__"` or env var; or use the `InMemorySpanExporter` test fixture.
- **`black`/formatter changes line numbers** that exemplars in source-link tools point to. Lock line-number-sensitive tooling on the CI side.
- **Lambda cold starts.** OTel initialization during a cold start adds 200-500ms. For sub-100ms cold starts, defer trace init or use the [AWS Lambda OTel layer](https://aws-otel.github.io/docs/getting-started/lambda).
- **`asyncio.gather(*tasks, return_exceptions=True)`** swallows exceptions silently. The span shows OK but a task raised. Iterate the results and `record_exception()`/`set_status()` per failure.
- **`structlog` in tests with `pytest.caplog`.** caplog hooks into stdlib `logging`; if your structlog output goes through the stdlib bridge, caplog sees it; if it goes direct to stdout, caplog doesn't. Configure your test environment carefully.
- **Multi-line tracebacks in JSON logs.** A JSON log line containing a multi-line traceback breaks line-based log shippers (Fluent Bit's default tail). Either escape `\n` (default JSON behavior) or configure a multi-line parser.
- **`sys.settrace`** is used by debuggers, profilers, and some auto-instrumentors. Two of them at once typically break — only one trace function lives. Document which is on in production.
- **C extension stack traces.** Native crashes in NumPy / Pillow / lxml don't produce Python tracebacks. Configure `faulthandler.enable()` at startup to get a C stack on segfault.
- **Containers without TTY.** If your stdout is buffered (Python's default), logs lag by seconds and crashes lose the last buffer. Set `PYTHONUNBUFFERED=1` always in container envs.
- **`__repr__` that does I/O.** Logging an object whose `__repr__` queries the DB for a name will hide a query in the log line and produce circular tracing. Implement cheap `__repr__` always.

---

## 21. The Staff-Level Standards Checklist

The bar a staff engineer should hold every Python service to before it serves production traffic. Print and tape to the desk.

### Service-level

- [ ] **OTel SDK initialized in `bootstrap.py` before app import.**
- [ ] **`OTEL_SERVICE_NAME`, `service.version`, `deployment.environment` resource attributes set.**
- [ ] **OTLP exporter pointed at a local Collector** (sidecar or daemonset), not vendor direct.
- [ ] **Selective auto-instrumentation** for HTTP framework, requests/httpx, DB driver, Redis, gRPC.
- [ ] **`structlog` configured with contextvars binding and OTel trace_id processor.**
- [ ] **Every log line carries `trace_id`** (verified with a test).
- [ ] **Stdlib `logging` bridged through structlog's processor chain** so library logs are structured too.
- [ ] **Log redaction processor** for emails, cards, and known secret keys.
- [ ] **`PYTHONUNBUFFERED=1` in the container env.**
- [ ] **Production log level is INFO**, not DEBUG.
- [ ] **Noisy library log levels tamed at startup** (`urllib3`, `botocore`, etc.).

### Metrics

- [ ] **`prometheus_client` multiprocess mode** if Gunicorn/Uvicorn workers > 1.
- [ ] **`PROMETHEUS_MULTIPROC_DIR` set**, wiped on startup, on a tmpfs.
- [ ] **`when_ready` and `child_exit` Gunicorn hooks wired up.**
- [ ] **Every counter ends in `_total`**, every histogram in `_seconds` or `_bytes`.
- [ ] **`http_requests_total{method, route, status_class}`** counter.
- [ ] **`http_request_duration_seconds_bucket`** histogram with **SLO-aligned buckets**.
- [ ] **Histogram exemplars wired to OTel `trace_id`.**
- [ ] **`http_inflight_requests`** gauge (saturation).
- [ ] **`asyncio_event_loop_lag_seconds`** for any async service.
- [ ] **DB pool gauges** (`db_pool_acquired`, `db_pool_idle`, `db_pool_wait`).
- [ ] **No PII / IDs / unbounded values in metric labels** (verified with CI test).
- [ ] **Cardinality budget audited** (top-N series count below platform threshold).
- [ ] **`/metrics` endpoint excluded from access logs and traces.**

### Tracing

- [ ] **Server span on every inbound request** (auto-instrumentor).
- [ ] **Client span on every outbound HTTP/gRPC/DB call.**
- [ ] **Trace context propagated** across threads (executor wrapper) and queues (Kafka/Celery middleware).
- [ ] **W3C Trace Context propagator + Baggage** (B3 only if needed).
- [ ] **SQLCommenter enabled** on the DB instrumentor.
- [ ] **Sampler is Parent-based + RouteAware** (low-rate default, 100% on critical routes, 0% on health).
- [ ] **Tail sampling at the Collector** for keep-on-error/slow.
- [ ] **`span.set_status(StatusCode.ERROR)` on every caught exception.**
- [ ] **`span.record_exception()` on every caught exception.**
- [ ] **`tracer_provider.shutdown()` called on SIGTERM** for graceful flush.

### Profiling

- [ ] **py-spy or austin sidecar** running continuously, pushing to Pyroscope/Parca.
- [ ] **`SYS_PTRACE` capability** granted to the profiling sidecar only.
- [ ] **pyinstrument middleware** behind a header gate for ad-hoc deep profiling.
- [ ] **`faulthandler.enable()`** at startup for native crash capture.
- [ ] **No cProfile in production.**

### Worker model

- [ ] **Worker count explicit** (`-w N`), not the implicit `--workers` default.
- [ ] **`preload_app = True`** with `post_fork` re-initializing OTel.
- [ ] **Graceful shutdown timeout ≥ exporter flush interval.**
- [ ] **`/healthz` and `/readyz` return fast** (< 50ms) and **don't open DB connections.**

### Testing

- [ ] **InMemorySpanExporter fixture** for span assertions in tests.
- [ ] **`caplog` log assertion tests** for critical audit events.
- [ ] **Cardinality CI test** asserting no forbidden labels.
- [ ] **Templated-route CI test** asserting no raw URLs in metric output.
- [ ] **Smoke test post-deploy** that issues a request and verifies the trace lands in the trace store.

### Standards

- [ ] **OTel package versions pinned** (api, sdk, exporter all same version).
- [ ] **Contrib instrumentors pinned** to a compatible version.
- [ ] **Dashboard JSON in git** with CI lint that every panel's metric exists.
- [ ] **SLO defined** for the service's primary user journey (doc 13).
- [ ] **Multi-window multi-burn-rate alert** wired to the SLO (doc 12).
- [ ] **Runbook linked from the alert** (doc 14).
- [ ] **Production Readiness Review passed** (doc 17).

If a service ticks every box above, it is *staff-engineer quality* in observability. The number of production Python services that meet this bar today is small. Closing the gap is the work.

---

**TL;DR Python observability.** *OTel SDK + structlog + prometheus_client multiprocess + selective auto-instrumentation + py-spy continuous profiling*, set up once in a `bootstrap.py` that runs before the app imports. Every log line carries `trace_id` via a contextvars-aware processor. Every metric label is bounded; every histogram has SLO-aligned buckets and exemplars. Worker model dictates the multiprocess setup. Async means contextvars, not threading.local. Profile with py-spy, never cProfile. Test the telemetry like you test the code. Pin OTel versions exactly. Treat it all as production code, not as a side project that engineers add at 4pm on Friday.
