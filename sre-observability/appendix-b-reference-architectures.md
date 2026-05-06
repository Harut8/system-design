# Appendix B — Reference Architectures

Three reference architectures for different scales: small (10-50 services), mid (50-500), hyperscale (500+). Use as starting points; adapt to the specifics in the rest of the folder.

---

## B.1 Small (10-50 services)

The startup / small-team architecture.

### B.1.1 Stack

```
┌────────────────────────────────────────────────────────────────┐
│  Apps (instrumented with OTel SDK)                             │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────┐
│  Vendor SaaS (Datadog / Honeycomb / New Relic)                │
│   - Metrics, logs, traces, errors all in one                   │
│   - APM auto-instrumentation                                   │
│   - Synthetic monitoring                                       │
│   - Alerts via Datadog Monitors / equivalent                   │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────┐
│  PagerDuty / Opsgenie  ── notifications                        │
└────────────────────────────────────────────────────────────────┘
```

### B.1.2 Choices

- **Vendor:** Datadog (most full-featured), Honeycomb (best for trace-heavy), Sentry (good for error-focused; less for metrics).
- **No self-hosted infrastructure** — engineering effort spent on product.
- **Pre-built dashboards** from vendor.
- **Alerts in vendor's language** (Datadog Monitors, etc.).

### B.1.3 SLO discipline

- 1-2 SLOs per critical user journey.
- Multi-window multi-burn-rate via vendor's SLO feature (if supported) or recording rules.
- Quarterly review.

### B.1.4 On-call

- 1 rotation (primary + secondary if 6+ engineers; else only primary).
- Compensation: stipend or comp time.
- Game days quarterly.

### B.1.5 Cost

| Item | Annual |
|---|---|
| Datadog (10-50 services) | $50K-$300K |
| PagerDuty | $5K-$30K |
| Total | $55K-$330K |

### B.1.6 Phase the rollout

| Phase | Goal | Timeline |
|---|---|---|
| 1 | Vendor signup + OTel SDK on top services | Week 1-2 |
| 2 | RED dashboards + first SLO | Month 1 |
| 3 | On-call + runbooks for top alerts | Month 2 |
| 4 | RUM + synthetic | Month 3 |
| 5 | Quarterly hygiene cycle | Month 4+ |

### B.1.7 What to skip (for now)

- Self-hosted anything.
- Lakehouse.
- Multi-region observability (unless multi-region service).
- Continuous profiling (until a perf problem demands it).
- AIOps.
- Full FinOps practice.

### B.1.8 The exit ramp

When approaching $1M/year vendor spend or 100+ services, revisit the build-vs-buy decision (`doc 39`). Probable next step: hybrid or self-hosted.

---

## B.2 Mid (50-500 services)

The growing-org architecture.

### B.2.1 Stack

```
┌────────────────────────────────────────────────────────────────┐
│  Apps (OTel SDK; org-wide standard; semantic conventions)     │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────┐
│  OTel Collector (DaemonSet agent + gateway Deployment)        │
│   - Auth, redaction, tail sampling, fan-out                   │
└────┬────────────────┬───────────────┬─────────────────────────┘
     │                │               │
     ▼                ▼               ▼
┌─────────┐    ┌─────────────┐    ┌──────────┐
│ Mimir   │    │ Loki        │    │ Tempo    │
│ (metrics│    │ (logs)      │    │ (traces) │
│ + Thanos│    │             │    │          │
│ for LTS)│    │             │    │          │
└────┬────┘    └──────┬──────┘    └────┬─────┘
     │                │                 │
     └────────────────┴─────────────────┘
                       │
                       ▼
              ┌──────────────────┐
              │  Grafana         │
              │  + Alertmanager  │
              └──────────────────┘
                       │
                       ▼
              ┌──────────────────┐
              │ PagerDuty        │
              │ + Backstage      │
              │ + Sentry (errors)│
              │ + Datadog RUM    │
              │ + Pyroscope      │
              └──────────────────┘
```

### B.2.2 Choices

- **Self-hosted Grafana stack** for metrics, logs, traces.
- **Vendor for specialty** (RUM, errors).
- **Sloth / Pyrra** for SLO compilation.
- **Backstage** for IDP.
- **Per-tenant isolation** (`doc 19`).
- **Cardinality budget per service** (`doc 18`).

### B.2.3 SLO discipline

- Multi-team SLO repo (OpenSLO).
- Multi-window multi-burn-rate alerts auto-generated.
- Per-team error-budget policies.
- Quarterly SLO review.
- Reliability backlog.

### B.2.4 On-call

- Per-team rotations (primary + secondary).
- 6+ engineers per primary rotation.
- Compensation defined.
- Quarterly retros.
- Onboarding pipeline (shadow → secondary → primary).

### B.2.5 Telemetry pipeline reliability

- Independent observation path with tier-0 alerts.
- Synthetic canary for end-to-end freshness.
- Game days quarterly.
- Multi-region active-active for high-tier services.

### B.2.6 FinOps

- Per-team cost attribution.
- Showback dashboards.
- Quarterly hygiene cycle.
- Annual budget review.

### B.2.7 Cost

| Item | Annual |
|---|---|
| Self-hosted compute + storage | $200K-$800K |
| Platform team (1.5-3 engineers) | $400K-$1M |
| Vendor (RUM, errors, profiling) | $100K-$300K |
| PagerDuty / Backstage | $50K-$150K |
| Total | $750K-$2.25M |

Comparable to a mid-tier SaaS-only deployment but with full control.

### B.2.8 Migration path from small

