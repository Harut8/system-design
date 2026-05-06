# Appendix A — Glossary

The full glossary of terms used across this folder. For the compact one-page version, see `doc 00 §19`.

---

## A

**Active series** — A time series currently being written to in a TSDB. The dominant cost driver in metric stores.

**Adaptive sampling** — Sampling whose rate varies with load or class (rare events kept more aggressively).

**Agent** — A process running on each host that collects telemetry from local sources and forwards it. See node-local agent.

**AIOps** — Application of ML / statistical methods to operations. Anomaly detection, alert grouping, LLM-assisted incident response.

**Alert** — A rule-evaluation event. May or may not page.

**Alertmanager** — Prometheus's purpose-built alert router. Dedup, group, route, silence.

**ANR** — Application Not Responding. Android OS-level signal that an app's main thread froze.

**APM** — Application Performance Monitoring. The category of tools (Datadog, New Relic, Dynatrace, etc.).

**Append-only** — A storage property: writes append; modification not allowed. Critical for audit logs.

**Attribute** — In OTel: a key-value pair attached to a span, log, or metric. Equivalent to "label" in Prometheus.

**Attribute registry** — Org's central catalog of attributes with classification, ownership, naming.

**Audit log** — Structured record of consequential actions, with the 5 W's. Tamper-resistant.

**Auto-instrumentation** — Telemetry generated without app code changes. eBPF, OTel auto-instr, framework hooks.

**Availability** — The fraction of time / requests a service is up. Often measured as `MTBF / (MTBF + MTTR)`.

## B

**Backfill** — Re-ingesting historical data after a recovery. Replay from durable buffer.

**Backpressure** — When downstream is slow, upstream queues fill; flow control kicks in (drop, shed, slow down).

**Baggage** — W3C-defined key-value context propagated across services (separate from trace).

**Beacon** — Browser RUM transport mechanism (sendBeacon, image pixel, etc.) that survives unload.

**Big bang migration** — Cut over all users on one day. Rare; risky.

**Blameless postmortem** — Retrospective that focuses on system contributing factors, not personal blame.

**Blast radius** — The maximum scope of impact of a failure or chaos experiment.

**Block** — Immutable on-disk unit of storage in a TSDB / log store. E.g., 2-hour Prometheus block.

**Browser RUM** — Real User Monitoring on browser pages.

**Burn rate** — Rate of error budget consumption relative to steady-state.

## C

**Canary** — Deployment to a small fraction of traffic; verified before full rollout.

**Cardinality** — Number of unique time series. Cost driver.

**Catalog** — Service catalog; the registry of services in an IDP.

**Causal inference / RCA** — Determining the root cause(s) of an incident.

**Cell architecture** — Many small isolated copies of a service serving traffic subsets. AWS, Slack, GitHub use.

**Certifications** — Compliance attestations (SOC2, ISO 27001, HIPAA-eligibility, FedRAMP).

**Chargeback** — Internal billing of observability cost to teams.

**Chaos engineering** — Discipline of fault injection to verify system resilience.

**Checkpoint** — In stream processing: persisted state for fault tolerance.

**Chunk** — Compressed run of samples within a block.

**Circuit breaker** — Pattern that stops calls to a failing dependency.

**CLS** — Cumulative Layout Shift; one of the Core Web Vitals.

**Code-first** — Configuration as code: dashboards, alerts, runbooks in Git.

**Cold storage** — Long-retention, cheap storage tier (object store, Glacier).

**Collector** — OTel Collector or equivalent; central layer that receives, processes, exports telemetry.

**Compaction** — Background merging of small storage blocks into larger ones.

**Composite SLI** — SLI combining multiple ratios with AND/OR.

**Compliance** — Regulatory regime (GDPR, HIPAA, PCI-DSS, etc.).

**Conntrack** — Kernel connection tracking table; common silent-failure source when full.

**Consumer lag** — In streaming: distance between producer offset and consumer offset.

**Containment** — Security incident response: stop the attacker before recovery.

**Context propagation** — Passing trace context across process boundaries via headers.

**Contract test** — Test that verifies a producer-consumer schema contract holds.

**Continuous profiling** — Stack-trace sampling stored over time; the 4th signal.

**Continuous verification** — Chaos engineering applied continuously, with measurable hypotheses.

**Core Web Vitals** — Google's standardized user-experience SLIs: LCP, INP, CLS.

