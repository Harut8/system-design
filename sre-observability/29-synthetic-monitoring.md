# 29 — Synthetic Monitoring

> Synthetic monitoring is the discipline of *manufacturing* traffic to verify the system works. It catches outages that RUM and server-side telemetry miss — because synthetic traffic is independent of real traffic patterns. The page that fires before any customer notices is almost always synthetic-driven.

This chapter is the active-measurement complement to `doc 21` (RUM). RUM tells you what real users experience; synthetic tells you what users *would* experience right now if they tried.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Synthetic vs RUM: when each shines](#2-synth-vs-rum)
3. [The four classes of synthetic check](#3-four-classes)
4. [HTTP / API checks](#4-http-checks)
5. [Browser checks (full-page)](#5-browser-checks)
6. [Multi-step / journey checks](#6-multi-step)
7. [Performance checks](#7-performance)
8. [Geographic distribution](#8-geographic)
9. [Internal vs external synthetics](#9-internal-external)
10. [The "synthetic SLO" pattern](#10-synth-slo)
11. [Tooling: Checkly, Datadog, Pingdom, k6, Grafana Synthetic](#11-tools)
12. [Synthetic in CI / pre-deploy gates](#12-ci-gates)
13. [The cost dimension](#13-cost)
14. [Anti-patterns](#14-anti-patterns)
15. [Worked example: synthetic for a checkout journey](#15-worked-example)
16. [Pitfalls](#16-pitfalls)
17. [Mental models](#17-mental-models)

---

## 1. Thesis

Three claims:

1. **Synthetic catches outages before users do.** It runs constantly; if it fails, you know within seconds. RUM only fires when a real user hits the broken thing.
2. **Synthetic and RUM are complements.** Synthetic for "is the system available?"; RUM for "what do real users experience?" Both ship.
3. **The most valuable synthetics are journeys, not endpoints.** A 200 response from `/health` is comforting and useless. A successful synthetic checkout from "browse → add to cart → pay → confirmation" is the actual SLO measurement.

If your team has uptime monitoring on a `/healthz` endpoint and calls it synthetic monitoring, you are catching the easiest 10% of outages and missing the rest. This chapter is the right shape.

---

## 2. Synthetic vs RUM: When Each Shines

| Dimension | Synthetic | RUM |
|---|---|---|
| **Catches outages before users** | Yes | No (real users must hit it) |
| **Measures what real users see** | No (synthetic env may differ) | Yes |
| **Reliable signal** | High (controlled) | Noisy (network, ad blockers, devices) |
| **Cost** | Per-check pricing; bounded | Per-session; scales with users |
| **Geographic coverage** | Plant probes anywhere | Limited to where users are |
| **Pre-launch** | Yes (before any users) | No |
| **Variant per user** | No (one variant) | Yes (real distribution) |
| **Frequency** | Configurable (1m, 5m, etc.) | Whenever users visit |

Both are necessary. Different questions; different answers; different deployment contexts.

### 2.1 Why both

- Synthetic detects "the page is down."
- RUM detects "the page is slow for users in Brazil."

Each misses what the other catches.

---

## 3. The Four Classes of Synthetic Check

### 3.1 HTTP / API check

A simple HTTP request to an endpoint. Verifies status code, response time, response body.

```yaml
type: http
url: https://api.example.com/health
method: GET
expected_status: 200
expected_body_contains: "ok"
timeout: 5s
frequency: 1m
```

The bread and butter. Cheap; runs at high frequency.

### 3.2 Browser check

A real browser (headless Chrome) loads a page. Captures Web Vitals, screenshots, errors.

```yaml
type: browser
url: https://example.com/checkout
viewport: 1920x1080
expected_text: "Checkout"
capture_screenshot: on_error
frequency: 5m
```

Slower (~5-10 seconds per check); more expensive; far more comprehensive.

### 3.3 Multi-step (journey) check

A scripted sequence: navigate, fill form, click, verify.

```javascript
// Playwright-style synthetic
await page.goto('https://example.com');
await page.click('text=Sign in');
await page.fill('#email', 'synthetic@example.com');
await page.fill('#password', '...');
await page.click('text=Submit');
await expect(page.locator('text=Dashboard')).toBeVisible();
```

The closest synthetic equivalent of "is the user journey working?" Often runs every 5-15 minutes; expensive per check.

### 3.4 API contract / chained API checks

Multiple API calls testing a flow:

```yaml
- step: login
  request: POST /auth/login
  capture: token
- step: get_account
  request: GET /accounts/me
  headers: { Authorization: "Bearer ${token}" }
- step: place_order
  request: POST /orders
  headers: { Authorization: "Bearer ${token}" }
  body: { ... }
  expected_status: 201
```

Like multi-step browser, but at the API layer. Faster than browser; tests business logic.

---

## 4. HTTP / API Checks

The default starting point.

### 4.1 What to check

- **Critical APIs.** Login, search, checkout, key reads.
- **Each microservice's own health.** Per-service synthetic.
- **DNS resolution.** Implicit in HTTP, but explicit DNS checks catch resolution lag.
- **TLS validity.** Cert expiry; chain trust.
- **CDN edge health.** From per-region probes.

### 4.2 Beyond status codes

A 200 response can still mean broken:
- Body says "Service unavailable" but status is 200 (misconfigured ALB).
- Search returns 0 results (silent corruption).
- API returns stale data (caching gone wrong).

Verify the *content*:

```yaml
expected_body_jsonpath: $.results | length > 0
expected_body_regex: |^\{.*"status":"healthy".*\}$|
```

### 4.3 Latency thresholds

Each check has a latency target. Page on regression.

```yaml
- name: api_login_latency
  threshold_p99: 500ms
  alert: page if exceeded for 5m
```

### 4.4 The `/healthz` trap

A `/healthz` endpoint is a liveness check; it tells you the process is alive. It's *not* an end-to-end check. A service can have healthy `/healthz` and broken business logic.

The right pattern: `/healthz` for liveness; *real-shape* synthetic checks for journeys.

---

## 5. Browser Checks (Full-Page)

The richest synthetic class.

### 5.1 What it tests

- Page loads.
- JavaScript executes.
- Network resources load.
- Web Vitals (LCP, INP, CLS).
- Visual rendering (with screenshots).

### 5.2 Headless vs headed

Almost all synthetic browser checks use headless (Puppeteer / Playwright). Headed is reserved for visual regression testing.

### 5.3 The integration with Web Vitals

Synthetic browser checks can report the same Web Vitals as RUM. The platform sees:
- RUM-side LCP (real users, noisy).
- Synthetic-side LCP (controlled, clean).

Synthetic-side as a baseline; RUM as the user reality.

### 5.4 The visual-regression bonus

Screenshots from each check enable visual regression detection: did the layout change unexpectedly between deploys?

Tools: Percy, Applitools, Chromatic. Not strictly synthetic monitoring, but adjacent.

### 5.5 The blocker problem

Browser checks must handle:
- Cookie banners.
- A/B test variations.
- Bot detection (the synthetic browser may itself be flagged).
- Authentication flows.

Each is a per-site engineering effort. Don't underestimate.

---

## 6. Multi-Step / Journey Checks

The "did the actual user journey work?" question.

### 6.1 Example: checkout journey

```javascript
test('checkout end-to-end', async ({ page }) => {
  await page.goto('https://example.com');
  await page.click('text=Shop');
  await page.click('text=Featured product');
  await page.click('text=Add to cart');
  await page.click('text=Checkout');
  await page.fill('#email', 'synthetic+ci@example.com');
  await page.fill('#address', '123 Main St');
  await page.fill('#card', '4242 4242 4242 4242');
  await page.fill('#cvc', '123');
  await page.click('text=Place order');
  await expect(page.locator('text=Order confirmed')).toBeVisible({ timeout: 30000 });
});
```

Verifies *the entire flow* works. If any step fails, the check fails; alert fires.

### 6.2 The data dimension

Synthetic users are real users in the system. Their orders are real orders (sort of). Considerations:

- **Synthetic accounts** — flagged in the system, treated specially (fraud doesn't apply, real money isn't charged).
- **Test mode payment** — Stripe Test Mode, etc.
- **Cleanup** — synthetic data periodically purged.
- **Filtering from analytics** — synthetic accounts excluded from product metrics.

Skipping the data dimension causes "synthetic users overflow into production analytics" pain.

### 6.3 Frequency vs cost

Multi-step checks cost ~10-100× a simple HTTP check. Frequency:

- Critical journeys: every 5 minutes.
- Important journeys: every 15 minutes.
- Niche journeys: every 30-60 minutes.

### 6.4 The flaky-test problem

Browser checks are flaky. Network blips, transient errors, A/B-test surprises. Defenses:
- Retry once on failure (auto-retry, count failures only on consistent fail).
- Multi-region: alert only when *multiple* regions fail.
- Quarantine flaky checks; fix them; re-add.

Without flakiness control, synthetic alerts become noise.

---

## 7. Performance Checks

Beyond availability: latency, throughput, regression.

### 7.1 The "p99 from this region" check

Periodic load tests against production from a controlled region:

```yaml
- name: api_throughput
  type: load
  target: https://api.example.com
  rps: 100
  duration: 1 minute
  expected_p99_latency: 500ms
  frequency: 1 hour
```

Catches: capacity regressions, slow degradations, post-deploy regressions.

### 7.2 The "deploy-and-validate" pattern

Post-deploy: run a synthetic-load check. If p99 regressed, auto-rollback.

```
deploy → wait 2m → run synth → fail? → rollback
                              → pass? → continue rollout
```

This is canary-deploy with synthetic verification. Highly effective.

### 7.3 The performance baseline

Synthetic captures *baseline* performance under controlled load. RUM noise is filtered out (single region, one device, no ad blockers). When the baseline degrades, attribution is precise.

---

## 8. Geographic Distribution

Synthetic checks run from multiple physical locations.

### 8.1 Why multi-region

- A check from us-east-1 won't catch an issue specific to eu-west-1.
- Geographic latency varies; one region's view is incomplete.
- DNS / CDN issues are often regional.

### 8.2 The probe set

Typical: 5-15 regions covering major user geographies. Examples:
- North America: us-east, us-west, ca-central.
- Europe: eu-west (Ireland), eu-central (Frankfurt), uk-london.
- Asia: ap-southeast (Singapore), ap-northeast (Tokyo), ap-south (Mumbai).
- Other: sa-east (São Paulo), af-south (Cape Town), au-southeast (Sydney).

### 8.3 The "any vs all" alert pattern

```
alert if check fails in 3+ regions in last 5 minutes
```

vs.

```
alert if check fails in any 1 region in last 5 minutes
```

The first reduces false positives (probe issues, regional probe outages); the second catches regional outages of your service.

In practice: page on the first; ticket on the second.

---

## 9. Internal vs External Synthetics

Different vantage points.

### 9.1 External

Run from outside your network. Catches:
- Public-facing availability.
- DNS issues.
- CDN issues.
- Internet-routing problems.

Tools: Pingdom, Datadog, Checkly, Grafana Synthetic.

### 9.2 Internal

Run from inside your network / cluster. Catches:
- Service-to-service availability.
- Internal API performance.
- Data-plane health.

Tools: blackbox-exporter, custom k6 scripts, in-cluster probes.

### 9.3 The use cases differ

| Use | External | Internal |
|---|---|---|
| Customer-facing journey | ✓ | (limited) |
| B2B API SLA | ✓ | (different SLAs internal) |
| Service-mesh health | (no) | ✓ |
| Internal admin tools | (no) | ✓ |
| CDN / edge | ✓ | (no) |
| Database freshness | (no) | ✓ |

You need both. Most teams have only external (vendor-bought) and miss internal.

---

## 10. The "Synthetic SLO" Pattern

Synthetic checks define their own SLOs.

### 10.1 The SLO

```yaml
- name: synth_checkout_journey_success
  metric: synth_check_success_total{check="checkout-journey"}
  total: synth_check_total{check="checkout-journey"}
  target: 0.999
```

99.9% of synthetic checkout journeys must succeed over 28 days.

### 10.2 The complement to RUM SLOs

RUM: "99% of *real* checkouts succeed."
Synth: "99.9% of *synthetic* checkouts succeed."

Synthetic SLO is tighter (controlled environment). Both monitored separately.

### 10.3 The first-line alert

The synthetic SLO is often the *first* indicator of an outage. Burn rate alerts on synth SLOs page within minutes.

### 10.4 The "synth fail; RUM fine" puzzle

Sometimes synth fails (probably synth issue) while RUM is healthy. Sometimes synth passes while RUM degrades (probably geographic / device-specific issue).

The combination tells you more than either alone.

---

## 11. Tooling: Checkly, Datadog, Pingdom, k6, Grafana Synthetic

### 11.1 Tool comparison

| Tool | Strength |
|---|---|
| **Checkly** | Modern, code-first, Playwright-native; growing fast |
| **Datadog Synthetic** | Mature, integrates with Datadog APM/RUM; expensive |
| **Pingdom** | Long-standing; simpler; cheap for basic |
| **k6 Cloud** | Load + synthetic; great for performance |
| **Grafana Synthetic Monitoring** | Open-source-friendly; integrates with Grafana stack |
| **AWS CloudWatch Synthetics** | AWS-native; cheap; basic |
| **GCP Cloud Monitoring uptime checks** | GCP-native; basic |
| **New Relic Synthetics** | Mature; integrated with New Relic APM |
| **Sentry Crons** | For monitoring scheduled jobs |
| **Self-hosted blackbox-exporter** | Open-source; for internal HTTP checks |

### 11.2 The SaaS vs self-hosted choice

SaaS: turnkey, multi-region, less ops.
Self-hosted: full control, cheaper at scale, requires ops.

Most teams: SaaS for external, self-hosted (blackbox-exporter) for internal.

### 11.3 The "code-first" trend

Modern tools (Checkly especially) treat checks as code:
- TypeScript / JavaScript with Playwright.
- Versioned in Git.
- Reviewed in PRs.
- Deployed via CI.

This is the synthetic equivalent of "alert-as-code." Strongly recommended.

### 11.4 Browser check costs

Browser checks (Playwright) cost ~10× HTTP checks. Multi-step browser flows ~50×. Budget accordingly.

---

## 12. Synthetic in CI / Pre-Deploy Gates

The intersection with deployment.

### 12.1 The pre-deploy synthetic

Before promoting a build to production:
1. Deploy to staging.
2. Run synthetic suite against staging.
3. Pass → promote. Fail → block.

This catches regressions that unit tests miss.

### 12.2 The post-deploy synthetic

After production deploy:
1. Wait for rollout to complete (or canary % to be reached).
2. Run synthetic suite from external probes.
3. Pass → continue. Fail → auto-rollback.

This is the canary's verification step. Without it, canary is "deploy and hope."

### 12.3 The "bake time"

Between rollout steps, run synthetic checks for N minutes (e.g., 5-10 min). Multiple checks = statistical confidence the deploy is good.

```
0%   → 5%   → 25%  → 50%  → 100%
            wait + run synth at each step
```

### 12.4 The CI-only synthetic

Some checks make sense only in CI:
- New feature flag rollout verification.
- Schema migration verification.
- Specific scenarios that can't run in production (test data, dangerous ops).

These augment but don't replace production synthetics.

---

## 13. The Cost Dimension

Synthetic isn't free.

### 13.1 The cost model

- HTTP checks: ~$0.001-$0.01 per check.
- Browser checks: ~$0.05-$0.50 per check.
- Multi-step browser: ~$0.10-$2.00 per check.
- Multi-region: cost × N.

A team with 10 multi-step browser checks running every 5 minutes from 5 regions:

```
10 × (60/5) × 24 × 30 × 5 × $0.50 = $108,000 / month
```

This is real. Don't blanket-multi-region everything.

### 13.2 Cost optimization

- Critical journeys: high frequency, multi-region.
- Important: medium frequency, fewer regions.
- Nice-to-have: low frequency, one region.

Tier the synth fleet like everything else.

### 13.3 The cost-benefit analysis

Synthetic catches outages that would otherwise be customer-visible. The cost of one major outage (~$50k-$10M depending on org) easily justifies $100k/year in synthetic checks. The math nearly always works.

---

## 14. Anti-Patterns

1. **`/healthz` only.** Catches 10% of outages.
2. **Single-region synthetic.** Regional outages invisible.
3. **No multi-step journeys.** Real flows untested.
4. **No data isolation for synthetic users.** Pollutes production.
5. **No flakiness control.** Synth alerts become noise.
6. **No post-deploy synthetic.** Canary is "deploy and pray."
7. **No cost-tiering.** Bill explodes.
8. **No internal synthetic.** Service-to-service health invisible.
9. **No code-first synth.** Drift; review skipped.
10. **No Web Vitals capture.** Browser checks underutilized.
11. **Ignored synth flakiness.** Real failures lost in noise.
12. **No SLO on synth.** Failure pattern unobserved.
13. **No retry / quarantine.** Flaky checks make on-call ignore.
14. **No cleanup of synth data.** Production analytics polluted.
15. **No coordination with CI.** Pre-deploy gate missing.

---

## 15. Worked Example: Synthetic for a Checkout Journey

Concrete and complete.

### 15.1 The checks

**Tier 1 (page on failure):**
- HTTP: `GET /healthz` from 5 regions, every 1 min.
- HTTP: `GET /api/products` from 5 regions, every 1 min.
- Multi-step: full checkout flow from 3 regions, every 5 min.
- API chain: login → cart → order from 2 regions, every 5 min.

**Tier 2 (ticket on failure):**
- Browser: page-load Web Vitals from 5 regions, every 15 min.
- API: secondary endpoints from 3 regions, every 5 min.

**Tier 3 (informational):**
- Browser: visual regression on key pages from 1 region, every 30 min.

### 15.2 The flakiness mitigation

- Retry once on failure within 60s.
- Page only if 2+ regions fail consecutively.
- Auto-quarantine after 3 false-positive incidents (paged for 30 days; reviewed weekly).

### 15.3 The cost

```
Tier 1: ~50 HTTP/min + 6 multi-step/5min = ~$3,500/month
Tier 2: ~5 browser/15min + 6 API/5min = ~$1,500/month
Tier 3: ~2 browser/30min = ~$300/month
Total: ~$5,300/month
```

### 15.4 The integration

- Synth metrics in Mimir alongside RUM and server-side.
- Synth failures fire in the same Alertmanager.
- Pre-deploy: run synth against staging; gate.
- Post-deploy: run synth against production canary; auto-rollback on fail.

### 15.5 The result

Outages detected:
- 4 regional CDN issues caught by single-region synth fails.
- 2 deploy regressions caught by post-deploy synth before reaching 100%.
- 1 silent corruption (DB returned wrong data) caught by content verification.
- 2 cert expiry issues caught proactively (tier 1).

Cost ~$60k/year. Outage prevention value: easily 10× that.

---

## 16. Pitfalls

1. **Healthz-only synth.** Misses 90% of outages.
2. **Single-region.** Geographic blind spots.
3. **No multi-step.** Real flows untested.
4. **No data isolation.** Synthetic pollutes prod.
5. **No flakiness control.** Alert noise.
6. **No post-deploy gate.** Bad deploys reach 100%.
7. **No tiering.** Everything multi-region; cost explodes.
8. **No content verification.** 200 with wrong body unnoticed.
9. **No internal synth.** Internal services unobserved.
10. **No SLO on synth.** Pattern detection missing.
11. **No retry-and-quarantine.** Flaky checks ignored.
12. **No code-first management.** Drift.
13. **No cleanup of synth data.** Analytics pollution.
14. **No CI integration.** Pre-deploy verification missing.
15. **Ignoring synth-fails-but-RUM-fine.** Probe issues unaddressed.

---

## 17. Mental Models

> **Synthetic catches outages before users do. RUM catches what only some users see.**

> **Healthz is a liveness check, not a synthetic check.**

> **The most valuable synthetics are journeys, not endpoints.**

> **Multi-region is necessary; tier it by criticality.**

> **Synth flakiness is a class of bug; control it explicitly.**

> **Post-deploy synth is the canary's verification.**

> **Synthetic users need data isolation. Production analytics excluded.**

> **Code-first synth = reviewed, versioned, drift-resistant.**

> **Synthetic-and-RUM together. Different signal sources; same picture.**

> **Cost-tier the synth fleet. Critical = frequent + multi-region; nice-to-have = sparse.**

Now go to `doc 30` (error tracking) — the third pillar of the user-facing observability triangle.
