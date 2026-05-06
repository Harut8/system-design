# Appendix C — PromQL / LogQL / TraceQL Recipe Book

Practical query recipes you'll reach for repeatedly. Grouped by query language and use case.

---

## C.1 PromQL

### C.1.1 Rate / throughput

```promql
# Requests per second over 5 minutes
sum(rate(http_requests_total[5m]))

# Per-service RPS
sum by (service) (rate(http_requests_total[5m]))

# Per-service per-status RPS
sum by (service, status) (rate(http_requests_total[5m]))
```

### C.1.2 Error rate

```promql
# Error rate as fraction
sum(rate(http_requests_total{status=~"5.."}[5m]))
  /
sum(rate(http_requests_total[5m]))

# Per-service error rate
sum by (service) (rate(http_requests_total{status=~"5.."}[5m]))
  /
sum by (service) (rate(http_requests_total[5m]))
```

### C.1.3 Latency percentiles

```promql
# P99 latency (classic histogram)
histogram_quantile(0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)

# Per-service P99
histogram_quantile(0.99,
  sum by (le, service) (rate(http_request_duration_seconds_bucket[5m]))
)

# Multiple percentiles in one query
histogram_quantiles(0.50, 0.95, 0.99,
  sum by (le, service) (rate(http_request_duration_seconds_bucket[5m]))
)
```

### C.1.4 SLO burn rate

```promql
# Burn rate (with SLO target = 99.9%)
(
  sum(rate(http_requests_total{status=~"5.."}[5m]))
    /
  sum(rate(http_requests_total[5m]))
) / 0.001
```

### C.1.5 Multi-window multi-burn-rate alert

```yaml
- alert: ServiceFastBurn
  expr: |
    (rate(http_5xx[1h]) / rate(http_total[1h])) / 0.001 >= 14.4
    and
    (rate(http_5xx[5m]) / rate(http_total[5m])) / 0.001 >= 14.4
  for: 2m
```

### C.1.6 Saturation

```promql
# CPU saturation
sum by (instance) (rate(node_cpu_seconds_total{mode!="idle"}[5m]))
  /
count by (instance) (node_cpu_seconds_total{mode="idle"})

# Memory pressure
1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)

# Disk usage
1 - (node_filesystem_avail_bytes / node_filesystem_size_bytes)
```

### C.1.7 Top-N queries

```promql
# Top 10 services by request rate
topk(10, sum by (service) (rate(http_requests_total[5m])))

# Top 10 services by error rate
topk(10, sum by (service) (rate(http_requests_total{status=~"5.."}[5m])))
```

### C.1.8 Exemplars

```promql
# Histogram with exemplars enabled (PromQL 2.26+)
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m]))) @ exemplars
```

### C.1.9 Recording rules pattern

```yaml
# Pre-compute heavy aggregates
- record: service:http_requests:rate5m
  expr: sum by (service) (rate(http_requests_total[5m]))

- record: service:http_request_duration:p99_5m
  expr: histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))
```

### C.1.10 Cardinality detection

```promql
# Top-N metrics by cardinality
topk(20, count by (__name__) ({__name__=~".+"}))

# Active series count
prometheus_tsdb_head_series

# Series creation rate
rate(prometheus_tsdb_head_series_created_total[5m])
```

### C.1.11 Anomaly detection (simple)

```promql
# Z-score anomaly: >3 stddev from 1h average
abs(metric - avg_over_time(metric[1h])) / stddev_over_time(metric[1h]) > 3

# Compare to last week
abs(rate(metric[5m]) - rate(metric[5m] offset 1w)) > threshold
```

### C.1.12 Capacity / headroom

```promql
# Headroom percentage
(1 - sum(rate(node_cpu_seconds_total{mode!="idle"}[5m])) / count(node_cpu_seconds_total{mode="idle"})) * 100
```

### C.1.13 Service-graph (auto-derived)

```promql
# Service-to-service latency p99 (Tempo / Mimir-derived)
histogram_quantile(0.99, sum by (client, server, le) (rate(traces_service_graph_request_server_seconds_bucket[5m])))

# Service-to-service error rate
sum by (client, server) (rate(traces_service_graph_request_failed_total[5m]))
  /
sum by (client, server) (rate(traces_service_graph_request_total[5m]))
```

### C.1.14 Pod / k8s queries

```promql
# Per-namespace memory usage
sum by (namespace) (container_memory_working_set_bytes)

# Per-namespace CPU
sum by (namespace) (rate(container_cpu_usage_seconds_total[5m]))

# Pod restart rate
sum by (namespace, pod) (rate(kube_pod_container_status_restarts_total[5m]))
```

### C.1.15 Kafka consumer lag

```promql
# Lag in time
kafka_consumergroup_lag_seconds{group="...", topic="..."}

# Lag rate of growth
deriv(kafka_consumergroup_lag_messages[10m])

# Per-partition lag
sum by (partition) (kafka_consumergroup_lag_messages{group="...", topic="..."})
```

### C.1.16 Database queries