If migrating from B.1:
1. Add OTel collector layer (vendor + self-hosted simultaneously).
2. Build self-hosted Mimir + Loki + Tempo.
3. Dual-write phase (3-6 months).
4. Migrate dashboards / alerts to Grafana.
5. Cutover writes.
6. Decommission vendor (after retention period).

Total: 9-18 months.

---

## B.3 Hyperscale (500+ services)

The platform-as-product architecture.

### B.3.1 Stack

```
┌────────────────────────────────────────────────────────────────┐
│  Apps (OTel; org-wide standard; per-team custom conventions)   │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────┐
│  OTel Collector (multi-region; auth; redaction; tenancy)      │
└────┬───────────────────────────────┬───────────────────────────┘
     │                               │
     ▼                               ▼
┌──────────┐                   ┌──────────────┐
│  Kafka   │                   │ Cold tee →   │
│  (durable│                   │ Iceberg/S3 → │
│  buffer) │                   │ ClickHouse / │
└────┬─────┘                   │ BigQuery     │
     │                         │ (lakehouse)  │
     ▼                         └──────────────┘
┌──────────┐                          │
│ Mimir    │                          │
│ Loki     │  ←── multi-tenant,       │
│ Tempo    │      multi-region        │
│ Pyroscope│      active-active       │
└────┬─────┘                          │
     │                                │
     └─────────┬──────────────────────┘
               │
               ▼
        ┌─────────────────┐
        │ Grafana (multi- │
        │ tenant; tiered) │
        └────────┬────────┘
                 │
                 ▼
        ┌─────────────────┐
        │ Alert routing,  │
        │ paging, IDP,    │
        │ scorecards      │
        └─────────────────┘
```

### B.3.2 Choices

- **Multi-region active-active** for hot stack.
- **Lakehouse** for cold + analytical (Iceberg + ClickHouse / BigQuery / Snowflake).
- **Per-tenant tier system** with quotas + pricing.
- **OpenSLO** + per-team SLOs (1000+ SLOs total).
- **Custom Backstage extensions** for org-specific scorecards.
- **Compliance regime support** (SOC2, HIPAA, GDPR, FedRAMP — possibly multiple).
- **Service mesh** with full observability integration.
- **Continuous chaos** in production.
- **AIOps for alert grouping** (large alert volumes).

### B.3.3 SLO discipline

- Per-team SLO ownership; platform team provides infrastructure.
- Journey-level SLOs at the org level.
- Multi-window multi-burn-rate auto-generated.
- Error-budget policy signed at multiple levels.
- Quarterly journey-SLO reviews; annual org-wide.

### B.3.4 On-call

- Multiple rotations per BU.
- 24/7 follow-the-sun where possible.
- Platform-team has its own on-call (own SLOs).
- Game days monthly; full disasters annually.

### B.3.5 FinOps

- Chargeback (full team accountability).
- Annual budget cycle with finance.
- Per-tenant pricing tiers.
- Continuous cost monitoring with alerts.
- Vendor relationships with multi-year contracts.

### B.3.6 Cost

| Item | Annual |
|---|---|
| Self-hosted compute + storage | $5M-$20M |
| Platform team (10-30 engineers) | $5M-$15M |
| Vendor (specialty) | $1M-$5M |
| Cloud egress / cross-region | $1M-$5M |
| Total | $12M-$45M |

Significant; but at this scale, observability is strategic.

### B.3.7 The maturity expectations

- All chapters of this folder fully implemented.
- Continuous improvement.
- Observability product team treats users (other engineers) as customers.
- Annual platform-team strategy review.

### B.3.8 The "why hyperscale" investment

For 500+ services and $10M+/year of vendor spend, hyperscale architecture is cheaper than vendor. For < 500 services, the engineer cost of hyperscale doesn't justify.

---

## B.4 Decision matrix

When picking the reference architecture:

| Org size | Engineer count | Vendor spend | Pick |
|---|---|---|---|
| < 50 services | < 100 | < $300K | Small (B.1) |
| 50-500 services | 100-1000 | $300K-$2M | Mid (B.2) |
| 500+ services | 1000+ | $2M+ | Hyperscale (B.3) |

Multi-cloud, regulated industries, or specific compliance requirements may push toward hyperscale at smaller scales.

The exact transition is gradual. A "size 80 services" org isn't strictly small or mid; it's evolving. Plan for the mid architecture as you grow.

---

## B.5 The "what if" scenarios

### B.5.1 What if we're regulated (HIPAA, FedRAMP)?

- Self-hosted earlier (B.2 from the start).
- BAAs with vendors.
- Audit logs separate; longer retention.
- Per-tenant isolation strict.
- Compliance cycle integrated.

### B.5.2 What if we're multi-cloud?

- OTel essential.
- Per-cloud deployment of Mimir / Loki / Tempo.
- Cross-cloud federation via Grafana.
- Cost monitoring per cloud.

### B.5.3 What if we're an observability vendor?

- Eat your own dog food.
- Customer-facing observability built on internal observability.
- Strategic value of the platform.
- Custom development at scale.

### B.5.4 What if we have an LLM workload?

- LLM-specific instrumentation (OTel GenAI).
- Token accounting and cost attribution.
- Eval harness as CI dependency.
- Vector-DB observability.
- See `doc 26`.

### B.5.5 What if we're going through an acquisition?

- Brownfield integration patterns (`doc 41`).
- Listen first; consolidate later.
- Federation as intermediate.

---

These reference architectures are starting points. The right architecture for your team is some hybrid; iterate.
