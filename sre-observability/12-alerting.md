# 12 — Alerting: From Threshold Spam to Pages That Mean Something

> Alerts are *the* most expensive object an observability platform produces. Every page wakes a human, and a woken human at 3 AM costs the company more than the entire month of telemetry storage that produced the alert. Yet most stacks treat alert rules as the cheapest object — engineers add them in PRs without review, never delete them, and tune by escalating verbosity ("now also page on 90% CPU"). This chapter is the Staff-Engineer view: alerts as a *budget-bound* product the platform team owns.

This is chapter 12. It assumes the vocabulary in `doc 00` (page vs alert vs ticket, MTTM, burn rate), the metrics-store internals in `doc 06`, and the consumption-layer overview in `doc 11`. The next chapter, `doc 13`, makes alert rules *generated from SLOs* rather than hand-written — that's the destination. This chapter is the engine.

---

## Table of Contents

1. [The thesis: alerts are products, not afterthoughts](#1-thesis)
2. [The Alertmanager architecture (and what it actually does)](#2-alertmanager-architecture)
3. [Rule evaluation: how a `PromQL` becomes a page](#3-rule-evaluation)
4. [The alerting state machine](#4-state-machine)
5. [Multi-window multi-burn-rate: the only good alerting pattern](#5-mwmbr)
6. [Symptom vs cause alerting](#6-symptom-vs-cause)
7. [Routing, grouping, deduplication](#7-routing)
8. [Inhibition, silencing, maintenance windows](#8-inhibition-silencing)
9. [Alert hygiene: the four-question audit](#9-alert-hygiene)
10. [The alert-as-code stack](#10-alert-as-code)
11. [Notification channels and the chain of custody](#11-notification-channels)
12. [Page hygiene: SLOs for alerts themselves](#12-page-hygiene)
13. [Anti-patterns and how to delete them](#13-anti-patterns)
14. [Grafana alerting vs Alertmanager (2026 state)](#14-grafana-vs-alertmanager)
15. [Worked example: an end-to-end checkout-error rule](#15-worked-example)
16. [Pitfalls](#16-pitfalls)
17. [Mental models](#17-mental-models)

---

## 1. Thesis

Three sentences a Staff Engineer should be able to defend in any forum:

1. **Every page is a question.** "Is this user-impacting? Is the on-call needed? Is action available?" If any answer is "no," the alert should not page — it should ticket, or be deleted.
2. **Alert rules have an SLO of their own.** *Precision* (a page should be real), *recall* (a real outage should page), *MTTA* (the page reaches a human fast). The platform team owns these like any other SLO.
3. **The right number of alerts is much smaller than you think.** A team with 20 services should run *fewer* than 20 paging alerts in steady state — most pages should come from a small set of generated SLO-burn-rate rules, not hand-written per-service thresholds.

If this seems aggressive: walk into a typical mature platform and audit the on-call. You will find dozens of rules nobody can attribute to a specific user impact, alerts that fire weekly with no action, and an on-call who has stopped reading 80% of pages within 90 seconds. That is the disease. This chapter is the cure.

---

## 2. The Alertmanager Architecture

Before any rule, understand the moving parts. **Alertmanager** is Prometheus's purpose-built alert router, but every modern alerting system has analogous components — Grafana Alerting, AWS AMP alertmanager, Pagerduty's rules engine, Mimir-Alertmanager, etc. The names differ; the responsibilities don't.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                        ALERTING SYSTEM ANATOMY                                │
│                                                                               │
│  ┌────────────────────┐                                                       │
│  │  RULE EVALUATOR    │   (Prometheus, Mimir-ruler, Grafana alert engine)     │
│  │                    │   evaluates expressions every `interval` (15s default)│
│  │   record(...) →    │   against the metric store; emits firing/resolved    │
│  │   alert(...)       │   alerts; stamps each with labels and annotations    │
│  └─────────┬──────────┘                                                       │
│            │ HTTP POST /api/v2/alerts                                         │
│            ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────────┐          │
│  │                   ALERTMANAGER (CLUSTER)                        │          │
│  │  ┌─────────────┐ ┌──────────────┐ ┌─────────────┐ ┌──────────┐ │          │
│  │  │ DEDUP +     │ │ GROUPING     │ │ INHIBITION  │ │ SILENCING│ │          │
│  │  │ STORE       │ │ (by labels)  │ │ (rule-based │ │ (manual, │ │          │
│  │  │             │ │              │ │  suppress)  │ │  windowed│ │          │
│  │  │ 1 alert per │ │ 1 notif per  │ │ "if A fires │ │ "mute    │ │          │
│  │  │ unique      │ │ group, not   │ │  silence B" │ │  this    │ │          │
│  │  │ fingerprint │ │ per alert    │ │             │ │  for 2h" │ │          │
│  │  └─────────────┘ └──────────────┘ └─────────────┘ └──────────┘ │          │
│  │                              │                                  │          │
│  │  ┌─────────────────────────────────────────────────────────┐    │          │
│  │  │  ROUTING TREE (label matchers → receiver)                │   │          │
│  │  │   match team=payments → receiver=payments-pagerduty     │   │          │
│  │  │   match severity=warn → receiver=#slack-on-call          │   │          │
│  │  └─────────────────────────────────────────────────────────┘   │          │
│  │                              │                                  │          │
│  │   ┌──────────┬───────────┬───┴────────┬───────────┬────────┐    │          │
│  │   ▼          ▼           ▼            ▼           ▼        ▼    │          │
│  │ PagerDuty  Opsgenie    Slack       Email       Webhook  Teams   │          │
│  │  (paging) (paging)  (informational)            (custom)         │          │
│  │                                                                 │          │
│  │  HA: 3 replicas, gossip-based dedup, "send only once" guarantee │          │
│  └─────────────────────────────────────────────────────────────────┘          │
└───────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 The hard part is dedup

Three Prometheus instances scrape the same target. All three evaluate the same rule. All three send "alert firing" to Alertmanager. **Alertmanager must dedupe** so on-call gets one page, not three. The mechanism is *fingerprinting*: a SHA1 of the alert's label set serves as the unique key. Identical fingerprints from different senders within `group_interval` collapse to one notification.

In the HA topology (3 Alertmanager replicas behind a Service), each replica receives all alerts from all senders. Replicas gossip via memberlist; only one elected for each notification actually sends to the receiver. The election uses a hash of the fingerprint mod replica-count, with replicas falling back if the chosen one is unhealthy.

> **Pitfall:** If you run 3 Alertmanager replicas without configuring `--cluster.peer` correctly, each replica thinks it's alone. On-call gets 3 pages per alert. This is the single most common Alertmanager bug.

### 2.2 Stateless vs stateful

Alertmanager keeps state: silences (a 2-hour mute), pending groups, notification logs. State is replicated via gossip but not durable across full cluster restart. **For long-term silences and audit, ship every notification to a side-effect log** (a Kafka topic or a database) — Alertmanager's own state shouldn't be your audit trail.

---

## 3. Rule Evaluation

A rule is a `PromQL` expression evaluated at a fixed interval. When the expression returns *any sample*, the corresponding alert is *active*. When the sample disappears, the alert *resolves*. Two flavors:

```yaml
# Recording rule — pre-computes a metric for re-use
- record: job:http_requests:rate5m
  expr: sum by (job) (rate(http_requests_total[5m]))

# Alerting rule — fires if expr is non-empty
- alert: HighErrorRate
  expr: |
    sum by (service) (rate(http_requests_total{code=~"5.."}[5m]))
      /
    sum by (service) (rate(http_requests_total[5m])) > 0.05
  for: 5m
  labels:
    severity: page
    team: payments
  annotations:
    summary: "5xx error rate > 5% on {{ $labels.service }}"
    runbook: "https://runbooks.example.com/high-error-rate"
    dashboard: "https://grafana.example.com/d/payments-red"
```

Two non-obvious mechanics:

### 3.1 The `for:` clause is dwell-time, not delay

`for: 5m` means *the expression must be continuously true for 5 minutes before the alert leaves "pending" and enters "firing."* Rule evaluation happens every `interval` (default 15s); the `for:` clause counts consecutive non-empty results.

If a single 15-second blip clears the expression, the `for:` timer resets. This prevents flapping alerts on transient one-sample errors but has a cost: **alerts have at minimum `for + group_wait` latency to first page.** If `for: 5m` and `group_wait: 30s`, your fastest-possible page is 5m30s after the event begins.

### 3.2 Recording rules are not optional at scale

A complex alert expression (joins, percentiles, ratios) re-evaluated every 15s on a multi-million-series store will eat the rule evaluator. **Recording rules pre-compute the expensive bits** and store them as new series; the alert rule then reduces to a cheap comparison.

```yaml
# expensive — evaluated each scrape
- alert: SlowEndpoints
  expr: |
    histogram_quantile(0.99,
      sum by (le, service) (rate(http_request_duration_seconds_bucket[5m]))
    ) > 1.0

# cheaper — pre-compute the p99 once, reuse in many alerts
- record: service:http_request_duration:p99_5m
  expr: |
    histogram_quantile(0.99,
      sum by (le, service) (rate(http_request_duration_seconds_bucket[5m]))
    )

- alert: SlowEndpoints
  expr: service:http_request_duration:p99_5m > 1.0
```

The recording rule runs once per scrape interval and is reused by N alert rules and dashboard panels. Recording rules are also how SLO compilers (Sloth, Pyrra) work — they generate dozens of recording rules per SLO so all derived alerts run cheaply.

---

## 4. The Alerting State Machine

Every alert moves through this finite state machine. Memorize it; it is the source of half of "why didn't it fire?" mysteries.

```
            ┌───────────┐
            │  inactive │  ←──────────────────────────┐
            └─────┬─────┘                              │
       expr non-empty                                   │
                  │                                    │
                  ▼                                    │
            ┌───────────┐  expr empty before `for:`   │
            │  pending  │ ─────────────────────────────┤
            └─────┬─────┘                              │
       `for:` elapsed                                  │
                  │                                    │
                  ▼                                    │
            ┌───────────┐                              │
            │  firing   │ ─── notification sent ───┐  │
            └─────┬─────┘                          │  │
                  │ expr becomes empty             │  │
                  ▼                                │  │
            ┌───────────┐                          │  │
            │ resolved  │ ─── resolved notif ──────┘  │
            └─────┬─────┘                              │
                  └──────────────────────────────────────┘
                       (after `resolve_timeout`)
```

Notable subtleties:

- **`pending` is invisible to receivers.** A 4-minute outage that resolves at 4m59s with `for: 5m` *never pages*. That's by design — the `for:` clause is the anti-flap. A by-product is that fast-but-resolve outages can slip through.
- **A "resolved" notification is itself a notification.** Some receivers (PagerDuty) consume it to auto-close the incident. Others (older Slack integrations) treat it as a separate message — leading to the "page resolved itself but on-call still got 5 messages" complaint.
- **Stale alerts.** If a rule evaluator dies, alerts it was firing become *stale*. Alertmanager's `resolve_timeout` (default 5m) defines when stale firing alerts auto-resolve. Tuning matters: too short means brief evaluator hiccups produce false-resolves; too long means real resolutions are reported late.

---

## 5. Multi-Window Multi-Burn-Rate

The single most important alerting pattern in modern SRE. If you take only one technique from this chapter, take this one.

### 5.1 Why thresholds break

Traditional alert: `p99_latency > 500ms for 5m`.

Failure modes:
1. **On every deploy.** New code restarts; tail latency briefly spikes; pages on every Tuesday afternoon.
2. **On rare slow customer.** One whale customer's request is genuinely slow; pages even though service is healthy for 99.9% of users.
3. **No relation to user impact.** 500ms might be fine for one endpoint, terrible for another. Threshold doesn't know.
4. **Under-pages slow burns.** A 6-hour gradual degradation that never hits 500ms but drains the error budget. No page. Customers leave.

The fix: alert on **burn rate against the SLO**, with two windows.

### 5.2 The burn-rate definition

```
SLO         = 99.9% (allowed bad fraction = 0.001)
Window      = 30 days
Total events= traffic × seconds in window
Allowed bad = total × 0.001

Steady-state allowed-bad rate = (allowed bad) / (window seconds)

Burn rate = current bad rate / steady-state allowed rate
```

A burn rate of **1** means the budget is being consumed exactly on schedule. A burn rate of **14.4** means the 30-day budget would be exhausted in `30 days / 14.4 = 50 hours`. Burn rate of **36** = budget gone in 20 hours. Burn rate is a unitless multiplier — that's what makes it composable across services with different SLOs.

### 5.3 The Google four-window pattern

From *Implementing SLOs* (Beyer et al., 2018). Four alert rules, two of which page, all using the same burn-rate primitive.

| Severity | Long window | Short window | Burn rate threshold | % budget consumed when fires |
|---|---|---|---|---|
| **Page (fast burn)** | 1 hour | 5 minutes | ≥ 14.4 | 2% |
| **Page (slow burn)** | 6 hours | 30 minutes | ≥ 6 | 5% |
| **Ticket** | 3 days | 6 hours | ≥ 1 | 10% |
| **Ticket (long-burn)** | 14 days | (none) | ≥ 1 | already over |

Why two windows per page? **The short window is the anti-stale guard.** If the long-window burn rate is high *only* because of an event that already ended, you've alerted on history. Requiring the short window to also be hot ensures the burn is *currently happening*. Without the short window, alerts fire after recovery and confuse on-call.

### 5.4 The PromQL

```yaml
# Pre-compute burn rate per window
- record: slo:checkout:burn_rate_5m
  expr: |
    (
      sum(rate(checkout_errors_total[5m]))
        /
      sum(rate(checkout_requests_total[5m]))
    ) / 0.001    # 0.001 = (1 - 99.9% SLO)

- record: slo:checkout:burn_rate_1h
  expr: |
    (sum(rate(checkout_errors_total[1h])) / sum(rate(checkout_requests_total[1h]))) / 0.001

- record: slo:checkout:burn_rate_30m
  expr: |
    (sum(rate(checkout_errors_total[30m])) / sum(rate(checkout_requests_total[30m]))) / 0.001

- record: slo:checkout:burn_rate_6h
  expr: |
    (sum(rate(checkout_errors_total[6h])) / sum(rate(checkout_requests_total[6h]))) / 0.001

# Page rules
- alert: CheckoutFastBurn
  expr: |
    slo:checkout:burn_rate_1h >= 14.4
    and
    slo:checkout:burn_rate_5m >= 14.4
  for: 2m
  labels: { severity: page, slo: checkout }
  annotations:
    summary: "Checkout SLO burning fast: 2% budget in last hour"

- alert: CheckoutSlowBurn
  expr: |
    slo:checkout:burn_rate_6h >= 6
    and
    slo:checkout:burn_rate_30m >= 6
  for: 15m
  labels: { severity: page, slo: checkout }
```

### 5.5 The math nobody shows you

Why exactly **14.4** and **6**? The thresholds are derived from "burn this fraction of the budget in this window."

```
Threshold = (acceptable_fraction_of_budget × full_budget_period) / window

For 1h fast-burn at 2% budget:
  threshold = (0.02 × 30 days) / 1 hour
            = (0.02 × 720 hours) / 1 hour
            = 14.4

For 6h slow-burn at 5% budget:
  threshold = (0.05 × 720 hours) / 6 hours
            = 6.0
```

This generalizes to any SLO window. For a 28-day SLO, the thresholds shift slightly. The Sloth/Pyrra generators do this math automatically — you supply the SLO and they generate the four rules with correct thresholds.

---

## 6. Symptom vs Cause Alerting

Symptom = user impact. Cause = the upstream resource state. **Always alert on symptom; *use* cause as diagnosis.**

### 6.1 Why

A common anti-pattern: page on `disk_used_pct > 80`. Justification: "if disk fills, we crash." But:
- Disk at 81% with no growth = false page.
- Disk at 70% growing fast = real problem, no page.
- Disk doesn't fill but a different service has the outage = the page on disk distracts.

The right pattern: page on `service_error_rate burning SLO`, *and provide a runbook step that says "check disk usage on dependent stores."* You alert on what users feel; you provide a causal diagnosis path on the runbook.

### 6.2 The exception: leading-indicator pages for human-paced problems

Some causes are *worth paging on directly* because:
1. They are deterministic predictors (not "might cause" — *will* cause).
2. They have a long fuse (hours of advance warning).
3. They auto-resolve no other way.

Examples:
- **Certificate expiry < 7 days.** Will cause an outage; nothing else catches it; deterministic.
- **Backup last successful > 36 hours.** RPO violation; failure invisible until disaster.
- **Replica lag > 30 minutes.** Downstream queries return stale data; no symptom yet, but data integrity breaking.

These are *ticket-worthy*, often *page-worthy* in some orgs. The test: *will the user impact happen, and is there a confident time-bound prediction?* If yes, page on cause is acceptable.

---

## 7. Routing, Grouping, Deduplication

Alertmanager's routing tree is the policy layer that turns "alerts firing" into "humans paged."

### 7.1 The routing tree

```yaml
route:
  receiver: default-slack
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity="page"]
      receiver: pagerduty-primary
      continue: true            # also evaluate further routes
      routes:
        - matchers: [team="payments"]
          receiver: pagerduty-payments
        - matchers: [team="data"]
          receiver: pagerduty-data
    - matchers: [severity="warning"]
      receiver: slack-warnings
    - matchers: [severity="info"]
      receiver: slack-info
```

The tree is evaluated top-down. First match wins, *unless* `continue: true` is set, in which case routing also continues down. A common mistake is mis-ordering so a more specific match never gets reached.

### 7.2 Grouping

`group_by` defines what alerts collapse together into a single notification. If 50 pods in a service all hit OOM at the same time, you want **one** page that says "checkout, 50 pods OOM," not 50 pages.

```yaml
group_by: [alertname, cluster, service]
```

Means: alerts sharing those three label values become one notification. The notification body lists the per-instance details.

`group_wait` (e.g., 30s) is the **first-notification delay**: when an alert *first* enters a new group, wait this long before notifying — to gather more alerts that might fall into the same group. Keeps a 50-pod failure as one page even if pods cascade over 30s.

`group_interval` (e.g., 5m) is the **subsequent-notification delay**: how long to wait before sending a new notification for the same group when *new alerts* join.

`repeat_interval` (e.g., 4h) is the **re-page delay**: if the group is still firing 4 hours later, page again (in case on-call missed the first one).

> **Mental model:** group_wait is the "wait for stragglers" timer; group_interval is the "don't spam mid-incident" timer; repeat_interval is the "make sure they didn't sleep through it" timer.

### 7.3 Inhibition

Suppress alert X when alert Y is firing. Used to prevent cascade-storms.

```yaml
inhibit_rules:
  - source_matchers: [alertname="ClusterDown"]
    target_matchers: [severity=~"page|warning"]
    equal: [cluster]
```

Reads: *if* `ClusterDown` is firing on cluster `prod-east`, suppress all page/warning alerts also matching cluster `prod-east`. The on-call gets one page (`ClusterDown`) instead of 200.

Inhibition is the single most underused Alertmanager feature. Most cascade outages page 50 services because nobody set up "if upstream is dead, suppress downstreams" rules.

### 7.4 Silencing

Time-bound, manual mute. Used for:
- Planned maintenance windows.
- Known-issue suppression while a fix is being deployed.
- "We know, stop paging us, this is a Tuesday deploy."

```yaml
matchers:
  - alertname=~"Checkout.*"
  - cluster="prod-east"
startsAt: 2026-05-05T13:00:00Z
endsAt:   2026-05-05T15:00:00Z
createdBy: alice@example.com
comment:   "Checkout deploy window — see ticket REL-1234"
```

Silences expire. They are *not* a substitute for fixing the alert. **A silence renewed >1× without action is a smell.** Audit silences quarterly; convert long-running ones into rule changes or deletions.

---

## 8. Inhibition vs Silencing vs Maintenance

Three superficially similar tools. Use them for different reasons.

| Tool | Trigger | Scope | Lifetime | Audit |
|---|---|---|---|---|
| **Inhibition** | An alert fires | Other alerts matching condition | While source alert fires | Static rule (in code) |
| **Silencing** | Manual | Matching alerts | Time-bounded | Per-silence record |
| **Maintenance window** | Scheduled | Often a whole service | Time-bounded | Calendar / change log |

Use **inhibition** for cascade suppression (always-on, code-reviewed). Use **silencing** for one-off "we know" suppressions. Use a **maintenance window concept** (often layered above silencing in incident-management tools like incident.io or FireHydrant) for planned work.

---

## 9. Alert Hygiene: The Four-Question Audit

Apply this audit to every alert quarterly. Delete or de-page anything that fails.

```
For each alert:

1. ACTIONABLE?     If it fires, is there a specific human action that
                    must happen now?                          [Yes/No]

2. URGENT?          Must that action happen within minutes,
                    not hours?                                 [Yes/No]

3. ATTRIBUTABLE?    Does the team paged actually own the
                    failing component?                         [Yes/No]

4. INFORMATIVE?     Does the alert message tell the on-call
                    what's wrong, what to do first, and where
                    to look (runbook + dashboard linked)?      [Yes/No]

Score:
  4/4  =  good page
  3/4  =  fix the missing dimension
  2/4  =  demote to ticket
  ≤1/4 =  delete
```

In a typical pre-audit stack, **30–50% of alerts score ≤ 2/4.** Deleting them is not loss — it is recovery of the on-call's attention.

### 9.1 The "did it fire? did it lead to action?" log

Maintain a quarterly per-alert log:

```
alert_name           fires_q  acks  led_to_action  category
HighErrorRate          47      47       12         retain
CertExpiry              3       3        3         retain
DiskUsageHigh          22      22        0         delete (no action ever)
PodRestartedFast       180    180        0         delete (false positive)
KafkaLag5m             30      30       30         retain
ProxyHighLatency      120    104        2         demote to ticket
```

The numbers tell you which to keep. *Lots of fires + few actions = delete or retune.*

### 9.2 Alerts as platform SLIs

Two platform SLIs every observability team should publish:

- **Page actionability rate** = `pages_with_action / total_pages`. Target ≥ 80%.
- **Mean pages per on-call shift.** Target ≤ 3 (sustained). Above this, on-call fatigue cascades.

Track these monthly. They are the single most honest measure of platform-team health.

---

## 10. Alert-as-Code

Alert rules belong in Git. Period. The reasons:

- **Review.** Every rule change goes through PR review like any code.
- **Diff.** "Did anyone change this rule before the incident?" is a `git log` away.
- **Test.** `promtool test rules` lets you write *unit tests* against alert expressions.
- **Generate.** SLO compilers (Sloth, Pyrra) emit rule files; these go in the same repo.
- **Roll back.** If a rule change paged 3× as many alerts, revert is one PR.

### 10.1 Layout

```
alerts/
  README.md
  generated/
    slo-checkout.yaml         # generated by Sloth
    slo-search.yaml
  manual/
    cert-expiry.yaml
    cluster-down.yaml
    inhibition-rules.yaml
  tests/
    slo-checkout.test.yaml    # promtool test rules
    cert-expiry.test.yaml
```

### 10.2 Test example

```yaml
# alerts/tests/cert-expiry.test.yaml
rule_files:
  - ../manual/cert-expiry.yaml

tests:
  - interval: 1m
    input_series:
      - series: 'cert_expiry_seconds{service="api"}'
        values: '604800-60x10'    # starts 7 days, decreases each step
    alert_rule_test:
      - eval_time: 5m
        alertname: CertExpiringSoon
        exp_alerts:
          - exp_labels:
              service: api
              severity: page
            exp_annotations:
              summary: "TLS cert for api expires in <7d"
```

Run on every PR via CI. The number of regressions caught by these tests is non-trivial — the *PromQL* type system doesn't catch "I rate'd a counter that's not actually a counter."

---

## 11. Notification Channels and the Chain of Custody

A notification is only as good as the chain that delivers it. Failure modes:

| Stage | Failure | How to detect |
|---|---|---|
| Rule evaluator | Down, lagging | `up{job="prometheus"}`, rule eval lag metric |
| Alertmanager | Down, gossiping wrong | `alertmanager_cluster_members`, side-channel canary |
| Receiver (PagerDuty etc.) | API down, rate-limited | Receiver-side delivery confirmation, periodic synthetic |
| Notification network | SMS, voice, push | Synthetic test page once a week per receiver |
| Human | Asleep, on PTO, phone off | Escalation policy with timeouts |

### 11.1 The synthetic page test

Once a week, an automated test page fires through the *full chain* — rule evaluator → Alertmanager → PagerDuty → on-call's phone. The on-call must ack within N minutes; if not, escalates. The exercise:

- Verifies the chain end-to-end.
- Catches stale on-call schedules, expired API keys, mis-routed services.
- Trains on-call on the ack flow without a real-incident penalty.

Skipping this is how teams discover their pager has been broken for two months *during* an outage.

### 11.2 Severity convention

Use exactly two levels for *automated routing*:

- **`severity=page`** → wakes a human, off-hours capable.
- **`severity=ticket`** → goes to a queue, reviewed during business hours.

Avoid `critical / major / minor / warning / info` — five levels ends with pagers tuned to "critical only," with `major` and `minor` going unread. Two levels are unambiguous.

You can layer richer metadata in *labels* (`team`, `slo`, `tier=1`) for routing precision; the *trigger* dimension stays binary.

---

## 12. Page Hygiene: SLOs for Alerts Themselves

The platform team owns alert rules as a *product*. Like any product, it has SLOs.

### 12.1 The four alert SLOs

| SLI | Definition | Reasonable target |
|---|---|---|
| **Precision** | `pages with real impact / total pages` | ≥ 80% |
| **Recall** | `real outages caught by alerts / real outages` | ≥ 95% |
| **MTTA** | Median ack time | ≤ 5 minutes |
| **Page rate** | Pages per on-call shift | ≤ 3 |

These can and should be **dashboards** in your platform's own observability:

- `precision = sum(actionable_pages) / sum(all_pages)` — labeled by on-call after the fact.
- `recall = 1 - (missed_outages / total_outages)` — measured from postmortems.
- `MTTA` from PagerDuty's API.
- `page_rate` from PagerDuty + on-call schedule.

Treat regressions on these as platform-team incidents. A 50% precision alert is a *bug*, not a tuning question.

### 12.2 The on-call survey

Once per quarter, survey on-call engineers:

- "Did you sleep through any pages this rotation?" (page volume signal)
- "How many pages required no action?" (precision signal)
- "How many pages had a useful runbook?" (annotation hygiene signal)
- "Was there an outage you only learned about from a customer?" (recall signal)

Survey results are the single richest qualitative signal on alert health. Aggregate quarterly; act on the top three complaints; close the loop in the team's own retro.

---

## 13. Anti-Patterns and How to Delete Them

A field guide. Each pattern is real; each fix is concrete.

### 13.1 "Threshold escalation"

Symptom: every quarter someone adds a stricter threshold ("now also at 70%, not just 80%").

Fix: convert to SLO + multi-window multi-burn-rate. The SLO target *is* the threshold; the burn rate auto-tunes urgency.

### 13.2 "Mystery alert"

Symptom: rule fires, on-call doesn't recognize it, no runbook, no team in labels.

Fix: every alert *must* have `team` label, `runbook` annotation, `summary` annotation. Enforce in CI.

### 13.3 "Notification spam"

Symptom: 20 pages from one outage.

Fix: review `group_by` and inhibition. Almost always undergrouped, undersilenced. Inhibition rule for upstream → downstream.

### 13.4 "Permanent silence"

Symptom: silence renewed quarterly; alert never deleted.

Fix: silence age > 30 days = automatic ticket. Either fix the alert (delete, retune, runbook the false positive) or accept it's permanent (delete).

### 13.5 "Death by deploy"

Symptom: alerts fire on every deploy.

Fix: the alert is not anti-flap. Add `for:` clause, switch to ratio/burn-rate, or coordinate with deploy markers (annotations) so alerts during a window are downgraded.

### 13.6 "Blast-radius surprise"

Symptom: a noisy alert affecting 20 services pages once per service. Cascade.

Fix: consolidate into one rule with `group_by: [cluster]` instead of `[service]`. Use `inhibit_rules` to silence downstream alerts when an upstream incident is firing.

### 13.7 "Silent failure"

Symptom: real outage goes un-paged. "We had no alert for that."

Fix: every postmortem ends with "what alert would have caught this?" and *that alert is added before the postmortem is closed*. Treat unmonitored failure modes as the highest-priority gap.

### 13.8 "Dashboard alerts"

Symptom: alerts evaluating against UI-only data (Grafana panels), not against the underlying metrics. UI breaks → alert fails silently.

Fix: alert rules live in `Prometheus`/`Mimir` rule evaluators, not in Grafana. Grafana alerts are acceptable only when the rule evaluator is Mimir-ruler or Grafana-Cloud-managed (where the rule isn't actually "in the UI").

### 13.9 "P95-latency-on-CPU"

Symptom: alerting on `node_cpu_seconds_total{mode!="idle"}` averaged across the cluster.

Fix: this is cause-alerting. Replace with the *symptom* (latency or error rate) that the CPU is allegedly causing. Keep CPU on a USE dashboard, not in a paging rule.

### 13.10 "Quarterly delete"

Antidote: scheduled, non-negotiable: every quarter, the on-call champion runs the four-question audit (§9), proposes deletions, and merges them. *Add* permission requires more justification than *delete* permission. Without scheduled deletion, alert count grows monotonically; with it, the platform stays sharp.

---

## 14. Grafana Alerting vs Alertmanager (2026 State)

Two competing models in the same ecosystem. Pick deliberately.

| Dimension | Prometheus + Alertmanager | Grafana Unified Alerting |
|---|---|---|
| **Rule eval engine** | Prometheus or Mimir-ruler | Grafana's own evaluator |
| **Multi-datasource rules** | Hard (need recording rules to bridge) | Native (one rule across Prom + Loki + Tempo) |
| **Storage of rules** | YAML files in Prometheus / Mimir | Grafana DB (also exportable as YAML / Terraform) |
| **HA model** | Prometheus + Alertmanager cluster | Grafana HA (Mimir-Alertmanager backend) |
| **Notification routing** | Alertmanager | Grafana's policy tree (compatible model) |
| **Rule-as-code workflow** | Excellent (file-based) | Terraform or grizzly; less mature |
| **Multi-tenancy** | Excellent (Mimir-Alertmanager) | Good (tenants → orgs) |
| **Best for** | Pure Prom/Mimir shop, rules-as-code culture | Grafana-Cloud-first or multi-datasource alerting |

The 2026 trend: Grafana Unified Alerting is the default in Grafana-Cloud and Grafana-stack shops; Alertmanager remains the default in pure Mimir/VictoriaMetrics shops with strong rules-as-code culture. Both stacks ultimately ship to the same delivery channels (PagerDuty, Slack, Opsgenie). Which one runs the eval is a deployment-tier decision; don't let it become a religion.

---

## 15. Worked Example: An End-to-End Checkout-Error Rule

The `/checkout` service. SLO: 99.9% of requests return 2xx within 500ms over 28 days.

### 15.1 Recording rules

```yaml
- record: checkout:requests:rate1m
  expr: sum(rate(checkout_requests_total[1m]))
- record: checkout:requests:rate5m
  expr: sum(rate(checkout_requests_total[5m]))
- record: checkout:requests:rate30m
  expr: sum(rate(checkout_requests_total[30m]))
- record: checkout:requests:rate1h
  expr: sum(rate(checkout_requests_total[1h]))
- record: checkout:requests:rate6h
  expr: sum(rate(checkout_requests_total[6h]))

- record: checkout:errors:rate1m
  expr: sum(rate(checkout_requests_total{outcome!="success"}[1m]))
- record: checkout:errors:rate5m
  expr: sum(rate(checkout_requests_total{outcome!="success"}[5m]))
- record: checkout:errors:rate30m
  expr: sum(rate(checkout_requests_total{outcome!="success"}[30m]))
- record: checkout:errors:rate1h
  expr: sum(rate(checkout_requests_total{outcome!="success"}[1h]))
- record: checkout:errors:rate6h
  expr: sum(rate(checkout_requests_total{outcome!="success"}[6h]))

- record: checkout:availability:burn5m
  expr: (checkout:errors:rate5m / checkout:requests:rate5m) / 0.001
- record: checkout:availability:burn30m
  expr: (checkout:errors:rate30m / checkout:requests:rate30m) / 0.001
- record: checkout:availability:burn1h
  expr: (checkout:errors:rate1h / checkout:requests:rate1h) / 0.001
- record: checkout:availability:burn6h
  expr: (checkout:errors:rate6h / checkout:requests:rate6h) / 0.001
```

### 15.2 Alert rules

```yaml
- alert: CheckoutAvailabilityFastBurn
  expr: |
    checkout:availability:burn1h >= 14.4 and checkout:availability:burn5m >= 14.4
  for: 2m
  labels:
    severity: page
    team: payments
    slo: checkout-availability
    journey: checkout
  annotations:
    summary: |
      Checkout availability SLO burning fast — 2% of 28d budget consumed in the last hour.
      Current 1h burn: {{ printf "%.1f" $value }}× normal.
    description: |
      Customers attempting checkout are seeing more errors than the SLO permits.
      Roll back the last deploy or enable the upstream-degraded kill-switch
      while triaging upstream causes.
    runbook: "https://runbooks.example.com/checkout-availability"
    dashboard: "https://grafana.example.com/d/checkout-red?from=now-2h"
    slo: "https://slo.example.com/checkout-availability"

- alert: CheckoutAvailabilitySlowBurn
  expr: |
    checkout:availability:burn6h >= 6 and checkout:availability:burn30m >= 6
  for: 15m
  labels:
    severity: page
    team: payments
    slo: checkout-availability
    journey: checkout
  annotations:
    summary: "Checkout SLO burning slow — 5% of 28d budget consumed in last 6 h."
    description: |
      Sustained, smaller-than-fast-burn but still over budget. Prioritize within the day.
    runbook: "https://runbooks.example.com/checkout-availability"
    dashboard: "https://grafana.example.com/d/checkout-red?from=now-12h"
```

### 15.3 Routing entry

```yaml
route:
  routes:
    - matchers: [team="payments", severity="page"]
      receiver: pagerduty-payments
      group_by: [alertname, slo, journey]
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
```

### 15.4 What the page reads like

```
[PAGE] CheckoutAvailabilityFastBurn — payments
Checkout availability SLO burning fast — 2% of 28d budget consumed in last hour.
Current 1h burn: 23.7× normal.

Customers attempting checkout are seeing more errors than the SLO permits.
Roll back the last deploy or enable the upstream-degraded kill-switch.

Runbook:    https://runbooks.example.com/checkout-availability
Dashboard:  https://grafana.example.com/d/checkout-red?from=now-2h
SLO:        https://slo.example.com/checkout-availability
```

The on-call sees: *what's wrong* (one sentence), *what to do first* (rollback), *where to look* (three links). Five-second comprehension at 3 AM. That is the bar.

---

## 16. Pitfalls

A consolidated list. Every one of these is real.

1. **Threshold-only alerting.** The single biggest source of alert fatigue. Replace with burn-rate.
2. **No `team` label.** Routing breaks; on-call doesn't know who owns it.
3. **No runbook annotation.** On-call wastes 10 minutes searching during the incident.
4. **`for:` too short.** Flapping on the slightest blip.
5. **`for:` too long.** Real outages page after 10 minutes of damage.
6. **Mean instead of percentile.** Tail latency invisible.
7. **Errors as count, not ratio.** Threshold breaks on traffic shift.
8. **Group_by overspecified.** 200 pods, 200 separate pages.
9. **Group_by underspecified.** 5 unrelated services collapse into one notification.
10. **Inhibition not used.** Cascade pages on every dependent service.
11. **Silence used as a fix.** Same silence renewed quarterly for years.
12. **No HA on Alertmanager.** Single replica = SPOF for the entire alerting plane.
13. **Cluster peer mis-configured.** 3 replicas → 3× notifications.
14. **No synthetic page test.** Pager broken for 2 months, undetected.
15. **Rules in Grafana panels.** UI reload breaks the alert.
16. **Severity inflation.** 5 severity levels, on-call only reads "critical."
17. **Alerts not in Git.** No review, no diff, no rollback.
18. **No CI tests on rules.** Regression: a label rename silently breaks a rule.
19. **Alerting on everything.** 200 alerts, 4 read.
20. **Never deleting alerts.** Quarterly hygiene is non-negotiable.

---

## 17. Mental Models

The compact, repeatable framings.

> **Every page is a question.** Three answers must all be "yes": real impact, on-call needed, action available. If any is "no," demote or delete.

> **Burn rate is the only universal alert primitive.** It's unitless, composes across services, and ties directly to error budgets. Everything else (thresholds, anomalies) is a special case.

> **Alert on symptoms; diagnose with causes.** Symptom in the rule; cause in the runbook. Otherwise the day the cause changes, the alert breaks.

> **The right alert count is small.** Most production stacks have 5–20 paging alerts per team. If yours has 80, you have a hygiene debt, not a sophistication advantage.

> **Alerts are a product. They have SLOs. The platform team owns them.** Precision, recall, MTTA, page rate. Track these like any other SLI.

> **Two levels: page or ticket.** The trigger dimension is binary. Severity beyond that goes in labels.

> **Alert-as-code is non-negotiable at scale.** Git, review, CI tests, generated rules from SLOs. Anything else doesn't scale past ~20 services.

Now go to `doc 13`. SLOs make the rules in this chapter *generated* rather than hand-written — and that is the destination.