```promql
# Per-query rate
rate(pg_stat_statements_calls{queryid="..."}[5m])

# Per-query latency
rate(pg_stat_statements_total_time_seconds{queryid="..."}[5m])
  /
rate(pg_stat_statements_calls{queryid="..."}[5m])

# Replica lag
pg_replication_lag_seconds{replica="..."}

# Connection pool saturation
db_connections_active / db_pool_max_size
```

---

## C.2 LogQL

### C.2.1 Basic filters

```logql
# All logs from a service
{service="checkout"}

# Errors only
{service="checkout"} |= "ERROR"

# Errors with specific text
{service="checkout"} |= "ERROR" |= "timeout"

# Excluding noise
{service="checkout"} |= "ERROR" != "expected"
```

### C.2.2 JSON parsing

```logql
# Parse JSON; filter on parsed field
{service="checkout"} | json | latency_ms > 500

# Format output
{service="checkout"} | json | line_format "{{.method}} {{.path}} {{.latency_ms}}ms"
```

### C.2.3 Logfmt parsing

```logql
{service="checkout"} | logfmt | level="error"
```

### C.2.4 Aggregations

```logql
# Error rate per service
sum by (service) (rate({level="error"}[5m]))

# Top 10 most-frequent error messages (with regex extraction)
topk(10,
  sum by (msg) (rate({service="checkout", level="error"} | regexp "msg=\"(?P<msg>[^\"]+)\""[5m]))
)
```

### C.2.5 Trace-id correlation

```logql
# All logs for a specific trace
{service=~".+"} | json | trace_id="a1b2c3..."
```

### C.2.6 Quantile of logged value

```logql
quantile_over_time(0.99,
  {service="checkout"} | json | unwrap latency_ms [5m]
)
```

### C.2.7 Pattern-based extraction

```logql
{service="checkout"} 
  | pattern "<_> <method> <path> <status> <_>" 
  | status=500
```

---

## C.3 TraceQL

### C.3.1 Basic span search

```traceql
# Find traces with HTTP 500 status
{ span.http.status_code = 500 }

# By service
{ resource.service.name = "checkout" }

# Multiple conditions
{ resource.service.name = "checkout" && span.http.status_code = 500 }
```

### C.3.2 Latency-based

```traceql
# Slow traces
{ span.duration > 1s }

# Slow spans within a service
{ resource.service.name = "checkout" && span.duration > 500ms }

# Spans matching name and slow
{ name = "POST /checkout" && span.duration > 1s }
```

### C.3.3 Cross-span queries

```traceql
# Traces where one span had error and another was slow
{ span.error = true } && { span.duration > 500ms }

# Traces visiting specific service AND status
{ resource.service.name = "payments" && span.http.status_code = 500 }
```

### C.3.4 Database-related traces

```traceql
{ span.db.system = "postgresql" && span.duration > 1s }
```

### C.3.5 Aggregations

```traceql
# Trace count
count_over_time({ span.error = true }[5m])

# Average latency by service
avg(duration) by (resource.service.name)
```

---

## C.4 SQL on the Lakehouse (ClickHouse / BigQuery / Snowflake)

### C.4.1 Year-over-year traffic

```sql
SELECT
  date_trunc('day', ts) AS day,
  sum(value) AS requests
FROM metrics
WHERE metric_name = 'http_requests_total'
  AND ts BETWEEN '2025-11-01' AND '2025-12-31'
   OR ts BETWEEN '2026-11-01' AND '2026-12-31'
GROUP BY day
ORDER BY day;
```

### C.4.2 Top customers by error count

```sql
SELECT
  attributes['customer_id_hash'] AS customer_hash,
  count(*) AS errors
FROM logs
WHERE level = 'error'
  AND ts > NOW() - INTERVAL '7 days'
GROUP BY customer_hash
ORDER BY errors DESC
LIMIT 100;
```

### C.4.3 Latency by customer tier (joined with warehouse)

```sql
SELECT
  c.tier,
  PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY s.duration_ns / 1e6) AS p99_ms
FROM spans s
JOIN customers c ON c.id = s.attributes['customer_id_hash']
WHERE s.ts > NOW() - INTERVAL '7 days'
  AND s.span_name = 'POST /checkout'
GROUP BY c.tier
ORDER BY p99_ms DESC;
```

### C.4.4 Cross-signal join

```sql
-- Errors per release for specific customers
SELECT
  l.attributes['release'] AS release,
  count(*) AS errors
FROM logs l
JOIN customers c ON c.id_hash = l.attributes['user_id_hash']
WHERE c.churned_date BETWEEN '2026-04-01' AND '2026-04-30'
  AND l.level = 'error'
GROUP BY release
ORDER BY errors DESC;
```

### C.4.5 Trace search with rich attribute filtering

```sql
SELECT trace_id, service_name, span_name, duration_ns
FROM spans
WHERE attributes['feature_flag'] = 'pricing_v2'
  AND ts BETWEEN '2026-05-01' AND '2026-05-06'
  AND duration_ns > 1e9
ORDER BY duration_ns DESC
LIMIT 100;
```

### C.4.6 RUM analysis

