# 32 — Compliance and Privacy

> Compliance is the layer that bounds what telemetry can do. GDPR, HIPAA, PCI-DSS, SOC2, FedRAMP, ISO 27001, CCPA — each imposes constraints on collection, retention, access, and erasure of telemetry. The platform team that doesn't bake compliance into the pipeline ends up bolting it on under audit pressure, badly.

This chapter is the compliance and privacy story for observability data, not for the org's customer data broadly.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The compliance regimes that govern telemetry](#2-regimes)
3. [Data classification: what's in your logs and traces](#3-classification)
4. [Redaction at the source](#4-redaction)
5. [Per-tier retention, per-tier access](#5-retention-access)
6. [The right-to-erasure problem](#6-erasure)
7. [Audit logging revisited (what counts)](#7-audit)
8. [Cross-border data flow](#8-cross-border)
9. [Encryption: in-flight and at-rest](#9-encryption)
10. [Vendor compliance and DPAs](#10-dpa)
11. [The compliance-aware schema design](#11-schema)
12. [The annual audit cycle](#12-audit-cycle)
13. [Anti-patterns](#13-anti-patterns)
14. [Worked example: a HIPAA-bound observability stack](#14-worked-example)
15. [Pitfalls](#15-pitfalls)
16. [Mental models](#16-mental-models)

---

## 1. Thesis

Three claims:

1. **Compliance is a design constraint, not a feature.** Bolt-on compliance is more expensive than designed-in compliance and produces audit findings.
2. **Observability data is regulated data.** It contains user identifiers, IP addresses, often access tokens, sometimes PII. Treat it as such.
3. **Redact at the source.** Once data is in the store, it's leaked. Redaction at the agent / SDK is non-negotiable for sensitive surfaces.

If your team's response to a compliance audit is "we have a lot of data and we're not sure what's in it," you're going to fail or buy expensive consulting. This chapter is the discipline.

---

## 2. The Compliance Regimes That Govern Telemetry

| Regime | Scope | Telemetry implications |
|---|---|---|
| **GDPR** (EU) | EU residents' data | Right to access, erasure, portability; data residency; lawful basis |
| **CCPA / CPRA** (California) | California residents | Right to access, deletion, opt-out of sale |
| **HIPAA** (US healthcare) | PHI (protected health info) | Strict access, audit, encryption, BAAs |
| **PCI-DSS** | Card data | Card numbers / CVV cannot be stored; PAN tokenized |
| **SOC2** | Service org controls | Audit trails; access reviews; defined retention |
| **FedRAMP** | US federal cloud | Higher bar; FIPS encryption; specific approvals |
| **ISO 27001** | Information security mgmt | Defined policies; reviewed annually |
| **LGPD** (Brazil) | Brazilian residents | GDPR-like |
| **PIPEDA** (Canada) | Canadian residents | Similar to GDPR (less strict) |
| **POPIA** (South Africa) | South African residents | Similar |
| **DPA** (UK) | UK residents | GDPR-derived post-Brexit |
| **APRA CPS 234** (Australia financial) | Financial services | Audit, encryption, BCP |

Each shapes telemetry retention, access, erasure, encryption, and storage location.

---

## 3. Data Classification: What's In Your Logs and Traces

Inventory before policy.

### 3.1 The classification taxonomy

| Tier | Examples | Telemetry handling |
|---|---|---|
| **Public** | Service name, request path | Standard; no special handling |
| **Internal** | Internal tenant IDs, server hostnames | Standard; access-controlled at the platform |
| **Confidential** | Email, IP, session IDs | Pseudonymize; restricted access |
| **Sensitive** | Names, addresses, payment-related metadata | Encrypt; minimum collection; high-restriction access |
| **Regulated** | Health data (HIPAA), full card numbers | Don't collect (or collect with explicit BAA / scope) |

### 3.2 The audit

Quarterly: sample 100 events from each store; classify what's in them. Flag unexpected sensitive data.

This is the "do we know what we have?" check. Many orgs fail it.

### 3.3 The "PII detection" pipeline

Automated PII detection in the telemetry pipeline:
- Regex (emails, phones, SSN patterns).
- NER models (names, addresses).
- Pattern detection (credit cards via Luhn check).

Tools: Google DLP, AWS Macie, presidio (open-source), built into many vendors.

The pipeline flags or redacts. False positive rate is non-zero; tune.

### 3.4 The "this should not exist" finding

Sometimes the audit finds data that shouldn't be there:
- Production credentials in logs.
- Session tokens in error reports.
- User PII in metric labels.

Each finding triggers:
- Immediate redaction / deletion.
- Root cause investigation.
- Process change to prevent recurrence.

---

## 4. Redaction at the Source

Cross-link to `doc 18 §8.4`. The first-line defense.

### 4.1 The principle

Redact before the agent ships data. Once data leaves the box, it's outside your control (and possibly your jurisdiction).

### 4.2 The mechanisms

- **SDK-level scrubbing.** Application-aware (e.g., Sentry's `before_send`).
- **Agent-level filters.** Fluent Bit `record_modifier`; Vector VRL transforms; OTel Collector attribute processors.
- **Code-level discipline.** Loggers that strip sensitive fields by default.

### 4.3 The patterns

```yaml
# Vector VRL example
.message = redact(.message, filters: ["us_social_security_number", "email", "credit_card"])

# OTel Collector
processors:
  attributes:
    actions:
      - key: user.email
        action: hash
      - key: card_number
        action: delete
```

### 4.4 The "deny-list vs allow-list" choice

- **Deny-list:** "redact these specific fields." Fast; misses unknowns.
- **Allow-list:** "only forward these specific fields." Slower; safer.

For sensitive surfaces (HIPAA, PCI), allow-list. For normal services, deny-list with periodic audits.

### 4.5 The redaction unit-test

Test redaction policies in CI: feed known PII; verify it's redacted.

```python
def test_email_redacted():
    log_line = "User logged in: alice@example.com"
    assert "alice@example.com" not in pipeline_redact(log_line)
```

Without these tests, redaction silently breaks (regex tweaks, version updates).

---

## 5. Per-Tier Retention, Per-Tier Access

The two-axis governance.

### 5.1 Per-data-class retention

| Data class | Retention |
|---|---|
| Operational logs | 7-30 days |
| Application traces | 7-14 days |
| Application metrics | 30 days hot, 1 year warm |
| Audit logs | 1-7 years (SOC2: 1y; HIPAA: 6y; GDPR: case-specific) |
| Security logs | 1-2 years |
| RUM / browser telemetry | 30 days |
| Session replay | 7-30 days |
| Profiles | 7-30 days |
| LLM prompts/completions | 7-30 days (or zero with provider's zero-retention) |

### 5.2 Per-data-class access

| Data class | Access |
|---|---|
| Operational | Service team + platform |
| Audit | Security team + compliance + select platform |
| Security | Security team only |
| RUM | Service team + platform; aggregated only for cross-team |
| Session replay | Strict: service team + customer success |
| LLM prompts | Service team + AI platform team |

### 5.3 The deletion guarantee

Retention enforcement is on the platform. When retention expires, data is deleted. Verifiable.

For object-store-backed (Mimir blocks, Tempo blocks): lifecycle policies.
For ingester / hot tier: explicit delete.
For audit: write-once with auto-expiry on the bucket.

### 5.4 The "data hangs around" failure

Common: data deleted from queries (filtered out) but still in storage. Compliance fail.

Fix: actual deletion, verified. Audit periodically.

---

## 6. The Right-to-Erasure Problem

GDPR / CCPA's hardest requirement.

### 6.1 The requirement

A user requests their data be deleted. The org has 30 days. Includes telemetry.

### 6.2 The challenge

Telemetry is keyed by user ID across many stores. Deletion means:
- Find all events with that user ID.
- Delete them from hot, warm, cold, archive.
- Verify.
- Document.

This is hard in immutable stores (object storage, append-only logs). And expensive — full-table scans.

### 6.3 The pseudonymization architecture

Cross-link to `doc 27 §11.4`. The standard solution:

- Telemetry stores `user_id_hash` (a salted hash), not the raw user ID.
- A separate, deletable `id_map` table holds `(user_id_hash → user_id)`.
- On erasure: delete the row from `id_map`; the telemetry remains but is unlinkable to the actual person.

This satisfies GDPR right-to-erasure (the data is no longer associated) without touching the immutable telemetry.

### 6.4 The reverse-engineering risk

A determined attacker with access to the telemetry might re-identify users via correlation. Defenses:
- High-entropy salt for the hash.
- Time-bounded salt rotation.
- Differential-privacy noise on aggregated reports (in extreme cases).

For most orgs, pseudonymization is sufficient.

### 6.5 The full-erasure case

For very high-stakes (health data, financial), full erasure may be required. Then the storage architecture must support it:
- Per-tenant partitioning (delete the partition).
- Searchable encryption (delete the key).
- Acceptance of higher cost.

---

## 7. Audit Logging Revisited (What Counts)

(Cross-link to `doc 27 §3`.)

### 7.1 Compliance-relevant actions

- Login / logout.
- Authorization decisions on sensitive resources.
- Data access (read or write) on tagged data classes.
- Admin actions.
- Configuration changes.
- Permission changes.
- Data export.
- Data deletion.

### 7.2 The format

The "5 W's": who, what, when, where, why. Plus tamper-evidence.

### 7.3 The retention

Per regime: 1-7 years. Always longer than operational logs.

### 7.4 The access pattern

- Read by security and compliance only.
- Audited reads (the "who queried the audit log" recursion).
- Tamper-evident (signed; hash-chained; immutable storage).

### 7.5 The integrity verification

Periodic: run a process that verifies:
- Hash chain unbroken.
- No tampering.
- Replication consistent.

Annual at minimum. Quarterly if regulated.

---

## 8. Cross-Border Data Flow

The data residency problem.

### 8.1 The basics

GDPR limits transfer of EU data to non-EU jurisdictions unless:
- The destination has adequate protection.
- A specific legal mechanism (Standard Contractual Clauses, Binding Corporate Rules).
- Data Subject explicit consent.

### 8.2 The telemetry implication

EU users' telemetry must be:
- Stored in the EU (most common).
- Or transferred via SCC + transfer impact assessment.
- Vendor must comply too.

### 8.3 The architecture

Per-region telemetry stack:
- EU users → EU Mimir / Loki / Tempo cluster.
- US users → US.
- APAC → APAC.

Cross-region queries: federated, with care.

### 8.4 The vendor question

Datadog, Sentry, etc. offer EU regions. Splunk too. Pin your accounts to the right region; verify in vendor contracts.

### 8.5 The Schrems II era

The EU-US Privacy Shield was invalidated; replaced by Data Privacy Framework (2023+). Continuously evolving. Stay current; consult legal.

### 8.6 Data nationalism

China's PIPL, Russia's data localization, India's DPDP Act, others. Each may require local data storage. Plan multi-jurisdiction architectures for global SaaS.

---

## 9. Encryption: In-Flight and At-Rest

The baseline.

### 9.1 In-flight

- TLS for all telemetry transport (agent → collector, collector → backend, app → collector).
- mTLS for service-to-service in regulated environments.
- Modern TLS (1.3 preferred; 1.2 acceptable; older deprecated).

### 9.2 At-rest

- Object storage encryption (S3 SSE, GCS encryption, Azure SSE).
- Database / TSDB at-rest encryption (most native).
- Customer-managed keys (CMK) for higher-trust regimes.

### 9.3 Key management

- KMS-backed (AWS KMS, GCP KMS, Azure Key Vault, HashiCorp Vault).
- Rotation policy.
- Access audit on key use.

### 9.4 The FIPS requirement

For US federal (FedRAMP): FIPS 140-2 / 140-3 validated cryptography. Constrains cipher suites. Most major clouds support it; verify.

### 9.5 Encryption is not access control

Encrypted data is still data. Access controls (IAM, RBAC) gate who can decrypt. Encryption alone doesn't satisfy compliance.

---

## 10. Vendor Compliance and DPAs

The vendor relationship layer.

### 10.1 The DPA (Data Processing Agreement)

Required by GDPR for any vendor processing EU data. The vendor commits to:
- Lawful basis for processing.
- Sub-processor disclosure.
- Security measures.
- Breach notification.
- Data subject request handling.
- Termination / return of data.

Sign one with every observability vendor. Track in your compliance system.

### 10.2 The BAA (HIPAA Business Associate Agreement)

For HIPAA: required with any vendor handling PHI. Strict.

Most observability vendors offer BAAs; not all by default. Negotiate.

### 10.3 The certifications

- SOC2 Type 2 (most major vendors).
- ISO 27001.
- HIPAA-eligible.
- PCI-DSS scope.
- FedRAMP-authorized (specific vendors).

Verify periodically. Vendor cert lapse = your compliance gap.

### 10.4 The sub-processor list

Vendors use sub-processors (their CDNs, their cloud providers, etc.). Each is a transitive risk. Vendors maintain a public sub-processor list; review on changes.

### 10.5 The breach-notification clause

In the DPA: vendor must notify within X hours of a breach. Specifies your timeline obligations to your users.

---

## 11. The Compliance-Aware Schema Design

Bake compliance into the data model.

### 11.1 Tag every field

```yaml
fields:
  user_id:
    classification: confidential
    pseudonymize: true
    retention: 30d
  email:
    classification: confidential
    retention: 7d
    redact_in_logs: true
  card_number:
    classification: regulated_pci
    never_log: true
```

The schema makes the rules visible; tooling enforces.

### 11.2 The OTel semantic-conventions overlap

OTel has stable attribute names. Map them to classifications:

```
http.request.headers.cookie    → confidential, redact
db.statement                   → varies; depends on table
user.id                         → confidential, pseudonymize
client.address                  → confidential (IP is PII in EU)
```

A central schema registry maps OTel attrs to your classifications.

### 11.3 The validation pipeline

CI tests:
- Logs from the new feature don't contain unredacted regulated data.
- Trace attributes don't include never-log fields.
- Metrics don't have user_id labels.

Automate. Otherwise, drift.

---

## 12. The Annual Audit Cycle

The institutional cadence.

### 12.1 Internal audit (quarterly)

The platform team self-audits:
- Sample logs for unexpected PII.
- Verify retention enforcement.
- Verify access controls.
- Verify encryption (at rest and in flight).
- Update the data classification.

### 12.2 External audit (annual, for SOC2 / ISO)

Auditors visit:
- Review policies.
- Sample evidence (audit logs, retention proof, access reviews).
- Test controls.
- Issue report (clean, qualified, adverse).

The platform team's job: provide evidence. Logs accessible. Reports automatable. Auditors don't write your runbooks; they verify them.

### 12.3 The "annual review" of policies

- Data retention policy.
- Access policy.
- Incident response.
- Vendor list.
- DPA list.
- Sub-processor list.

Reviewed annually; signed by leadership.

### 12.4 The continuous-compliance pattern

Modern: continuous monitoring of compliance posture. Tools: Drata, Vanta, Secureframe. They probe your controls, alert on drift.

For observability:
- Alert when retention not enforced.
- Alert on access policy drift.
- Alert on missing audit logs.

---

## 13. Anti-Patterns

1. **No data classification.** Don't know what you have.
2. **No source redaction.** Sensitive data leaks to the store.
3. **One retention for all.** Audit too short, debug too long.
4. **No right-to-erasure path.** GDPR fails.
5. **No DPAs with vendors.** Legal gap.
6. **No audit-log integrity.** Tampering invisible.
7. **No cross-border architecture.** EU data in US illegally.
8. **No encryption at rest.** Compliance gap.
9. **No CI tests on redaction.** Silent regressions.
10. **No quarterly audit.** Drift.
11. **No annual policy review.** Drift.
12. **PII in metric labels.** Compliance + cost.
13. **Vendor cert lapse undetected.** Compliance gap.
14. **No data subject request playbook.** GDPR / CCPA escalations chaotic.
15. **No breach notification process.** Slow notification = bigger fines.

---

## 14. Worked Example: A HIPAA-Bound Observability Stack

Concrete and complete.

### 14.1 The org

- Healthcare SaaS; PHI in workloads.
- ~50 services.
- HIPAA + SOC2 + state privacy laws.
- US-only (initially).

### 14.2 The architecture

- Self-hosted Mimir + Loki + Tempo on AWS in HIPAA-eligible regions.
- BAAs in place with AWS, Datadog (RUM only), Sentry (errors only with strict scrubbing).
- Encryption at rest with customer-managed KMS keys.
- TLS everywhere; mTLS service-to-service.
- Audit logs to write-once S3 with 7-year retention.

### 14.3 The redaction pipeline

OTel collector with attribute processors:
- Drop `request.body` from logs (contains PHI sometimes).
- Hash `user.id` (pseudonymize).
- Drop any field starting with `phi.` from outbound spans.
- Allow-list approach for span attributes (not deny-list).

CI tests verify: PHI fields never reach the store.

### 14.4 The right-to-erasure

User requests deletion → automated pipeline:
1. Pull `user.id` and its hash from id_map.
2. Mark for deletion.
3. Async delete from operational stores (where retention is short).
4. Delete the row in id_map (un-pseudonymizes audit logs without touching them).
5. Confirm deletion to the user.

The id_map is the "delete switch." Audit logs remain; their PII linkage is broken.

### 14.5 The audit

Annual SOC2 audit. Auditor asks:
- Show me retention enforcement for audit logs. (Lifecycle policy + spot check.)
- Show me access control on PHI logs. (RBAC config + recent denials.)
- Show me a sample data subject request. (Documented response in compliance system.)
- Show me the redaction policy. (Schema registry + CI tests + sample logs.)

Each answered with auto-generated evidence. Audit completed in 2 weeks instead of 8.

### 14.6 The annual review

Policies reviewed by legal + security + platform team:
- Schema classifications.
- Vendor list and DPAs.
- Retention.
- Access policies.

Signed; published; the auditor reads them next year.

### 14.7 The result

- Zero compliance findings two years running.
- Data subject requests handled within SLA.
- Vendor pivots possible (changed RUM vendor with one DPA-signing meeting).
- Engineers don't fight compliance; the schema enforces.

---

## 15. Pitfalls

1. **No classification.** Don't know what you have.
2. **No source redaction.** Sensitive data leaks.
3. **One retention.** Mismatched.
4. **No erasure path.** GDPR fails.
5. **No DPAs.** Legal gap.
6. **Audit logs untrusted.** Tampering invisible.
7. **No regional architecture.** Cross-border violations.
8. **No at-rest encryption.** Compliance gap.
9. **No CI tests for redaction.** Silent regressions.
10. **No quarterly audit.** Drift unseen.
11. **No annual policy review.** Drift.
12. **PII in metric labels.** Compliance + cost.
13. **No vendor cert tracking.** Lapse undetected.
14. **No DSR playbook.** Erasure / access requests chaotic.
15. **No breach process.** Slow notification.

---

## 16. Mental Models

> **Compliance is a design constraint, not a feature.**

> **Data classification first. Then policy. Then enforcement.**

> **Redact at the source. Once stored, leaked.**

> **Per-tier retention; per-tier access. Different rules for different data classes.**

> **Pseudonymize for right-to-erasure. id_map is the delete switch.**

> **DPAs and BAAs with every vendor. Verify; track.**

> **Cross-border requires architectural separation. EU data in EU.**

> **Encryption is necessary, not sufficient. Access controls gate decryption.**

> **CI tests on redaction. Otherwise silent regressions.**

> **Annual review of policies. Otherwise drift.**

Now go to `doc 33` (federated multi-region).
