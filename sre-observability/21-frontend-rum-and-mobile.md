# 21 — Frontend / RUM / Mobile Observability

> Server-side health is *not* user-perceived health. A backend running at 99.99% can serve users hitting a 5-second First Contentful Paint, a JavaScript bundle that crashes on Safari, or a mobile app that drains battery. The signals that matter to the customer live in the *client* — the browser, the mobile app, the embedded device — and the discipline of capturing them is fundamentally different from server-side observability.

This chapter is about the half of the stack most platform teams forget. The infrastructure for capturing browser and mobile telemetry has its own constraints (battery, network, privacy, ad blockers) and its own signals (Web Vitals, crash-free sessions, ANRs).

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why client-side is fundamentally different](#2-why-different)
3. [The three client surfaces](#3-three-surfaces)
4. [Web Vitals: the user-experience SLIs](#4-web-vitals)
5. [The browser RUM SDK and its constraints](#5-browser-sdk)
6. [Beacons, transport, and ad blockers](#6-beacons)
7. [JavaScript error tracking and source maps](#7-js-errors)
8. [Mobile observability: native and crash reporting](#8-mobile)
9. [Mobile-specific signals: battery, ANR, frame drops](#9-mobile-signals)
10. [Session replay (and the privacy tightrope)](#10-session-replay)
11. [Sampling on the client](#11-client-sampling)
12. [Linking RUM to backend traces](#12-rum-trace-link)
13. [The geography problem](#13-geography)
14. [PII at the client](#14-client-pii)
15. [Tools (2026)](#15-tools)
16. [Anti-patterns](#16-anti-patterns)
17. [Worked example: Web Vitals SLO for a checkout page](#17-worked-example)
18. [Pitfalls](#18-pitfalls)
19. [Mental models](#19-mental-models)

---

## 1. Thesis

Three claims:

1. **The user's experience is measured at the user's device, not at your gateway.** Backend p99 of 200ms is irrelevant if the page takes 5s to render due to render-blocking JS, oversized images, or a bloated bundle.
2. **Client telemetry has constraints server telemetry never sees.** Battery, network throttling, ad blockers, browser sandboxing, store review processes, OS heterogeneity. The transport, sampling, and consent story is unique.
3. **RUM and synthetic are complements, not alternatives.** Synthetic catches outages before users do; RUM catches what *only some* users see. Both ship; they answer different questions.

If your team optimizes server p99 without measuring client TTFB and FCP, you are optimizing the wrong end of the request. This chapter is about the right end.

---

## 2. Why Client-Side Is Fundamentally Different

Server observability assumes:
- Predictable hardware.
- Fast, reliable network.
- The observer (you) controls the stack.
- No privacy regime governs the telemetry.
- Data flows where you say it flows.

Client observability assumes *none* of these:

| Assumption | Server | Client |
|---|---|---|
| Hardware | Homogeneous fleet | 20K phone models, 5K browsers, 100 OS versions |
| Network | Fast, reliable | 3G in rural India; 5G in Tokyo; flight Wi-Fi |
| Battery | Infinite | Finite; users notice |
| Sandbox | Bare metal / VM / container | Browser CSP; iOS sandbox; Android permissions |
| Telemetry consent | None | GDPR consent banners; CCPA opt-out; tracking-prevention browsers |
| Transport reliability | TCP/TLS to your servers | Beacons through ad blockers, network shifts, app backgrounding |
| Lifecycle | Long-running | Page can close mid-event; app can be killed |
| Time | Server clock | User clock; off by hours; time zone unknown |

These constraints reshape every part of the pipeline.

---

## 3. The Three Client Surfaces

| Surface | Examples | SDK constraints |
|---|---|---|
| **Browser (web)** | React, Vue, Angular, vanilla JS | Tiny bundle (every kilobyte counts on cold load), CSP-compliant, ad-block-resilient |
| **Mobile native** | iOS (Swift/Obj-C), Android (Kotlin/Java) | App Store / Play review; OS power constraints; crash handling |
| **Mobile cross-platform** | React Native, Flutter, Ionic | Bridge to native crash handling; build pipeline complexity |

Plus a fourth, increasingly important: **edge / IoT** — set-top boxes, embedded, smart TVs. Same problems amplified (worse hardware, weaker connectivity, no UI for consent).

---

## 4. Web Vitals: The User-Experience SLIs

Google standardized the user-experience SLIs in 2020 as **Core Web Vitals**. They've matured through 2026 and are now the universal language.

### 4.1 The metrics

| Metric | What it measures | Threshold (good) |
|---|---|---|
| **LCP** (Largest Contentful Paint) | Time until the largest visible element renders | ≤ 2.5s |
| **INP** (Interaction to Next Paint) | Median latency of user interactions (replaced FID in 2024) | ≤ 200ms |
| **CLS** (Cumulative Layout Shift) | Total unexpected layout shift score | ≤ 0.1 |
| **TTFB** (Time to First Byte) | Server response time as measured at the browser | ≤ 800ms |
| **FCP** (First Contentful Paint) | Time until any content appears | ≤ 1.8s |

LCP / INP / CLS are the *Core* Vitals (used in Google search ranking). TTFB and FCP are diagnostic.

### 4.2 The measurement

These are computed by the browser (Performance Observer API) and reported via the SDK. The SDK is small (~5-10 KB minified) and called via:

```js
import { onLCP, onINP, onCLS } from 'web-vitals';
onLCP(console.log);
onINP(console.log);
onCLS(console.log);
```

In a production RUM SDK, the callbacks ship to the backend via a beacon.

### 4.3 SLOs on Web Vitals

```yaml
journey: checkout-page-load
slis:
  - name: lcp_under_2_5s
    threshold: 2.5
    target: 0.75    # 75% of page loads under 2.5s LCP
  - name: inp_under_200ms
    threshold: 200ms
    target: 0.75
  - name: cls_under_0_1
    threshold: 0.1
    target: 0.75
```

Google's "good Core Web Vitals" criterion is **75th percentile under threshold**, not p50 or p99. This is the de facto standard.

### 4.4 The geographic dimension

Web Vitals vary wildly by geography. A page that's 1.5s LCP in San Francisco may be 6s in Lagos. The right SLO is segmented:

```yaml
- name: lcp_under_2_5s_us
  threshold: 2.5s
  filter: country=us
  target: 0.85
- name: lcp_under_4s_global
  threshold: 4.0s
  filter: country!=us
  target: 0.75
```

Without segmentation, the U.S. SLO drives global investment toward U.S.-focused CDNs and not enough toward latency-sensitive code-splitting.

---

## 5. The Browser RUM SDK and Its Constraints

A RUM SDK has a hard set of design constraints.

### 5.1 Bundle size

Every kilobyte added to the SDK delays first paint. SDKs are aggressively size-optimized:

- Sentry browser SDK: ~30 KB gzipped.
- Datadog RUM: ~25 KB.
- New Relic Browser: ~25 KB.
- web-vitals (lightweight): ~3 KB.

Loading strategy:
- **Synchronous in <head>** — captures everything but blocks first paint.
- **Async after first paint** — misses earliest events; doesn't block rendering.
- **Lazy on user interaction** — minimal early data; full data after engagement.

The right choice depends on what you're measuring. For Core Web Vitals, you *must* measure pre-paint events; sync or `defer` is required.

### 5.2 CSP compatibility

Content Security Policy headers restrict what scripts can run and what hosts can be reached. RUM SDKs must:
- Avoid `eval` and `Function` constructors.
- Not require `unsafe-inline` (some inline init is OK if hashed).
- Beacon to allowed hosts.

Misconfigured CSP is the #1 cause of "the SDK isn't reporting anything." Validate headers in QA.

### 5.3 Performance overhead

The SDK itself shouldn't degrade the page it observes. Targets:

- CPU overhead: ≤ 1% on average.
- Memory: ≤ 5 MB resident.
- Long-task budget: any single SDK operation < 50ms.

Tools instrument themselves; you can verify with Chrome DevTools' Performance tab.

### 5.4 Sandboxing

Service workers, iframes, web workers — each is a different execution context. The SDK must:
- Track each context separately.
- Correlate events across contexts (e.g., a fetch in a service worker should still link to the parent navigation).

---

## 6. Beacons, Transport, and Ad Blockers

The hardest engineering problem in browser RUM.

### 6.1 The transport mechanisms

| Mechanism | Pros | Cons |
|---|---|---|
| **`navigator.sendBeacon`** | Survives page unload; small; non-blocking | Limited to 64 KB per call |
| **`fetch` with `keepalive: true`** | More flexible than sendBeacon | Same 64 KB limit |
| **`XMLHttpRequest`** (sync) | Universal; survives unload (somewhat) | Blocks main thread; deprecated for unload |
| **WebSocket** | Persistent connection; small overhead per event | Doesn't survive unload; complex |
| **Image pixel** | Works through every blocker | One bit of data per request |

Modern SDKs use `sendBeacon` for unload events and `fetch` (with `keepalive`) for in-page events. Image pixel as a last-resort fallback.

### 6.2 Ad blockers

Browser ad blockers (uBlock, AdBlock, Brave's built-in) filter requests to known telemetry domains. uBlock's filter list blocks ~30% of standard RUM endpoints by default.

The defenses:
- **Custom subdomain on your own DNS:** `rum.example.com` instead of `browser-intake-datadoghq.com`. Bypasses the blocklist for now.
- **First-party context:** the beacon endpoint is on the same origin as the site.
- **Acceptable-loss mindset:** accept that 20-40% of RUM data is lost; calibrate your SLOs to "of users with telemetry available."

The ethics: blocking RUM is increasingly users' choice; respect it. Don't fight users harder than you ship features.

### 6.3 The unload problem

A user clicks a link mid-page. The next page loads. *Did your beacon fire?*

`sendBeacon` is designed exactly for this — the request is queued by the browser and survives navigation. But:
- iOS Safari sometimes drops them under memory pressure.
- Tab discard (Chrome's tab freeze) silently kills queued beacons.
- Background-tab pages may not get to fire timing data.

Defense: emit telemetry *eagerly* during the page (every 30 seconds, on visibility changes), not only on unload. Treat unload data as best-effort.

### 6.4 The privacy regime

GDPR (EU), CCPA (California), LGPD (Brazil), PIPEDA (Canada), and many others. Each governs:
- What data you can collect without consent.
- What constitutes consent.
- Right to access, deletion.
- Cross-border transfer.

The 2026 norm: a consent banner gates most RUM. Telemetry is collected in two tiers:
- **Strictly-necessary** (anonymized errors, performance) — usually no consent required.
- **Detailed** (session replay, user-identified) — requires opt-in.

Implementing this correctly is a 3-month project, not a week.

---

## 7. JavaScript Error Tracking and Source Maps

One of the richest signals from the browser.

### 7.1 The capture

Modern SDKs hook:
- `window.onerror`
- `window.onunhandledrejection` (for promise rejections)
- React error boundaries / Vue error handlers
- `console.error` (sometimes)

Each error captures: stack, browser/OS, page URL, user (if identified), session, breadcrumbs (preceding events).

### 7.2 The source map problem

Production JavaScript is minified: `i.x = function (e, t) { return o(e) + p(t) }` is unreadable. Stack traces in production point to minified locations.

Source maps map minified back to original. They live in `.map` files alongside the JS bundle. RUM SDKs (or the backend) apply the source map post-receipt.

The architecture:
- Build pipeline emits `.map` files.
- `.map` files are uploaded to the RUM backend (NOT served to users — that leaks source).
- Backend symbolizes incoming stack traces against the source maps.
- Users see "checkout.tsx:42" not "main.7f3a.js:3:104".

### 7.3 The "missing source map" failure

The single most common bug:
- Build replaces source maps each deploy.
- Old errors arrive (users on old cached bundle).
- Source maps for the old bundle are gone.
- Errors are unsymbolicated; useless.

Fix: keep source maps for at least the longest cache TTL (typically 30+ days).

### 7.4 The PII-in-stack-trace problem

A stack trace can contain user data: `validateEmail("alice@example.com")` may capture the argument. Defenses:
- Configure the SDK to redact arguments.
- Strip query strings from URLs.
- Hash any user-identifiable values before sending.

---

## 8. Mobile Observability: Native and Crash Reporting

Mobile is a different beast.

### 8.1 What mobile SDKs do

- **Crash reporting.** Native crashes (NSException, SIGSEGV, ANR) captured and uploaded.
- **Performance monitoring.** App start time, screen render time, network calls.
- **User actions.** Taps, screens viewed, time per screen.
- **Custom events.** Business-level analytics.
- **Session tracking.** Foreground / background transitions.

### 8.2 The build-pipeline integration

Mobile SDKs require build-time integration:
- iOS: dSYM (debug symbol) files uploaded for symbolication of crashes.
- Android: ProGuard / R8 mapping files for symbolication.
- React Native / Flutter: source maps for the JS bundle.

Each upload happens at build time, before the app is signed. The backend uses these to symbolicate incoming stack traces.

### 8.3 Crash-free sessions / users

The dominant mobile SLI:

```
crash-free-sessions = (sessions without a crash) / (total sessions)
crash-free-users    = (users with zero crashes in 7d) / (total users)
```

The SLO for a healthy app: 99.5%+ crash-free sessions, 99%+ crash-free users.

A drop from 99.7% to 99.4% in a release is significant — a few-tenths drop affects thousands of users.

### 8.4 The release-health dashboard

Each release has its own crash and performance metrics:

```
Release: 6.4.0 (deployed 2026-05-01)
  Adoption: 62% of MAU
  Crash-free sessions: 99.65%
  ANR rate: 0.12%
  App start time p95: 2.1s
  
Release: 6.4.1 (deployed 2026-05-04)
  Adoption: 23%
  Crash-free sessions: 99.72%   ← improving
  ANR rate: 0.10%
  App start time p95: 2.0s
```

This is *the* mobile-team dashboard. Pre-release builds (TestFlight, internal Play tracks) get the same treatment.

### 8.5 The store-review constraint

App stores review SDKs. Some SDKs are flagged for excessive data collection. The defensive posture:
- Use SDKs already vetted on the App Store / Play.
- Don't roll your own crash reporter (Apple flags it).
- Document data collection clearly in privacy nutrition labels.

---

## 9. Mobile-Specific Signals: Battery, ANR, Frame Drops

The signals that don't exist on the server.

### 9.1 ANR (Application Not Responding)

Android's "your app froze" error. The OS detects the main thread blocked for >5 seconds and kills the app. Equivalent on iOS: the watchdog termination.

ANR rate is a key SLI. Caused by:
- Synchronous I/O on the main thread.
- Long-running computations.
- UI thread starvation.

Tools: Firebase Crashlytics ANR reports, Sentry's mobile SDK, vendor APMs.

### 9.2 Frame drops / jank

Render performance: how often does the UI miss the 16.67ms (60 FPS) or 8.33ms (120 FPS) budget?

```
frame_drop_rate = (frames over budget) / (total frames)
```

Janky UI feels broken even if functionally correct.

### 9.3 Battery / power profile

Some tools measure battery drain attributable to your app. Hard but measurable on iOS via the Energy Log; Android via Battery Historian.

The signal is rare in dashboards but critical for app survival — battery-hungry apps get uninstalled.

### 9.4 Network call performance

Each HTTP call from the app: latency, success rate, payload size. Particularly important on cellular networks.

```
checkout_api_latency_p99{network=lte} = 850ms
checkout_api_latency_p99{network=wifi} = 230ms
```

The cellular tail is often 5-10× WiFi. Understand both.

---

## 10. Session Replay (and the Privacy Tightrope)

The most powerful and most fraught client telemetry.

### 10.1 What session replay does

Records the user's session (clicks, scrolls, DOM mutations, network calls) and replays it for engineers. Lets you *see* what the user saw when they hit the bug.

### 10.2 The implementations

Modern session replay isn't a video — it's a *DOM diff stream* that the backend reconstructs into a playback. Storage is ~10 KB per session minute (vs. video at MB).

Tools: FullStory, Sentry Replay, Hotjar, LogRocket, Heap, Datadog Session Replay.

### 10.3 The privacy concerns

Replay captures *everything the user sees*: passwords, credit cards, PII, content of documents. Mishandling is catastrophic.

Defenses:
- **Mask by default.** Inputs of type `password`, `email`, `tel` are masked. Marked-sensitive elements are masked.
- **Block elements:** `<div data-private>` is excluded from capture.
- **Configurable per-form, per-element redaction.**
- **At-rest encryption + tight RBAC.**

Even with all this: review the captured replays periodically. PII leaks find ways through.

### 10.4 The compliance line

Session replay is allowable under GDPR with consent and legitimate interest, with careful redaction. HIPAA / PCI generally prohibit it. Talk to legal *before* enabling.

### 10.5 The cost

Session replay is *expensive*:
- Storage at scale: ~10 MB/user/day.
- Bandwidth (DOM diff stream).
- Vendor pricing (per-session at most vendors).

Don't capture every session. Sample (10-20%) plus capture-on-error for full coverage of broken cases.

---

## 11. Sampling on the Client

Different rules apply.

### 11.1 What you sample

- **Sessions.** Capture telemetry for X% of sessions; full data for that subset.
- **Errors.** Always 100% (errors are rare; you want them all).
- **Performance metrics (Web Vitals).** 100% — they aggregate cheaply.
- **Replay.** Sampled (cost).
- **User actions.** Sampled (volume).

### 11.2 Sticky sampling

If you sample, sample *consistently per user*. A user who lands in the sample for one session should land in the sample for all sessions until their session_id rotates. Otherwise you lose continuity (a user's repeated bug looks like sporadic events).

Implementation: hash `user_id` (or a stable random ID) modulo bucket count.

### 11.3 The dynamic-sampling pattern

```js
sampleRate = 0.10   // baseline 10%

if (errorOccurred) {
  flushFullSession()  // 100% on error
}

if (isHighValueCustomer) {
  sampleRate = 1.0    // important users always sampled
}
```

The vendor often hides this behind a "smart sampling" toggle. Verify the rules; demand transparency.

---

## 12. Linking RUM to Backend Traces

The cross-stack jump.

### 12.1 The handshake

The browser RUM SDK generates a `trace_id` for the page load (or the session). It propagates this via `traceparent` header on outbound requests. The backend receives the header, joins the trace.

```
Browser → fetch(url, {headers: {traceparent: "00-{trace_id}-{span_id}-01"}})
                                            ↑
                                            generated by RUM SDK
Backend → reads traceparent, opens server span as child of browser-side root
```

Now the trace contains *both* the browser-side timing (network, render) and the backend-side timing (gateway, services). Engineers debugging a slow page can see end-to-end.

### 12.2 The CORS gotcha

`traceparent` is not on the standard CORS allowlist. The backend must respond with:

```
Access-Control-Allow-Headers: traceparent, tracestate
```

Otherwise browsers strip the header. The trace is broken.

### 12.3 The W3C standard

The W3C Trace Context spec (`traceparent`, `tracestate`) is the universal standard. OTel browser SDKs (Sentry, Datadog, Honeycomb, OTel-JS) all emit it. Adopt it; don't roll your own.

### 12.4 The exemplar pattern (revisited for RUM)

A RUM "slow page load" alert can include exemplars — clickable trace IDs that jump to the backend trace. Same UX as backend exemplars (`doc 11 §7`), but the entry point is RUM.

---

## 13. The Geography Problem

Client telemetry is *geographically distributed* in a way servers usually aren't.

### 13.1 The signal varies by location

- Latency (TTFB, LCP) varies by physical distance to your CDN PoPs.
- Network reliability varies by region.
- Browser distribution varies (more Chrome in U.S., more Edge in some markets, more Safari in mobile-first countries).

### 13.2 Per-region SLIs

Bake geography into your SLI definitions:

```yaml
- name: lcp_under_3s_global
  filter: country IN (us, uk, de, fr, jp, ...)
  target: 0.75
- name: lcp_under_5s_emerging
  filter: country IN (in, br, ng, id, ...)
  target: 0.75
```

Different targets for different markets. Otherwise the U.S. dominates the average and emerging-market UX rots.

### 13.3 The CDN signal

CDN-level timing (PoP, cache hit/miss, time-to-edge) is essential for diagnosing client latency. Most CDNs (Cloudflare, Fastly, CloudFront) emit Server-Timing headers; RUM SDKs capture them.

```
Server-Timing: cf-cache-status;desc=hit, ttfb;dur=45, edge-pop;desc=fra
```

The browser can attribute "this LCP was 4s — 3.5s of which was page weight, .5s of which was network."

---

## 14. PII at the Client

The browser sees more PII than your servers do.

### 14.1 What's PII at the client

- URL paths (`/account/12345`).
- Form values (sometimes captured).
- localStorage / sessionStorage contents.
- Headers (cookies, auth tokens).
- DOM contents (in session replay).
- User-agent (fingerprintable).
- Screen dimensions (fingerprintable).

### 14.2 Redaction defaults

Modern SDKs ship with sensible defaults:
- Mask password / email / tel inputs.
- Don't capture localStorage by default.
- Don't capture cookies.
- Strip URLs of common PII patterns (numeric IDs, query strings).

Configure beyond defaults aggressively. The cost of leaked PII is far higher than the cost of lost telemetry.

### 14.3 The audit

Quarterly: a privacy engineer (or platform team) reviews captured telemetry. Sample 10 sessions; look for unmasked PII. Find any → tighten redaction config.

This is the only honest way to verify redaction works. Don't trust the SDK defaults forever.

---

## 15. Tools (2026)

| Category | Tools |
|---|---|
| **Browser RUM** | Datadog RUM, Sentry, New Relic Browser, Dynatrace, Honeycomb, Grafana Faro |
| **Mobile crash + perf** | Firebase Crashlytics, Sentry Mobile, Bugsnag, Embrace, Instabug |
| **Session replay** | FullStory, Sentry Replay, LogRocket, Hotjar, Datadog |
| **Web Vitals (free)** | web-vitals.js, Chrome User Experience Report (CrUX) |
| **Synthetic** | Checkly, Datadog Synthetic, k6 Cloud, Pingdom (covered in `doc 29`) |
| **Open-source RUM** | Grafana Faro (browser RUM); OTel browser SDK (emerging) |

The 2026 emerging trend: **OTel browser SDK** is gaining adoption as a vendor-neutral alternative. As of mid-2026 it's still experimental for production but converging.

---

## 16. Anti-Patterns

1. **Server-only SLOs.** No client SLI; user experience invisible.
2. **Mean-based RUM metrics.** Tail dominates; mean lies.
3. **No source maps in prod.** Every error stack is unreadable.
4. **No geographic segmentation.** Emerging-market UX rots silently.
5. **Sync RUM in `<head>`.** Blocks first paint; defeats the purpose.
6. **No consent gating.** GDPR violation; fines.
7. **Session replay without redaction audit.** PII leaks.
8. **Sampling without stickiness.** Lost user continuity.
9. **CORS misconfig blocks `traceparent`.** RUM-to-backend trace broken.
10. **Vendor lock-in via proprietary trace IDs.** W3C is the standard.
11. **No ad-blocker fallback.** 30-40% data loss treated as random noise.
12. **No release-tagged metrics.** Can't attribute regressions to deploys.
13. **Mobile crash reporting without dSYM upload.** Stack traces useless.
14. **No mobile ANR signal.** App freezes ignored until users complain.
15. **Treating session replay as a server-side log.** Storage cost / privacy debt.

---

## 17. Worked Example: Web Vitals SLO for a Checkout Page

Concrete and end-to-end.

### 17.1 The journey

`/checkout` page. Goal: users perceive it as fast.

### 17.2 The SLIs

```yaml
journey: checkout-page-experience
slis:
  - name: lcp_under_2_5s_us
    metric: rum_lcp_seconds_bucket{le="2.5", page="checkout", country="us"}
    total: rum_lcp_seconds_count{page="checkout", country="us"}
    target: 0.85
  - name: lcp_under_4s_global
    metric: rum_lcp_seconds_bucket{le="4.0", page="checkout"}
    total: rum_lcp_seconds_count{page="checkout"}
    target: 0.75
  - name: inp_under_200ms
    metric: rum_inp_seconds_bucket{le="0.2", page="checkout"}
    total: rum_inp_seconds_count{page="checkout"}
    target: 0.75
  - name: cls_under_0_1
    metric: rum_cls_bucket{le="0.1", page="checkout"}
    total: rum_cls_count{page="checkout"}
    target: 0.75
  - name: js_error_rate
    metric: rum_js_errors_total{page="checkout"}
    inverse_total: rum_page_loads_total{page="checkout"}
    target: 0.999  # < 0.1% pages have errors
```

### 17.3 The dashboards

- LCP histogram per page, segmented by country and device.
- INP percentiles, by interaction type.
- CLS distribution.
- JS error rate per release.
- Top 10 slow pages.
- Slowest 10 customer countries.

### 17.4 The alerts

- Burn-rate (multi-window) on each Web Vitals SLI.
- Spike alert on JS error rate per release (deploy regression detection).
- Geographic anomaly: any country's LCP suddenly 50% worse.

### 17.5 The cross-stack link

JS errors include a trace_id; click an error to jump to the corresponding backend trace and see if the upstream API was slow.

### 17.6 The result

- A team sees user experience honestly.
- Regressions caught at deploy time, not after customer reports.
- Geographic segmentation reveals an Africa-region issue (CDN PoP misconfigured); fix lands within a sprint.

---

## 18. Pitfalls

1. **No client telemetry at all.** Server health hides client experience.
2. **No Web Vitals SLOs.** Page-load regressions ship undetected.
3. **No source maps.** Crashes unsymbolicated.
4. **No geographic segmentation.** Aggregate hides regional pain.
5. **No CORS for traceparent.** RUM-backend link broken.
6. **No ad-blocker resilience.** ~30% telemetry loss treated as noise.
7. **No release tagging.** Regressions un-attributable.
8. **No consent flow.** GDPR fines.
9. **Session replay PII leak.** Compliance disaster.
10. **No quarterly privacy audit.** PII leak persists for months.
11. **Mobile no-ANR signal.** Frozen apps invisible.
12. **No crash-free SLO.** Reliability regression unmeasured.
13. **Sync SDK in head.** First paint blocked; RUM ironically slows the page.
14. **No CDN timing capture.** Client-network split unclear.
15. **No comparison to synthetic.** RUM is messy; synthetic is the baseline.

---

## 19. Mental Models

> **The user's experience is at the user's device. Server p99 is necessary but not sufficient.**

> **Web Vitals are the universal user-experience SLIs. LCP / INP / CLS at the 75th percentile.**

> **RUM and synthetic are complements. Synthetic for outages; RUM for "only some users."**

> **Sample sessions; capture errors at 100%.**

> **Stickify sampling per user. Otherwise continuity is lost.**

> **Source maps are non-negotiable in prod. Without them, error tracking is useless.**

> **Consent is a feature, not a barrier. Build the gating once, ship globally.**

> **Geography is the biggest hidden dimension. Segment SLOs accordingly.**

> **Session replay is powerful and dangerous. Default-mask, audit quarterly.**

> **W3C trace context links the browser to the backend. Without it, end-to-end traces don't exist.**

Now go to `doc 22` (service mesh observability) — the L7 layer of the data plane that sits between client and server.
