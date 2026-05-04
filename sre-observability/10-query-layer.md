# 10 — The Query Layer: PromQL, LogQL, TraceQL, SQL

The query layer is where storage meets the human. Every dashboard panel, every alert rule, every ad-hoc 3 a.m. investigation, every capacity report flows through it. The data sitting on disk is worth nothing until a query can pull the right slice cheaply, fast enough to support exploration. This chapter goes deep on the four query languages a Staff Engineer in this space must read fluently and write competently: **PromQL**, **LogQL**, **TraceQL**, and **SQL on telemetry**.

These four languages exist because the storage layer underneath each signal has a different physical shape. You cannot use the same engine for "what is the rate of HTTP 500s?" and "show me every log line for trace 4f2a…" without making one of the two miserable. The query languages are the user-visible projection of the storage choice.

> **Mental model:** A query language is a contract between *what the storage knows how to do quickly* and *what the human wants to ask*. PromQL is fast at "aggregate-by-label-over-window"; LogQL is fast at "select-by-label, then grep"; TraceQL is fast at "select-by-trace-id, then walk-the-DAG"; SQL is fast at whatever you're willing to pay to scan.

---

## Table of Contents

1. [The Big Picture: Why Four Languages](#1-the-big-picture-why-four-languages)
2. [PromQL Deep Dive](#2-promql-deep-dive)
3. [LogQL Deep Dive](#3-logql-deep-dive)
4. [TraceQL Deep Dive](#4-traceql-deep-dive)
5. [SQL on Telemetry](#5-sql-on-telemetry)
6. [Cross-Engine Concerns: Limits, Caching, Sharding, Federation](#6-cross-engine-concerns)
7. [End-to-End Performance: How a Query Actually Runs](#7-end-to-end-performance)
8. [Pitfalls](#8-pitfalls)
9. [What's Changing in 2024–2026](#9-whats-changing)
10. [Mental Models and Glossary Additions](#10-mental-models-and-glossary)

---

## 1. The Big Picture: Why Four Languages

Each storage engine in chapters 06–08 made a different physical trade-off. The query language sits directly on top.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          QUERY LAYER                                    │
│                                                                         │
│   PromQL ───────────►  TSDB     (Prom, Mimir, VM, Thanos, M3)           │
│                        chunks indexed by label set                      │
│                                                                         │
│   LogQL  ───────────►  Loki     (label index + object-store chunks)     │
│                        Lucene──►  ES / OpenSearch (full inverted idx)   │
│                                                                         │
│   TraceQL ──────────►  Tempo    (object store keyed by trace_id)        │
│                        Jaeger   (span store + service graph)            │
│                                                                         │
│   SQL    ───────────►  ClickHouse, BigQuery, Snowflake, DuckDB,         │
│                        Druid, Pinot                                     │
│                        columnar tables for any signal                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  query-time joins via trace_id
        ┌─────────────────────────────────────────────────────┐
        │  Correlation: metric exemplar → trace → log line    │
        │  This is the only thing that makes triage possible. │
        └─────────────────────────────────────────────────────┘
```

| Signal | Storage shape | Best at | Bad at |
|---|---|---|---|
| Metrics | float64 + label set, time-ordered, per-series chunks | aggregate-over-window, group-by-label | per-event detail, free-text search |
| Logs (Loki-style) | label-indexed streams of opaque blobs | "stream X over time window Y, then filter" | full-text without label hint |
| Logs (ES/OS) | inverted index per token, JSON doc store | arbitrary text + faceted search | cost; cardinality of unique terms |
| Traces (Tempo) | object store keyed by trace_id | "give me trace ID X" | "find traces where …" without spanmetrics |
| Traces (CH) | columnar wide table, one row per span | rich predicates, joins, analytics | retrieval cost per single trace |
| Telemetry on lakehouse | parquet on object store, query-on-read | ad-hoc analytics, joins, ML | sub-second dashboards |

The languages reflect these shapes. PromQL has no `JOIN` because the TSDB has no concept of cross-series rows. LogQL has no `GROUP BY` over arbitrary fields because the index doesn't know about them. TraceQL has structural operators (`>>`, `>`) because the data model is a DAG. SQL has all of them because the storage was *built* to do everything; you pay for that flexibility per query.

> **Pitfall:** The most common architectural mistake is forcing one language onto a storage shape it wasn't built for. Running PromQL against a relational table or SQL against Prometheus-as-a-source-of-truth both work in toy demos and collapse at production scale.

---

## 2. PromQL Deep Dive

PromQL is the lingua franca of metrics. Mimir, VictoriaMetrics, Thanos, Cortex, and Grafana Cloud all speak some flavor of it — VM with extensions, the others largely faithfully. Reading PromQL is now a baseline skill; writing it well is rarer.

### 2.1 The Four Data Types

PromQL has exactly four data types. Every operator's signature is some combination of these.

| Type | Shape | Example | Where it shows up |
|---|---|---|---|
| **Instant vector** | set of `(label_set, value)` at a single timestamp | `up{job="api"}` | dashboard panels, alert expressions |
| **Range vector** | set of `(label_set, [(t,v), …])` over a window | `up[5m]` | input to `rate()`, `avg_over_time()`, etc. |
| **Scalar** | single float | `1024`, `vector(1)` | thresholds, time-shifts |
| **String** | only inside `label_replace` and a few label functions | `"abc"` | almost never user-facing |

The grammar is strict: a range vector cannot be displayed directly. `up[5m]` returns an error in Grafana panels. You must wrap it in a range function — `rate(up[5m])`, `last_over_time(up[5m])`, etc. — that collapses time back to a single point per series.

### 2.2 Range Vectors — What `[5m]` Physically Means

Mentally, `http_requests_total[5m]` evaluated at `t=12:00:00` returns: *for every series matching `http_requests_total`, the list of all (timestamp, sample) pairs whose timestamp lies in `(11:55:00, 12:00:00]`*.

Physically, the TSDB:
1. Resolves the metric name + label matchers via the postings index → list of series IDs.
2. For each series ID, identifies which chunks intersect `[t-5m, t]` (chunks are roughly 2-hour bounded; one or two will be in scope).
3. Decompresses XOR-encoded chunks (or native histograms) into raw samples.
4. Emits the matching subset.

The cost of the query is roughly **(series matched) × (samples per series in window)**. With a 15-second scrape interval, 5 minutes is ~20 samples per series. With 10,000 matching series, that's 200k samples — trivial. With 10M matching series (regex-too-permissive on a high-cardinality label), the same query reads 200M samples and triggers OOM.

> **Mental model:** A range vector is *just an array of samples per series*. PromQL's range functions (`rate`, `avg_over_time`, `quantile_over_time`, etc.) are reductions over that array. Once you see this, every PromQL idiom becomes obvious.

### 2.3 `rate()` vs `increase()` vs `irate()` — When Each Lies

These three functions all derive "per-second change" from a counter, with different trade-offs.

| Function | What it does | When to use | When it lies |
|---|---|---|---|
| `rate(c[5m])` | Linear regression over the window | Dashboards, alerts, the default | Window too short (< 4× scrape interval) → noisy; window too long → hides spikes |
| `increase(c[5m])` | Same as `rate(c[5m]) * 5*60` — total over window | "How many events in the last 5 minutes" panels | Same gotchas as `rate`; off-by-one if window not aligned to scrape |
| `irate(c[5m])` | Slope between the *last two* samples in the window | Live debugging, very recent latency spikes | Misses anything older than the last two samples; useless in alerting (flaps) |

All three handle **counter resets** automatically: when sample N+1 < sample N, the function assumes the counter was reset to zero and the delta is `sample[N+1] - 0`, not `sample[N+1] - sample[N]`. This is critical because process restarts reset every counter to zero.

The 4×-scrape-interval rule for `rate()`:

```
scrape_interval = 15s
recommended:  rate(c[1m]) at minimum, rate(c[5m]) typical, rate(c[1h]) for SLO calc
NOT this:     rate(c[15s])   — only one or zero samples; returns NaN often
NOT this:     rate(c[30s])   — exactly two samples; one missing scrape kills it
```

Why? `rate()` requires at least *two* samples in the range to compute a slope. A 30-second window holds two scrapes if everything's healthy; one missing scrape leaves one sample and `rate()` returns nothing. A 1-minute window survives a single missing scrape; 5-minute windows survive several.

> **Pitfall:** `rate(http_requests_total[1h])` at 12:00:00 returns the *average rate over the past hour*, not the *current rate*. If traffic just doubled, the panel will show the slow ramp, not the spike. Use `rate([5m])` for "what is happening right now" and `rate([1h])` for "what has been steady."

### 2.4 `sum`, `avg`, `min`, `max` and the `by` / `without` Clauses

Aggregation operators reduce an instant vector along selected label dimensions.

```promql
# Total request rate per service, summing across all instances/methods/routes
sum by (service) (rate(http_requests_total[5m]))

# Same thing, expressed as "sum across everything except service"
sum without (instance, method, route, status, pod, container) (
  rate(http_requests_total[5m])
)
```

`by` says *keep these labels, drop the rest*. `without` says *drop these labels, keep the rest*. `without` is the safer default: when someone adds a new label to the metric, `by` silently keeps too few dimensions and the panel changes meaning, while `without` keeps the new label automatically.

A subtle one: averaging a rate across instances *is not* the same as the rate of the sum.

```promql
# Average per-instance request rate (rarely what you want)
avg by (service) (rate(http_requests_total[5m]))

# Total request rate for the service (almost always what you want)
sum by (service) (rate(http_requests_total[5m]))
```

The first answers "how loaded is a typical instance?". The second answers "how busy is the service?". They differ by `count(instances)`. Most dashboards mean the second and use the first by accident.

### 2.5 `histogram_quantile()` — Bucket Math and Why p99 Is Always an Estimate

Classic Prometheus histograms are pre-bucketed at instrumentation time. The metric `http_request_duration_seconds_bucket{le="0.5"}` is the *count of requests that completed in ≤ 0.5 s*, and there is one such series per bucket boundary (`le=0.005`, `le=0.01`, …, `le=10`, `le=+Inf`).

To compute p99 you ask the engine to estimate which bucket the 99th-percentile sample fell into:

```promql
histogram_quantile(
  0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

The mechanics:
1. `rate(..._bucket[5m])` per-series rate of each cumulative bucket counter.
2. `sum by (le)` aggregates across whatever you don't want to keep, leaving only the bucket dimension.
3. `histogram_quantile(0.99, …)` finds which `le` boundary contains the 99th percentile of the (estimated) underlying distribution and **linearly interpolates** within that bucket.

This is an estimate twice over: once because the bucket count loses sub-bucket detail, and once because the inter-bucket interpolation assumes a uniform distribution within each bucket (it isn't). The error is bounded by the bucket width above the true quantile.

```
buckets:    le=0.1   le=0.25  le=0.5   le=1.0   le=2.5   le=+Inf
counts:      8000     9500    9990     9999     9999     10000
              80%      95%    99.9%    99.99%   99.99%

True p99? Somewhere between le=0.25 and le=0.5 — the engine picks the
midpoint by default.  If the real distribution clusters near 0.25, p99 is
overestimated; if near 0.5, underestimated.  Either way, the answer is
"somewhere in [0.25, 0.5]" — never a precise number.
```

**Bucket choice is the SLI.** If your SLO is "p99 < 250 ms," you need a bucket boundary at exactly 0.25. Otherwise you're alerting on an interpolated number that drifts with traffic shape. A common default is exponential boundaries (`0.005, 0.01, 0.025, …, 10`); pick boundaries tight around your SLO target.

> **Pitfall:** Computing `histogram_quantile` over a `sum without (le)` aggregation will silently return wrong numbers — you must keep `le` (it's the bucket axis) and aggregate everything else. Most "p99 looks weird" tickets are this.

### 2.6 Native Histograms (Prometheus 2.40+, Mimir, Grafana 9+)

Classic histograms have two failure modes: bucket count multiplies cardinality (each bucket is a series), and the bucket layout is fixed at compile time. Native histograms (sometimes called sparse histograms) solve both: a single series per metric, exponentially-spaced buckets generated automatically, populated only where data lands.

```promql
# Same syntax as classic, different storage:
histogram_quantile(0.99, sum(rate(http_request_duration_seconds[5m])))
#  ↑ no _bucket suffix      ↑ no `by (le)` — the bucket axis is internal
```

Wins:
- ~100× smaller storage for the same precision.
- No bucket-boundary tuning required.
- Higher precision automatically (relative error fixed at ~5% per bucket).

Costs:
- Requires Prometheus 2.40+ on both sides (scrape + remote_write).
- Grafana 9.5+ to render properly.
- Some Mimir/Thanos versions still in flight; check before relying.

> **Mental model:** Classic histograms are *one series per bucket boundary you pre-declared*. Native histograms are *one series whose value is itself a small histogram*. The wire format and storage change; the PromQL syntax is preserved.

### 2.7 Subqueries `[5m:1m]` — The Gotcha

Subqueries let a range function nest over the result of an instant query, evaluated at a sub-step:

```promql
# Max of (5-minute rate) over the past hour, sampled every 1 minute:
max_over_time(
  rate(http_requests_total[5m])[1h:1m]
)
```

This is read as: "evaluate `rate(...)[5m]` at every 1-minute step over the last 1h, take the max of those 60 values."

The gotchas:
- Subqueries are **expensive** — they evaluate the inner expression `(window/step)` times per outer evaluation.
- The default step (when omitted: `[1h:]`) equals the global query step, which can be coarser than expected during long range queries — the query loses fidelity at zoom.
- Recording rules cannot use subqueries directly; you have to materialize the inner expression to a separate recording rule first.

Use subqueries sparingly. If you find yourself reaching for them every panel, you probably need a recording rule.

### 2.8 Recording Rules vs Ad-Hoc

A recording rule is a PromQL expression evaluated on a fixed schedule (e.g. every 30 s) with the result written back as a *new metric*. They exist for three reasons:

1. **Cost.** Heavy queries (long-window aggregations across many series) are evaluated once instead of every dashboard refresh.
2. **Stability.** Alert expressions read the precomputed series instead of recomputing on every evaluation, eliminating jitter.
3. **Layering.** SLO computations naturally chain — `service:request:rate1m` → `slo:request:burnrate6h` — and recording rules give you the layer.

The naming convention (Prometheus operator style):

```
<level>:<metric>:<operations>

http_request_duration_seconds:rate1m         # service-level rate
service:http_request_duration_seconds:p99_5m # service-level p99
slo:checkout:burnrate_6h                     # SLO-level burn rate
```

The colon-separated levels make it visually obvious whether a metric is raw (no colon) or derived (one or more). Most teams enforce this with a CI check.

> **Pitfall:** Recording rules that themselves perform `rate()` on already-aggregated series can compound rounding errors. The rule of thumb: aggregate first (`sum`), then rate (`rate(sum_series[5m])` *or* `sum(rate(raw[5m]))` — both are valid; pick one and stick with it).

### 2.9 Engine Internals — Planner, Vector Matching, Evaluator

The Prometheus engine (the canonical reference; Mimir/Thanos/VM share the model with extensions) executes a query in three phases:

```
1. PARSE
   PromQL → AST (vector_selector, function_call, binary_op, aggregation)

2. PLAN / OPTIMIZE
   - Push label matchers down to storage (postings lookup)
   - Identify constant subexpressions
   - Determine evaluation step ranges (query.timeRange × step)

3. EVAL
   For each evaluation timestamp t in [start, end]:
     a. fetch series via storage.SelectSorted(matchers, [t-lookback, t])
     b. for range functions, also fetch [t-window, t]
     c. apply function (rate, sum, histogram_quantile, …)
     d. for binary ops, perform vector matching (label set intersection)
     e. emit instant vector at t
```

The single most expensive primitive is `storage.Select`, which under the hood does:
- `postings.Get(metricName=...)` — list of series IDs matching the metric name.
- For each label matcher, intersect with `postings.Get(label=value)` — set intersection on roaring bitmaps.
- For each surviving series, locate chunks intersecting the time range from the head block + on-disk blocks.
- Decompress the matching segments.

The work scales with **(series matched) × (chunks per series) × (samples per chunk)**. Bad regex matchers (`pod=~".*"`) skip the postings short-circuit and force a full series scan; this is one of the top two reasons a Prometheus dies.

#### Vector matching

Binary operators between two instant vectors require *matching* — pairing series by label set. The default is one-to-one matching on identical labels:

```promql
errors_total / requests_total   # implicit: match on all labels except __name__
```

When the label sets don't match, you control matching explicitly:

```promql
# Many-to-one: many error-typed series per request series
errors_total / on (service, route) group_left requests_total

# Ignoring labels that exist on only one side
errors_total / ignoring (status) requests_total
```

`group_left` and `group_right` are the cardinality-direction modifiers. They are unintuitive and worth pausing on every time you read or write them. The "left" / "right" refers to *which side has many series for each one on the other side* — `group_left` means *the left side is many*. Reading it as "join, with the many-side on the left" is the trick.

> **Pitfall:** `errors / requests` returns no series at all if a single label is mismatched between numerator and denominator (e.g. one has `endpoint=...` and the other doesn't). Silent empty result. Always test in a panel before alerting on it.

### 2.10 Common Bugs to Look For in Code Review

| Bug | What it looks like | Fix |
|---|---|---|
| Bare counter in panel | `http_requests_total` (monotonic; rises forever) | wrap in `rate()` or `increase()` |
| Off-by-one window | `rate(c[15s])` with 15s scrape | use `[1m]` minimum |
| Mixing units | `latency_ms / 1e6` to get seconds | the metric is already seconds; unit hygiene at *instrumentation* |
| Lost labels in `by` | `sum by (service)` drops `cluster` | use `without` or list every label |
| `histogram_quantile` over wrong axis | `sum without (le) (...)` | always keep `le` for the histogram axis |
| Counter that resets to non-zero | rates briefly negative on restart | nothing to fix; `rate()` handles it; suppress alerts during deploy windows |
| `label_replace` regex eats everything | `label_replace(v, "x", "$1", "y", "(.*)")` matches empty | anchor the regex (`"^(.+)$"`) |
| `up == 0` alert flaps | scrape miss vs target down | use `absent_over_time(up[5m])` or `up == bool 0` for clarity |
| Quantile of a quantile | `quantile(0.99, p99_recording_rule)` | quantiles don't compose — re-derive from histogram |
| Subquery in alert expression | costly + flappy | move to recording rule |

---

## 3. LogQL Deep Dive

LogQL is Loki's query language. It is intentionally familiar to PromQL users — same operators, same notion of vectors — but the *underlying data model* is different: a stream of log lines indexed only by labels, never by content.

### 3.1 The Three-Tier Grammar

Every LogQL query has up to three parts, separated by pipes:

```
{stream selector} |= "filter" |~ "regex" | json | label_format ... | unwrap field
                   ─────────── pipeline ───────────
                                                                      ─ unwrap ─
                                                                      (metric)
```

```
┌──────────────────┬──────────────────────┬──────────────────────────┐
│ Stream selector  │ Log pipeline         │ Optional metric query    │
│ (mandatory)      │ (filters / parsers)  │ (turn logs to series)    │
├──────────────────┼──────────────────────┼──────────────────────────┤
│ {app="checkout"} │ |= "error"           │ rate(...[5m])            │
│ Uses index.      │ Brute-force scan.    │ Aggregates lines/sec.    │
└──────────────────┴──────────────────────┴──────────────────────────┘
```

**Stream selector is the only thing the index helps you with.** Every byte of the pipeline runs on the raw chunks Loki must read from object storage.

### 3.2 Label-Only Index — Why Loki Is Fast When It's Fast

Loki's index maps `(label_set, time_range) → chunk_id_list`. Lookups by label hit the index; everything inside the chunk is opaque bytes. To answer `{app="checkout"} |= "error"` Loki:

1. Index lookup → list of chunks for `app=checkout` in the time range.
2. Fetch chunks from object storage (S3/GCS/Azure blob).
3. Decompress, scan each line for the substring `"error"`.
4. Return matches.

This is fast as long as the *label-selected stream volume* is reasonable. If `{app="checkout"}` matches 50 GB of logs and you grep for `"error"`, you scan 50 GB. The index does not help with content.

The corollary: **label cardinality is the only knob that matters for Loki performance**. A handful of labels (cluster, namespace, app, pod) is enough; adding `request_id` or `user_id` as a label destroys the index.

| Label cardinality strategy | Loki cost | Query UX |
|---|---|---|
| 5–20 distinct values per label, ~6 labels | Low | Fast; index is small; chunks are large and well-compressed |
| 1k–1M distinct values per label | Medium → high | Index dominates RAM; chunks become small (one per stream, poor compression) |
| Per-request unique label | Catastrophic | Loki refuses ingest with `too_many_streams` |

### 3.3 Pipeline Operators

The pipeline is everything after the stream selector. It is evaluated line-by-line on the chunks Loki streams from storage.

| Operator | Purpose | Example |
|---|---|---|
| `\|=` | line contains substring | `\|= "error"` |
| `!=` | line does not contain | `!= "healthcheck"` |
| `\|~` | line matches regex | `\|~ "5\\d\\d"` |
| `!~` | line does not match regex | `!~ "(?i)debug"` |
| `\| json` | parse as JSON; promote fields to extracted labels | `\| json \| status_code = "500"` |
| `\| logfmt` | parse `key=value` format | `\| logfmt \| level="error"` |
| `\| regexp` | named-capture regex into labels | `\| regexp "user=(?P<user>\\w+)"` |
| `\| pattern` | indexed pattern parser, faster than regex | `\| pattern "<_> <method> <path> <status>"` |
| `\| unpack` | unpack a nested JSON `_entry` field (Promtail packs) | `\| unpack` |
| `\| line_format` | rewrite the displayed line | `\| line_format "{{.user}} did {{.action}}"` |
| `\| label_format` | rename / synthesize labels | `\| label_format env=$cluster` |
| `\| drop / keep` | reduce extracted labels | `\| keep level, status_code` |

The order matters. Filters before parsers run on the raw line (cheap). Filters after parsers run on extracted fields (must parse first). A common optimization is: `{app="x"} |= "error" | json | level="error"` — substring filter eliminates 95% of lines before JSON parsing runs.

> **Mental model:** Push filters as far left as possible. Substring (`|=`) is cheaper than regex (`|~`) which is cheaper than parse-then-filter. The cost compounds across millions of lines.

### 3.4 Metric Queries — Logs to Time Series

LogQL can produce instant/range vectors from logs, identical in shape to PromQL output. This is what makes Grafana panels work uniformly.

```logql
# Lines per second, grouped by level
sum by (level) (
  rate({app="checkout"} | json | __error__="" [5m])
)

# Bytes per second per stream
sum by (app) (bytes_rate({namespace="prod"}[1m]))

# 99th-percentile parsed latency from log lines
quantile_over_time(0.99,
  {app="api"} | json | unwrap latency_ms [5m]
) by (route)
```

The `unwrap` clause turns an extracted field into a numeric value the engine can aggregate. Without it, `quantile_over_time` has nothing to work on.

`__error__=""` filters out lines where parsing failed (a label Loki adds automatically when `| json` or `| logfmt` errors). Without it, your metric query silently includes parse failures.

### 3.5 Execution: Sharding and Split-by-Time

Loki query frontends shard a query in two dimensions:

```
Query: {app="x"} |= "err" over [now-24h, now]

1. SPLIT BY TIME (`split_queries_by_interval: 30m`)
   → 48 sub-queries, one per 30m window
   → enables parallelism + per-shard caching

2. SHARD BY STREAM (TSDB index v3+ supports it)
   → each split further fans out to N queriers
   → each querier reads 1/N of the streams matching the selector

3. AGGREGATE in the frontend
   → merge sub-results, apply final reductions
```

Both are essential for large queries. A 24-hour query without splitting is a single querier reading 24 hours of chunks from S3 sequentially; with splitting it's 48 queriers in parallel.

The cache layer at the frontend level (typically Memcached) stores per-shard results, so a query repeated within the cache TTL skips storage entirely for the cached splits — only the trailing "live" split is computed fresh. This is why dashboards refresh fast even on long ranges.

### 3.6 Anti-Patterns

| Anti-pattern | Why it hurts | Better |
|---|---|---|
| `{job=~".+"}` | matches every stream in the cluster | always be specific in the selector |
| Use `request_id` as a label | cardinality explosion | keep it in the line; filter via `\|=` |
| `\| json` on every query for high-cardinality fields | extracted labels become "structured metadata" with their own cost | parse once via Promtail/Vector pipeline; index labels you actually filter on |
| Catastrophic regex `(a+)+b` | nested quantifiers → backtracking explosion on adversarial input | use anchored, non-nested regex; prefer `\| pattern` |
| `count_over_time({...}[24h])` for ingest budgeting | reads 24 h of chunks every minute | recording rule (Loki ruler) |
| Live tail with no selector | all-tenant fan-out at the frontend | always `{tenant=...,namespace=...}` |

---

## 4. TraceQL Deep Dive

TraceQL is Tempo's query language for distributed traces. It exists because the prior generation (Jaeger query, Zipkin query) supported only `service + operation + tag` lookup, which is far too narrow for real triage.

### 4.1 The Trace Data Model (Recap)

A trace is a DAG of spans. Each span has:

```
trace_id          (16 bytes, propagated end to end)
span_id           (8 bytes, unique per span)
parent_span_id    (8 bytes, links to caller)
service.name      (resource attribute, the producer)
name              (operation name, e.g. "POST /checkout")
kind              (SERVER | CLIENT | INTERNAL | PRODUCER | CONSUMER)
start, duration   (ns precision)
attributes        (key-value, span-level: http.status_code, db.statement, ...)
events            (timestamped logs within the span)
links             (cross-trace references)
status            (OK | ERROR + message)
```

A trace is identified by `trace_id`. Spans in the same trace share it; that's the entire join key.

### 4.2 Span Filters

The basic TraceQL form selects spans by attribute predicates:

```traceql
# Spans in the auth service that errored with 500
{ resource.service.name = "auth" && span.http.status_code >= 500 }

# Spans on a specific user (high-card attribute on the span, not a metric label)
{ span.user.id = "u_42" }

# Spans tagged as DB calls slower than 1s
{ span.db.system != nil && duration > 1s }
```

The namespace prefixes are mandatory:
- `resource.*` — attributes attached to the resource (service-level, generally unchanging within a process).
- `span.*` — attributes on the individual span.
- `event.*` — attributes on a span event.
- `trace:*` — special trace-level fields (`trace:duration`, `trace:rootSpan`, `trace:rootName`).

Comparison operators are PromQL-flavored (`=`, `!=`, `<`, `<=`, `>`, `>=`, `=~`, `!~`).

### 4.3 Structural Operators — The DAG Walk

This is what makes TraceQL more than "Jaeger search v2." Structural operators express relationships between spans within the same trace.

| Operator | Meaning | Example |
|---|---|---|
| `>>` | descendant | `{ A } >> { B }` — A has B somewhere in its subtree |
| `>` | direct child | `{ A } > { B }` — B's parent is A |
| `<<` / `<` | ancestor / direct parent (reverse of above) | |
| `~` | sibling | `{ A } ~ { B }` — same parent |
| `&&` / `\|\|` | intersection / union of span sets | |

Concrete example: "find traces where an auth-service request triggered a slow DB call":

```traceql
{ resource.service.name = "auth" }
  >> { span.db.system = "postgres" && duration > 500ms }
```

This is a *trace*-level match — Tempo returns the traces (not just spans) where both conditions appear with the structural relationship satisfied. The result count is in traces; the engine then loads the full trace for inspection.

### 4.4 Aggregations and Spanmetrics

A trace store with no metric capability forces two storage stacks (Tempo + Mimir). Modern Tempo bridges this: the `metrics-generator` component reads ingested spans and produces *RED metrics per service edge* into Prometheus remote_write.

The auto-generated metrics (Tempo defaults):
- `traces_spanmetrics_calls_total{service, operation, status_code}`
- `traces_spanmetrics_duration_seconds_bucket{...}` (histogram)
- `traces_service_graph_request_total{client, server}` and latency histograms

Once these exist, the service graph in Grafana is just a PromQL query against `traces_service_graph_*`. The trace store becomes the source of metrics, not just spans.

TraceQL itself supports inline aggregations:

```traceql
{ resource.service.name = "checkout" } | rate()
{ resource.service.name = "checkout" } | quantile_over_time(duration, 0.99)
```

This compiles internally into spanmetrics queries plus filter evaluation. Useful but slower than precomputed series.

### 4.5 Tempo's Index-Less Architecture

Unlike Jaeger or ES-backed tracing stores, Tempo does not maintain an inverted index over span attributes. Instead:

```
Per block (~5 min wall clock of ingest):
  - Parquet file partitioned by trace_id range
  - Bloom filter per (column, block) — "is this attr=value present?"
  - Footer with per-row-group min/max for early skip
  - Optional: dedicated columnar index for hot attrs (since Tempo 2.0)

Search:
  - For each block whose bloom filter says "maybe":
      - Read parquet row groups whose min/max overlap predicates
      - Filter rows
  - Aggregate matching trace_ids
  - Fetch full traces for the matches
```

The trade-off: lookup-by-trace-id is one parquet read (cheap). Predicate search ("find error traces in the last hour") scans bloom filters across many blocks — fast for small windows, slow for long ones, and the cost scales with the number of distinct attributes you've ingested. The Parquet schema (`tempodb` v2/v3/v4) keeps evolving as Grafana Labs tunes this trade-off.

### 4.6 TraceQL vs Jaeger Query vs ClickHouse SQL

| Capability | Jaeger Query | TraceQL | ClickHouse SQL on traces |
|---|---|---|---|
| Lookup by trace_id | yes | yes | yes |
| Service + operation + tag | yes | yes | yes |
| Numeric span attribute predicates | limited | yes | yes |
| Structural operators (`>>`, `>`, etc.) | no | yes | possible via self-joins (verbose) |
| Aggregation across spans | no | yes (limited) | yes (full SQL) |
| Cross-trace analytics (counts, histograms over arbitrary fields) | no | partial via spanmetrics | yes |
| Cost per trace lookup | low | low | medium (must hit columnar storage even for a single trace) |
| Cost per analytic query | n/a | medium | medium-to-high (depends on partitioning) |

The pattern in mature stacks: **Tempo for retrieval and dashboards** (cheap, dense), **ClickHouse for analytics** (costly but flexible) — feed both from the same OTel Collector.

> **Mental model:** TraceQL is "I want to find traces and inspect them." SQL on traces is "I want to compute statistics over traces." They are different jobs and the tools split that way.

---

## 5. SQL on Telemetry

Over the last few years, SQL has become a first-class telemetry query language. ClickHouse, BigQuery, Snowflake, Druid, Pinot, and DuckDB are all production-grade telemetry stores at various scales. The reasons are practical:

- Engineers already know SQL.
- A single store can hold metrics + logs + traces for cross-signal joins.
- Lakehouses (Iceberg, Delta) make multi-year retention cheap on object storage.
- Materialized views replace recording rules with general-purpose roll-ups.

### 5.1 Why ClickHouse Won the Logs/Traces Niche

ClickHouse's MergeTree engine is purpose-built for the telemetry shape:

- **Columnar storage**: read only the columns you ask for; logs with 50 attribute columns scan only the matching predicates.
- **`ORDER BY (service, timestamp)`**: time-based partition pruning + service-locality reads.
- **LZ4 / ZSTD compression**: 10–20× smaller on disk vs ES.
- **Async inserts + parts merging**: high ingest with no client-side batching.
- **Skip indexes (bloom, set, minmax)** per column: cheaper than full inverted index, sufficient for telemetry.
- **`MATERIALIZED VIEW`** pipelines: roll-ups happen automatically on insert.

A representative logs schema:

```sql
CREATE TABLE logs (
  ts            DateTime64(9, 'UTC'),
  trace_id      FixedString(16),
  span_id       FixedString(8),
  service       LowCardinality(String),
  level         LowCardinality(String),
  body          String CODEC(ZSTD(3)),
  attrs         Map(String, String),
  resource_attrs Map(String, String)
)
ENGINE = MergeTree
PARTITION BY toStartOfHour(ts)
ORDER BY (service, ts)
TTL ts + INTERVAL 30 DAY TO VOLUME 'cold',
    ts + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;
```

`LowCardinality(String)` dictionary-encodes values with low distinct counts (service, level, region) — typically 5–10× more compact than plain strings. `Map(String, String)` for `attrs` is opaque to indexes but cheap to store; promote frequently-queried attributes to dedicated columns.

### 5.2 Materialized Views as Roll-Ups

The recording-rule equivalent in SQL telemetry is a materialized view that maintains an aggregated table on insert:

```sql
CREATE MATERIALIZED VIEW logs_5m_stats
ENGINE = SummingMergeTree
PARTITION BY toStartOfDay(ts_5m)
ORDER BY (service, level, ts_5m)
AS SELECT
  toStartOfFiveMinutes(ts) AS ts_5m,
  service,
  level,
  count() AS lines,
  sum(length(body)) AS bytes
FROM logs
GROUP BY ts_5m, service, level;
```

Now a dashboard query that previously scanned hundreds of GB of raw logs reads a tiny aggregate table:

```sql
SELECT ts_5m, sum(lines)
FROM logs_5m_stats
WHERE service = 'checkout' AND ts_5m > now() - INTERVAL 24 HOUR
GROUP BY ts_5m ORDER BY ts_5m;
```

> **Mental model:** A materialized view in ClickHouse is *just a trigger that runs on every insert and writes to another table*. The "view" name is misleading; it's an automatic insertion pipeline.

### 5.3 Joining Metrics, Logs, and Traces

The killer feature SQL gives you that PromQL/LogQL/TraceQL can't:

```sql
-- "For traces in the last hour where the root span errored,
--  what error log lines accompanied them?"
SELECT
  l.ts, l.service, l.body
FROM traces t
JOIN logs l USING (trace_id)
WHERE t.ts > now() - INTERVAL 1 HOUR
  AND t.root_status = 'ERROR'
  AND l.level IN ('ERROR', 'WARN')
ORDER BY l.ts;
```

In a Loki+Tempo+Mimir stack you would: query Tempo for error trace_ids → for each, query Loki — N queries, each cheap, but the end-to-end latency adds up and you can't aggregate across the result set easily. The SQL form does it in one query; the cost is whatever the columnar engine charges to scan the predicate.

A second example, harder to express in PromQL:

```sql
-- Top 10 slowest endpoints in the last hour, with example trace_ids
SELECT
  resource_service AS service,
  span_name        AS endpoint,
  quantile(0.99)(duration_ns) / 1e6 AS p99_ms,
  any(trace_id)    AS exemplar_trace
FROM spans
WHERE ts > now() - INTERVAL 1 HOUR
  AND span_kind = 'SERVER'
GROUP BY service, endpoint
ORDER BY p99_ms DESC
LIMIT 10;
```

`any(trace_id)` returns a single trace_id from the matching set — instant exemplar.

### 5.4 Recreating PromQL Idioms in SQL

```sql
-- rate(metric[5m]) per service, last hour
SELECT
  toStartOfMinute(ts) AS minute,
  service,
  -- counter rate as (cur - prev) / dt over a 5-min window
  greatest(0, (max(value) - min(value))) / 300.0 AS rate_per_sec
FROM metrics
WHERE name = 'http_requests_total'
  AND ts > now() - INTERVAL 1 HOUR
GROUP BY minute, service
ORDER BY minute;

-- p99 from a histogram_bucket family
SELECT
  service,
  quantileExact(0.99)(duration_ms) AS p99
FROM logs
WHERE ts > now() - INTERVAL 5 MINUTE
GROUP BY service;
```

The SQL form is more verbose but exposes every choice — window length, edge handling, aggregation semantics — that PromQL hides behind `rate()`. For an SLO calculation that auditors will read, this can be a feature. For a 4-a.m. on-call Grafana panel, PromQL wins on conciseness.

### 5.5 Cost: Scan-Bytes vs Reservation

The two pricing models in lakehouse-style SQL stores:

| Model | Vendor examples | Cost driver | When it bites |
|---|---|---|---|
| Scan-bytes | BigQuery on-demand, Athena, Snowflake (per-credit-second) | data physically scanned per query | unbounded users, exploratory queries on wide tables |
| Reservation / cluster | ClickHouse Cloud, Snowflake reserved, BigQuery flat-rate | nodes/credits provisioned | underutilized clusters, peak vs steady gap |

Scan-bytes models are user-friendly until someone runs `SELECT * FROM logs WHERE message ILIKE '%error%'` on a 50 TB table during an incident. Reservation models hide this cost but force you to right-size up front.

Mitigations:
- **Partitioning** (by hour/day) for predictable pruning.
- **Materialized views** so dashboards never hit raw tables.
- **Query labels + per-team quotas** so cost attribution is possible.
- **Circuit breakers**: BigQuery `--maximum_bytes_billed`, ClickHouse `max_bytes_to_read` per user.

> **Pitfall:** The first time a junior engineer SELECT-stars a 1 TB table on BigQuery, the bill is in the four figures. Set per-user `maximum_bytes_billed` defaults and require explicit override.

---

## 6. Cross-Engine Concerns

Every production query layer faces the same orthogonal concerns regardless of which language is on top.

### 6.1 Limits That Save Your Backend

The matrix of limits a multi-tenant query layer must enforce:

| Limit | Default magnitude | What it prevents |
|---|---|---|
| `max_query_length` | 30d–90d | All-time scans that pin chunk caches |
| `max_query_lookback` | 1y | Same as above for historical reads |
| `max_samples` (Prom) | 50M | OOM from `count_values` on huge series |
| `max_series` per query | 100k–1M | One bad regex pulling 100M series |
| `max_chunks_per_query` (Loki) | 2M | Long-window grep on a noisy stream |
| `max_bytes_per_query` (Loki) | 100GB–1TB | Same |
| `query_timeout` | 30s–2m | Slow-query queue head-of-line blocking |
| `max_concurrent_queries` per tenant | 16–64 | One tenant's notebook running 100 parallel queries |
| `cardinality_limit` on aggregations | 100k labels | `count by (request_id)` hostile queries |

The pattern across vendors is identical: layer-7 limits at the query frontend, per-tenant overrides, and circuit-breaker errors that surface as visible Grafana panel errors rather than silent timeouts.

### 6.2 Caching — Multiple Layers

A modern query frontend has **three** distinct caches:

```
┌────────────────────────────────────────────────────────────────┐
│ L1: Query-result cache                                         │
│     key = (query_string, time_range_aligned_to_step)           │
│     value = serialized result                                  │
│     TTL = step (e.g. 1m for a 1m step query)                   │
├────────────────────────────────────────────────────────────────┤
│ L2: Sub-query / split cache                                    │
│     key = (query_string, time_split[i])                        │
│     each split that's "in the past" can be cached forever      │
│     only the live tail is recomputed                           │
├────────────────────────────────────────────────────────────────┤
│ L3: Chunk / index cache                                        │
│     key = chunk_id  (stable, content-addressed)                │
│     value = decompressed chunk                                 │
│     hot working set in Memcached / in-process LRU              │
└────────────────────────────────────────────────────────────────┘
```

Mimir, Thanos, and Loki all have these three layers. Tempo replaces L3 with a parquet-row-group cache and bloom-filter cache. The combined hit rates determine whether your dashboards refresh in 200 ms or 3 s.

The "step alignment" trick at L1 is subtle but vital. A panel at 1-min step refreshing every 10s would otherwise miss the cache half the time because the time range "rolls"; aligning the range to step boundaries makes consecutive refreshes hit the same cache key for all but the trailing point.

### 6.3 Sharding

Three orthogonal sharding axes:

| Axis | When to shard on it | Implication |
|---|---|---|
| **By time** | Always for large-range queries | Enables parallelism + cache reuse for older splits |
| **By tenant** | Multi-tenant deployments | Per-tenant pools prevent noisy neighbors |
| **By series / label hash** | When a single tenant's query is too big | Each shard reads ~1/N of the series; results merged in frontend |

Mimir's read path uses all three. Loki's TSDB index v3 supports the third (`tsdb_max_query_parallelism`). VictoriaMetrics shards by `__name__` hash.

### 6.4 Federation vs Read-Replication

Two patterns for cross-cluster reads:

```
FEDERATION
  query → frontend → fan out to N regional Prometheis → aggregate
  + simple, no replication
  - latency = max(per-region latency); a slow region drags everything
  - aggregation across regions can lose precision (averages of averages)

READ-REPLICATION
  every cluster's data shipped to a central long-term store (Mimir / Thanos)
  query → central store
  + uniform queries; long retention; consistent precision
  - cost of central storage; ingest pipeline complexity
```

Most mid-and-up organizations end up with both: federation for "live" queries within a region, read-replication for global SLO dashboards and long-term analysis.

### 6.5 Adaptive Query Routing — Hot vs Cold Tier

Long-retention metric stores tier data: recent in-memory + SSD, older on object store with downsampling (5m or 1h roll-ups). A smart query router picks the right tier:

```
query: rate(http_requests_total[5m]) for last 7d at step 1m
  → recent tier (15s raw resolution, sufficient)

query: avg_over_time(checkout_p99[1d]) for last 6 months at step 1d
  → cold tier (1h downsampled, enough for daily granularity)
```

Mimir's "blocks-storage" config encodes this; Thanos has `--query.partial-response` plus a tier-aware fan-out. Get this wrong and either you can't render long-range charts (no downsample tier) or you lose precision on recent data (always querying downsamples).

---

## 7. End-to-End Performance

What happens, step by step and millisecond by millisecond, when an engineer hits a Grafana panel.

### 7.1 PromQL: a 5-minute rate over 24 hours

```
T+0ms     Grafana panel sends
            GET /api/v1/query_range?
              query=sum%20by%20(service)%20(rate(http_requests_total[5m]))
              &start=...&end=...&step=60s

T+5ms     Mimir query frontend receives request
            - Validates against per-tenant max_query_length, max_samples
            - Computes splits: 24h / 30m = 48 sub-queries
            - For each split, checks L1 cache by (query, aligned_range)

T+8ms     Cache hit on 47/48 splits (older windows already computed)
            Only the trailing 30m-window split is fresh

T+10ms    Trailing split is dispatched to a querier pod
            - Querier resolves matchers via TSDB postings
            - Postings: http_requests_total → 12,000 series IDs
            - Iterates chunks in head block + last on-disk block

T+45ms    Querier reads ~12,000 × 5 chunks = 60k chunks
            - Most resident in chunk cache (Memcached) → microsecond gets
            - ~2,000 missed → fetched from object storage (S3 GET)

T+180ms   Decompression + rate() evaluation
            - 12,000 series × 20 samples per 5m window
            - sum by (service) reduces to ~30 series

T+185ms   Result returned to frontend
            - Frontend merges with 47 cached splits
            - Step-aligned, returns 24h × (1 minute) = 1440 points × 30 series

T+200ms   Grafana renders the panel

T+10s     Panel auto-refresh
            - 47 splits still cached; only the *new* trailing split is computed
            - Result available in <50ms; user perceives instant refresh
```

The architecture is engineered so that the *first* render of a 24h range is the expensive one (~200ms) and every subsequent refresh is nearly free.

### 7.2 LogQL: substring search across 1 hour

```
T+0ms     {app="checkout"} |= "vendor.timeout" over [now-1h, now]

T+5ms     Loki query frontend
            - split_queries_by_interval = 30m → 2 splits

T+10ms    Each split → query scheduler → 4 querier pods
            (tsdb_max_query_parallelism = 4)

T+15ms    Per-querier:
            - Index lookup: app=checkout in window → 200 chunks
            - 200 / 4 = 50 chunks per querier
            - Object store GET (S3) for chunks not in chunk cache
              ~30 missed × 50ms each = 1.5s of S3 latency, hidden by parallelism

T+1.6s    Each chunk decompressed, line-by-line scan for "vendor.timeout"
            - 200 chunks × ~50k lines × ~120 bytes = ~1.2 GB scanned
            - 8 querier cores at ~2 GB/s per core = ~75ms

T+1.7s    Frontend collects 4 partial result sets, merges, deduplicates
            - 14 matching lines returned

T+1.8s    Grafana log panel renders the lines
```

The main cost is S3 round-trips. Caches and parallelism hide this; without them, the query is a single querier sequentially fetching chunks for tens of seconds.

### 7.3 TraceQL: structural search over 1 hour

```
T+0ms     { resource.service.name = "auth" } >> { span.db.system = "postgres" 
            && duration > 500ms } over [now-1h, now]

T+5ms     Tempo query frontend
            - Identifies blocks intersecting [now-1h, now]: 12 blocks (~5min each)

T+10ms    For each block:
            - Bloom filter check on resource.service.name=auth → 11/12 pass
            - Bloom filter check on span.db.system=postgres → 9/12 pass
            - Combined: 9 candidate blocks

T+50ms    For each candidate block:
            - Read parquet row groups for spans matching either side
            - Apply structural-relationship evaluation (parent/child traversal)
            - Collect candidate trace_ids

T+1.2s    ~340 candidate trace_ids identified

T+1.3s    For each candidate trace_id:
            - Read full trace from object store (one parquet read)
            - Verify the structural condition holds end-to-end

T+2.5s    Final result: 87 traces match
            - Returned as a list of (trace_id, root_span, duration) summaries

T+2.6s    Grafana shows clickable trace list
```

The cost is dominated by per-trace parquet reads in the verification step. Tempo 2.x has incrementally moved more verification into the streaming pass to reduce this; configuration tuning (`max_search_duration`, `concurrent_jobs`) directly affects it.

### 7.4 SQL: cross-signal join

```
T+0ms     SELECT … FROM traces JOIN logs USING (trace_id) … (see §5.3)

T+5ms     ClickHouse coordinator parses query, identifies partitions
            - traces: hourly partitions × last 1h = 1 partition
            - logs:   hourly partitions × last 1h = 1 partition

T+10ms    Per-shard (4 shards):
            - Read traces partition: predicate root_status='ERROR'
              skip-index match → 1 row group out of ~50 read
            - Build hash table of trace_ids (~120 IDs)
            - Read logs partition: scan with trace_id IN (...) pushed down
              skip-index helps; ~5% of rows scanned

T+800ms   Each shard streams matching log lines back to coordinator

T+900ms   Coordinator merges, sorts, returns

T+950ms   Grafana renders table panel
```

The cost is a function of (1) how well skip-indexes prune the scan and (2) how many log lines match. ORDER BY in the table schema and bloom skip-indexes on `trace_id` are the difference between sub-second and 30-second responses.

---

## 8. Pitfalls

A condensed list of query-time mistakes that cost time, money, or pages.

| # | Pitfall | Where it bites | Mitigation |
|---|---|---|---|
| 1 | `rate()` window too short for scrape interval | Flapping alerts, NaN panels | Window ≥ 4× scrape interval |
| 2 | `histogram_quantile` over `sum without (le)` | Wrong percentiles | Always keep `le`, drop the rest |
| 3 | Bare counter on a panel | Monotonic line, useless chart | Wrap in `rate()` / `increase()` |
| 4 | High-card label as Loki stream label | Index OOM, ingest failures | Keep in line, filter via `|=` |
| 5 | `\| json` on every query for fields you query often | Wasted CPU per query | Promote to indexed labels in pipeline stage |
| 6 | Catastrophic regex backtracking | One adversarial input pegs queriers | Anchor regex, avoid nested quantifiers, prefer `\| pattern` |
| 7 | TraceQL search without time bounds | Scans every parquet block | Always bound `[start, end]` to ≤ 24h |
| 8 | SQL `SELECT *` on telemetry tables | Multi-TB scans | Per-user `max_bytes_billed`, never `*` |
| 9 | No per-tenant query limits | One team's runaway notebook 8x's tail latency | Configure `max_query_*` per tenant |
| 10 | Vector-matching mismatch (`errors / requests`) | Empty results, no error surfaced | Test in panel; use `on (...) group_left` explicitly |
| 11 | Subqueries in alert rules | Flappy + expensive | Move to recording rules |
| 12 | Quantile of a quantile | Wrong percentiles, untraceable | Re-derive from histogram buckets |
| 13 | `up == 0` alerts during scrape misses | False pages on temporary DNS hiccups | Use `absent_over_time(up[5m]) > 0.5` |
| 14 | `count_over_time({...}[24h])` recomputed every refresh | Loki overload | Loki ruler recording rule |
| 15 | Federation across regions for a 28-day SLO | Slow + lossy | Read-replicate to central long-term store |
| 16 | Cold-tier query where hot-tier suffices | Object-store cost spike | Configure tier router; downsample policies |
| 17 | Trace search with no service filter | Tempo block scan across all services | Always include `resource.service.name = …` |
| 18 | Materialized view with wrong PARTITION BY | Hot partitions → hotspots | Partition by time, order by entity |
| 19 | Forgetting the `__error__=""` filter in LogQL metric queries | Parse failures silently included | Always filter unless you mean to count failures |
| 20 | Manual conversion between scrape units | `rate(latency_ms[5m]) / 1000` etc. | Fix unit at instrumentation; use `_seconds` suffix per OTel semconv |

---

## 9. What's Changing

The query layer is one of the most actively-evolving areas in observability. As of 2024–2026:

### 9.1 Native Histograms in PromQL (Prometheus 2.40+, Mimir 2.10+)

A single series replaces the bucket fan-out. Same `histogram_quantile` API on the user side; ~100× storage reduction and tunable precision per metric. Adoption is still in flight; double-check exporter support before relying on it for SLOs.

### 9.2 Exemplars Everywhere

Exemplars (a sample point that carries a `trace_id`) bridge metrics to traces. Configured at the SDK (Prometheus client + OTel both support), shipped via remote_write, indexed in Mimir/Prometheus, rendered as clickable dots on Grafana panels. Once a team adopts them, the metric → trace jump is one click.

### 9.3 TraceQL Metrics

Tempo 2.4+ added inline aggregation operators (`| rate()`, `| quantile_over_time()`) that compile into spanmetrics under the hood. The line between "trace store" and "metrics store" is intentionally blurring.

### 9.4 Loki Bloom Filters

Loki 3.0+ ships per-token bloom filters at the chunk level, enabling cheap pre-filtering of substring queries before chunk decompression. A `|=` query that previously scanned 50 GB now scans only the chunks whose blooms match.

### 9.5 Mimir Read Path: Query-Sharding by Default

Mimir 2.x enables query-sharding for nearly all heavy queries, splitting them by series-hash across queriers automatically. The user-visible effect: a query that took 10 s now takes 1.5 s, with the trade-off of more queriers under load.

### 9.6 SQL on Telemetry Going Mainstream

ClickHouse-on-OTLP (the official `clickhouseexporter` in OTel Collector) is now the default for many startups skipping the Loki/Tempo/Mimir trio. Grafana 10+ has first-class ClickHouse plugins, including PromQL-on-ClickHouse via translation. The ergonomic gap is closing.

### 9.7 LLM-Generated PromQL

Grafana, Datadog, and several startups expose "ask in English, get a query" features. Reasonable for exploration, dangerous for SLO definitions and alerts (the LLM is happy to write a query that returns plausible-looking nonsense). Treat as a productivity assist, not a source of truth.

---

## 10. Mental Models and Glossary

Mental models specific to the query layer:

> **A query language is the user-visible contract for what the storage knows how to do quickly.** Pick the language that matches your storage's strengths.

> **`rate()` is regression, not derivative.** It fits a line through the samples in the window; counter resets are detected and corrected. Never compute `rate` by hand.

> **The label set is the index.** PromQL's speed comes from the postings index over labels; the moment you regex match `.+` you're scanning the universe.

> **Loki's index is just a label-set lookup.** Everything inside the chunk is opaque bytes; queries on log content scan, not search.

> **A trace is a DAG and TraceQL is graph queries on it.** Structural operators are the only thing that distinguishes it from "tag search."

> **SQL on telemetry trades flexibility for cost-per-query.** Use it when you need joins or arbitrary analytics; use the native query languages when you need cheap dashboards.

> **Three caches, three sharding axes.** Every healthy query frontend has them.

| Term | Precise meaning |
|---|---|
| **Instant vector** | A set of (label_set, value) at one timestamp |
| **Range vector** | A set of (label_set, [(t,v), …]) over a window |
| **Matchers** | Label selectors (`name="value"`, `re=~"..."`) — the index lookup keys |
| **Postings** | The inverted index from label/value to series IDs |
| **Step alignment** | Aligning query time ranges to step boundaries to maximize cache reuse |
| **Recording rule** | A precomputed PromQL expression written back as a new metric |
| **Spanmetrics** | RED-style metrics auto-derived from spans by the trace store |
| **Stream selector** | The `{label="value"}` prefix of a LogQL query — the only index-using part |
| **Pipeline (LogQL)** | The post-selector chain of filters, parsers, and label operators |
| **Structural operator (TraceQL)** | An operator expressing parent/child/descendant/sibling between span sets |
| **Bloom filter (Tempo)** | Per-block probabilistic data structure for skipping non-matching blocks |
| **MergeTree** | ClickHouse's columnar storage engine family; the basis for telemetry tables |
| **Materialized view** | An automatic insertion pipeline that maintains an aggregated table |
| **Query frontend** | The component that splits, caches, schedules, and merges sub-queries |
| **Query sharding** | Splitting a query across N queriers by series-hash for parallelism |
| **Adaptive routing** | Sending a query to hot or cold storage based on its time range |

---

**TL;DR query layer.** *Four languages, four storage shapes. PromQL is matchers + range vectors + reductions, fast on label-indexed time series. LogQL is selector + pipeline, fast at "select a stream and grep." TraceQL is span filters + structural operators, fast at trace retrieval with rich predicates. SQL is everything else, paid by the byte. Across all four: limits + caches + sharding + adaptive routing are non-negotiable; without them the query layer is the bottleneck the whole stack waits on.*
