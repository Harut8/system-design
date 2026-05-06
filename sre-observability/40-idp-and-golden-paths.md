# 40 — IDP and Golden Paths

> The platform team's lever for raising the floor across the org isn't documentation; it's a *paved road*. Service templates with observability built in. A catalog showing readiness scores. A scorecard that surfaces drift. The Internal Developer Platform (IDP) — Backstage and its peers — is the substrate; observability is one of the most important "tracks" running on top of it.

This chapter is about the integration of observability into the IDP — how Backstage / port.io / cortex.io etc. consume observability data and produce paved roads.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [What an IDP is](#2-what-idp)
3. [The "golden path" pattern](#3-golden-path)
4. [Service catalog: the substrate](#4-catalog)
5. [Service scorecards](#5-scorecards)
6. [Integrating observability into the IDP](#6-integration)
7. [Templates with observability built in](#7-templates)
8. [The TechDocs / runbook integration](#8-techdocs)
9. [The on-call / paging integration](#9-on-call)
10. [The IDP as PRR engine](#10-prr-engine)
11. [Tools (2026 landscape)](#11-tools)
12. [Anti-patterns](#12-anti-patterns)
13. [Worked example: a Backstage observability rollout](#13-worked-example)
14. [Pitfalls](#14-pitfalls)
15. [Mental models](#15-mental-models)

---

## 1. Thesis

Three claims:

1. **Documentation doesn't scale; templates do.** Telling engineers "remember to add an SLO" produces 50% compliance. Generating the SLO at service-creation time produces 100%.
2. **The IDP is the platform team's UX.** Service teams don't read observability docs; they navigate the catalog, copy a template, follow the scorecard. Make those great.
3. **Observability data flows *into* the IDP.** SLO compliance, error rates, on-call ownership, capacity headroom — surfaced in the catalog. Engineers see their service's health alongside its docs.

If your team has Backstage but no observability integration, you're missing the easiest force-multiplier the platform team has. This chapter is the right shape.

---

## 2. What an IDP Is

The internal developer platform.

### 2.1 The components

- **Service catalog:** registry of services, ownership, dependencies.
- **Templates / scaffolds:** "create new service" pre-baked.
- **Scorecards / maturity:** quality dimensions with scores.
- **Documentation:** TechDocs, runbooks, decisions.
- **Integration with operational tools:** observability, CI/CD, cloud, SCM.
- **APIs / plugins:** extensible.

### 2.2 The 2026 landscape

| Tool | Strength |
|---|---|
| **Backstage** (Spotify) | Open-source; extensible; widest adoption |
| **port.io** | Commercial; rich UX; opinionated |
| **cortex.io** | Commercial; scorecard-strong; observability-integrated |
| **Atlassian Compass** | Atlassian-shop integration |
| **OpsLevel** | Service-quality-focused |
| **roadie.io** | Backstage-as-a-service |

Backstage dominates open-source; commercial tools serve the "we don't want to build it" market.

### 2.3 The platform-team value

- One place to find everything about a service.
- Onboarding accelerator.
- Maturity / quality visibility.
- Standards enforcement (templates).
- Self-service tooling.

The IDP is *the* place service teams interact with platform capabilities.

---

## 3. The "Golden Path" Pattern

The paved road.

### 3.1 What it is

A documented, opinionated, supported way to build a service. With:
- Pre-built service templates.
- Pre-configured CI/CD.
- Pre-wired observability.
- Pre-defined SLO templates.
- Pre-built dashboards.
- Pre-configured alerts.

A team using the golden path gets all of this for free at service creation.

### 3.2 Why it matters

Without golden paths:
- Each service team reinvents.
- Quality varies wildly.
- PRRs fail because services missed something.
- Migrations are 50× harder.

With golden paths:
- Quality is consistent.
- PRRs pass at first attempt.
- Migrations affect templates, propagate.
- Service teams focus on business logic.

### 3.3 The "deviation tax"

Teams may deviate from the golden path for legitimate reasons:
- Special compliance requirement.
- Specific framework choice.
- Specific scale need.

Make deviation visible (in the catalog); document; require justification. Don't forbid; don't make implicit.

### 3.4 The "one path, well-supported" principle

Not 5 golden paths (Java + Go + Python + Node + Rust). One per language, max. Otherwise the platform team supports many; quality drops.

If 5 languages are real, 5 golden paths are required. But each must be a real first-class citizen.

---

## 4. Service Catalog: The Substrate

The registry.

### 4.1 The shape

A YAML-or-API registry of every service:

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: checkout-svc
  description: Handles checkout flow
  tags: [tier-1, payments]
  annotations:
    pagerduty.com/integration-key: <key>
    grafana.com/dashboard-url: <url>
    github.com/project-slug: org/checkout-svc
spec:
  type: service
  lifecycle: production
  owner: payments-team
  dependsOn:
    - resource:postgres-checkout
    - component:auth-svc
    - component:pricing-svc
```

### 4.2 The annotations

The catalog connects to other systems via *annotations*:

```yaml
annotations:
  grafana.com/dashboard-uid: checkout-svc-red
  prometheus.io/alerting-rules: payments/checkout-alerts
  pagerduty.com/escalation-policy: payments-on-call
  github.com/project-slug: org/checkout-svc
  backstage.io/techdocs-ref: dir:.
```

Annotations are how observability data appears in the catalog page.

### 4.3 The auto-discovery

Manual catalog entry is tedious. Automate:
- Discover services from k8s (annotations).
- Discover from cloud (tagged resources).
- Discover from CI/CD (pipelines).

Backstage has discovery providers for k8s, AWS, GitHub, etc. Configure; auto-populate.

### 4.4 The owner authority

Every catalog entry has an owner. The owner is responsible:
- For the service operationally.
- For its quality scores.
- For its on-call.
- For its docs.

Without ownership, services rot. The catalog enforces ownership presence.

---

## 5. Service Scorecards

The maturity dimension.

### 5.1 The pattern

Each service is scored across dimensions:
- Observability completeness.
- Documentation completeness.
- Test coverage.
- Security posture.
- Reliability metrics (SLO compliance).
- Production readiness (`doc 17`).

### 5.2 The score

```
checkout-svc — Score: 87/100

Observability:    92/100  ✓ SLO defined  ✓ Alerts  ✓ Dashboards  ⚠ Profiling not enabled
Documentation:    85/100  ✓ README  ✓ Runbooks  ⚠ Architecture diagram outdated
Reliability:      95/100  ✓ Error budget healthy  ⚠ Cross-region missing
Security:        100/100  ✓ All checks pass
Test coverage:    72/100  ⚠ Below 80% target

Last review: 2026-04-15
Next review: 2026-07-15
```

### 5.3 The mechanism

Scorecards pull data from sources:
- Observability platform: SLO definitions exist? Alerts have runbooks? Dashboards in catalog?
- Git: README exists? Architecture diagram updated recently?
- CI: test coverage reports?
- Security tools: vulnerability scan results?

Score is auto-computed; teams see in the IDP UI.

### 5.4 The cross-team scoreboard

Aggregate view: which teams have low scores? Where are gaps?

```
Team             Avg score   #services
identity         92          12
payments         88          8
search           85          15
data-pipeline    72          5      ← needs attention
```

Visibility creates pressure. Teams compete.

### 5.5 The score as PRR continuation

(Cross-link to `doc 17 §8`.) The PRR scorecard *is* the IDP scorecard. Continuous; living; visible.

---

## 6. Integrating Observability Into the IDP

The data flow.

### 6.1 What the IDP shows

Per service:
- Current SLO compliance (live).
- Error budget remaining.
- Recent alerts.
- Recent incidents.
- Linked dashboards.
- On-call current/upcoming rotation.
- Trace samples / recent slow requests.

All embedded in the catalog page; no jumping between tabs.

### 6.2 The integration mechanics

Backstage (and others) plugin architecture:
- Grafana plugin: embeds dashboards.
- PagerDuty plugin: shows current on-call.
- Sentry plugin: shows recent errors.
- Custom plugin: shows SLO compliance from your stack.

Each plugin queries the source (Grafana, PagerDuty, Sentry, etc.) via API.

### 6.3 The performance dimension

A catalog page that takes 30 seconds to load is unused. Performance:
- Cache plugin responses.
- Async loading (skeleton UI; data populates).
- Per-plugin SLO (e.g., < 2s render).

### 6.4 The auth dimension

Plugins need credentials to query downstream. Use service-account tokens; respect RBAC; audit.

### 6.5 The "single pane" promise

The IDP's value is concentration. Engineers don't tab-juggle 7 tools to debug. They land on the service page; everything is there.

Match this promise. Otherwise the IDP is just one more tab.

---

## 7. Templates With Observability Built In

The golden path mechanism.

### 7.1 The template includes

Software template for "new HTTP service in Go":

```
new-service/
  src/                       # bootstrap code
  Dockerfile
  helm/                      # k8s manifests
    deployment.yaml
    service.yaml
    servicemonitor.yaml      # ← Prometheus scrape config
  observability/             # ← here
    slo.yaml                 # ← OpenSLO file
    alerts.yaml              # ← generated rules
    dashboard.json           # ← Grafana dashboard
    runbook.md               # ← skeleton runbook
  catalog-info.yaml          # ← IDP catalog entry
  README.md
```

Service team runs `backstage create new-service`; everything is created.

### 7.2 The SLO template

```yaml
# observability/slo.yaml
apiVersion: openslo/v1
kind: SLO
metadata:
  name: ${{values.serviceName}}-availability
spec:
  service: ${{values.serviceName}}
  indicator:
    spec:
      ratioMetric:
        good:
          metricSource:
            type: Prometheus
            spec:
              query: 'sum(rate(http_requests_total{service="${{values.serviceName}}", code!~"5.."}[5m]))'
        total:
          metricSource:
            type: Prometheus
            spec:
              query: 'sum(rate(http_requests_total{service="${{values.serviceName}}"}[5m]))'
  timeWindow:
    - duration: 28d
      isRolling: true
  objectives:
    - target: 0.999
```

Variable substitution at template instantiation. Service has an SLO at minute 1.

### 7.3 The dashboard template

A Grafana dashboard JSON parameterized on service name. Imports automatically; viewable in IDP.

### 7.4 The runbook skeleton

```markdown
# ${{values.serviceName}} Runbook

## Service overview
${{values.description}}

## Owner
${{values.owner}}

## Common alerts

### ${{values.serviceName}}AvailabilityFastBurn
**What this means:** [TODO: fill in by team]
**Immediate action:** [TODO]
**Escalation:** [TODO]

[continued template]
```

Service team fills in the TODOs as part of onboarding. Without the skeleton, they'd skip.

### 7.5 The CI / deploy

Templates include CI workflows that:
- Lint observability config.
- Validate SLO YAML.
- Verify dashboard JSON.
- Run synthetic post-deploy.

The PRR (`doc 17`) is partly automated by the templates.

---

## 8. The TechDocs / Runbook Integration

Documentation as code.

### 8.1 TechDocs

Backstage's documentation framework: docs in Markdown in the service repo; rendered in the catalog.

### 8.2 The runbook integration

Runbooks live in the repo (cross-link `doc 14 §7`). TechDocs renders them in the catalog page.

When an alert fires, the runbook URL points to the TechDocs page. Click → docs render → ops follows.

### 8.3 The architecture-diagram integration

Diagrams as code (Mermaid, PlantUML) in the repo; rendered.

Architecture changes go through PR; the IDP shows current diagram. No drift.

### 8.4 The decision log

ADRs (Architecture Decision Records) in the repo; rendered in TechDocs.

Engineers see "why is this designed this way" alongside "how is it currently performing." Powerful for new team members.

---

## 9. The On-Call / Paging Integration

The operational layer.

### 9.1 What the catalog shows

- Current on-call (primary, secondary).
- Schedule for the next week.
- Recent pages (for this service).
- Average MTTA.

### 9.2 The PagerDuty / Opsgenie integration

Plugin queries the on-call API. Displays in the catalog.

Engineers know who to contact; no Slack archaeology.

### 9.3 The alert-volume signal

If a service is generating excessive pages (`doc 12 §9`), surface it in the scorecard. The platform team sees which services are loud.

### 9.4 The cross-team paging

When a service depends on another and pages cascade, the catalog's dependency graph shows who to escalate to.

---

## 10. The IDP as PRR Engine

The continuous readiness layer.

### 10.1 The mechanics

The PRR scorecard (`doc 17 §10`) lives in the IDP. Auto-evaluated:
- SLO defined? (check the SLO repo)
- Runbook exists? (check the repo)
- Catalog entry complete? (check the catalog itself)
- Capacity plan recent? (check the plan repo)
- ... etc.

### 10.2 The visibility

Per service:
- PRR score.
- Items met / pending / blocking / exception.
- Last review date.
- Next review due.
- Action items open.

### 10.3 The org-wide view

Cross-team dashboard showing:
- Services below score threshold.
- Items most commonly missing.
- Trend over time.

### 10.4 The "PRR-in-PR" flow

When a PR is opened against a service that affects readiness items, the IDP can comment on the PR:

> "This change reduces the service's PRR score from 87 to 82 (canary deploy disabled). Confirm intent."

Service teams see the consequence at PR time.

---

## 11. Tools (2026 Landscape)

Already covered in §2.2. The 2026 trend: deeper observability integration. Backstage's Grafana, PagerDuty, Sentry, OpenSLO plugins are all maturing. Commercial tools (port.io, cortex.io) build observability-rich dashboards as core.

---

## 12. Anti-Patterns

1. **No catalog.** Engineers don't know what exists.
2. **Catalog without observability data.** Catalog is just a list.
3. **No templates.** Each team reinvents.
4. **Templates without observability.** Services launch unobserved.
5. **No scorecards.** Maturity invisible.
6. **Scorecards without enforcement.** Decorative.
7. **No ownership.** Services orphaned.
8. **No automation in scoring.** Manual; rots.
9. **Multiple golden paths.** Quality dilutes.
10. **No deviation tracking.** Implicit divergence.
11. **No on-call integration.** Engineers tab-juggle.
12. **No runbook integration.** Stale at incident.
13. **Performance neglected.** IDP slow; unused.
14. **No cross-team scoreboard.** No pressure.
15. **Plugin auth too lax.** Security gap.

---

## 13. Worked Example: A Backstage Observability Rollout

Concrete and complete.

### 13.1 Phase 1: catalog (month 1)

- Deploy Backstage.
- Auto-discover services from k8s namespaces and labels.
- Manual cleanup; assign ownership.
- 200 services in the catalog.

### 13.2 Phase 2: integrations (month 2-3)

- Grafana plugin: dashboards embedded.
- PagerDuty plugin: on-call shown.
- Sentry plugin: error counts.
- Custom SLO plugin: pulls from the SLO repo.

### 13.3 Phase 3: templates (month 4-5)

- Golden path for Go HTTP service.
- Golden path for Python async worker.
- Golden path for Kafka consumer.
- Each template includes: SLO, alerts, dashboard, runbook skeleton, catalog entry, CI.

### 13.4 Phase 4: scorecards (month 6-9)

- Scorecard categories: observability, docs, reliability, security.
- Auto-scoring from the IDP; pulls from sources.
- Cross-team scoreboard.

### 13.5 Phase 5: PRR-in-IDP (month 10-12)

- PRR checklist embedded in scorecards.
- New-service flow gates on PRR items.
- PR-comment integration.

### 13.6 Outcomes

- Average PRR score: 65 → 87 over 12 months.
- New-service onboarding: 2 weeks → 2 days.
- Cross-service findability: dramatically improved.
- On-call: less context-switching during incidents.
- Postmortems: engineers cite "found this in the IDP" as a triage step.

### 13.7 The cost

- 1.5 engineers for 12 months on the rollout.
- Backstage operational cost: marginal.
- Infrastructure: small Kubernetes deployment.

The investment pays for itself in: faster onboarding, fewer un-instrumented services, better on-call experience.

---

## 14. Pitfalls

1. **No catalog.** Discovery broken.
2. **Catalog without integrations.** Just a registry.
3. **No templates.** Reinvention.
4. **Templates without observability built-in.** Services launch broken.
5. **No scorecards.** Maturity invisible.
6. **Scorecards manual.** Stale.
7. **No PRR integration.** Continuous readiness missing.
8. **Slow IDP.** Unused.
9. **No ownership.** Orphaned.
10. **Multiple golden paths.** Dilution.
11. **No deviation tracking.** Implicit drift.
12. **No cross-team view.** No pressure.
13. **No runbook integration.** Stale at incident.
14. **No on-call view.** Tab juggling.
15. **No quarterly review of templates.** Drift.

---

## 15. Mental Models

> **Documentation doesn't scale; templates do.**

> **The IDP is the platform team's UX. Engineers live in it.**

> **Golden paths raise the floor across the org. One per stack, well-supported.**

> **The catalog is the substrate. Annotations connect everything.**

> **Scorecards make maturity visible. Visibility creates pressure.**

> **Auto-evaluation is mandatory. Manual scoring rots.**

> **The IDP is the continuous PRR engine. Scorecards are PRR-as-living-document.**

> **Templates with observability built in produce 100% compliance. Documentation produces 50%.**

> **Performance matters. Slow IDP is unused.**

> **The IDP integrates everything. Without integration, it's a list.**

Now go to `doc 41` (brownfield integration) — the last new chapter.
