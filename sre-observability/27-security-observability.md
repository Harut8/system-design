# 27 — Security Observability

> Security and reliability share more telemetry than they admit. Audit logs, anomaly signals, behavior baselines, dependency graphs — both teams want them, both teams under-invest, both teams reinvent. This chapter is about the overlap, the line, and how an SRE-grade observability stack feeds the SOC without becoming the SOC.

This chapter is for Staff Engineers building observability platforms. The detailed offensive / defensive security material — MITRE ATT&CK technique-by-technique mapping, SIEM rule writing, threat hunting — lives in security-team docs. Here we cover the platform-team responsibilities: signals, integration, retention, the boundary.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The line: SRE telemetry vs SOC telemetry](#2-line)
3. [Audit logs: the universal substrate](#3-audit-logs)
4. [Authentication and authorization signals](#4-authn-authz)
5. [Anomalous behavior baselines](#5-baselines)
6. [Network security telemetry (revisited)](#6-network)
7. [eBPF for security: Tetragon, Falco, Tracee](#7-ebpf-security)
8. [Application security signals: WAF, RASP, fraud](#8-app-security)
9. [SIEM integration patterns](#9-siem)
10. [MITRE ATT&CK mapping](#10-mitre)
11. [Compliance audit trails](#11-compliance)
12. [Detection engineering: rules-as-code](#12-detection-engineering)
13. [Response: paging the SOC vs paging on-call](#13-response)
14. [Privacy of security telemetry itself](#14-privacy)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: a platform-SOC integration](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **The platform team is the SOC's data supplier, not its delegate.** Security needs the same logs, traces, and audit data the platform produces. The platform's job: make them queryable; ship them where SOC needs them.
2. **Detection ≠ response.** SREs can detect security signals (and should), but the response process is the SOC's. Don't conflate the two.
3. **Security observability has its own retention and integrity rules.** Audit logs need years of retention. Tamper-resistance matters. The cost / compliance trade-off is structurally different from operational logs.

If your platform team treats security telemetry as "not our problem," you'll discover at the next breach that half the data is missing or unqueryable. Conversely, if you treat security as "just more observability," you'll miss the response, retention, and integrity discipline that security demands.

---

## 2. The Line: SRE Telemetry vs SOC Telemetry

| Dimension | SRE | SOC |
|---|---|---|
| **Question** | "Is the system working?" | "Is anyone attacking?" |
| **Latency to detect** | Seconds to minutes | Hours to days (often) |
| **Retention** | Hot 7-30d; warm 90d; cold 1y | Years (compliance) |
| **Integrity** | Useful but not load-bearing | Tamper-resistance critical |
| **Audience** | On-call engineers | Security analysts |
| **False-positive cost** | Pager fatigue | Investigation cost |
| **Tool of record** | Grafana / Datadog / Honeycomb | Splunk / Elastic / Sentinel / SIEM |

Both want logs. Both want traces. Both want metrics. Different *uses* of the same substrate.

### 2.1 The shared substrate

The cleanest architecture: one pipeline, multiple consumers.

```
service → agent → collector → (a) hot SRE store
                            → (b) cold security store (with longer retention)
                            → (c) SIEM (filtered, security-relevant subset)
```

The collector fans out. SRE gets fast, recent data. SOC gets long-retention, possibly with additional enrichment (geoip, threat intel).

### 2.2 The "don't do SOC's job" rule

Platform engineers should *not*:
- Write SIEM detection rules (security team's expertise).
- Be on-call for security incidents (separate rotation).
- Triage authentication anomalies (SOC's job).

Platform engineers *should*:
- Make the data available, queryable, joinable.
- Ensure retention meets compliance.
- Maintain audit-log integrity.
- Provide the substrate for detection rules.

---

## 3. Audit Logs: The Universal Substrate

The single most important security signal.

### 3.1 What's an audit log

A structured record of *something a human or service did that mattered*:
- API call to admin endpoint.
- Database query against sensitive data.
- Configuration change.
- Permission change.
- Login / logout.
- Secret access.
- Code deploy.

### 3.2 The "5 W's" of audit logs

```
Who    — actor (user, service account, automation)
What   — action taken
When   — UTC timestamp, precise
Where  — source IP, region, environment
Why    — request context (ticket, change ID, justification)
```

A complete audit log answers all five. Missing any one = limited forensic value.

### 3.3 The structure

```json
{
  "timestamp": "2026-05-06T14:32:18Z",
  "actor": {
    "type": "user",
    "id": "alice@example.com",
    "session_id": "...",
    "auth_method": "sso"
  },
  "action": {
    "type": "database.query",
    "resource": "customers.email",
    "operation": "select"
  },
  "context": {
    "source_ip": "10.1.2.3",
    "user_agent": "psql/15.2",
    "request_id": "...",
    "trace_id": "...",
    "ticket": "SEC-1234"
  },
  "outcome": "success"
}
```

### 3.4 The integrity dimension

Audit logs must be:
- **Append-only.** No modification after write.
- **Tamper-evident.** Cryptographic hash chain or signed at write time.
- **Replicated.** Multi-region storage; can't be deleted by destroying one cluster.
- **Time-stamped accurately.** NTP-synchronized; ideally signed by a trusted time source.

For high-stakes use (financial, healthcare): immutable storage (S3 Object Lock, Azure immutable blobs).

### 3.5 The "audit log of audit log queries"

For SOC2 / HIPAA / PCI: log who queried the audit logs. Meta-audit. Yes, this scales infinitely; each level is one read step.

---

## 4. Authentication and Authorization Signals

The first-line security observability layer.

### 4.1 What to capture

```
auth_logins_total{outcome, method, user_type}
auth_failures_total{outcome, method, reason}
auth_mfa_challenges_total{outcome, factor}
auth_session_duration_seconds_bucket
authz_denials_total{resource, action, principal_type}
```

### 4.2 The signals that matter

- **Failed login spike** for a user → credential stuffing.
- **Successful login from new geography** → potential account takeover.
- **Authz denial spike** → privilege probing.
- **MFA failure rate** → phishing / brute force.
- **Long-running sessions** → forgotten devices.

### 4.3 The "impossible travel" pattern

User logs in from California; 5 minutes later, from Romania. Geographically impossible. Classic signal.

```
auth_logins_geo_change_speed{user}
```

Compute as distance/time between successive logins. > airline speed = anomalous.

### 4.4 The session signal

Active session count, by user. Sudden growth = compromised account being used by N actors.

### 4.5 SSO / IDP integration

For Okta / Azure AD / Google Workspace / Auth0: their audit logs are gold. Stream them into your SIEM via SCIM / their event API. The platform team's role: ensure pipelines exist; data lands where needed; retention is correct.

---

## 5. Anomalous Behavior Baselines

The "user did something unusual" pattern.

### 5.1 What baselines

- **API call volume per user.** Sudden 100× spike = exfiltration suspect.
- **Sensitive-resource access patterns.** User accessing customer table at 3 AM.
- **Service-to-service call patterns.** New service-to-service edges.
- **Data egress volume.** Bytes leaving the cluster per service.

### 5.2 The mechanism

Statistical baselines (mean + stddev over a window) with anomaly alerts on deviation. ML enhances but rule-based often suffices.

### 5.3 The "lateral movement" signal

A compromised service starts calling services it never called before. The talk-map (`doc 24 §13`) plus baseline = lateral-movement detection.

```
new_edge_in_service_graph{src, dst}
```

Alert security on first occurrence; let them triage.

### 5.4 The user-and-entity behavior analytics (UEBA) pattern

Vendor tools (Splunk UBA, Microsoft Sentinel UEBA, Securonix) build per-user/per-service behavior baselines and detect anomalies. Useful at scale; expensive; require deep integration.

---

## 6. Network Security Telemetry (Revisited)

Cross-link to `doc 24` (network observability).

### 6.1 Security-relevant network signals

- **Connections to known-bad IPs** (threat intel feeds).
- **DNS queries to known-bad domains.**
- **Connections out to non-allowlisted destinations.**
- **Sudden traffic to new external destinations.**
- **Port scans (high failed-connect rate to many ports).**

### 6.2 Threat intel integration

Threat intel feeds (commercial: CrowdStrike, Mandiant; community: AlienVault OTX) provide IP / domain / hash blocklists. Integrate at the SIEM:

```
network_connection_to_known_bad_ip_total{source}
dns_query_to_suspicious_domain_total{source}
```

Page the SOC on hits.

### 6.3 Egress monitoring

Most exfiltration goes out the egress. Monitor:
- Per-service egress volume (baseline + anomaly).
- Destinations (allowlist + new-destination alerts).
- Encryption (unencrypted traffic to external = red flag).

### 6.4 The flow-log + threat-intel join

VPC flow logs + threat intel (joined at the SIEM) = "what services have talked to known-bad IPs in the last 30 days." Forensic gold during incident response.

---

## 7. eBPF for Security: Tetragon, Falco, Tracee

eBPF observability extends to security.

### 7.1 The tools

| Tool | Strength |
|---|---|
| **Falco** | CNCF-graduated; rule-based runtime security; long-standing |
| **Tetragon** (Cilium) | eBPF + k8s-native; less mature than Falco but rich |
| **Tracee** (Aqua) | eBPF; runtime-security focused |
| **Sysdig Secure** | Commercial; built on eBPF |

### 7.2 What they capture

Kernel-level events:
- Process starts (with full command-line, parent process, user).
- File reads/writes on sensitive paths.
- Network connections.
- Privilege escalations (setuid, capabilities).
- Container escapes.

### 7.3 The signal volume

eBPF security tools emit *many* events. Filter aggressively:
- Only capture events from sensitive paths / containers.
- Sample.
- Ship to security pipeline, not the regular log pipeline.

### 7.4 The detection-rule example

```yaml
# Falco rule
- rule: Writing to ssh authorized_keys
  desc: An attempt to write to ssh authorized_keys file
  condition: open_write and fd.name endswith ".ssh/authorized_keys"
  output: Detected unauthorized SSH key write (user=%user.name file=%fd.name)
  priority: WARNING
```

Rules express "this kernel event sequence = suspicious." Tools fire when matched.

### 7.5 The platform-team role

- Deploy the eBPF agent fleet-wide.
- Ensure data flow to SIEM.
- Manage agent upgrades / kernel-version compatibility.
- *Not* write the rules — that's security's domain.

---

## 8. Application Security Signals: WAF, RASP, Fraud

Higher-layer signals.

### 8.1 Web Application Firewall (WAF)

Pre-application protection: blocks known attack patterns (SQL injection, XSS, path traversal, etc.).

```
waf_blocks_total{rule_id, source_ip, target}
waf_challenges_total{type}      # captchas, JS challenges
```

WAFs: Cloudflare WAF, AWS WAF, Akamai, Imperva, ModSecurity (open-source).

### 8.2 Runtime Application Self-Protection (RASP)

In-process protection: intercepts attacks at the application runtime. Niche; mostly Java enterprise.

### 8.3 Fraud signals

For commerce / financial: per-user transaction patterns, velocity checks, device fingerprinting, behavioral signals.

```
fraud_score_distribution{feature}
fraud_blocks_total{reason}
```

Tools: Stripe Radar, Sift, Forter, Riskified, custom.

### 8.4 The bot-vs-human classifier

Many requests come from bots. Some bots are legitimate (Googlebot); others are abusive (scrapers, credential-stuffers).

```
bot_classification_total{verdict}
```

Bot management tools: Cloudflare Bot Management, Akamai Bot Manager, Datadome.

---

## 9. SIEM Integration Patterns

How security data ends up in the security team's tool.

### 9.1 The architecture

```
service logs ──┐
audit logs ────┼──→ collector ──→ Kafka ──┬──→ SRE log store (hot, short retention)
auth events ───┤                          │
network flows ─┘                          └──→ SIEM (Splunk / Sentinel / Elastic / Chronicle)
                                                with longer retention + threat intel
```

### 9.2 The filter

Not all logs are security-relevant. The collector / Kafka topic filters:
- Application access logs → SIEM.
- Application debug logs → SRE only.
- Audit logs → SIEM (always, full retention).
- Auth events → SIEM (always).

### 9.3 Common SIEMs

| SIEM | Strength |
|---|---|
| **Splunk** | Long-time leader; expensive; rich |
| **Microsoft Sentinel** | Azure-native; competitive pricing |
| **Elastic Security** | Elastic-based; open-source-friendly |
| **Google Chronicle (Backstory)** | Hyperscale ingest; flat pricing |
| **Sumo Logic Cloud SIEM** | SaaS; mid-market |
| **Panther** | SaaS; SQL-based detection |
| **CrowdStrike LogScale (Humio)** | Fast indexing; cost-effective |

### 9.4 The cost dimension

SIEM ingest is the security team's biggest line item. Common annual costs: $200k – $5M. Filter aggressively at the collector — only security-relevant data goes to the SIEM.

### 9.5 The OCSF / standardization angle

OCSF (Open Cybersecurity Schema Framework) is an emerging schema standard for security events. Adoption growing through 2025-2026. Use it where possible; reduces tool-specific transformation.

---

## 10. MITRE ATT&CK Mapping

The lingua franca of detection.

### 10.1 What it is

A taxonomy of attacker tactics and techniques. Each technique has an ID (`T1078: Valid Accounts`, `T1059: Command-Line Interpreter`).

### 10.2 The mapping

For every detection rule, tag it with the MITRE technique it covers:

```yaml
- name: ssh_authorized_keys_write
  mitre: T1098.004  # SSH Authorized Keys persistence
  description: Modification of ~/.ssh/authorized_keys
```

### 10.3 The coverage view

A dashboard: which MITRE techniques does our detection cover? Where are gaps?

```
Tactic                        Coverage
Initial access                85%
Execution                     70%
Persistence                   60%   ← needs work
Privilege escalation          75%
...
```

This is the security team's own SLI. The platform team enables it (data substrate); the security team owns the rules and the score.

---

## 11. Compliance Audit Trails

Specific to regulated environments.

### 11.1 What various regimes require

| Regime | Audit log requirement |
|---|---|
| **SOC2** | All admin actions logged; access to logs audited; 1-year retention |
| **HIPAA** | All PHI access logged; 6-year retention |
| **PCI-DSS** | All cardholder data access logged; 1-year hot, 1-year cold |
| **GDPR** | All personal-data access logged; right-to-erasure tracked |
| **FedRAMP** | Comprehensive; OS-level audit |
| **ISO 27001** | Defined logging policy; reviewed annually |

### 11.2 The "who accessed customer X's data" query

The single most common compliance ask. Must be answerable in minutes.

```
SELECT actor, action, timestamp
FROM audit_logs
WHERE resource_id = 'customer-123'
  AND timestamp > '2026-01-01'
ORDER BY timestamp;
```

Build the data model so this is fast. Otherwise compliance-team requests stall for weeks.

### 11.3 The annual access review

For SOC2 etc.: annually, every privileged user's access is reviewed for justification. Audit logs feed this.

```
- list of users with admin role
- count of admin actions per user in the year
- for each, recent justification
```

The platform team provides the data; security/compliance owns the review.

### 11.4 The right-to-erasure intersection

GDPR right-to-erasure conflicts with security audit retention. Resolution: pseudonymize the user ID in audit logs; map table separately and erasable. Audit logs retain "user X did Y," but X can be unlinked from the actual person.

This is hairy. Get legal involved early.

---

## 12. Detection Engineering: Rules-as-Code

The modern security-team practice.

### 12.1 The discipline

Detection rules:
- Versioned in Git.
- Reviewed in PRs.
- Tested against historical data ("would this rule have fired on the breach we know about?").
- Deployed via CI.

### 12.2 The tools

- **Sigma:** open-source, vendor-neutral detection rule format.
- **Splunk Enterprise Security:** vendor.
- **Panther:** SQL-based, code-first.
- **Elastic Detection Engine:** rules in Elasticsearch.
- **Chronicle YARA-L:** Google's detection language.

### 12.3 The shape of a rule

```yaml
title: Suspicious SSH key write
id: 12345
status: stable
description: Detects writes to ~/.ssh/authorized_keys files
references:
  - https://attack.mitre.org/techniques/T1098/004/
tags:
  - attack.persistence
  - attack.t1098.004
logsource:
  product: linux
  service: file
detection:
  selection:
    file.path|contains: "/.ssh/authorized_keys"
    event.type: "write"
  condition: selection
fields:
  - user.name
  - file.path
  - process.name
falsepositives:
  - Legitimate SSH key updates by sysadmins
level: medium
```

### 12.4 The platform-team interface

Detection rules need data. The platform team provides:
- Schemas (what fields exist).
- SLAs on data freshness.
- Query performance.
- Retention.

Not the rules themselves.

---

## 13. Response: Paging the SOC vs Paging On-Call

The boundary at incident time.

### 13.1 Two distinct response paths

| Path | Trigger | Responder |
|---|---|---|
| **SRE on-call** | Service down, latency burn, capacity | Service team's primary |
| **SOC on-call** | Detection rule fires; suspected attack | Security analyst |

### 13.2 Where they meet

Some incidents are both: a DDoS is a security event AND a reliability event. Both teams page; coordinate.

The pattern: **separate Slack channels** (#incident-XYZ and #sec-incident-XYZ); single timeline; one Incident Commander (usually SRE for availability, security for breach).

### 13.3 The security incident has different rules

- Containment, not recovery, comes first. Cut off the attacker before restoring service.
- Communication is *more* restricted (don't tip the attacker).
- Forensic preservation is essential (don't delete evidence by restarting).
- Legal involvement may be required.

These differ from operational incidents. Platform on-call should hand off to security and *not improvise*.

### 13.4 The "is this security?" decision tree

A page fires. Triage:
- Does the alert come from a security rule? → SOC.
- Does the symptom match an attack pattern (data egress spike, anomalous logins)? → page SOC also.
- Otherwise: SRE.

When in doubt, page security alongside. Worst case: false alarm and SOC investigates briefly. Better than missing a breach.

---

## 14. Privacy of Security Telemetry Itself

The recursive concern.

### 14.1 What's sensitive in security logs

- User PII (the "who" of the 5 W's).
- IP addresses (PII in some jurisdictions).
- Session content.
- Query content.
- Investigation notes.

### 14.2 The access controls

- Audit logs accessible only to security and select platform engineers.
- Investigation notes locked further (only on the incident team).
- No cross-team browsing.
- Audit trail of audit-log queries (§3.5).

### 14.3 The "internal threat actor" defense

A platform engineer with access to security logs *is* a privileged role. Defenses:
- Two-person review for sensitive queries.
- All queries logged.
- Separation of duties (on-call SRE doesn't have raw security data access; SOC does).

### 14.4 The data-residency concern

Security logs may contain user data subject to data-residency laws. EU users' security logs in EU storage; etc. Often *easier* to comply with for security than ops, because security is centralized. But still requires care.

---

## 15. Anti-Patterns

1. **Platform team owns SOC.** Conflict of interest; expertise gap.
2. **No audit logs.** Forensics impossible.
3. **Audit logs without integrity.** Tampering invisible.
4. **No SIEM filter.** Storage explosion; compliance costs.
5. **Audit logs same retention as ops.** Compliance violation.
6. **No threat-intel integration.** Known-bad activity invisible.
7. **No anomalous-behavior baselines.** Lateral movement undetected.
8. **No MITRE coverage view.** Gaps invisible.
9. **No detection-as-code.** Rules drift; reviews skipped.
10. **No SOC + SRE coordination during overlap incidents.** Wires crossed.
11. **No right-to-erasure compatibility for audit logs.** GDPR violation.
12. **Egress unmonitored.** Exfiltration invisible.
13. **eBPF security agent missing.** Runtime threats undetected.
14. **Audit-log access uncontrolled.** Internal-threat exposure.
15. **No rule-testing pipeline.** Detection regression invisible.

---

## 16. Worked Example: A Platform-SOC Integration

Concrete and complete.

### 16.1 The org

- 200 services on EKS.
- SRE platform team owns observability stack (Mimir + Loki + Tempo).
- Security team runs Splunk Enterprise.
- Compliance: SOC2 + HIPAA (some workloads).

### 16.2 The pipelines

**Audit log pipeline:**
- Apps emit structured audit events to a dedicated topic.
- Collector signs each event with HMAC at ingestion.
- Stored in a write-once S3 bucket (Object Lock; 7-year retention).
- Replicated to Splunk for analyst access.

**Operational log pipeline:**
- Apps emit structured logs to standard collector.
- Hot tier in Loki (30 days).
- Sampled stream to Splunk (security-relevant logs only).

**Network telemetry:**
- VPC flow logs to S3.
- Cilium Hubble flow data to dedicated topic.
- Both indexed in Splunk.

**Authentication telemetry:**
- Okta event API → collector → Splunk.
- All login / authz events captured.

### 16.3 The SIEM detections

Security team owns ~250 rules in Sigma format. Examples:
- Failed login spike per user.
- Authz denials spike.
- New service-to-service edge in mesh.
- Data egress > baseline.
- SSH key write on sensitive nodes.
- Privileged container starts.

### 16.4 Platform-team's role

- Maintain the pipelines.
- SLOs on telemetry freshness (audit events to Splunk in < 60s).
- SLO on retention (no audit log lost).
- Provide schemas + sample queries.
- Quarterly review with security: data needs, gaps, costs.

### 16.5 The compliance audit

Annual SOC2 audit asks:
- Are admin actions logged? — yes, audit log + retention proof.
- Are access reviews done? — yes, quarterly via Okta + audit-log queries.
- Is monitoring effective? — yes, MITRE coverage report.

Platform team delivers data. Security team delivers narrative.

### 16.6 The incident

A service is hit by credential stuffing. Auth logs spike. SOC's rule fires. SOC on-call investigates; coordinates with SRE on-call (the affected service is also experiencing latency due to auth-load). Both contain; both write postmortem; security postmortem includes incident-specific evidence preservation.

The platform-team-built substrate enabled the response.

---

## 17. Pitfalls

1. **Audit logs missing.** Forensics impossible.
2. **No tamper-evidence.** Integrity unknown.
3. **No long-term retention for audit.** Compliance fail.
4. **No SIEM integration.** Security blind.
5. **No threat-intel.** Known-bad not flagged.
6. **No MITRE mapping.** Coverage unknown.
7. **No detection-as-code.** Quality drift.
8. **Platform owns SOC.** Conflict and gap.
9. **No SOC-SRE coordination.** Confusion at incident time.
10. **Audit-log access uncontrolled.** Insider risk.
11. **No anomaly baselines.** Behavioral detection missing.
12. **Egress unobserved.** Exfiltration invisible.
13. **No GDPR compatibility on audit logs.** Right-to-erasure broken.
14. **eBPF security missing.** Runtime threats invisible.
15. **No annual review of telemetry needs.** Drift.

---

## 18. Mental Models

> **The platform team is the SOC's data supplier; not its delegate.**

> **Audit logs answer the 5 W's: who, what, when, where, why.**

> **Audit logs are append-only and tamper-evident. Or they're not audit logs.**

> **Detection ≠ response. Different teams; different processes.**

> **MITRE ATT&CK is the lingua franca. Map detections to it.**

> **eBPF is to security what it is to networking: a step-change in observability.**

> **SIEM filter is mandatory; full-stream is unaffordable.**

> **Right-to-erasure intersects with audit retention. Pseudonymize.**

> **Containment-first for security incidents; recovery-first for reliability.**

> **Detection-as-code is the discipline. Rules in Git; reviewed; tested.**

Now go to `doc 28` (telemetry pipeline reliability) — observing the observer, the most under-instrumented system in your stack.
