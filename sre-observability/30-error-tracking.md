# 30 — Error Tracking

> Error tracking is its own discipline — separate from logs, metrics, and traces. An error tracker groups events by stack-trace fingerprint, tracks release health, dedupes intelligently, and surfaces *new errors* with surgical precision. Treating errors as just another log type loses 90% of the value.

This chapter is about Sentry, Rollbar, Bugsnag, Honeybadger, and the surrounding patterns. The 2026 trend is convergence with tracing (one platform for both); the discipline remains distinct.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why error tracking is distinct from logs](#2-distinct-from-logs)
3. [The grouping algorithm](#3-grouping)
4. [Release health: regressions and new errors](#4-release-health)
5. [Source maps and symbolication revisited](#5-source-maps)
6. [Mobile crash reporting specifics](#6-mobile)
7. [Backend exception tracking](#7-backend)
8. [Linking errors to traces and users](#8-linking)
9. [Sampling errors (you usually shouldn't)](#9-sampling)
10. [Error tracking SLOs](#10-slos)
11. [The PII concern](#11-pii)
12. [Tools (2026 landscape)](#12-tools)
13. [Anti-patterns](#13-anti-patterns)
14. [Worked example: error tracking for a polyglot stack](#14-worked-example)
15. [Pitfalls](#15-pitfalls)
16. [Mental models](#16-mental-models)

---

## 1. Thesis

Three claims:

1. **Errors deserve their own pipeline.** Grouping by stack-trace fingerprint is non-trivial; release-health dashboards are non-trivial; "new error since deploy" detection is non-trivial. A general log store doesn't do these well.
2. **Release health is the dominant signal.** Per-release, what's the crash-free rate? What new error patterns appeared? Which release should we roll back? These are *the* questions the error tracker answers.
3. **Errors are the highest-signal log class.** Per-event, an error tells you more than any other log line. Capture them all (almost no sampling); attach maximum context (breadcrumbs, user, trace).

If your team logs errors to the same store as INFO logs and queries them with `level=error`, you're missing release-health analysis, fingerprint-based grouping, "new error" detection, and trend visualization. This chapter is the right setup.

---

## 2. Why Error Tracking Is Distinct From Logs

| Dimension | Logs | Error tracker |
|---|---|---|
| Volume | High (TB/day) | Low (MB/day) |
| Per-event richness | Medium | Very high |
| Grouping | By label | By stack fingerprint |
| Trend | Hard to see | Native |
| Release attribution | Manual | Built-in |
| Symbolication | Sometimes | Always |
| User attribution | Sometimes | Always |
| New-error detection | No | Native |
| Cost model | Per-volume | Per-event (and free at low rates) |

Both are necessary. The right architecture: errors flow to *both* the error tracker (for analysis) and the log store (for cross-reference / forensic).

---

## 3. The Grouping Algorithm

The single hardest thing error trackers do.

### 3.1 The problem

A bug fires 50,000 times in 24 hours. Each fire generates an event. You don't want 50,000 issues — you want *one* issue with a count of 50,000.

### 3.2 The naive grouping

By exception class + message? Fails: same bug with different `user_id` in message generates different groups.

By stack trace exact-match? Fails: framework code appears at different lines per request.

### 3.3 The fingerprint

Modern error trackers fingerprint by a *normalized stack trace*:
- Strip in-flight values (line numbers may stay; arguments stripped).
- Drop framework / library frames; keep app frames.
- Hash the result.

Same fingerprint = same issue, regardless of:
- Different request paths.
- Different users.
- Different argument values.
- Different error messages (within reason).

### 3.4 The custom-fingerprint escape hatch

For tricky cases, the SDK lets you specify a fingerprint:

```python
sentry_sdk.set_tag("custom_fingerprint", f"checkout-{vendor}-fail")
```

Used when default fingerprinting groups too aggressively (different bugs end up in one issue) or too granularly (same bug split into many issues).

### 3.5 The dedup window

Within a short window (seconds), repeated fires of the same fingerprint are collapsed. Reduces network volume; doesn't lose the count.

---

## 4. Release Health: Regressions and New Errors

The dominant analysis dimension.

### 4.1 The release-tagged event

Every error event carries the release version it occurred on:

```json
{
  "release": "checkout-svc@2026.5.5",
  "exception": "ConnectionError",
  "fingerprint": "...",
  "user": "alice",
  ...
}
```

### 4.2 The crash-free rate

```
crash-free-sessions = (sessions without errors) / (total sessions)
crash-free-users    = (users with zero errors in 7d) / (total users)
```

For mobile (revisited from `doc 21 §8.3`): the dominant SLI.

For backend: per-service, per-release error rate.

### 4.3 The "new in this release" signal

The most valuable single feature of error trackers.

```
For release N: which fingerprints appeared that weren't in release N-1?
```

A new fingerprint = a new bug introduced this release. Page on it (or block the deploy if caught in canary).

### 4.4 The release-health dashboard

```
Release           Adoption   Crash-free   New issues   Regressed   Resolved
2026.5.4 (prev)   100%       99.65%       —            —           —
2026.5.5 (curr)   62%        99.70%       2            1           5
2026.5.6 (next)   8%         99.71%       0            0           1
```

Pattern: each release should improve, not regress. New issues per release should trend downward.

### 4.5 The "regression" signal

A fingerprint marked "resolved" reappears in a later release = regression. Different from "new in release" — this issue was *fixed* and came back. High-priority signal.

### 4.6 The deploy gate

Pre-promotion to 100%: did any new fingerprints appear in the canary? Regressions? If yes, halt rollout.

---

## 5. Source Maps and Symbolication Revisited

(Cross-link to `doc 21 §7.3`.) Critical for error trackers.

### 5.1 The browser case

Production JavaScript is minified. Stack: `main.7f3a.js:3:104`. Useless.

The error tracker needs the source map for that build. Symbolicates: `checkout.tsx:42`. Now you can fix.

### 5.2 The mobile case

iOS dSYMs and Android ProGuard mappings. Each build's mapping uploaded; trackers symbolicate incoming crashes.

### 5.3 The native binary case

Native services (Go, C++, Rust) with stripped binaries: debug symbols (DWARF) uploaded; tracker resolves `addr2line`-style.

### 5.4 The "missing mapping" failure

Same as `doc 21 §7.3`: keep mappings for the longest-cached version. Stack traces from old builds need their mappings.

### 5.5 The cost

Source maps and symbol files are *not* small. Total stored across all releases over years can reach hundreds of GB. Plan retention; archive old.

---

## 6. Mobile Crash Reporting Specifics

(Cross-link to `doc 21 §8`.) Some mobile-specific concerns.

### 6.1 The native crash

A native crash (SIGSEGV, NSException, ANR) terminates the process. The crash handler must:
1. Capture the stack at crash time.
2. Persist it locally (the network might be down).
3. Send on next launch.

The crash arrives *after* the user has restarted the app — sometimes hours later. The error tracker handles this.

### 6.2 The breadcrumb trail

User actions before the crash: clicks, screens viewed, network calls. Captured in a circular buffer; included with the crash.

```
Breadcrumbs (last 30):
  19:42:01  view: HomeScreen
  19:42:05  click: button#search
  19:42:06  network: GET /search?q=...
  19:42:07  view: SearchResults
  19:42:09  click: result-3
  19:42:10  network: GET /products/12345
  19:42:11  view: ProductDetail
  19:42:11  CRASH: SIGSEGV in render()
```

The breadcrumbs reproduce the user's path. Invaluable for reproducing.

### 6.3 The release-adoption signal

```
Release 6.4.0:  62% adoption
Release 6.4.1:   8% adoption (rolling out)
Release 6.3.x:   30% (older versions, slow upgraders)
```

If 6.4.1 has more crashes than 6.4.0, halt rollout. Bake-time calibrated by adoption %.

### 6.4 The OS / device dimension

Mobile errors filter by OS version, device model:

```
Top crashes on iOS 17.3:
  - libdispatch crash on iPhone 12 series only (8% of crashes)

Top crashes on Android 14:
  - Camera permission denial on OnePlus devices (12%)
```

Per-device debugging is a key mobile use case.

---

## 7. Backend Exception Tracking

The server-side story.

### 7.1 What gets captured

Every uncaught exception (or explicit `capture_exception`) sends:
- Stack trace (symbolicated).
- Local variable values (often).
- Request context (URL, params, headers).
- User identity (if known).
- Trace context (`trace_id`).
- Service / release tag.
- Server / pod / region.

### 7.2 The "should I log it AND track it?" question

Yes. Errors should:
- Go to the log store (for cross-reference with other logs).
- Go to the error tracker (for grouping, trends, release health).

Some teams consolidate via the OTel SDK: error events emitted as both log records and error events.

### 7.3 The custom-context pattern

Beyond defaults, attach context that helps debug:

```python
with sentry_sdk.push_scope() as scope:
    scope.set_user({"id": user.id, "tenant": user.tenant})
    scope.set_tag("feature", "checkout")
    scope.set_extra("cart_size", len(cart.items))
    do_checkout()
```

If checkout raises, the error tracker has user, tenant, feature, cart-size — enough to reproduce.

### 7.4 The "low-volume errors are gold" insight

A 5xx error happening once a day affecting one user is invisible in metrics. The error tracker surfaces it as an issue. Often these are real bugs that affect a small fraction of users; without error tracking, they're never noticed and never fixed.

---

## 8. Linking Errors to Traces and Users

The integration that turns triage into one-click.

### 8.1 The error → trace link

Every error includes `trace_id`. The error tracker links to the trace.

```
Error: ConnectionError
  trace_id: a1b2c3...
  → [Open trace in Tempo]
```

Click → see the full request that failed. Root cause analysis collapses from 30 minutes to 30 seconds.

### 8.2 The error → user link

If the user is identified (via SSO / app account), the error includes their ID. The error tracker tracks "users affected by this issue."

```
Issue: ConnectionError in checkout
  Users affected: 12 (in last 24h)
  Top affected users: alice, bob, charlie, ...
```

Useful for: customer support ("customer X reports an issue → look up their errors"), severity assessment ("this issue affects whales").

### 8.3 The user → errors view

A view: "all errors for this user." Used by support to triage customer complaints.

### 8.4 The trace → error link

In the trace view, error spans are highlighted, link to the error issue.

### 8.5 The end-to-end story

A user reports an issue → search for their errors → see the issue → click to trace → see the failing span → see the related logs → fix.

Without the integration, this is multiple tabs and manual cross-referencing. With it, ~60 seconds.

---

## 9. Sampling Errors (You Usually Shouldn't)

The decision.

### 9.1 The default

Capture 100% of errors. They're rare; per-event richness justifies the cost.

### 9.2 The exceptions

Some cases warrant sampling:
- A noisy-error storm — one bug fires 1M times in an hour. Adaptive sampling caps it at, say, 10K.
- High-traffic services with high error counts — sampling at the same rate as traces.
- Mobile crashes from obsolete versions where you've already fixed the bug.

### 9.3 The "rate limit" pattern

Most error trackers default-rate-limit per-fingerprint: max N events/min for the same fingerprint. Captures the dedup-count without sending each event.

### 9.4 The failure mode of over-sampling

If you sample errors at 10%, your detection lags by 10× and you miss small-signal bugs. Default to 100%; sample only on noise.

---

## 10. Error Tracking SLOs

The shapes.

### 10.1 The crash-free SLO

```yaml
- name: crash_free_sessions
  metric: sessions_without_errors / total_sessions
  target: 0.995
```

For mobile: classical. For backend: equivalent at the journey level.

### 10.2 The new-issue rate

```yaml
- name: new_issues_per_release
  target: < 3
```

Each release introduces fewer than 3 new error fingerprints.

### 10.3 The MTTR for issues

```yaml
- name: error_mttr
  metric: time_from_first_seen_to_resolved
  target: P95 < 7 days
```

Issues that linger reflect technical-debt accumulation.

### 10.4 The unresolved-issue count

```yaml
- name: unresolved_high_severity_issues
  target: < 10
```

High-severity issues should be addressed.

---

## 11. The PII Concern

Errors leak more PII than logs.

### 11.1 What can leak

- **Stack traces.** Argument values often included.
- **Request bodies.** Captured by default in some SDKs.
- **User identifiers.** Email / phone often.
- **Local variables.** Frame-level captures.

### 11.2 Defenses

SDK config:
```python
def before_send(event, hint):
    # scrub PII
    event = scrub(event, ["email", "phone", "ssn", "credit_card"])
    return event

sentry_sdk.init(before_send=before_send, ...)
```

Built-in scrubbers in modern SDKs. Configure them aggressively. Audit periodically.

### 11.3 The retention dimension

Errors stored: same data-retention regime as audit / logs. GDPR-compatible (right-to-erasure mapped to user IDs).

### 11.4 The vendor / hosting choice

Error data flows to a vendor (Sentry SaaS, Datadog, etc.) by default. For regulated workloads, self-hosted Sentry / Bugsnag On-Premise / equivalent.

---

## 12. Tools (2026 Landscape)

| Tool | Strength |
|---|---|
| **Sentry** | Multi-platform; open-source core; widely used |
| **Rollbar** | Mature; competing feature set |
| **Bugsnag** | Mobile-strong; release health |
| **Honeybadger** | Smaller; Ruby/Rails roots |
| **Airbrake** | Long-running; established |
| **Datadog Error Tracking** | Integrated with Datadog APM |
| **New Relic Errors Inbox** | Integrated with NR APM |
| **Dynatrace** | Integrated with Dynatrace |

The trend: error tracking as a *feature* of the broader APM. Sentry remains a strong standalone, especially for orgs without an APM vendor commitment.

### 12.1 Self-hosted options

- Self-hosted Sentry (full feature; ops cost).
- GlitchTip (open-source, Sentry-API-compatible).
- Honeybadger self-hosted.

For regulated workloads or cost-sensitive at scale.

### 12.2 The OTel angle

OTel logs SDK can emit error events with full context. Some error trackers (Sentry's OTel SDK) consume OTLP directly. The convergence is happening; the standalone error tracker still has features OTLP doesn't standardize (grouping, release health UI).

---

## 13. Anti-Patterns

1. **Errors logged to general log store only.** No grouping, no release health.
2. **No fingerprint customization.** Wrong grouping persists.
3. **No source maps in prod.** Stack traces useless.
4. **No release tag.** Regression detection broken.
5. **No user attribution.** Affected-user count missing.
6. **No trace_id link.** Triage requires manual cross-reference.
7. **Error sampling too aggressive.** Small-signal bugs missed.
8. **PII unhandled.** Compliance violation.
9. **No "new in release" alert.** Regressions ship.
10. **No release-health dashboard.** Bad releases promoted.
11. **Mobile dSYMs not uploaded.** Crash stacks unsymbolicated.
12. **Stale release retention.** Old crashes useless.
13. **No deploy-gate based on errors.** Bad deploys reach 100%.
14. **No SLO on crash-free.** Quality unmeasured.
15. **Error tracker ignored by team.** Issues accumulate; never fixed.

---

## 14. Worked Example: Error Tracking for a Polyglot Stack

Concrete and complete.

### 14.1 The org

- iOS app, Android app, web app (React).
- 50 backend services in Go and Python.
- Sentry as the error tracker.

### 14.2 Per-platform setup

**iOS:**
- Sentry iOS SDK initialized at launch.
- dSYM auto-upload via Sentry CLI in CI.
- `release` tag set to build number + version.
- User identified post-login.
- Breadcrumbs for navigation, network, lifecycle events.

**Android:**
- Sentry Android SDK + ProGuard mapping upload.
- ANR detection enabled.
- Release / version tags.

**Web:**
- Sentry Browser SDK loaded async after first paint.
- Source maps uploaded per build (Sentry CLI in CI).
- Trace context propagated.
- React error boundaries integrated.

**Backend (Go):**
- Sentry Go SDK + auto-instrumentation for HTTP/gRPC.
- Uncaught panics captured.
- `release` from build tag.

**Backend (Python):**
- Sentry Python SDK + Django/Flask integration.
- Uncaught exceptions captured.
- Release tagged.

### 14.3 The release-health dashboard

For each release across each platform:
- Crash-free sessions / users.
- New issues introduced.
- Regressed issues.
- Resolved issues.

### 14.4 The deploy gate

Post-deploy:
- Wait 10 minutes for canary traffic.
- Check Sentry: any new fingerprints in this release? Regressions?
- If yes: halt rollout, alert team.
- If no: continue to next stage.

### 14.5 The on-call integration

Sentry alerts route to PagerDuty:
- New critical-tier issue: page.
- High-frequency new issue: page.
- Issue threshold (>1000 events/hour): page.
- Other new issues: ticket.

### 14.6 The cross-link

Every error has a link to:
- The trace (Tempo).
- Recent logs from the same trace_id (Loki).
- The release commit (Git).
- The deploy event (deployment system).

End-to-end debugging from a single click.

### 14.7 The result

- Mobile crash-free rate maintained at 99.7%.
- Backend errors caught in canary 80% of the time before full rollout.
- Average issue MTTR: 5 days.
- New-error count per release: trending down.
- Cross-link usage: ~60% of issue triage uses the trace link.

---

## 15. Pitfalls

1. **No error tracker.** Per-event grouping / release health missing.
2. **Same store for errors and logs.** Lose grouping and analytics.
3. **No source maps.** Stack traces useless.
4. **No release tagging.** Regression detection broken.
5. **No deploy gate on errors.** Bad releases promoted.
6. **No user identification.** Affected-user counts unavailable.
7. **No trace_id link.** Slow triage.
8. **Aggressive sampling of errors.** Small-signal bugs missed.
9. **PII in error events.** Compliance violation.
10. **No release-health dashboard.** Bad releases unobserved.
11. **dSYMs / mappings not uploaded.** Mobile stacks unsymbolicated.
12. **No "new in release" alerts.** Regressions ship.
13. **Issue queue ignored.** Tech debt accumulates.
14. **No on-call integration.** Critical errors wait for triage.
15. **No SLO on crash-free.** Quality unmeasured.

---

## 16. Mental Models

> **Errors deserve their own pipeline. Grouping, release health, fingerprints — not what general log stores do.**

> **Release health is the dominant signal. Per-release crash-free, new errors, regressions.**

> **Source maps / dSYMs / symbol files: non-negotiable. Otherwise you can't read the stack.**

> **100% capture (almost). Errors are rare and rich; sample only under storm.**

> **Link errors to traces and users. End-to-end triage from one click.**

> **Custom fingerprints when default grouping fails.**

> **Deploy gate on new errors. Bad deploys halt before reaching 100%.**

> **Crash-free SLO is the dominant mobile reliability metric.**

> **Errors are gold. Low-volume, high-signal. Treat them as such.**

> **PII scrubbing is non-negotiable. Errors leak more than logs.**

The next batch of chapters (`doc 31`+) covers enterprise patterns: FinOps, compliance, federated multi-region, schema governance, lakehouse, DR, vendor migration, continuous verification, build-vs-buy, IDP, brownfield. Then appendices.
