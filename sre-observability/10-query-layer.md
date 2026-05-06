# 10 — The Query Layer: PromQL, M3QL, LogQL, TraceQL, SQL

The query layer is where storage meets the human. Every dashboard panel, every alert rule, every ad-hoc 3 a.m. investigation, every capacity report flows through it. The data sitting on disk is worth nothing until a query can pull the right slice cheaply, fast enough to support exploration. This chapter goes deep on the four query languages a Staff Engineer in this space must read fluently and write competently: **PromQL**, **LogQL**, **TraceQL**, and **SQL on telemetry**.

These four languages exist because the storage layer underneath each signal has a different physical shape. You cannot use the same engine for "what is the rate of HTTP 500s?" and "show me every log line for trace 4f2a…" without making one of the two miserable. The query languages are the user-visible projection of the storage choice.

> **Mental model:** A query language is a contract between *what the storage knows how to do quickly* and *what the human wants to ask*. PromQL is fast at "aggregate-by-label-over-window"; LogQL is fast at "select-by-label, then grep"; TraceQL is fast at "select-by-trace-id, then walk-the-DAG"; SQL is fast at whatever you're willing to pay to scan.

---

## Table of Contents

1. [The Big Picture: Why Four Languages](#1-the-big-picture-why-four-languages)
2. [PromQL Deep Dive](#2-promql-deep-dive) — including [§2.11 M3QL — The Graphite-Native Cousin](#211-m3ql--the-graphite-native-cousin-of-promql)
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

### 2.11 M3QL — The Graphite-Native Cousin of PromQL

M3QL is the query language native to **M3**, the open-source TSDB Uber built when their Graphite/Carbon stack hit a wall (~2015) and which now backs metrics at a few of the largest fleets in the world. M3 also speaks PromQL (with extensions) and Graphite Carbon line protocol; M3QL is its third dialect — a Graphite-compatible, **pipeline-based** query language that thinks of time series as **named flows transformed by composable functions** rather than as label-vector algebra.

If you ever inherit a fleet running M3, Graphite, KairosDB, or any Graphite-compatible store, M3QL is what the dashboards and alert rules look like. It's also worth reading even if you don't run M3: many of the function shapes (`asPercent`, `holtWintersAberration`, `nPercentile`, `summarize`) are the closest thing the metrics world has to standard library functions and they keep showing up across vendors.

#### 2.11.1 Mental Model — Series Names, Not Label Vectors

Where PromQL identifies a series by its label set (`http_requests{service="api",route="/checkout"}`), M3QL identifies it by its **dotted name plus tags**:

```
stats.gauges.api.checkout.p99
   ▲       ▲    ▲    ▲      ▲
   │       │    │    │      └─ leaf metric
   │       │    │    └──────── route
   │       │    └─────────── service
   │       └────────────── metric type
   └──────────────────── prefix
```

Modern M3 layers **Graphite tags** on top: `cpu.idle;host=web01;dc=us-east1`. The query engine treats both as first-class — you can select by glob (`stats.gauges.api.*.p99`), by tag (`seriesByTag('service=api','dc=us-east1')`), or both.

Where PromQL is **algebraic** ("aggregate vectors over labels"), M3QL is **functional-pipeline** ("apply transforms in series, left-to-right, like Unix pipes"). Same execution model as Graphite's render API:

```
PromQL:  sum by (service) (rate(http_requests_total[5m]))
M3QL:    aliasByNode(
           sumSeriesWithWildcards(
             nonNegativeDerivative(stats.api.*.requests),
             2),
           2)
         ▲                ▲                  ▲                  ▲
         outer wrap       aggregator         counter→delta       input
         (last applied)
```

Read M3QL **outside-in** but execute **inside-out** — the deepest function applies first.

#### 2.11.2 Series Selection — Globs, Braces, seriesByTag

| Syntax | What it matches |
|---|---|
| `stats.api.requests` | the single named series |
| `stats.api.*.requests` | one wildcard segment between `api` and `requests` |
| `stats.api.{checkout,cart,payment}.requests` | brace alternation — three series unioned |
| `stats.api.[a-z]*.requests` | character-class glob |
| `stats.api.**.requests` | recursive glob (M3 extension) |
| `seriesByTag('service=api','dc!=staging')` | tag-based selection; supports `=`, `!=`, `=~`, `!=~` |
| `seriesByTag('name=requests','service=~api.*')` | regex tag value (M3-specific) |

`seriesByTag` is the modern primitive — it sidesteps dotted-name positional fragility and is closest in spirit to PromQL's label selectors. New deployments should prefer it.

```m3ql
# Old style — fragile if names ever get a new segment
sum(stats.gauges.api.{checkout,cart,payment}.p99)

# Tag style — same intent, migration-safe
seriesByTag('name=p99','service=~(checkout|cart|payment)') | sum
```

#### 2.11.3 The Two Most-Confused Pairs: avg vs avgSeries, sum vs sumSeries

The single most common bug in M3QL/Graphite code review is mistaking the **per-point across-series** family for the **per-series along-time** family. They look almost identical and answer entirely different questions.

| Function | Reduces along | Output | PromQL equivalent |
|---|---|---|---|
| `averageSeries(s1,s2,…)` (alias `avg`) | **across series** at each `t` | one series, value = mean of inputs at each `t` | `avg(...)` |
| `sumSeries(s1,s2,…)` (alias `sum`) | across series at each `t` | one series | `sum(...)` |
| `averageSeriesWithWildcards(pat, *positions)` | across series, **collapsing dotted-name positions** | fewer series than input | `avg by (...)` |
| `sumSeriesWithWildcards(pat, *positions)` | across series, collapsing positions | fewer series | `sum by (...)` |
| `summarize(s, '5min', 'avg')` | **along time** per series, into 5-min buckets | same series, lower resolution | n/a — distinct concept |
| `movingAverage(s, '5min')` | **along time** per series, sliding window | same series, same resolution | `avg_over_time(s[5m])` |
| `averageAbove(seriesList, n)` | **filter**: keep series whose avg over window > n | subset of input series | no direct equivalent |

Memorize this:

> `averageSeries` averages **across the bundle** of series at each timestamp. `summarize(...,'avg')` and `movingAverage` average **along time** for each series. `averageAbove` is a *filter*, not an aggregator.

Concrete example — three pods report CPU:

```
        t1    t2    t3
pod-a:  0.10  0.20  0.30
pod-b:  0.50  0.50  0.50
pod-c:  0.90  0.80  0.70
```

`averageSeries(pod-a, pod-b, pod-c)` (across-series, per-timestamp):

```
fleet:  0.50  0.50  0.50    # mean at each t; per-pod identity lost
```

`summarize(<each>, '5min', 'avg')` (along-time, per-series):

```
pod-a: 0.20    pod-b: 0.50    pod-c: 0.80    # one point per 5-min bucket
```

`movingAverage(<each>, '2pt')` (along-time sliding window, per-series):

```
pod-a: NaN, 0.15, 0.25    pod-b: NaN, 0.50, 0.50    pod-c: NaN, 0.85, 0.75
```

PromQL forces you to spell which axis you mean (`avg(x)` vs `avg_over_time(x[5m])`); M3QL collapses both into similarly-named functions. Code review for "is this the right axis?" is the most valuable discipline you can enforce in an M3QL shop.

#### 2.11.4 sumSeriesWithWildcards — The Closest Thing to `sum by (...)`

This is the function that takes the most explanation. Suppose you have:

```
stats.api.checkout.us-east1.p99
stats.api.checkout.us-west2.p99
stats.api.cart.us-east1.p99
stats.api.cart.us-west2.p99
```

You want "sum p99 across regions, grouped by service" — i.e. *collapse the region position*, keep the rest.

```m3ql
sumSeriesWithWildcards(stats.api.*.*.p99, 3)
#                                          ▲
#                  positions are 0-indexed; 3 = the region segment
```

Result:

```
stats.api.checkout..p99     ← position 3 collapsed (notice the empty slot)
stats.api.cart..p99
```

Collapse multiple positions at once:

```m3ql
sumSeriesWithWildcards(stats.api.*.*.p99, 1, 3)
# collapses both 'api' (pos 1) and the region (pos 3)
```

`averageSeriesWithWildcards` works identically but uses mean. The generic form is `aggregateWithWildcards(seriesList, func, *positions)` where `func ∈ {sum, avg, max, min, last, count, stddev, range, multiply, diff}` — a single primitive replaces a dozen named variants.

For tag-style series, the modern equivalent is `aggregateGroupByTags`:

```m3ql
aggregateGroupByTags('sum', 'service', seriesByTag('name=p99'))
# pick aggregator, pick keep-tags, supply selector — reads almost exactly like
# PromQL's "sum by (service) (...)"
```

Or its alias `groupByTags(seriesList, callback, *tags)`. Both compile to the same plan.

#### 2.11.5 The Moving-Window Family — Sliding Aggregations

M3QL's moving-window functions slide a window along time per series, point-for-point at native resolution. They are the analog of PromQL's `_over_time` family:

| M3QL | What it does | PromQL equivalent |
|---|---|---|
| `movingAverage(s, '5min')` | mean of points in trailing 5-min window | `avg_over_time(s[5m])` |
| `movingSum(s, '5min')` | sum in trailing window | `sum_over_time(s[5m])` |
| `movingMin(s, '5min')` | min in trailing window | `min_over_time(s[5m])` |
| `movingMax(s, '5min')` | max in trailing window | `max_over_time(s[5m])` |
| `movingMedian(s, '5min')` | median in trailing window | `quantile_over_time(0.5, s[5m])` |
| `movingWindow(s, '5min', 'stddev')` | generic moving window with named aggregator (`avg`, `sum`, `min`, `max`, `median`, `stddev`, `count`) | `stddev_over_time(s[5m])` |
| `moving(s, '5min', func)` | shorter alias for `movingWindow` | n/a |

Window argument forms: `'5min'`, `'1h'`, `'30s'`, `'1d'` — string with unit. Some implementations also accept an integer point count (`movingAverage(s, 20)` = "trailing 20 samples"), which is fragile when scrape interval changes; the time-string form is the safe default.

Two subtleties that surprise people:

- **Edge of the window**: `movingAverage` includes the **current** sample. `movingAverage(s, '5min')` at `t=12:00:00` averages samples in `(11:55:00, 12:00:00]` — same convention as PromQL.
- **Sparse data**: with fewer samples than the window holds, the function returns the average of *what is there*, not NaN. `avg_over_time` does the same; `rate()`-equivalents (`perSecond`) differ — they require ≥ 2 samples.
- **`xFilesFactor`** (default 0.5 in Graphite, often 0 in M3): the fraction of points in the window that must be non-NaN for the function to emit a value at that timestamp. From M3 source:

  ```go
  if effectiveXFF(windowPoints, nans, xFilesFactor) {
      vals.SetValueAt(i, avg)        // emit only if nan_count/total < (1 - xff)
  }                                  // else leave NaN
  ```

  Setting `xFilesFactor=0` makes the function emit on any non-NaN sample (lenient); `0.99` requires nearly the full window (strict). Different from PromQL, which has no equivalent — `avg_over_time` always emits if ≥ 1 sample exists. Pin this explicitly when porting Graphite dashboards or correctness-critical alerts.

Bigger gotcha: M3QL has **two distinct families** that beginners conflate.

```
movingAverage(s, '5min')   →  same step as input;  each point = mean of preceding 5min
                              (smoothing, no resolution change)

summarize(s, '5min', 'avg') →  one point per 5-min bucket
                              (downsampling, lower resolution)
```

`summarize` is what you want for **dashboards spanning weeks** (downsample for performance); `movingAverage` is what you want for **smoothing a noisy signal at native resolution**. Reaching for one when you wanted the other is a frequent source of "the graph looks weird at long ranges" tickets.

#### 2.11.6 Counter Math — nonNegativeDerivative and perSecond

M3 stores counters as monotonically-increasing values, just like Prometheus. The `rate()` / `increase()` analogs:

| M3QL | What it does | PromQL equivalent |
|---|---|---|
| `nonNegativeDerivative(s)` | per-step delta, treats decreases as resets (returns NaN at the reset point) | similar to `delta(s[2*step])` with reset handling |
| `perSecond(s)` | `nonNegativeDerivative(s) / step_seconds` | `rate(s[step])` |
| `derivative(s)` | per-step delta, **no** reset handling | `delta(s[2*step])` (raw) |
| `integral(s)` | running cumulative sum from window start | `sum_over_time(rate(s[step])[T:step])` cumulative |
| `scaleToSeconds(s, seconds)` | rescale a counted-per-step series to "per N seconds" | manual multiplication |

`perSecond` is the right primitive for "requests per second from a counter." It is **not** a sliding-window estimate — it is the difference between consecutive samples divided by the step. This makes it more accurate than PromQL's `rate()` for short windows but more sensitive to single missing scrapes.

```m3ql
# Smoothed instantaneous rate ≈ rate(http_requests_total[5m])
movingAverage(perSecond(stats.api.requests), '5min')
```

Counter resets — `nonNegativeDerivative` returns **NaN** at the reset point by default, unlike PromQL's `rate()` which extrapolates a zero-crossing. To make the gap visible: leave it. To paper over: `transformNull(nonNegativeDerivative(s), 0)`.

**The actual reset/wraparound math.** From M3's source (`src/query/graphite/native/builtin_functions.go`):

```go
difference := value - previousValue
if difference >= 0 {
    return difference                              // normal forward step
}
if !math.IsNaN(maxValue) && maxValue >= value {
    return (maxValue - previousValue) + value + 1  // wraparound through maxValue
}
return math.NaN()                                  // unknown reset → drop
```

Three regimes:

1. `cur >= prev` → normal `cur - prev`.
2. `cur < prev` **with `maxValue` provided** → assumes the counter overflowed: emits `(maxValue − prev) + cur + 1`. Useful for **fixed-width counters** like SNMP `Counter32` (`maxValue=4294967295`).
3. `cur < prev` **without `maxValue`** → returns NaN. The default for software counters that reset to zero on restart.

PromQL's `rate()` differs: it always treats `cur < prev` as a reset to zero and emits `cur` as the delta (effectively `(0 − 0) + cur`), never NaN. M3QL leans conservative; Prom leans best-effort. For SLO accounting, M3QL's NaN is honest; for live dashboards, Prom's extrapolation is friendlier.

#### 2.11.7 Percentile Functions — Three Different Things, Same Word

M3QL's percentile functions are the most subtly-misused family, because there are at least **four** different "percentile" operations and they look alike.

| Function | What it returns | PromQL/SQL analog |
|---|---|---|
| `nPercentile(seriesList, n)` | for **each input series**, a single horizontal line at the nth percentile of its own samples over the query range | `quantile_over_time(0.<n>, s[<range>])` flat-lined |
| `percentileOfSeries(seriesList, n, interpolate=False)` | a **single output series** whose value at each `t` is the nth percentile **across the bundle** of input series at `t` | `quantile by () (0.<n>, ...)` — quantile across-series, per-timestamp |
| `removeAbovePercentile(seriesList, n)` | input series with values above the nth percentile **of each series** masked to NaN | per-series clipping; no direct PromQL |
| `removeBelowPercentile(seriesList, n)` | symmetric — values below masked | per-series clipping |
| `averageOutsidePercentile(seriesList, n)` | **filters whole series**: drops series whose average lies inside the nth-percentile band | series-level outlier filter |

Side-by-side mental model:

```
inputs (3 series, 5 points each):
s1: 1   2   3   4   5
s2: 10  20  30  40  50
s3: 100 200 300 400 500

nPercentile(_, 90):
s1: 4.6, 4.6, 4.6, 4.6, 4.6     ← 90th pct of s1's own values, flat line
s2: 46,  46,  46,  46,  46       ← 90th pct of s2's own values
s3: 460, 460, 460, 460, 460      ← 90th pct of s3's own values

percentileOfSeries(_, 90):
single series:  82, 164, 246, 328, 410
                ▲    ▲    ▲    ▲    ▲
              90th pct of [1,10,100], [2,20,200], …  at each timestamp
              (interpolation between s2 and s3 since 90% of 3 series = 2.7)
```

The first answers "what is the long-run 90th-percentile baseline of each pod's CPU"; the second answers "at every minute, what is the 90th-percentile pod's CPU". Both useful, almost never interchangeable.

A worked p99 latency query in three styles, **all returning roughly the same number**:

```m3ql
# 1. Histogram-style: percentileOfSeries across pod p99 series at each t
percentileOfSeries(seriesByTag('name=p99','app=checkout'), 99, true)

# 2. Time-window quantile per series, then aggregate across pods
aggregateGroupByTags(
  'avg', 'service',
  movingWindow(seriesByTag('name=latency_ms','app=checkout'), '5min', 'median')
)

# 3. Bucketed-counter approach (the closest to PromQL's histogram_quantile)
#    Uses pre-bucketed counters (one series per le bucket boundary)
asPercent(
  stats.api.checkout.latency_bucket.le_0_25,
  stats.api.checkout.latency_bucket.le_inf
)
# read the boundary at which this crosses 99 — done by the panel, not by M3QL
```

M3 (unlike Prometheus) does **not** ship a built-in `histogram_quantile` for classic bucketed histograms in M3QL — you usually drop into PromQL via M3's PromQL endpoint for that. M3QL's percentile family is best for **gauge-shaped** latency series (each pod publishes its own quantile-summary metric).

> **Pitfall:** `nPercentile` returning a flat horizontal line is *correct* — the function's job is to plot the threshold, not the series' values. Users who think "p99 went flat??" are reading the wrong function. Use `movingWindow(..., 'median')` or `percentileOfSeries(...)` if you want the percentile to vary over time.

**The actual percentile algorithm.** From M3's source (`src/query/graphite/common/percentiles.go`), the percentile computation is:

```go
fractionalRank := (percentile / 100.0) * float64(len(series) + 1)
rank          := int(fractionalRank)
rankFraction  := fractionalRank - float64(rank)

if !interpolate {
    rank = rank + int(math.Ceil(rankFraction))   // round up
}

result := series[rank-1]                          // 1-indexed pick

if interpolate && rank != len(series) {
    next   := series[rank]
    result = result + rankFraction * (next - result)   // linear interp
}
```

This is **NIST Method R-6** ("rank = q·(n+1)"), not R-7 (NumPy/Excel default, "q·(n−1)+1"). The two diverge at small `n`:

```
input series (sorted): [10, 20, 30, 40, 50],  n=5,  q=0.99

R-6 (M3/Graphite):  rank = 0.99 * 6 = 5.94  → with interp: 50 + 0.94*(NaN) = 50 (clamped)
R-7 (NumPy):        rank = 0.99 * 4 + 1 = 4.96  → 40 + 0.96*(50-40) = 49.6
```

For series with thousands of points the difference is negligible; for sparse series (a handful of pods, short window) it can be 5–10%. If your audit/SLO calc compares M3 percentiles to a NumPy notebook and they disagree by a few percent, this is why.

`interpolate=False` is the default and uses `Ceil(rankFraction)` to round rank up — i.e. picks the *higher* sorted value at the bucket boundary. Conservative for SLOs (over-reports the percentile slightly).

#### 2.11.8 asPercent — Ratios with Built-In Pairing

`asPercent` is one of the most-used and least-understood M3QL functions. Its three forms:

```
asPercent(seriesList)                     # each point as % of sum of seriesList at that t
asPercent(seriesList, total=N)            # each point as % of constant N
asPercent(seriesList, totalSeries)        # each series as % of corresponding totalSeries
asPercent(seriesList, totalSeries, *nodes) # each series as % of total, paired by *nodes
```

Worked examples:

```m3ql
# CPU usage as % of total cluster CPU at each timestamp
asPercent(stats.gauges.host.*.cpu)
# → for each host series, value(t) = host_cpu(t) / sum(all_host_cpu(t)) * 100

# Error ratio per service (modern tag style)
asPercent(
  aggregateGroupByTags('sum', 'service', seriesByTag('name=errors')),
  aggregateGroupByTags('sum', 'service', seriesByTag('name=requests'))
)
# pairs by the keep-tags (service); requires both sides to share that tag

# Memory usage % vs a hard 64 GB cap
asPercent(stats.gauges.host.*.mem_bytes, total=68719476736)

# Per-host cpu as % of *that host's* CPU limit, paired by hostname (position 3)
asPercent(stats.gauges.host.*.cpu, stats.gauges.host.*.cpu_limit, 3)
```

The last form is the powerful one — `*nodes` does the **pairing** that PromQL's vector matching does. Without it, M3QL pairs first-with-first by series order, which is fragile.

**The actual `asPercent` formula** (from M3 source, with NaN/zero guards):

```go
for i := 0; i < n; i++ {
    v := series.ValueAt(i)
    t := total.ValueAt(i)
    if !math.IsNaN(v) && !math.IsNaN(t) && t != 0 {
        out.SetValueAt(i, (v / t) * 100.0)
    }   // else NaN — silent skip
}
```

Three things to notice:

- **Multiplied by 100**, not by 1. The output is a percent, not a fraction. Applying it twice gives 10000-scale numbers.
- **`t == 0` produces NaN**, not infinity. Panels show a gap, not a spike. (Worth knowing for "ratio panel went blank" tickets — the denominator hit zero.)
- **NaN propagates silently.** A single missing scrape on either side leaves a NaN at that timestamp — no error, no warning. Wrap in `keepLastValue()` if you need a continuous line.

When `*nodes` is supplied, both the numerator and the denominator series are first **bucketed by the node values** (`getNodeOrTag(series, n)` joined with `.`), then each bucket's numerator is divided by the same bucket's denominator. The bucketing key is exactly `aggregateGroupByTags`'s key — same primitive under the hood.

Equivalents:

| Goal | PromQL | M3QL |
|---|---|---|
| `a / sum(a)` per timestamp | `a / scalar(sum(a))` | `asPercent(a)` |
| `a / b` paired by `service` | `a / on(service) b` | `asPercent(a, b, 2)` (if `service` is at position 2) or `asPercent(a, b)` with `aggregateGroupByTags('sum','service',...)` on both sides |
| `a / 100` constant | `a / 100` (note: returns a fraction not a %) | `asPercent(a, total=100)` |

#### 2.11.9 Multi-Tag Aggregations — aggregateGroupByTags

The single most useful modern M3QL function for tag-style data is `aggregateGroupByTags`. The closest analog to PromQL's `sum by (...)` style:

```m3ql
# Sum requests per (service, dc), aggregating over everything else
aggregateGroupByTags(
  'sum',
  'service','dc',
  seriesByTag('name=requests')
)
```

Equivalents at a glance:

| Goal | PromQL | M3QL (tag style) |
|---|---|---|
| sum across all series, drop all labels | `sum(x)` | `sumSeries(seriesByTag(...))` |
| sum keeping `service`, `dc` | `sum by (service,dc) (x)` | `aggregateGroupByTags('sum','service','dc',seriesByTag(...))` |
| avg keeping `service` | `avg by (service) (x)` | `aggregateGroupByTags('avg','service',seriesByTag(...))` |
| count series per `service` | `count by (service) (x)` | `aggregateGroupByTags('count','service',seriesByTag(...))` |
| max keeping `service`, `dc` | `max by (service,dc) (x)` | `aggregateGroupByTags('max','service','dc',seriesByTag(...))` |
| stddev keeping `service` | `stddev by (service) (x)` | `aggregateGroupByTags('stddev','service',seriesByTag(...))` |
| keeping all labels, no aggregation | `x` | `seriesByTag(...)` |

The dotted-name analog uses position-based functions:

| Goal | M3QL (dotted-name) |
|---|---|
| sum across the third position only | `sumSeriesWithWildcards(stats.api.*.*.x, 3)` |
| avg across the third position only | `averageSeriesWithWildcards(stats.api.*.*.x, 3)` |
| sum across two positions | `sumSeriesWithWildcards(pat, 2, 4)` |
| any aggregator across positions | `aggregateWithWildcards(pat, 'max', 3)` |

`aggregateSeriesLists(listA, listB, 'sum', xFilesFactor=...)` is a related primitive — it pairs `listA[i]` with `listB[i]` element-by-element and applies the aggregator. Useful for "per-pod CPU + per-pod memory pair" panels.

#### 2.11.10 Aliasing — Making Output Readable

PromQL keeps the label set on every series and Grafana renders them via legend format strings. M3QL outputs are dotted strings, so you must **rewrite the name** to get a useful legend.

```m3ql
aliasByNode(stats.api.*.requests, 2)
# input series:   stats.api.checkout.requests
# alias result:   "checkout"   ← position 2 alone

aliasByNode(stats.api.*.requests, 2, 3)
# alias result:   "checkout.requests"   ← positions joined with '.'

aliasByMetric(stats.api.checkout.requests)
# alias result:   "requests"   ← last segment

aliasByTags(seriesByTag('name=requests'), 'service','dc')
# alias result:   "checkout, us-east1"

aliasSub(stats.api.*.requests, '^stats\\.api\\.([^.]+)\\.requests$', '\\1 rps')
# alias result:   "checkout rps"

alias(s, 'fleet RPS')
# constant alias regardless of input
```

Without aliasing, panels render literal full nested query strings — every legend entry is the *query*, not the *thing*. **Always alias the outer expression.**

#### 2.11.11 Filter & Rank Series — exclude, grep, highest*, weightedAverage

M3QL's filters operate on **whole series**, not on individual samples. Useful for top-N panels:

| Function | What it does |
|---|---|
| `exclude(seriesList, regex)` | drop series whose name matches |
| `grep(seriesList, regex)` | keep only series whose name matches |
| `highestAverage(seriesList, n)` | top-N series by average value over the range |
| `highestCurrent(seriesList, n)` | top-N by most recent value |
| `highestMax(seriesList, n)` | top-N by max value |
| `lowestCurrent(seriesList, n)` | bottom-N by most recent |
| `lowestAverage(seriesList, n)` | bottom-N by average |
| `mostDeviant(seriesList, n)` | top-N by stddev (most volatile) |
| `currentAbove(seriesList, n)` | drop series whose **current** value < n |
| `currentBelow(seriesList, n)` | drop series whose current value > n |
| `averageAbove(seriesList, n)` | drop series whose average over range < n |
| `averageBelow(seriesList, n)` | symmetric |
| `removeBelowValue(seriesList, n)` | mask points < n to NaN (per-point, not per-series) |
| `removeAboveValue(seriesList, n)` | mask points > n |
| `weightedAverage(values, weights, *nodes)` | weighted mean across two paired lists |

```m3ql
# Top 10 services by p99 latency, with friendly names
aliasByNode(
  highestAverage(seriesByTag('name=p99'), 10),
  'service'
)

# CPU-weighted mean p99 latency across pods (giving busier pods more weight)
weightedAverage(
  seriesByTag('name=p99'),
  seriesByTag('name=cpu'),
  'pod'
)
```

PromQL's equivalents are clunkier — `topk(10, ...)` does similar work but "top 10 by average over a window" requires `topk(10, avg_over_time(x[5m]))` and there is no direct stddev-based selector or weighted-average primitive.

#### 2.11.12 Math Operators and Per-Series Combinators

M3QL replaces PromQL's binary vector matching with explicit functions:

| Goal | PromQL | M3QL |
|---|---|---|
| `a + b` | `a + b` (with implicit matching) | `sumSeries(a, b)` |
| `a / b` | `a / b` | `divideSeries(a, b)` |
| `a * b` | `a * b` | `multiplySeries(a, b)` |
| `a - b` | `a - b` | `diffSeries(a, b)` |
| ratio of series to sum | `a / sum(b)` | `asPercent(a, sumSeries(b))` |
| filter NaN gaps with last value | `last_over_time(a[5m])` | `keepLastValue(a)` |
| treat NaN as zero | `... or vector(0)` | `transformNull(a, 0)` |
| treat NaN as something else | n/a | `transformNull(a, 999)` |
| absolute value | `abs(x)` | `absolute(x)` |
| log | `ln(x)` / `log2(x)` | `logarithm(x, base)` |
| invert | `1 / x` | `invert(x)` |
| min/max threshold per point | `clamp_min(x, n)` / `clamp_max(x, n)` | `removeBelowValue(x, n)` / `removeAboveValue(x, n)` (via NaN), or `keepLastValue` chains |

#### 2.11.13 Scaling, Time-Shifting, Forecasting

| Function | Purpose |
|---|---|
| `scale(s, n)` | multiply every point by `n` |
| `offset(s, n)` | add `n` to every point |
| `scaleToSeconds(s, n)` | rescale step-counted series to "per N seconds" |
| `timeShift(s, '-1d')` | shift the series in time (week-over-week panels) |
| `timeStack(s, '-1d', 0, 7)` | overlay 7 daily-shifted copies on one panel |
| `holtWintersForecast(s, bootstrapInterval='7d', seasonality='1d')` | exponential-smoothing forecast |
| `holtWintersConfidenceBands(s, delta=3, ...)` | upper/lower forecast bands at `delta` stddevs |
| `holtWintersAberration(s, delta=3, ...)` | series of "deviation from forecast" — primitive for anomaly alerts |

Holt-Winters is the closest a Graphite/M3 stack gets to anomaly detection without external tooling. The `bootstrapInterval='7d'` arg means "use the prior week to seed the model"; `seasonality='1d'` means "the cycle repeats daily."

**The actual update equations** (M3 source, `holtWintersForecast`):

```
intercept_t = α · (actual_t − seasonal_{t−L}) + (1−α)(intercept_{t−1} + slope_{t−1})
slope_t     = β · (intercept_t − intercept_{t−1}) + (1−β) · slope_{t−1}
seasonal_t  = γ · (actual_t − intercept_t) + (1−γ) · seasonal_{t−L}

forecast_t  = intercept_t + slope_t + seasonal_{t−L+1}
```

with **hard-coded constants** `α=0.1, β=0.0035, γ=0.1` and `L = seasonality / step` (the season length in points). Notice:

- **`α=0.1`** means the forecast adapts slowly to changes in level — a sustained traffic shift takes ~10 sample periods to be reflected.
- **`β=0.0035`** is *extremely* small; the trend term barely moves. This is a deliberate choice for noisy ops data, but it means H-W will under-react to genuine ramp-ups.
- **`γ=0.1`** means the seasonal component also adapts slowly — week-of-quarter seasonality drift takes ~10 days to show up.
- The constants are **not configurable** in M3QL. If you need tuned forecasting, export the data to Python (`statsmodels.tsa.holtwinters`) and pin α/β/γ from cross-validation.

`holtWintersConfidenceBands` returns `forecast ± delta · σ_forecast` where `σ_forecast` is itself an exponentially-smoothed estimate of the residual stddev. `holtWintersAberration` is `actual − forecast` clipped to the confidence band — non-zero only when actual is outside the band. Alerts on aberration are the standard "anomaly alert" idiom but tend to fire on every minor cyclic anomaly; threshold at `delta=4` or higher for production noise tolerance.

The triple-exponential model behaves badly on **irregular seasonality** (weekly business cycles, holidays). Use it for diurnal patterns; use external tooling for anything else.

#### 2.11.14 Common Idioms — Cheat Sheet

```m3ql
# RPS per service, smoothed over 5 minutes
aliasByNode(
  movingAverage(
    perSecond(sumSeriesWithWildcards(stats.api.*.requests, 3)),
    '5min'),
  2)

# Error ratio per service
asPercent(
  aliasByNode(sumSeriesWithWildcards(stats.api.*.errors, 3), 2),
  aliasByNode(sumSeriesWithWildcards(stats.api.*.requests, 3), 2))

# Top 5 noisiest services this week (highest stddev of latency)
aliasByTags(mostDeviant(seriesByTag('name=p99'), 5), 'service')

# 7-day-ago overlay for capacity panel
timeShift(sumSeries(stats.api.*.requests), '-7d')

# Percent of fleet pods over CPU 0.8
asPercent(
  countSeries(removeBelowValue(stats.gauges.host.*.cpu, 0.8)),
  countSeries(stats.gauges.host.*.cpu))

# Anomaly band: current value vs Holt-Winters forecast
holtWintersAberration(stats.api.checkout.requests)

# p99 of per-pod latencies, sliding window per pod, then 95th percentile
# of the pod fleet at each timestamp
percentileOfSeries(
  movingWindow(seriesByTag('name=latency_ms'), '5min', 'median'),
  95, true)
```

#### 2.11.15 SQL Equivalents — Translating M3QL Idioms

When migrating off Graphite/M3 onto a SQL telemetry store (ClickHouse most often), the translations are mechanical:

| M3QL | SQL (ClickHouse-flavored) |
|---|---|
| `sumSeries(seriesByTag('name=requests'))` | `SELECT toStartOfMinute(ts) m, sum(value) FROM metrics WHERE name='requests' GROUP BY m` |
| `aggregateGroupByTags('sum','service',...)` | `SELECT m, tags['service'] svc, sum(value) FROM metrics WHERE name='requests' GROUP BY m, svc` |
| `averageSeries(...)` | `SELECT m, avg(value) FROM metrics WHERE ... GROUP BY m` |
| `movingAverage(s, '5min')` | `avg(value) OVER (PARTITION BY series ORDER BY ts RANGE BETWEEN INTERVAL 5 MINUTE PRECEDING AND CURRENT ROW)` |
| `summarize(s, '5min', 'avg')` | `SELECT toStartOfFiveMinutes(ts) m5, avg(value) FROM metrics GROUP BY m5` |
| `perSecond(counter)` | `(value - lagInFrame(value) OVER (PARTITION BY series ORDER BY ts)) / dateDiff('second', lagInFrame(ts) OVER (...), ts)` |
| `nonNegativeDerivative(c)` | `if(value >= lagInFrame(value) OVER (PARTITION BY series ORDER BY ts), value - lagInFrame(value) OVER (...), NULL)` |
| `highestAverage(seriesList, 10)` | `... GROUP BY series ORDER BY avg(value) DESC LIMIT 10` |
| `mostDeviant(seriesList, 10)` | `... GROUP BY series ORDER BY stddevPop(value) DESC LIMIT 10` |
| `nPercentile(s, 99)` | `quantile(0.99)(value) OVER (PARTITION BY series)` (same value at every t) |
| `percentileOfSeries(s, 99, true)` | `quantile(0.99)(value)` after `GROUP BY toStartOfMinute(ts)` (across-series, per-bucket) |
| `removeAbovePercentile(s, 99)` | `if(value <= quantile(0.99)(value) OVER (PARTITION BY series), value, NULL)` |
| `asPercent(a, b)` | `100.0 * a.v / b.v` after `JOIN ON ts AND a.tags['service'] = b.tags['service']` |
| `asPercent(seriesList)` (no total) | `100.0 * value / sum(value) OVER (PARTITION BY ts)` |
| `weightedAverage(v, w, *nodes)` | `sum(v.val * w.val) / sum(w.val) GROUP BY ts, <nodes>` after a JOIN on the node tags |
| `holtWintersForecast(s)` | not native — usually delegate to a UDF or extract to Python/Pandas |
| `seriesByTag('service=~api.*')` | `WHERE tags['service'] LIKE 'api%'` (or `match(tags['service'], '^api.*')`) |
| `aliasByNode(s, n)` | column aliasing in SQL: `SELECT ... AS service` |

Concrete side-by-side. M3QL:

```m3ql
aliasByNode(
  movingAverage(
    perSecond(sumSeriesWithWildcards(stats.api.*.requests, 3)),
    '5min'),
  2)
```

ClickHouse SQL:

```sql
WITH per_minute AS (
  SELECT
    toStartOfMinute(ts)         AS m,
    splitByChar('.', name)[3]   AS service,
    sum(value)                  AS v
  FROM metrics
  WHERE name LIKE 'stats.api.%.requests'
  GROUP BY m, service
),
rate AS (
  SELECT
    m, service,
    greatest(0, v - lagInFrame(v) OVER (PARTITION BY service ORDER BY m))
      / 60.0                    AS rps
  FROM per_minute
)
SELECT
  m, service,
  avg(rps) OVER (
    PARTITION BY service ORDER BY m
    RANGE BETWEEN INTERVAL 5 MINUTE PRECEDING AND CURRENT ROW
  ) AS rps_smoothed
FROM rate
ORDER BY service, m;
```

Per-percentile example. M3QL:

```m3ql
aliasByTags(
  percentileOfSeries(
    seriesByTag('name=latency_ms','app=checkout'),
    99, true),
  'app')
```

ClickHouse SQL:

```sql
SELECT
  toStartOfMinute(ts)                          AS m,
  tags['app']                                  AS app,
  quantileExactWeightedInterpolated(0.99)(value, 1) AS p99_across_pods
FROM metrics
WHERE name = 'latency_ms'
  AND tags['app'] = 'checkout'
GROUP BY m, app
ORDER BY m;
```

Three things to notice across both translations:

1. **Pipeline → CTE chain.** Every M3QL pipe stage becomes its own CTE. The execution order matches reading SQL top-down vs reading M3QL outside-in.
2. **Position math in dotted names** becomes `splitByChar` indexing — fragile; migrations should re-shape names into proper tag columns first.
3. **Per-series window functions** (`PARTITION BY service`) replace M3QL's implicit "applies to every series in the bundle" semantic.

For migrations off Graphite/M3 onto **PromQL** instead of SQL, the pattern is also mechanical: `perSecond`→`rate`, `movingAverage`→`avg_over_time`, `aggregateGroupByTags('sum','service')`→`sum by (service)`, `aliasByTags`→Grafana legend format, `nPercentile`→`quantile_over_time`. The hard cases are the same: per-series filters (`highestAverage`, `mostDeviant`, `weightedAverage`), forecasting (`holtWintersForecast`), time-stack overlays (`timeStack`), and the across-series quantile (`percentileOfSeries`) — none of which have native PromQL forms. Plan to drop those panels onto SQL or accept a fidelity loss.

#### 2.11.16 Pitfalls Specific to M3QL

- **Forgetting `aliasByNode` / `aliasByTags`** — output series names are full query strings, not human-readable. Every panel needs an alias.
- **`avgSeriesWithWildcards` vs `averageAbove`** — the first reduces dimensions, the second is a series-level filter. Names are dangerously similar.
- **`summarize` vs `movingAverage`** — first downsamples (lower resolution); second smooths at native resolution.
- **`nPercentile` vs `percentileOfSeries`** — first is a flat horizontal line per series; second is a single across-series series varying with `t`.
- **`asPercent` without explicit `*nodes`** — pairs by series order, which silently breaks when series counts on either side differ.
- **`perSecond` on gauges** — only meaningful for counters. M3 will compute it on a gauge and produce nonsense.
- **Position indexing in dotted names** — adding a new naming segment shifts every position. Tag-based queries (`seriesByTag` + `aggregateGroupByTags`) are migration-safe; positional queries are not.
- **`derivative` vs `nonNegativeDerivative`** — the former returns negative numbers across a counter reset; only the latter handles resets correctly.
- **Glob fan-out** — `stats.**.requests` against millions of series fans out hugely. Equivalent to PromQL's "regex matcher with no anchored prefix"; same OOM consequence.
- **Holt-Winters bootstrapping** — uses prior `bootstrapInterval` of data implicitly, so a query for "last 1h" actually reads "last 1h + 7d." Set query timeouts accordingly.
- **`xFilesFactor`** in `summarize`/`aggregate` — the threshold of "fraction of points that must be non-NaN to emit a value." Default behavior across vendors disagrees; pin it explicitly when correctness matters.

> **Mental model:** PromQL is **set-of-vectors algebra**. M3QL is **left-to-right pipeline of named series**. Both handle the same workloads at scale; the linguistic difference reshapes how you think about a query, not what's possible. When you see an M3QL expression you don't recognize, mentally translate it to "selector → reducer → window → alias" and the PromQL form usually pops out.

#### 2.11.17 Verified Source References

The math in the sub-sections above is taken directly from the M3 and Graphite source trees. If you need to chase an edge case, these are the canonical files:

| Function family | M3 source | Graphite source |
|---|---|---|
| Percentile algorithm | `src/query/graphite/common/percentiles.go` (`GetPercentile`, `NPercentile`, `RemoveByPercentile`) | `webapp/graphite/render/functions.py` (`_getPercentile`, `nPercentile`, `removeAbovePercentile`) |
| Counter math | `src/query/graphite/native/builtin_functions.go` (`nonNegativeDerivative`, `perSecond`) | `functions.py` (`nonNegativeDerivative`, `_nonNegativeDelta`) |
| Aggregation + wildcard collapsing | `src/query/graphite/native/aggregation_functions.go`, `common/aggregation.go` (key built via `aggKey`/`getNodeOrTag`) | `functions.py` (`aggregateWithWildcards`, `aggKey`) |
| `asPercent` | `src/query/graphite/native/builtin_functions.go` (`asPercent`) — formula `(v/t)*100` | `functions.py` (`asPercent`) |
| Moving window + xFilesFactor | `common/moving.go`, `native/builtin_functions.go` (`movingWindow`, `effectiveXFF`) | `functions.py` (`movingWindow`, `xff`) |
| Holt-Winters | `native/builtin_functions.go` (`holtWintersForecast`, `holtWintersIntercept`, `holtWintersSlope`, `holtWintersSeasonal`) — α=0.1, β=0.0035, γ=0.1 hard-coded | `functions.py` (`holtWintersAnalysis`) |
| `summarize` (downsampling) | `native/builtin_functions.go` (`summarize`, `summarizeValues`) | `functions.py` (`summarize`) |

Two practical tips for chasing behavior diffs in production:

- **M3 vs Graphite drift.** The Graphite reference is Python; M3's port is Go and has been re-implemented (not transpiled). Most behavior matches, but a few defaults diverge — notably `xFilesFactor` defaults and percentile interpolation behavior. When in doubt, write a one-row test and compare both engines on the same input.
- **"Why is my number off by epsilon?"** — Graphite-family engines round many intermediate results to 6 decimal places (see `round(delta / step, 6)` in `perSecond`). PromQL does not round. Don't stack equality alerts on derived M3QL series.

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