```sql
SELECT
  page,
  country,
  PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY web_vitals['lcp']) AS p75_lcp_ms
FROM rum_events
WHERE ts > NOW() - INTERVAL '7 days'
GROUP BY page, country
HAVING p75_lcp_ms > 2500;
```

### C.4.7 Drift detection

```sql
WITH this_week AS (
  SELECT topic_cluster, count(*) AS cnt
  FROM llm_events
  WHERE ts > NOW() - INTERVAL '7 days'
  GROUP BY topic_cluster
),
last_week AS (
  SELECT topic_cluster, count(*) AS cnt
  FROM llm_events
  WHERE ts BETWEEN NOW() - INTERVAL '14 days' AND NOW() - INTERVAL '7 days'
  GROUP BY topic_cluster
)
SELECT
  COALESCE(this.topic_cluster, last.topic_cluster) AS topic,
  COALESCE(this.cnt, 0) AS this_count,
  COALESCE(last.cnt, 0) AS last_count,
  ABS(COALESCE(this.cnt, 0) - COALESCE(last.cnt, 0)) AS diff
FROM this_week this
FULL OUTER JOIN last_week last
  ON this.topic_cluster = last.topic_cluster
ORDER BY diff DESC
LIMIT 50;
```

---

## C.5 Common patterns by use case

### C.5.1 "Is the deploy bad?"

```promql
# Error rate post-deploy vs pre-deploy
sum(rate(http_5xx[10m] offset 0))   / sum(rate(http_total[10m] offset 0))
  /
sum(rate(http_5xx[10m] offset 30m)) / sum(rate(http_total[10m] offset 30m))
> 2
```

### C.5.2 "What changed before the incident?"

Search:
- Recent deploys (deploy markers).
- Recent config changes (audit logs).
- Recent feature flag changes.

```sql
SELECT * FROM audit_logs
WHERE ts BETWEEN '2026-05-06 14:00' AND '2026-05-06 14:30'
  AND action.type IN ('deploy', 'config.change', 'feature_flag.toggle')
ORDER BY ts;
```

### C.5.3 "Find the slow span"

```traceql
{ resource.service.name = "checkout" && span.duration > 1s }
```

Then click the trace; look at the waterfall; identify the slow span.

### C.5.4 "Which customer is affected?"

```sql
SELECT DISTINCT attributes['customer_id_hash']
FROM logs
WHERE level = 'error'
  AND attributes['error_type'] = 'specific_error'
  AND ts > NOW() - INTERVAL '1 hour';
```

### C.5.5 "Capacity headroom on this resource"

```promql
1 - (
  sum(rate(node_cpu_seconds_total{mode!="idle"}[5m]))
  /
  count(node_cpu_seconds_total{mode="idle"})
)
```

### C.5.6 "Top retrying calls"

```promql
topk(10,
  sum by (target_service) (rate(http_client_retries_total[5m]))
)
```

### C.5.7 "Per-tenant cost"

```promql
sum by (tenant) (
  rate(otelcol_exporter_sent_metric_points_total[5m])
) * scalar(rate_per_metric_point)
```

(Cost rate hardcoded as a scalar.)

### C.5.8 "Dashboard staleness"

```promql
time() - max(prometheus_tsdb_head_max_time_seconds)
```

> 60s = the data is more than 60s behind real-time.

---

## C.6 PromQL / LogQL antipatterns to avoid

- `rate(metric[1m])` with a 15s scrape interval: the 1m window only has 4 samples; statistically noisy.
- `sum(metric{label1="X"})` without `by`: aggregates everything; loses the dimension.
- `histogram_quantile()` over a non-histogram metric: returns NaN.
- Multiple `rate()` of the same series at different windows in one query: usually the wrong approach.
- LogQL `unwrap` on a string field: returns NaN; must extract numeric first.
- TraceQL conditions on attributes that aren't indexed: full-scan; slow.

---

## C.7 The "I don't remember the exact syntax" cheat

```
PromQL:
  Filter:       metric{label="value"}
  Range:        metric{...}[5m]
  Rate:         rate(counter[5m])
  Quantile:     histogram_quantile(0.99, sum by (le) (rate(...[5m])))
  Aggregate:    sum by (label) (...)
  Top:          topk(N, ...)

LogQL:
  Filter:       {label="value"}
  Search:       |= "text"
  Parse JSON:   | json
  Filter parsed: | field > 100
  Aggregate:    sum by (label) (rate({...}[5m]))

TraceQL:
  Span attrs:   { span.http.status_code = 500 }
  Resource:     { resource.service.name = "..." }
  Duration:     { span.duration > 1s }
  Combination:  { ... && ... }
  Cross-span:   { ... } && { ... }

SQL on lakehouse:
  Time filter:  WHERE ts > NOW() - INTERVAL 'N days'
  Percentile:   PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY value)
  Map access:   attributes['key']
  Date trunc:   date_trunc('day', ts)
```

---

This recipe book is a starter pack. Extend with your team's most-used patterns. Review quarterly; delete unused; promote new patterns to the canon.