**CPU profile** — Sampled stack traces showing where CPU time was spent.

**Crash-free sessions** — Mobile/RUM SLI: fraction of sessions without crashes.

**CRD** — Kubernetes Custom Resource Definition.

**Cross-cluster query** — Query that spans multiple regional clusters.

**Cumulative metric** — A counter; monotonically increasing.

## D

**Dashboard** — Composed view of panels for a specific question.

**Data-as-code** — Config / dashboards / alerts in Git.

**Data classification** — Tagging data by sensitivity (public, internal, confidential, regulated).

**Data plane** — The runtime layer that handles real traffic. (vs. control plane.)

**Data residency** — Legal requirement that data stay in specific geographies.

**Debug log** — Verbose log for diagnostic purposes; sampled aggressively.

**Decommissioning** — Final step of vendor migration: turning off the old vendor.

**Deny-list / Allow-list** — Approaches to attribute filtering. Allow-list is safer.

**Dependency graph** — Service-to-service map.

**Deployment marker** — Annotation on dashboards showing when a deploy happened.

**Detection engineering** — Security practice of writing and testing detection rules.

**Dimension** — A label or attribute in metrics.

**Disaster recovery (DR)** — Recovery from catastrophic failure.

**Distributed tracing** — Capturing a request's path across services as a tree of spans.

**DLQ (Dead Letter Queue)** — Queue for messages that failed processing.

**Downsampling** — Reducing sample resolution; one-way information loss.

**dSYM** — iOS debug-symbol files, required for crash symbolication.

**Dual-write** — Migration pattern: write to both old and new vendor during transition.

## E

**eBPF** — Extended Berkeley Packet Filter; kernel-level programmable observability.

**Egress** — Outbound traffic from a network.

**Embedding drift** — In ML / RAG: change in vector representations over time.

**Encryption at rest** — Encrypting stored data.

**Encryption in flight** — Encrypting data in transit (TLS).

