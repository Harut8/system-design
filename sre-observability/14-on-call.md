# 14 — On-Call: The Human Reliability Layer

> The most expensive component in any observability stack is the human who answers the page at 3 AM. Every architectural decision in this folder eventually serves or fails that human. This chapter is about designing for *that human's* sustainability — rotation math, runbook standards, page-response economics, the on-call's own SLOs, and the cultural norms a Staff Engineer must hold the line on.

This chapter assumes `doc 12` (alerting) and `doc 13` (SLOs). Pages already arrive correctly; the question now is what happens when they reach a human.

---

## Table of Contents

1. [The thesis: on-call is paid engineering, not goodwill](#1-thesis)
2. [Rotation design: math and humanity](#2-rotation-design)
3. [The two-tier model](#3-two-tier)
4. [Compensation, ethics, and the legal floor](#4-compensation)
5. [Handoff: what makes one rotation set up the next](#5-handoff)
6. [Runbooks: the on-call's only friend at 3 AM](#6-runbooks)
7. [Runbook-as-code](#7-runbook-as-code)
8. [The page response loop](#8-page-response)
9. [On-call health metrics (the platform SLOs of a rotation)](#9-on-call-health)
10. [The on-call survey](#10-on-call-survey)
11. [Onboarding and shadow rotations](#11-onboarding)
12. [Multi-team escalation](#12-escalation)
13. [Anti-patterns and ten-year fixes](#13-anti-patterns)
14. [Tools: PagerDuty, Opsgenie, Squadcast, incident.io](#14-tools)
15. [On-call for the observability platform itself](#15-platform-on-call)
16. [Worked example: a checkout-team rotation](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims a Staff Engineer must defend in any compensation or staffing conversation:

1. **On-call is engineering work.** It is not goodwill, not "part of the salary," not optional. It is a *product* the team delivers — reliability — and the labor must be staffed and paid like any other product responsibility.
2. **A bad on-call shift is a system problem, not a person problem.** The fix is in the alerts, the runbook, the architecture, the staffing — never "be more diligent." Treating bad shifts as personal failings is the fastest way to lose senior engineers.
3. **The on-call is a platform user.** Their experience is a platform SLI. If they can't ack a page in 5 minutes, find the runbook, run a clean playbook, and hand off — that's a *platform team bug*, not an on-call's bug.

If your org has on-call but doesn't (a) compensate it, (b) staff it for sustainability, (c) measure its health, and (d) treat its complaints as bugs — you have on-call in name only, and you are about to lose people.

---

## 2. Rotation Design

The math behind a sustainable rotation.

### 2.1 The rotation primitives

| Variable | Definition | Sustainable target |
|---|---|---|
| `N` | People in rotation | ≥ 6 for primary 24/7 |
| `D` | Length of one shift (days) | 7 (a "week") is standard |
| `cadence` | How often each person is on | once every `N × D / 7` weeks |
| `pages_per_shift` | Pages received during the shift | ≤ 3 (sustained mean) |
| `sleep_pages` | Pages between 22:00 – 06:00 | ≤ 1 per shift, ≤ 1 per quarter for an individual |
| `business_hours_load` | Pages 09:00 – 17:00 | ≤ 2/day |

### 2.2 The "1-in-N" formula

If you rotate `N` engineers for a 7-day shift, each engineer is on-call **1 in N weeks** = `52 / N` weeks per year.

| `N` | Time on-call | Sustainable? |
|---|---|---|
| 3 | 17.3 weeks/year (33%) | No — burnout in 1-2 quarters |
| 4 | 13 weeks/year (25%) | Tolerable for short stretches |
| 6 | 8.7 weeks/year (17%) | Sustainable for 24/7 |
| 8 | 6.5 weeks/year (12.5%) | Comfortable, allows leave |
| 10+ | < 5 weeks/year | Excellent; expensive |

**The "≥ 6 people for 24/7 primary" rule is empirical** (verified across hundreds of teams in PagerDuty's published research, the Google SRE book, and Honeycomb's 2022 on-call survey). Below 6, attrition becomes the bottleneck — engineers leave faster than you can hire.

### 2.3 Coverage models

| Model | Description | When |
|---|---|---|
| **Follow-the-sun** | Three rotations across regions, each covers business hours | Best for global orgs ≥ 18 engineers; eliminates sleep pages |
| **Single time zone, 24/7** | One rotation covers all 24 hours | Works for ≥ 6 engineers; pays the sleep-page cost |
| **Business-hours only** | Pages outside hours fall to a smaller secondary | Works only if SLOs allow >12h MTTR for off-hours pages |
| **Two-tier (primary + secondary)** | Primary first, secondary backstop | Standard at scale (§3) |

The follow-the-sun model is the *gold standard* if you have the headcount geographically distributed. It eliminates the single most damaging on-call externality: sleep deprivation. If you don't have the heads, single-region 24/7 is fine — but compensate sleep pages explicitly.

### 2.4 Shift length

7-day shifts are standard. Reasoning:
- Short enough that any one person isn't dominant in handling a big incident.
- Long enough to amortize the context-switch cost (you ramp into "incident mode," then fully relax for `N-1` weeks).
- Fits naturally with weekly business cadence (planning, retros).

**24-hour shifts** are rare in tech (common in medicine). They can work for very high-page volumes where the on-call is essentially a full-time role for that day. Avoid in normal operations.

**14-day shifts** are too long — burnout, handoff degradation. Avoid.

### 2.5 Override and swap mechanics

Every rotation has emergency overrides. The mechanics:

- **Soft swap.** Two team members trade weeks; PagerDuty/Opsgenie supports this with one click.
- **Day-level override.** "I have a wedding Saturday; cover me." Soft swap one day.
- **Hard medical / family.** Team lead silent-rotates and announces. No questions asked.
- **PTO bake-in.** Schedule rotations *after* PTO calendar; never schedule someone for vacation week.

Overrides should be ~5% of shifts. Higher = staffing too thin.

---

## 3. The Two-Tier Model

For any non-trivial production service.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    PAGE FIRES                                            │
│                         │                                                │
│                         ▼                                                │
│            ┌────────────────────────┐                                    │
│            │  PRIMARY ON-CALL       │   responds in ≤ 5 min              │
│            │  (~1 person / week)    │   handles                          │
│            └─────────────┬──────────┘                                    │
│                          │                                               │
│                  ack within 5m?                                          │
│                  │              │                                        │
│                 yes            no (or, primary asks)                     │
│                  │              │                                        │
│                  │              ▼                                        │
│                  │   ┌────────────────────┐                              │
│                  │   │ SECONDARY ON-CALL  │   responds in ≤ 15 min       │
│                  │   │ (~1 person / week) │   acts as IC if needed       │
│                  │   └─────┬──────────────┘                              │
│                  │         │                                             │
│                  │     ack/handle                                        │
│                  │         │                                             │
│                  ▼         ▼                                             │
│            ┌──────────────────────────┐                                  │
│            │ ESCALATION (manager,     │  if both miss, or if scope       │
│            │ team lead, IC)           │  warrants formal IC              │
│            └──────────────────────────┘                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Primary

- Owns initial response.
- Triages: actual incident? false positive? known issue?
- For real incidents: declare, mitigate, escalate to secondary if scope grows.
- Owns the handoff at end of shift.

### 3.2 Secondary

- Backstops the primary (missed page, asleep, in a meeting, etc).
- Available within 15 minutes for any escalation.
- Becomes Incident Commander for non-trivial incidents (because primary should be focused on action, not coordination).
- Often the first reviewer of postmortem timelines.

### 3.3 Why two tiers, not one

Three reasons:
1. **Primary will miss pages.** Not because they're lazy — because life. Sleep, kids, a doctor's office. Secondary catches.
2. **Two people handle incidents better than one.** Coordination + action splits cleanly. Primary acts; secondary commands.
3. **Secondary is the bench for next quarter's primary.** Onboarding model: shadow → secondary → primary.

### 3.4 The "secondary becomes IC" pattern

For incidents > 15 minutes, the *primary* should not be Incident Commander. Cognitive load is too high — they're investigating, communicating, deciding, and trying to fix all at once. The secondary takes over IC; primary focuses on technical action. This is the single most important on-call mechanic for incident quality. Practice it; codify it.

---

## 4. Compensation, Ethics, and the Legal Floor

This section is short, blunt, and load-bearing.

### 4.1 The ethical floor

On-call is *work performed outside of contracted hours* with non-trivial cognitive load (you carry a phone, you can't drink, you can't travel without coverage, sleep is interrupted). It is paid labor. Anything else is wage theft.

### 4.2 Common compensation models

| Model | Detail | Notes |
|---|---|---|
| **Stipend per shift** | $200–$1500 per week of primary | Most common; flat, predictable |
| **Hourly outside hours** | 1.5–2× hourly rate for time on-call | Used in regulated industries (EU, Germany especially) |
| **Page-incident pay** | Per-incident bonus | Risky — incentivizes unfixed alerts |
| **Time off** | Day in lieu after sleep-paged shift | Excellent practice; pairs with stipend |
| **None** | "Part of your salary" | Anti-pattern; expect attrition |

A reasonable baseline for senior engineers in 2026: **$500–$1000 weekly stipend + comp time for any night-paged shift.** Sub-Staff levels ($300–$500) typically scale with seniority.

### 4.3 The legal floor

Some jurisdictions (Germany, France, UK in regulated industries, California in some interpretations) have *legally mandated* on-call pay. Get HR involved early. Non-compliance is a liability.

### 4.4 Why this matters technically

Underpaid on-call → fast attrition → small rotation → more pages per person → worse alert hygiene → more pages → worse alerts → 3-people rotations and outages. Compensation is *upstream* of every reliability outcome. Staff Engineers who advocate for it are doing reliability engineering, not HR work.

---

## 5. Handoff: What Makes One Rotation Set Up the Next

The single most under-engineered part of on-call. Most teams "hand off" by silence — Friday at 5pm, primary stops paying attention, new primary starts at 9 Monday with no context.

### 5.1 The handoff document

A 5-minute write-up at the end of every shift:

```markdown
# On-Call Handoff: 2026-04-29 – 2026-05-05

## Outgoing: Alice
## Incoming: Bob

## Open issues handed off
- INC-1234: Checkout p99 elevated, mitigated via vendor
  kill-switch. Vendor liaison engaged. Expected resolution Tue.
  Watch: panel "checkout-vendor-degradation".
- INC-1240: Kafka consumer lag warning, resolved but suspect
  recurrence. Look at consumer-A if you see lag warning again.

## Pages this shift
- 7 pages, 5 actionable, 2 false positive.
  - 2 false positives both came from the same rule
    (cert-expiry-warning); ticket REL-456 to retune.

## Things to watch
- Deploy Tuesday 14:00 — checkout team. Increased blast risk.
- Migration step 4 starts Wed. Secondary should join.

## Documentation
- Updated runbook for "checkout vendor degradation" with the
  vendor liaison phone number.

## Anything else
- I'd raise the question whether HighQueueLength should be
  ticket, not page. Let's talk in Friday retro.
```

### 5.2 The handoff meeting

15 minutes. Outgoing primary, incoming primary, optionally secondaries. Walks through the document. Outgoing primary explicitly *transfers ownership* of any open page or pending action.

### 5.3 Why this matters

Without a handoff, every shift starts cold. The incoming primary spends Monday morning scrambling to understand what's currently broken. With a handoff, the new shift begins at full situational awareness and can focus on improvement. Handoffs are *the* lowest-cost, highest-impact on-call ritual.

---

## 6. Runbooks: The On-Call's Only Friend at 3 AM

A runbook is a *step-by-step procedure* for handling a specific kind of alert or incident. It is read at 3 AM by someone half-awake whose pattern matching is impaired.

### 6.1 What makes a good runbook

```
Title: Checkout Availability Burn — Fast Burn

OWNED BY: Payments team
LAST REVIEWED: 2026-04-15 (Alice)

WHAT THIS PAGE MEANS
Customer checkouts are failing more than the SLO permits. 2% of
the 28-day error budget burned in the last hour.

IMMEDIATE ACTIONS (first 5 minutes)
1. Open the dashboard: <link>
2. Identify the failing component:
   - 5xx from gateway? → §A
   - 5xx from upstream payment vendor? → §B
   - timeouts > 5%? → §C

§A. GATEWAY 5XX
1. Check recent deploys: <link>
2. If deploy in last 30 min: roll back via <command>.
3. If no recent deploy: page secondary, escalate to IC.

§B. VENDOR 5XX
1. Check vendor status page: <link>
2. Enable kill-switch flag for vendor: <command>
3. Page the vendor liaison: +1-555-0123
4. Customer-facing comms: status page update via <link>

§C. TIMEOUTS
[continued...]

ESCALATION
- Page secondary if symptoms persist > 15 min.
- Declare formal incident if customer impact > 10 min.
- Open #incident-checkout in Slack; @ team lead.

VERIFY MITIGATION
- Dashboard: <link>
- SLO panel: <link>
  Burn rate must drop below 1× within 5 minutes of mitigation.

POST-INCIDENT
- File postmortem ticket: <link>
- Update this runbook with anything you learned.
```

### 6.2 The runbook standards

Six rules every runbook must follow:

1. **Linked from the alert.** The page itself includes the runbook URL in the annotation. No searching at 3 AM.
2. **Ownership clearly named.** Which team owns this, who reviewed it last.
3. **Immediate actions in the first paragraph.** Don't bury the lede in context. The on-call wants the *playbook*, not the architecture.
4. **Branching by symptom.** Not "do all of A then all of B" — "if X, do A; else if Y, do B."
5. **Concrete commands, not "investigate."** Copy-paste-able commands or links. "Check the dashboard" is not a runbook step; "open <link>; if the panel `gateway-error-rate` shows red, run `kubectl rollout undo deploy/checkout`" is.
6. **Mitigation criteria.** How do you know when you're done? "Burn rate drops below 1× for 5 minutes" — concrete and verifiable.

### 6.3 The "no runbook = no alert" rule

If an alert has no runbook, it doesn't ship. CI rejects rules without `runbook` annotations. Without this rule, on-call inherits a graveyard of half-documented alerts and triages by archaeology.

---

## 7. Runbook-as-Code

Runbooks belong in the same git repo as the service. Why:

- **Version-controlled.** "Did the runbook change before the incident?" is a `git log` away.
- **Reviewable.** Runbook changes go through PR review; reviewers catch ambiguities.
- **Diffable.** Postmortem updates to the runbook show as a clear diff.
- **Linked from code.** A runbook for "kafka-consumer-lag" lives near the consumer code; it's likely to be updated when the code changes.

### 7.1 Layout

```
service-checkout/
  src/
  Dockerfile
  runbooks/
    README.md
    checkout-availability-fast-burn.md
    checkout-latency-burn.md
    vendor-degraded.md
    db-replica-lag.md
  alerts/
    rules.yaml          # references runbooks via annotation
```

The `runbook` annotation in alert rules is a URL that resolves to the markdown file (e.g., `https://github.com/org/service-checkout/blob/main/runbooks/checkout-availability-fast-burn.md`). PR diff tooling can warn when a rule changes without a runbook update.

### 7.2 Runbook reviews

Quarterly: each runbook is read by the on-call champion. Two questions:

1. *Are the commands still correct?* (Tools change; URLs rot; flags are renamed.)
2. *Has anyone used this runbook in the last quarter? If so, was it sufficient?*

Stale runbooks are a worse failure than no runbook — they actively mislead. Test them.

### 7.3 Runbook automation

Some runbook steps can be *executed*, not just *documented*. Tools (Rundeck, StackStorm, GitHub Actions, internal "runbook-as-Slack-bot" patterns) let the on-call run a step with one command.

```
/runbook checkout-vendor-kill-switch
✅ Kill switch enabled for vendor: stripe-eu (by alice@, 2026-05-05 03:14)
```

The audit log captures who ran what, when. The on-call doesn't fat-finger a multi-arg `kubectl` at 3 AM. *This is the highest-leverage on-call tooling investment most teams haven't made.*

---

## 8. The Page Response Loop

What an on-call actually does, second-by-second, when paged.

```
T+0    Page fires.
T+30s  Phone vibrates / voice call / Slack ping.
T+90s  On-call sees notification. Reads alert summary. Acks page.
       (PagerDuty starts MTTA timer.)
T+120s Opens runbook + dashboard from links in the page.
T+5m   Decides: real or false positive?
        - false: silence with comment, file ticket.
        - real: continue.
T+7m   Begins immediate actions per runbook.
T+10m  If not mitigated, considers declaring incident, paging secondary.
T+15m  Secondary engaged; primary stays on action; secondary takes IC.
T+20m  Mitigation in place; user impact ending.
T+30m  Verifying mitigation via SLO panel.
T+45m  Mitigation confirmed. Status page updated. Postmortem ticket
       filed with timeline so far.
T+1h   Handoff if shift ends; otherwise continue investigation.
T+1d   Postmortem draft due (per the postmortem cadence).
```

The 0-15-minute window is the most important. Every architectural choice that shaves 30 seconds off this loop is worth pursuing — the on-call's cognitive bandwidth is most strained then.

### 8.1 The cognitive load curve

```
    cognitive load
     │
     │     ▲
     │    /│\
     │   / │ \
     │  /  │  \      ← peak around 5-10 min: triage, decide, act
     │ /   │   \
     │/    │    \
     ├─────┼─────────────────────→ time
     T+0   T+10m    T+30m    T+1h
```

The peak at 5-10 minutes is when the page has been ack'd, the runbook is open, and the on-call is *deciding what to do*. Every UX choice that simplifies this moment helps. Conversely: anything that adds load (multiple unrelated pages, ambiguous runbook, slow dashboard) compounds.

---

## 9. On-Call Health Metrics

The platform team's SLOs for on-call quality.

### 9.1 The four health metrics

| Metric | Definition | Healthy target |
|---|---|---|
| **Pages per shift** | Total paged events per 7-day rotation | ≤ 3 (mean), ≤ 7 (95th) |
| **Sleep-pages per shift** | Pages 22:00 – 06:00 local | ≤ 1 |
| **MTTA** | Median ack time | ≤ 5 minutes |
| **Actionable rate** | Fraction of pages that led to action | ≥ 80% |

Track these per team. Publish them. Treat regressions as platform incidents.

### 9.2 The "weeks since a clean shift" counter

A morale-positive metric: *days/weeks since the last shift had < 3 pages and zero sleep pages*. Counterintuitively, this is the metric that drives the most positive behavior — teams compete to extend the streak. The metric is *visible* and *aspirational*, not punitive.

### 9.3 Pager fatigue

Symptoms (watch for these in retros):
- Engineers ack pages in < 30s without reading the title — they've stopped processing.
- Sleep pages drop in MTTA but the engineer can't recall what the page was about the next morning.
- Engineers swap shifts more than 5% of the time.
- Senior engineers pre-emptively change teams "to escape on-call."

The fix is *never* "be more diligent." It is alert hygiene, automation, runbook investment, sometimes architecture. Treat fatigue as a system failure.

### 9.4 The lifelong-fitness analogy

On-call sustainability is like physical fitness. You can't grind it; you have to *train* it.

- Page count = volume of work; too high causes injury.
- Alert hygiene = good form; bad form injures even at low volume.
- Recovery (sleep, time off) = essential.
- Variety (paired secondaries, cross-team) = keeps engagement.
- A "10× engineer" theory of on-call is wrong; it's a team sport.

---

## 10. The On-Call Survey

Quarterly, anonymous, 5 questions:

1. How many shifts did you have last quarter, and how many were "good" (manageable, learned something) vs "bad" (burnt out, embarrassed by the alerts)?
2. Did any shift include a page you couldn't fix? Why?
3. Did any runbook fail you? Which one?
4. Was there an incident you only learned about from a customer, not an alert?
5. What one thing would have made the last quarter better?

Aggregate. Action top 3. Report back next survey.

The survey is the single most efficient feedback channel. Engineers complain about on-call in 1:1s, but those complaints are siloed and rarely reach the platform team. The survey is the structured channel that turns complaints into backlog items.

---

## 11. Onboarding and Shadow Rotations

Don't put a new engineer on primary on day 1.

### 11.1 The progression

| Week | Role | What they do |
|---|---|---|
| 1-2 | Shadow | Sits with primary; reviews historical pages; reads runbooks |
| 3-4 | Secondary | Real role, but primary is the catch |
| 5-8 | Paired primary | A senior engineer is paired secondary |
| 9+ | Solo primary | Standard rotation |

Two-month onboarding is appropriate for production-load services. Shorter for lower-stakes services. Longer (3-6 months) for highly-regulated or financially-impactful services.

### 11.2 The shadow rotation

A shadow doesn't ack pages — they *receive a copy* of every page the primary gets, with no SLA. They watch the primary respond, ask questions, draft what they would have done.

Shadow shifts are surprisingly high-signal. New engineers learn what kinds of pages happen, what the runbooks miss, what the team's tribal knowledge looks like. They emerge as much better primaries.

### 11.3 The "first incident" milestone

Every new on-call has a first solo incident. Celebrate it. Then debrief it: what went well, what didn't, what's missing from the runbook. The first incident's postmortem updates the runbooks for everyone.

---

## 12. Multi-Team Escalation

When the page crosses team boundaries.

### 12.1 The "owner of the journey" pattern

Every top user journey (`doc 13 §10`) has an *owning team*. When a journey-burning page fires, that team's primary is paged first. They:

1. Triage; identify the contributing service.
2. If the issue is in their service, fix.
3. If the issue is in another service, *page that service's primary too*. Don't try to fix someone else's code at 3 AM.

### 12.2 The dependency graph

A page from a downstream team about a *root* failure should never escalate up to a third team. The dependency graph (often expressed in Backstage / catalog systems) defines the chain.

```
Checkout journey
  ├── checkout-svc (Payments team)        ← owns journey SLO
  ├── auth-svc (Identity team)
  ├── pricing-svc (Catalog team)
  └── order-svc (Orders team)

When journey burns and the cause is auth-svc:
  - Payments team primary pages first (journey owner).
  - Payments primary pages Identity team primary.
  - Identity primary takes ownership of the technical fix.
  - Payments primary stays IC, runs comms.
```

This is the **journey-owner-as-IC** pattern. It scales remarkably well.

### 12.3 The "bear with me" rule

When a primary pages another team mid-incident, courtesy matters: a quick "we have an active incident, here's what we know, what we need from you" message saves 5 minutes of confusion. Codify it in the runbook.

---

## 13. Anti-Patterns and Ten-Year Fixes

Each one is real; each fix is concrete.

### 13.1 The "everything is a page"

Symptom: 30+ alerts paging, 5 actionable. Engineers ignore most.
Fix: §9 in `doc 12` — the four-question audit. Delete the 25 unactionable ones in one PR.

### 13.2 The "single hero"

Symptom: one engineer handles 80% of pages, knows everything, never sleeps.
Fix: this is staffing failure, not heroism. The hero is a single point of failure for the entire team. Recruit, document tribal knowledge, rotate aggressively.

### 13.3 The "I'll figure it out"

Symptom: no runbooks; on-call investigates from scratch every time.
Fix: every alert without a runbook gets one *or* gets deleted. Quarterly runbook review.

### 13.4 The "don't ack until I figure it out"

Symptom: on-call delays acking to "look professional."
Fix: ack immediately means "I see the page, working on it." It is *not* a commitment that you understand. Train the team to ack within 60s, then investigate.

### 13.5 The "single tier"

Symptom: only primary; no secondary.
Fix: any non-trivial service needs two tiers. Period.

### 13.6 The "all night, alone"

Symptom: a 4-hour incident handled by one person, no IC, no secondary.
Fix: at the 15-minute mark, secondary must engage and take IC. Codify in runbook.

### 13.7 The "compensation invisible"

Symptom: nobody knows what the on-call stipend is, or there isn't one.
Fix: document it in the team handbook; review annually with finance/HR.

### 13.8 The "PTO penalty"

Symptom: on-call rotation continues during vacation; engineer covers anyway.
Fix: PTO is sacred. Cover before scheduling. Hard rule.

### 13.9 The "shame the missed page"

Symptom: a missed sleep-page leads to a 1:1 lecture.
Fix: missed pages are *system* problems. Treat them as a backup-coverage gap to fix, not a personal failing. (Routine missed pages are different — that's a fitness or fit problem, handled in a different conversation.)

### 13.10 The "rotation never reviewed"

Symptom: the rotation has the same shape since 2019; team has changed.
Fix: every 6 months, on-call champion reviews rotation. Adjust composition, length, compensation.

---

## 14. Tools

The 2026 landscape.

| Tool | Best for | Notes |
|---|---|---|
| **PagerDuty** | Default for most US-based teams | Mature; expensive; great escalation policies |
| **Opsgenie** | Atlassian shops | Decent; integrates with Jira |
| **Squadcast** | Cost-conscious mid-size | Cheaper; growing feature set |
| **Grafana OnCall** | Grafana-stack shops | Open-source; integrates tightly with Grafana Alerting |
| **incident.io** | Modern incident management + paging | "On-call as a feature of incident management"; rapidly adopted |
| **FireHydrant** | Incident-management-first; pagers as side feature | Good for mature SRE teams |
| **Splunk On-Call (formerly VictorOps)** | Splunk shops | Solid; less momentum |
| **Self-hosted (Cabot, Karma)** | Cost-extreme orgs | Possible; rarely worth it |

The 2026 trend is *consolidation of paging into incident-management platforms* (incident.io, FireHydrant). The all-in-one suite reduces tool sprawl: one place for paging, incident creation, comms, postmortem.

### 14.1 What to evaluate

When picking:

1. **Schedule complexity.** Multi-region follow-the-sun? Holiday handling? Override workflow?
2. **Escalation policies.** Multi-tier with timeouts? Page chains?
3. **Mobile reliability.** App quality matters more than feature count.
4. **Audit log.** "Who ack'd when, who silenced when" must be queryable.
5. **API maturity.** For automation: rotation queries, ack-from-Slack, runbook integration.
6. **Incident integration.** Does it create incident records, or just send pings?
7. **Cost at scale.** Per-user pricing is the silent budget killer at 200+ engineers.

---

## 15. On-Call for the Observability Platform Itself

A subtle pitfall: the team that operates the observability stack also has on-call. It's the most stressful on-call in the org, because their pages are *about the tool everyone else uses to detect outages*.

### 15.1 Special considerations

- **Independent paging path.** If your alerting system is the thing that's down, you can't page via it. A second, independent paging path (synthetic check → external pager) is mandatory.
- **Higher-severity SLO.** The platform must be more reliable than the services it observes. If your service-tier SLO is 99.9%, your platform-tier SLO should be 99.95–99.99%.
- **Internal status page.** A separate, lightweight page that says "the observability platform is healthy" — independent of the platform itself.

### 15.2 The "dogfooding" trap

The observability team observes itself with the same stack. When the stack breaks, the observation of the stack breaks. The fix is to have *some* signals on a *minimal independent path*: a tiny Prometheus instance scraping a few critical platform metrics, exporting to a different paging path. Not all the data — just enough to detect "the main platform is down."

### 15.3 The platform engineering mindset

The observability platform team holds itself to platform-product standards (`doc 17` PRR / `doc 19` multi-tenancy / `doc 36` DR). Their on-call quality is the *upper bound* on every other team's on-call quality. Invest accordingly.

---

## 16. Worked Example: A Checkout-Team Rotation

Concrete, end-to-end.

### 16.1 Team composition

8 engineers. 2 senior, 4 mid, 2 junior. All in PST (single-region 24/7).

### 16.2 Rotation design

- **Primary:** rotates weekly among 6 (excluding 2 most-junior). 1-in-6 = 8.7 weeks/year.
- **Secondary:** rotates weekly among 8 (all participate). 1-in-8 = 6.5 weeks/year. Provides shadow exposure for juniors.
- **Compensation:** $700/week stipend primary, $300/week secondary. Day-in-lieu after any night-paged shift.
- **Onboarding:** new hires shadow 4 weeks, secondary 4 weeks, paired primary 4 weeks before solo.

### 16.3 Page volume

Steady state:
- 2 pages / shift mean
- 95th percentile: 5 pages / shift
- Sleep pages: 0–1 per shift
- MTTA: ~3 minutes
- Actionable rate: 87%

### 16.4 The handoff ritual

Every Friday at 16:00 PST:
- Outgoing primary writes handoff doc by 15:30.
- 15-minute meeting at 16:00 with incoming primary (and secondaries if available).
- Incoming primary takes over PagerDuty schedule at 17:00.
- Outgoing primary still nominally "on" until midnight PST (in case of immediate incident continuity).

### 16.5 Quarterly retro

90 minutes. The on-call champion (a rotating role, 6-month term) facilitates:

1. Page volume trend per shift.
2. Top 3 most-fired alerts; review hygiene.
3. Top 3 runbooks used; were they sufficient?
4. Survey results.
5. Backlog of automation / hygiene items.
6. Decision: any rotation changes for next quarter?

### 16.6 Year-end metrics (made-up but realistic)

- Mean pages/shift: 2.3 → 1.6 (year-over-year)
- Actionable rate: 81% → 89%
- MTTA: 4.5 min → 2.8 min
- Sleep pages per quarter: 7 → 3
- Engineer satisfaction (survey 1–5): 3.4 → 4.2

These are the numbers that justify the on-call investment to leadership. Without them, on-call is invisible labor; with them, it's a measurable engineering practice.

---

## 17. Pitfalls

1. **Rotation < 6 people for 24/7.** Burnout, attrition, downstream alert decay.
2. **No compensation.** Wage theft; attrition.
3. **No secondary tier.** Missed pages, single-point-of-failure during incidents.
4. **No handoff.** Each shift starts blind.
5. **Runbooks not linked.** On-call wastes minutes searching at peak cognitive load.
6. **Runbook with no concrete commands.** "Investigate" isn't a step.
7. **Stale runbooks.** Worse than no runbook — actively misleads.
8. **No on-call survey.** Complaints are anecdotal; never reach the platform team.
9. **PTO not protected.** Vacations get cancelled; trust erodes.
10. **Single hero.** One person knows everything; SPOF for the team.
11. **No onboarding.** New hires panic on first shift.
12. **No two-tier IC pattern.** Primary tries to investigate, communicate, decide, fix simultaneously — drops one.
13. **Page actionability not measured.** Hygiene degrades silently.
14. **No quarterly retro.** Issues accumulate.
15. **Platform on-call without independent paging path.** The thing is down; the page can't fire.

---

## 18. Mental Models

> **On-call is paid engineering work, not goodwill.** Compensate it. Staff it. Measure it.

> **Bad shifts are system bugs.** Fix the alerts, the runbooks, the architecture. Never blame the human.

> **The on-call is a platform user.** Their experience is a platform SLI; the platform team owns it.

> **Two tiers, IC pattern.** Primary acts; secondary commands. For any non-trivial incident.

> **Six people minimum for 24/7.** Below that, the math breaks.

> **The runbook is the on-call's only friend at 3 AM.** Linked, concrete, branching, mitigation criteria.

> **Handoff is the highest-leverage ritual.** 15 minutes at end of shift = full situational awareness for the next.

> **Rotate roles, not heroics.** A team that depends on one expert is one resignation away from collapse.

> **Survey quarterly. Action top 3. Repeat.** The structured channel that turns complaints into improvements.

> **Platform on-call is the upper bound.** The observability team's reliability caps everyone else's.

Now go to `doc 15` (incident response & postmortem) — what happens when on-call's mitigation isn't enough and the situation becomes a formal incident.
