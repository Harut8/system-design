# 34 — Schema and Semantic-Conventions Governance

> The same metric called `http_request_duration_seconds` in service A and `request_latency_ms` in service B is two metrics, not one. Cross-service queries break. SLO definitions diverge. Dashboard reuse becomes copy-paste-and-edit. The discipline of *naming things consistently* is what makes a fleet of services queryable as a single system. Schema governance is the platform team's product for this.

This chapter is about OTel semantic conventions, attribute registries, breaking-change policy, and the contract-test approach that prevents drift.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The OTel semantic conventions](#2-otel-conventions)
3. [Attribute registries](#3-registry)
4. [Naming rules](#4-naming)
5. [Cardinality classification per attribute](#5-cardinality-class)
6. [The PII / sensitivity classification (revisited)](#6-pii-class)
7. [Schema versioning](#7-versioning)
8. [Breaking-change policy](#8-breaking)
9. [Contract tests](#9-contract-tests)
10. [Schema registries (Avro, Proto, JSON Schema)](#10-schema-registries)
11. [Cross-language schema enforcement](#11-cross-language)
12. [Anti-patterns](#12-anti-patterns)
13. [Worked example: an org-wide schema rollout](#13-worked-example)
14. [Pitfalls](#14-pitfalls)
15. [Mental models](#15-mental-models)

---

## 1. Thesis

Three claims:

1. **A schema is the contract between producers and consumers of telemetry.** Without it, every dashboard is bespoke; every SLO query is brittle; every refactor breaks something downstream.
2. **OTel semantic conventions are the substrate for cross-service queries.** Adopting them is non-negotiable for any org with > 10 services. Standardize there; extend with custom only when needed.
3. **Schema governance is platform-team work.** It's not "everyone's responsibility" — that means nobody's. The platform team owns the registry, the policy, and the enforcement.

If your team has 50 services and 50 different ways to spell "service.name," your cross-service queries don't work and the Staff Engineer should fix it. This chapter is the right shape.

---

## 2. The OTel Semantic Conventions

The standard.

### 2.1 What they are

A community-maintained spec defining attribute names and meanings for common telemetry domains:

- HTTP: `http.request.method`, `http.response.status_code`, `http.route`.
- Database: `db.system`, `db.statement`, `db.name`.
- Messaging: `messaging.system`, `messaging.destination.name`, `messaging.kafka.partition.number`.
- RPC: `rpc.system`, `rpc.service`, `rpc.method`.
- Cloud: `cloud.provider`, `cloud.region`, `cloud.account.id`.
- Container: `container.id`, `container.image.name`.
- Kubernetes: `k8s.pod.name`, `k8s.namespace.name`.
- Resource: `service.name`, `service.version`, `deployment.environment`.

Hundreds of attributes; growing.

### 2.2 The stability tiers

OTel marks each convention as:
- **Stable** — won't change.
- **Experimental** — may change.
- **Deprecated** — being phased out.

Use stable; tolerate experimental with caution; migrate off deprecated.

### 2.3 The payoff

Adopt OTel conventions and:
- Tempo, Datadog, Honeycomb, Jaeger all *recognize* the attributes.
- Auto-derived service graphs use them.
- Cross-language services agree on naming.
- Vendor migrations easier (data already standard).

### 2.4 The vendor-specific addition

Most observability vendors *also* recognize their own conventions. OTel often takes precedence; a vendor-specific extension might add attributes but typically not rename OTel ones.

---

## 3. Attribute Registries

The org-specific layer.

### 3.1 What goes in the registry

Beyond OTel:
- Business-specific attributes: `customer_tier`, `feature_flag`, `order_priority`.
- Org-specific resource attributes: `team`, `cost_center`.
- Custom span attributes: `pricing.vendor`, `auth.method`.
- Metric labels.

Each attribute has:
- Name.
- Description.
- Type (string, int, bool, etc.).
- Cardinality classification (low / medium / high).
- Sensitivity classification (public / confidential / regulated).
- Values (enum, if bounded).
- Owner (which team).
- Status (active / deprecated).

### 3.2 The registry as YAML

```yaml
# attribute-registry.yaml
attributes:
  - name: customer_tier
    description: Customer subscription tier
    type: string
    cardinality: low
    sensitivity: internal
    values: ["free", "starter", "professional", "enterprise"]
    owner: customer-platform
    status: active

  - name: feature_flag
    description: Active feature flag for the request
    type: string
    cardinality: medium
    sensitivity: internal
    owner: experimentation
    status: active
    notes: |
      Limit cardinality by ensuring flags are short-lived;
      flags that have been on/off for >90 days should not appear here.
```

### 3.3 The repository

A central Git repo. Every attribute change goes through PR. The platform team reviews.

### 3.4 Auto-generated docs

From the YAML, generate:
- A searchable docs site (Backstage plugin, MkDocs, custom).
- Per-attribute pages.
- Cross-references (which dashboards / alerts / SLOs use this attribute).

---

## 4. Naming Rules

The lexicon.

### 4.1 The OTel naming rules (apply to custom too)

- **lowercase**, dot-separated.
- Hierarchy: domain.subject.descriptor (`http.request.method`).
- No abbreviations: `database.statement` not `db.stmt`.
- Verbose over cryptic: `http.response.status_code` not `code`.
- Singular: `customer_tier` not `customer_tiers`.

### 4.2 Metric naming

Suffix conventions:
- `_total` for monotonic counters.
- `_seconds`, `_bytes` for units.
- `_bucket`, `_count`, `_sum` for histograms.

```
checkout_requests_total                     ← counter
checkout_request_duration_seconds_bucket    ← histogram
queue_size                                  ← gauge
```

### 4.3 Label naming

Same conventions as attributes. **Crucially**: same label name = same meaning across all metrics. `customer_tier` always means the same thing.

### 4.4 The "don't reinvent" rule

Before adding a custom attribute: search OTel conventions. If it exists, use it. Custom only for genuinely org-specific.

### 4.5 The "no spaces, no special characters" rule

Names: `[a-z0-9_]` and dots. Otherwise: query bugs, escape rules, dashboard mismatches.

---

## 5. Cardinality Classification per Attribute

Cross-link to `doc 18 §5`.

### 5.1 The classifications

| Class | Cardinality | Examples | Use as metric label? |
|---|---|---|---|
| **Low** | < 100 | `http.method`, `customer_tier`, `region` | Yes |
| **Medium** | 100-10K | `http.route`, `service.name`, `kubernetes.pod` | Cautious |
| **High** | 10K-1M | `http.url` (with parameters), `customer_id` | No (logs/traces) |
| **Unbounded** | > 1M | `request_id`, `session_id`, `email` | Never |

### 5.2 The registry constraint

The registry stores the class. Platform tooling enforces:
- Low: use freely.
- Medium: monitor; alert on cardinality growth.
- High: must be in trace span attributes / log fields, not metric labels.
- Unbounded: never as label; sometimes as span attribute (with rationale).

### 5.3 The CI check

PRs adding new attributes are reviewed for classification. PRs using a high/unbounded attribute as a metric label fail CI.

```
ERROR: customer_id (cardinality: unbounded) used as metric label.
       Move to trace span or log field.
       See: docs/attribute-registry/customer_id.md
```

This is the cardinality discipline mechanically enforced.

---

## 6. The PII / Sensitivity Classification (Revisited)

Cross-link to `doc 32 §3`.

### 6.1 The intersection with the registry

Each attribute has a sensitivity tag:

```yaml
- name: user.email
  sensitivity: confidential
  redact_in_logs: true
  hash_in_traces: true
  never_in_metric_labels: true
```

### 6.2 The enforcement

- Logs pipeline applies redaction based on registry.
- CI checks: spans / logs / metrics don't use never-allowed attributes.
- Audit periodically.

### 6.3 The registry as compliance artifact

The registry, with sensitivity tags, is a compliance artifact:
- Auditor asks: "what PII fields are you storing in telemetry?" Answer: the registry's `sensitivity: confidential` rows.
- Documentation of redaction policies.
- Data classification framework.

Signed by privacy / compliance lead annually.

---

## 7. Schema Versioning

Telemetry schemas evolve.

### 7.1 The version dimension

Every schema change is versioned. The change log records:
- What changed (added attribute, renamed attribute, deprecated).
- When.
- Why.
- Migration path.

### 7.2 Version compatibility

| Change | Backwards-compat? |
|---|---|
| Add new optional attribute | Yes |
| Add new required attribute | No (breaks producers without it) |
| Rename attribute | No (without alias) |
| Change attribute type | No |
| Remove deprecated attribute | No (after deprecation period) |
| Change cardinality classification | Maybe (if tightening, may break existing usage) |

### 7.3 The deprecation period

Attribute deprecation: 6+ months minimum. Two phases:
- Mark deprecated; emit warnings; both old and new accepted.
- After period: old removed; consumers must use new.

### 7.4 The OTel migration path

OTel itself versions semantic conventions: `1.0`, `1.20`, etc. The collector can map between versions for backwards compatibility. Use this; don't manually rename.

---

## 8. Breaking-Change Policy

The governance.

### 8.1 What counts as breaking

- Attribute removed.
- Attribute renamed (without alias).
- Type changed.
- Required-vs-optional changed.
- Cardinality classification tightened.

### 8.2 The policy

Breaking changes require:
- Platform-team approval.
- Migration plan documented.
- Deprecation period (6+ months).
- Communication (engineering all-hands; doc).
- Tooling for the migration (collector mapping; lint).
- Rollback plan.

### 8.3 The "don't break unless you must"

Most schema "improvements" don't justify breakage. Add new attribute; deprecate old slowly; eventually remove. Patience over correctness.

### 8.4 The exception: security / compliance

Sometimes a breaking change is required (PII detected; must be removed immediately). Then:
- Emergency change.
- Apply at the collector (drop the attribute).
- Producers may continue emitting; the platform strips.
- Plan to fix producers in a normal cycle.

---

## 9. Contract Tests

The mechanical enforcement.

### 9.1 The pattern

Service A produces telemetry. Service B / dashboard / alert consumes it. The contract: attribute X exists, with cardinality Y, with values Z.

A test verifies the contract holds.

### 9.2 The producer test

In service A's CI:

```python
def test_emits_required_attributes():
    spans = capture_spans(do_checkout())
    for span in spans:
        assert "service.name" in span.attributes
        assert "customer.tier" in span.attributes
        assert span.attributes["customer.tier"] in ["free", "starter", "professional", "enterprise"]
```

The test fails if the attribute is missing or has wrong values.

### 9.3 The consumer test

In dashboard / alert CI:

```yaml
# Test that the alert's PromQL produces results given the expected labels
test:
  query: 'rate(http_requests_total{service="checkout", customer_tier="enterprise"}[5m])'
  expect: result_count > 0
```

Or the alert's labels are validated against the registry.

### 9.4 The cross-team test

When service A changes its schema, downstream consumers (alerts, dashboards) get notified. Optionally, consumer tests run as part of A's CI.

---

## 10. Schema Registries (Avro, Proto, JSON Schema)

For structured event data.

### 10.1 The case

Kafka events, RPC payloads, structured logs — these have schemas. A schema registry stores them, enforces compatibility.

### 10.2 The tools

- **Confluent Schema Registry** (Kafka).
- **AWS Glue Schema Registry**.
- **Buf Schema Registry** (Protobuf).
- **JSON Schema with $schema URLs.**

### 10.3 The compatibility modes

- **BACKWARD:** consumers using new schema can read data from old.
- **FORWARD:** consumers using old can read new.
- **FULL:** both.
- **NONE:** breaks freely (avoid).

For telemetry: usually BACKWARD (consumer adapts to producer evolution).

### 10.4 Schema registry as part of the org

For org-wide structured telemetry: a single registry. All teams contribute schemas; review like the attribute registry.

### 10.5 The CI gate

PR that changes a schema runs:
- Compatibility check against the registry.
- Block if breaking.

---

## 11. Cross-Language Schema Enforcement

The polyglot challenge.

### 11.1 The problem

Same attribute name across Go, Python, Java, Node services. Each has its own SDK; each has its own way to set attributes.

### 11.2 The OTel approach

OTel SDKs provide *typed* attributes for known semantic conventions:

```go
// Go
import "go.opentelemetry.io/otel/semconv/v1.21.0"
span.SetAttributes(semconv.HTTPRequestMethod("POST"))
```

```python
# Python
from opentelemetry.semconv.trace import HttpAttributes
span.set_attribute(HttpAttributes.HTTP_REQUEST_METHOD, "POST")
```

The SDK ensures the *string name* is consistent (`http.request.method`). Cross-language alignment guaranteed.

### 11.3 The custom-attribute pattern

For org-specific attributes: code generated from the registry YAML.

```yaml
# attribute-registry.yaml
- name: customer.tier
  type: string
```

→ generated Go module:
```go
package attrs
const CustomerTier = "customer.tier"
```

→ generated Python module:
```python
class Attrs:
    CUSTOMER_TIER = "customer.tier"
```

Each language has typed access. String drift impossible.

### 11.4 The reflection / runtime check

The collector validates: incoming attributes match the registry. Unknown attributes warned; future-proof.

Some teams allow unknown (for innovation); others require pre-registration. Pick based on org size and rigor.

---

## 12. Anti-Patterns

1. **No registry.** Drift; cross-service queries break.
2. **No naming convention.** Each team invents.
3. **Custom attribute names instead of OTel.** Lose ecosystem benefit.
4. **No cardinality classification.** Bombs ship.
5. **No PII classification.** Compliance gap.
6. **No versioning.** Breaks invisibly.
7. **No deprecation period.** Consumer breakage.
8. **No CI enforcement.** Drift silent.
9. **Polyglot string drift.** Cross-language inconsistency.
10. **Schema registry separate from attribute registry.** Two sources of truth.
11. **No annual schema audit.** Stale entries accumulate.
12. **No cross-team change review.** Silent breakage.
13. **Inline literal attribute names everywhere.** No central control.
14. **No code generation from registry.** Manual sync; drift.
15. **No documentation.** Engineers don't know what's available.

---

## 13. Worked Example: An Org-Wide Schema Rollout

The story.

### 13.1 The starting state

- 200 services.
- Mix of OTel-instrumented and pre-OTel.
- No central schema; each team chose attribute names.
- Cross-team metric queries break (`service` vs `service_name` vs `svc.name`).

### 13.2 The plan

Six-month rollout:

**Month 1: foundation**
- Adopt OTel semantic conventions as standard.
- Create central attribute registry.
- Document naming rules.

**Month 2: tooling**
- Code generation from registry to all languages.
- CI check: PRs using non-registry attributes fail.
- Lint for common naming mistakes.

**Month 3: migration of high-traffic services**
- Top 20 services migrate to OTel + registry.
- Dashboards updated for new names.
- Alerts updated.

**Month 4-5: rest of services**
- Per-team migration plan.
- Platform team supports.

**Month 6: cleanup**
- Deprecated names removed.
- Legacy aliases at the collector retired.
- Old dashboards retired.

### 13.3 The migration mechanics

For renamed attributes: the collector emits both old and new names during transition. Once all consumers migrated, drop the old.

```yaml
# Collector transition rule
processors:
  attributes/migrate:
    actions:
      - key: svc.name           # old
        from_attribute: service.name
        action: insert           # also emit old name during transition
```

Consumers (dashboards, alerts) migrate to new name; verify; then drop the rule.

### 13.4 The result

- Cross-service queries work.
- Standard dashboards usable across teams.
- New service onboarding faster (registry tells them what to emit).
- Compliance audit easier (sensitivity tags central).

### 13.5 The cost

- 6 months × 1 engineer at 30% = ~$60k.
- Saved: indeterminate hours of "why doesn't my query work" debugging.

For a 200-service org, the math is overwhelming. The investment pays back in months.

---

## 14. Pitfalls

1. **No registry.** Drift.
2. **No CI enforcement.** Registry ignored.
3. **No deprecation period.** Breaking.
4. **No code generation.** Manual drift.
5. **No PII tags.** Compliance.
6. **No cross-team review.** Breakage.
7. **No naming convention.** Inconsistency.
8. **OTel skipped for custom.** Ecosystem benefit lost.
9. **No cardinality tags.** Bombs.
10. **Schema and attribute registries separate.** Two sources of truth.
11. **No rollout plan.** Migration chaotic.
12. **No collector translation rules.** Hard transitions.
13. **No annual audit.** Drift.
14. **Inline string usage.** Updates skipped.
15. **No documentation site.** Engineers ignore.

---

## 15. Mental Models

> **Schema is a contract. Without it, dashboards and SLOs break invisibly.**

> **OTel semantic conventions are the substrate. Adopt; extend with custom only as needed.**

> **The registry is the source of truth. Code generation derives from it.**

> **Cardinality and sensitivity classifications enforce cardinality discipline and compliance.**

> **Versioning + deprecation period for breaking changes. 6 months minimum.**

> **Contract tests catch breakage in CI.**

> **Polyglot enforcement via code generation. Otherwise string drift.**

> **The collector handles transitions. Both old and new emitted; drop later.**

> **Schema registry is platform-team work. "Everyone's responsibility" = no one's.**

> **Annual audit of registry. Drift is the long-term enemy.**

Now go to `doc 35` (telemetry lakehouse).