**Envoy** — High-performance proxy used in service meshes (Istio's data plane).

**Error budget** — Allowed bad events in a window: `(1 − SLO) × total events`.

**Error budget policy** — Documented response to budget burn (freeze, etc.).

**Eval harness** — Test suite for LLM / ML quality.

**Event-based SLI** — SLI computed as good_events / total_events.

**Eventual consistency** — Consistency model where replicas converge over time.

**Exemplar** — Pointer from a histogram bucket to a specific trace_id.

**Exception (PRR)** — Documented waiver of a PRR item.

**Exception (programming)** — Runtime error caught by error tracker.

**Exporter** — Component that sends telemetry to a backend.

**Extract / inject** — OTel SDK methods for context propagation.

## F

**Fail-back** — Reverting from DR back to primary after recovery.

**Failure domain** — Scope of correlated failures (zone, region, etc.).

**Fast burn** — Burn-rate alert for rapid budget consumption.

**FCP** — First Contentful Paint; Web Vital.

**Federation** — Aggregating queries across multiple stores or clusters.

**Field-level access control** — RBAC at the column / field level (Splunk, Snowflake).

**FinOps** — Financial operations: cost management for engineering.

**Fingerprint** — In error tracking: hash of a normalized stack trace for grouping.

**Five whys** — Toyota technique; replaced by contributing factors in modern postmortems.

**Flaky test** — Test that intermittently fails for non-product reasons.

**Flow log** — Network-flow record (NetFlow, sFlow, VPC flow logs).

**Forecast envelope** — Range estimate (best / expected / stretch / black-swan).

**Forensic** — Backwards-looking analysis (compliance, security).

**Forward index** — Mapping from series ID to samples.

**Freshness SLI** — Fraction of events queryable within an age threshold.

**Function-as-a-service** — Lambda / Cloud Functions; ephemeral compute.

## G

**Game day** — Scheduled chaos exercise where the team responds to injected failures.

**Gauge** — Instantaneous metric value (can go up or down).

**GDPR** — EU data protection regulation.

**Gen-AI** — Generative AI; LLMs and image / video generation.

**Glue work** — Coordination / mentorship / review work; under-credited.

**Golden path** — Paved-road default for new services; templates with observability built in.

**Golden signals** — Latency, traffic, errors, saturation.

**Grafana** — Open-source dashboard platform.

**Grafana Faro** — Open-source browser RUM.

**Grafana Loki** — Open-source log store with label-index architecture.

**Grafana Mimir** — Open-source TSDB; Cortex fork.

**Grafana Tempo** — Open-source trace store; object-store-backed.

**Grouping (Alertmanager)** — Collapsing related alerts into one notification.

**Grouping (errors)** — Fingerprint-based clustering of error events.

## H

**Handoff** — End-of-shift transition document for on-call.

**Headroom** — `(capacity − usage) / capacity`. Forward-looking capacity metric.

**Head sampling** — Sampling decision at SDK before propagation.

**HIPAA** — US healthcare data regulation.

**Histogram** — Bucketed counts of values; percentiles computable at query time.

**Holt-Winters** — Forecasting method for seasonal data.

**Hot storage** — Fast, recent-data tier.

**Hub-and-spoke** — Federation architecture: central hub queries autonomous spokes.

**Hybrid** — Multi-vendor or build+buy combination.

**Hypothesis (chaos)** — "When X happens, steady state is preserved within Y."

## I

**Iceberg** — Apache Iceberg; table format for lakehouse.

**IC (Incident Commander)** — Single human running an incident response.

**IDP** — Internal Developer Platform.

**Immutable** — Write-once; cannot be modified.

**Independent path** — Observation path that doesn't depend on the main pipeline.

**Inflection point** — Scale at which build becomes cheaper than buy.

**Inhibition** — Suppressing alerts when a related alert is firing.

**Ingester** — Component that accepts incoming telemetry into storage.

**Integration tests** — Tests against real (not mocked) dependencies.

**Internal synthetic** — Synthetic checks running from inside the network.

**INP** — Interaction to Next Paint; Web Vital.

**Inverted index** — Mapping from term/label to set of records.

**Iceberg / Delta / Hudi** — Lakehouse table formats.

## J

**Jaeger** — Open-source distributed tracing system.

**Journey** — User-facing flow that crosses multiple services.

**Just-in-time provisioning** — Dynamic capacity scaling.

## K

**Kafka** — Distributed event streaming platform.

**Kanban** — Workflow visualization.

**Kill switch** — Mechanism to halt a chaos experiment / disable a feature.

**Kiali** — Istio's service-graph dashboard.

## L

**Lakehouse** — Data architecture combining lake's storage with warehouse's metadata.

**Label** — Key-value pair attached to a metric in Prometheus.

**Lag** — Distance between produced and consumed offsets in streaming.

**Latency** — Time from request to response.

**LCP** — Largest Contentful Paint; Web Vital.

**Lifecycle policy** — Storage policy that moves data between tiers automatically.

**Linkerd** — Lightweight service mesh.

**Little's Law** — `L = λ × W`. Concurrent items = arrival rate × time in system.

**LLM** — Large Language Model.

**Loadshedding** — Controlled dropping of less-important load.

**Locality routing** — Routing requests to same-zone destinations.

**LogQL** — Loki's query language.

**Long-window** — In multi-window alerting: the longer (e.g., 1h, 6h) window.

**Loss budget** — Acceptable telemetry loss percentage.

## M

**Mean** — Arithmetic average. Lies about latency tails.

**Median** — 50th percentile. Honest about the typical case.

**Memberlist** — Gossip protocol library used by Alertmanager etc.

**Mesh** — Service mesh.

**Metric** — A named time series.

**Metric storm** — Sudden large emission of metrics.

**Migration** — Moving from one vendor / stack to another.

**MITRE ATT&CK** — Taxonomy of attacker tactics and techniques.

**Mitigation** — Stopping the bleed during an incident.

**ML drift** — Model performance degradation over time.

**Model router** — Routes LLM requests to cheap-or-expensive models.

**MTBF** — Mean Time Between Failures.

**MTTA** — Mean Time To Acknowledge.

**MTTD** — Mean Time To Detect.

**MTTM** — Mean Time To Mitigate.

**MTTR** — Mean Time To Recover / Resolve.

**Multi-tenant** — Stack serving multiple isolated tenants.

**Multi-window multi-burn-rate** — Alert pattern using two windows + burn-rate threshold.

## N

**Native histogram** — Prometheus 2.40+ sparse exponential histogram.

**Near-miss** — Event that could have caused impact but didn't.

**NetFlow** — Switch-emitted flow records.

**New Relic** — APM vendor.

**Nines** — Shorthand for SLO percentage (3 nines = 99.9%).

**Node** — A host (physical or virtual).

**Node-local agent** — Per-host telemetry agent.

**Noisy neighbor** — Tenant whose load affects others.

## O

**Object store** — S3, GCS, Azure Blob: durable, cheap, slow storage.

**Observability** — Property that internal state can be inferred from external outputs.

**OCSF** — Open Cybersecurity Schema Framework.

**Off-heap memory** — Non-JVM-heap memory in Java apps.

**On-call** — Rotation of engineers responsible for paging response.

**Open-loop** — Load-test pattern: traffic at fixed RPS regardless of latency.

**OpenMetrics** — CNCF standardization of Prometheus exposition format.

**OpenSLO** — Open-source spec for SLO YAML.

**OpenTelemetry / OTel** — CNCF project for vendor-neutral instrumentation.

**OTLP** — OTel's wire protocol.

**Outage** — Complete unavailability subset of incidents.

## P

**Page** — Automated, urgent, off-hours-capable escalation.

**Page hygiene** — Discipline of maintaining alert quality.

**Paged event** — An alert that woke someone.

**Parquet** — Columnar file format.

**Partition** — Subset of data with a shared key (Kafka, Iceberg).

**Partition skew** — Uneven distribution across partitions.

**Pause** — Delay during stop-the-world events (GC, etc.).

**Percentile** — Value below which X% of samples fall.

**PII** — Personally Identifiable Information.

**Pipeline reliability** — Reliability of the telemetry pipeline itself.

**Pixie** — eBPF-based auto-instrumentation tool.

**Plan capture** — Saving database query execution plans.

**Postmortem** — Blameless retrospective document.

**Postings list** — Inverted index: label-value → set of series IDs.

**ppof** — Go's profiling format.

**Pre-aggregation** — Computing aggregates at write time.

**PRR (Production Readiness Review)** — Pre-launch checklist for service quality.

**Probabilistic data structures** — HyperLogLog, Count-Min Sketch.

**Profile** — Weighted set of stack traces; the 4th observability signal.

**Prometheus** — Open-source TSDB and monitoring system.

**PromQL** — Prometheus's query language.

**Pseudonymization** — Replacing direct identifiers with reversible tokens.

**Pull** — Server-initiated metric scrape.

**Push** — Client-initiated metric/log/trace export.

## Q

**QoS** — Quality of Service.

**Quantile** — Synonym for percentile.

**Query frontend** — Component that splits, caches, schedules queries.

**Quota** — Per-tenant cap on resource use.

## R

**RAG** — Retrieval-Augmented Generation. LLM pattern using retrieved context.

**Rate limit** — Request-per-time-unit cap.

**RCA** — Root Cause Analysis.

**RED** — Rate, Errors, Duration. Per-service signals.

**Regression** — A change that worsens performance / quality.

**Release** — A specific deployed version of code.

**Release health** — Per-release reliability metrics.

**Reliability backlog** — Engineering work prioritized for reliability improvement.

**Remote_write** — Prometheus's outbound push protocol.

**Replication** — Multiple copies of data for redundancy.

**Reservation** — Pre-purchased compute commitment for discount.

**Reservoir sampling** — Statistical sampling of N items from a stream.

**Resilience** — System's ability to absorb perturbations.

**Retention** — How long data is stored.

**Retransmit** — TCP packet sent again because no ACK received.

**RPO** — Recovery Point Objective. Maximum data loss.

**RTO** — Recovery Time Objective. Maximum recovery time.

**RTT** — Round-trip time.

**Rollback** — Reverting a deploy to a previous version.

**Rollup** — Pre-aggregated metric.

**RUM** — Real User Monitoring.

**Runbook** — Versioned, linked, step-by-step incident-response procedure.

## S

**Sampling** — Keeping a subset of telemetry to manage cost.

**Saturation** — How "full" a resource is; queueing past capacity.

**Schema-on-read** — Schema applied at query time.

**Schema-on-write** — Schema applied at write time.

**Schema registry** — Catalog of schemas with compatibility checks.

**Scrape** — One Prometheus scrape: HTTP GET against `/metrics`.

**Scribe** — Incident-response role: writes the timeline.

**SDK** — Software Development Kit.

**Sentry** — Error tracking platform.

**Series** — One metric's unique label-set combination.

**Service mesh** — Layer of proxies between services (Istio, Linkerd, Cilium).

**SEV** — Severity tier (SEV-1, SEV-2, etc.).

**Shadow mode** — Running a system in parallel without operational dependency.

**Shed-load policy** — Documented loss-budget by signal type.

**Showback** — Cost visibility without billing.

**Sidecar** — A proxy container injected into every pod.

**Sigma** — Open-source detection-rule format.

**Silence** — Time-bounded suppression of alerts.

**SLA** — Service Level Agreement (external promise).

**SLI** — Service Level Indicator (a measurement).

**SLO** — Service Level Objective (internal target).

**Slow burn** — Burn-rate alert for gradual budget consumption.

**Smoothed RTT** — TCP's running estimate of RTT.

**SOC2** — US service-organization compliance attestation.

**SOC** — Security Operations Center.

**Source map** — Mapping from minified code back to source.

**Span** — One operation in a trace; has name, kind, duration, attributes.

**Span attribute** — Key-value pair on a span.

**Splunk** — Logs / SIEM platform.

**Spot** — Pre-emptible cloud compute at discounted rate.

**Standard Contractual Clauses** — Legal mechanism for cross-border data transfer.

**Steady state** — Normal system behavior used as chaos baseline.

**Stickify** — Make sampling consistent per user/session.

**STL decomposition** — Time-series decomposition: trend + season + residual.

**Streaming** — Asynchronous event-based architecture.

**Strangler pattern** — Gradual migration via dual-write.

**Stress test** — Load test pushing past capacity.

**Symbol file** — Debug-symbol file (dSYM, ProGuard map, .pdb).

**Symbolication** — Resolving stack-trace addresses to function names.

**Synthetic** — Manufactured traffic for testing.

**System Tap** — eBPF-style kernel instrumentation (older).

## T

**Tail-at-scale** — Jeff Dean's 2013 paper on tail latency.

**Tail sampling** — Sampling decision at gateway after trace assembly.

**Tamper-evident** — Modifications detectable.

**TCO** — Total Cost of Ownership.

**TechDocs** — Backstage's documentation framework.

**Telemetry** — Raw observability data.

**Tempo** — Grafana's trace store.

**Tenant** — Isolated unit in a multi-tenant system.

**Tenant ID** — Identifier for a tenant.

**Threshold alert** — Static-threshold-based alert. Largely replaced by burn-rate.

**Tier** — Hierarchical service classification (tier-1, tier-2).

**Tiered storage** — Hot/warm/cold/archive tiers with different prices.

**Time-series database (TSDB)** — Database optimized for time-series.

**TLS** — Transport Layer Security.

**Toil** — Manual, repetitive, automatable, scaling work.

**Tokens (LLM)** — Units of text in LLM input/output.

**Top-K** — Sketch keeping the K most-frequent items.

**Trace** — DAG of spans across services.

**Trace context** — W3C `traceparent` and `tracestate` headers.

**Trace ID** — Unique identifier for a trace.

**Trace sampling** — Keeping a subset of traces.

**Traceparent** — W3C trace-context header.

**TraceQL** — Tempo's query language.

**Traffic-weighted** — Aggregating with weight by traffic share.

**Transactional** — ACID-compliant.

**TTFT** — Time To First Token (LLM streaming).

## U

**Upgrade** — Software-version transition.

**USE** — Utilization, Saturation, Errors. Per-resource signals.

**Utilization** — Fraction of time a resource is busy.

## V

**Vendor lock-in** — Dependence on a vendor that's hard to leave.

**Vendor migration** — Moving from one vendor to another.

**VictoriaMetrics** — Open-source TSDB (alternative to Mimir).

**Visual regression** — Detecting UI rendering changes.

**vLLM** — High-performance LLM inference engine.

**VPC flow logs** — Cloud equivalent of NetFlow.

## W

**WAL** — Write-Ahead Log. Durability layer in TSDBs.

**WAF** — Web Application Firewall.

**Watermark** — Stream-processing concept: "we've seen all events up to time T."

**Web Vitals** — Google's user-experience SLIs.

**Window function** — PromQL function over a time window.

**Workflow** — Multi-step process (CI/CD, deploy, etc.).

## X

**X-Forwarded-For** — HTTP header for original client IP.

## Y

**YAML** — Config format used widely in observability tooling.

## Z

**Zero-retention** — Vendor mode where no data is stored after the request.

**Zone** — Availability zone.

---

This glossary is the working vocabulary for the folder. For a compact reference, see `doc 00 §19`.
