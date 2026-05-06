# 15 — Incident Response and Postmortem

> The first 60 minutes decide how bad an incident is for the customer. The next 60 days decide what the org learns from it. Most teams optimize neither — they "respond" by improvising, and they "postmortem" by writing a document nobody reads. This chapter is the Staff-Engineer-grade discipline for both halves: the live-fire incident loop, and the institutional-memory practice that turns each incident into one fewer next time.

This chapter assumes `doc 12` (alerting), `doc 13` (SLOs), and `doc 14` (on-call). Pages have arrived; the on-call is on the keyboard. Now what?

---

## Table of Contents

1. [Thesis: incidents are inevitable; learning is optional](#1-thesis)
2. [The incident lifecycle, end to end](#2-lifecycle)
3. [Severity classification (and why most stacks get it wrong)](#3-severity)
4. [Roles: IC, scribe, ops, comms, exec liaison](#4-roles)
5. [The first 60 minutes](#5-first-60-minutes)
6. [Mitigation before understanding](#6-mitigation)
7. [Communications: status page, internal, executive](#7-comms)
8. [The IC's checklist](#8-ic-checklist)
9. [Tooling: incident.io, FireHydrant, Slack-native, custom](#9-tooling)
10. [Game days and tabletop exercises](#10-game-days)
11. [The blameless postmortem in depth](#11-blameless)
12. [Postmortem structure: timeline, factors, action items](#12-postmortem-structure)
13. [Action items: tracking, closure, the 70% bar](#13-action-items)
14. [The "five whys" trap and what to do instead](#14-five-whys)
15. [Postmortem reviews: open, learning, archived](#15-postmortem-reviews)
16. [The institutional memory layer](#16-institutional-memory)
17. [Anti-patterns](#17-anti-patterns)
18. [Worked example: a 47-minute checkout incident](#18-worked-example)
19. [Pitfalls](#19-pitfalls)
20. [Mental models](#20-mental-models)

---

## 1. Thesis

Three claims to defend:

1. **Mitigation precedes understanding.** During an incident, the bleed must stop *before* the cause is understood. Engineers who insist on diagnosing first while the customer suffers cause the most damage. Stop the bleed, then learn.
2. **Postmortems are blameless or they're worthless.** The moment an org tolerates "but Alice deployed it" framing, every future engineer hides their part. Truth flows from psychological safety; without it, you have writeups, not postmortems.
3. **Action items are the postmortem product, not the document.** A postmortem with ten brilliant insights and no closed action items is theater. Track action item closure rate as a platform SLO; below 70% is a fire.

If your org has incidents but the same kind keeps happening every quarter, your problem is not the incidents. It's the postmortem-to-action loop. This chapter is the loop.

---

## 2. The Incident Lifecycle

```
   workload ──► alert fires ──► page ──► ack
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ DETECT   │   MTTD: time from event to anyone noticing
                                     └────┬─────┘
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ DECLARE  │   "this is an incident; assign IC"
                                     └────┬─────┘
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ TRIAGE   │   roles assigned; impact assessed
                                     └────┬─────┘
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ MITIGATE │   STOP THE BLEED. Roll back, drain, fail over.
                                     └────┬─────┘   MTTM: time from detect to bleed stopped.
                                          │
                                          ▼
                                     ┌──────────┐
                                     │COMMUNICATE│  status page, executives, customers — every 30m
                                     └────┬─────┘
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ RESOLVE  │   primary symptoms cleared; impact ended
                                     └────┬─────┘   MTTR.
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ HANDOFF  │   IC writes timeline; ensures backoff; schedules
                                     └────┬─────┘   postmortem.
                                          │
                                          ▼
                                     ┌──────────┐
                                     │POSTMORTEM│   blameless writeup, action items
                                     └────┬─────┘
                                          │
                                          ▼
                                     ┌──────────┐
                                     │ FOLLOW UP│   action items closed; backlog updated
                                     └──────────┘
```

The single most-violated rule: **the order matters.** Engineers love to skip "mitigate" and go straight to "understand." That choice multiplies customer impact.

---

## 3. Severity Classification

Without a severity scheme, every incident is treated the same. With a *bad* severity scheme, every incident is somehow "SEV-2." The single most common org-mistake.

### 3.1 The four-tier scheme that works

| Severity | Customer impact | Examples | Response |
|---|---|---|---|
| **SEV-1** | All customers, complete failure of a critical journey, or data loss / corruption | Checkout entirely down; data integrity violated | All hands; war room; exec on call; status page red; comms every 15m |
| **SEV-2** | Significant fraction of customers, or full failure of a non-critical journey, or sustained degradation | 30% of checkouts failing; admin panel down | IC + 2-3 engineers; #incident channel; status page yellow; comms every 30m |
| **SEV-3** | Limited customer impact, or single-customer issue, or warning | One feature broken; one tenant impaired | One engineer + on-call; tracked but not war-roomed |
| **SEV-4** | Internal-only; near-miss; precursor | A retry succeeded; no customer impact but a real issue | Logged, ticketed, analyzed in retros |

### 3.2 The declaration rule

**If in doubt, declare a higher severity. You can downgrade later.** Under-declaration costs customer impact; over-declaration costs ~30 minutes of engineer-hours. The asymmetry is obvious; the human bias is still toward under-declaration ("we don't want to look alarmist").

### 3.3 The SEV-1 trigger sentence

When the on-call says: *"I'm not sure if I can fix this in 15 minutes,"* declare SEV-1. The trigger isn't certainty of severity — it's *uncertainty of timing* combined with customer impact. Beat the impulse to "wait and see."

### 3.4 SEV-1 specifics

- Wakes the on-call manager (always).
- Pages exec liaison if customer impact > 30 minutes (or any data loss/corruption).
- Triggers status-page red.
- Customer comms within 30 minutes.
- Postmortem mandatory; 24-hour SLA on draft.
- Action items go to the next sprint's planning automatically.

---

## 4. Roles

The single biggest improvement most teams can make to incident response is **explicit role assignment within the first 5 minutes.** Without it, everyone is tries to do everything; nothing gets done well.

### 4.1 The five roles

| Role | Owns | Doesn't own |
|---|---|---|
| **Incident Commander (IC)** | Coordination, decisions, declaring start/end, role assignment, comms cadence | Hands-on technical fix |
| **Ops Lead** | Hands-on technical action — running commands, watching dashboards, executing the runbook | Coordination, comms |
| **Scribe** | Writing the timeline as events happen; capturing what was tried and when | Decisions or technical action |
| **Communications Lead** | Status page updates, internal Slack, executive briefings, customer comms | Technical detail |
| **Exec Liaison** | One-way channel from IC to senior leadership; protects engineers from exec interruption | Decisions about the fix |

### 4.2 Why separate IC from Ops

The IC is the conductor; the Ops Lead is the soloist. The conductor's job is *coordination* — who knows what, what was tried, what's the next decision. Trying to also play the solo means the orchestra falls apart.

In small teams, IC and Ops *can* be the same person for SEV-3 and below. For SEV-1 and SEV-2, they *must* be different. This is the single most important on-call mechanic (revisited from `doc 14 §3.4`).

### 4.3 Why scribe matters

The scribe writes a timestamped log of *everything*. Hypothesis tested. Command run. Symptom changed. Decision made. This becomes the postmortem's spine. Without a scribe, the postmortem is reconstructed by archaeology after the fact — and is wrong.

A scribe doesn't have to be an engineer. A program manager, a junior IC trainee, anyone with fast typing and basic technical literacy can scribe.

### 4.4 The unwritten role: the executive

In a SEV-1, executives want to *help*. Their help is usually noise — they ask for status, distract the IC, demand updates the team can't yet provide. The exec liaison's job is to *protect the team from this* by being the executives' single point of contact.

A good exec liaison says, in the words of incident-management practitioner Štěpán Davidovič: *"I will get you an update every 15 minutes. Please do not contact the responders directly. Here is what we know: [...]."* And then enforces the boundary. This is hard the first time; it gets easier.

---

## 5. The First 60 Minutes

A minute-by-minute outline of what *should* happen. Print this, post it in the war room.

```
T+0      Page fires. On-call ack within 90 seconds.
T+2m     Triage: real or false? Open the dashboard linked from the page.
T+5m     Decision point:
           - false positive? File ticket; close.
           - real but contained? Apply runbook; track but don't escalate.
           - real, growing? DECLARE.

T+5m     DECLARE. Open #incident-XYZ in Slack. Page secondary.
         Assign IC (secondary, by default).
T+7m     IC announces: "I am IC for incident XYZ. Severity: assessing.
         We need: [scribe, ops lead, comms]. Speak up."
T+10m    Roles assigned. Severity declared. Status page updated:
         "We are investigating reports of <symptom>."

T+10m    Ops Lead begins runbook execution.
T+12m    First customer-impact assessment by Comms Lead:
         "Approx N customers affected based on telemetry."

T+15m    First MITIGATION attempt. (Roll back? Kill switch? Drain?)
T+20m    Verification. Did it work?
         - Yes: continue to monitoring.
         - No: try next mitigation. Page additional team if needed.

T+30m    Status page update: "We have identified <symptom>; mitigation
         in progress. Next update in 30 minutes."
T+30m    Internal Slack update from IC. Exec liaison sends executive update.

T+45m    If not mitigated: re-assess. Is severity higher than declared?
         Escalate. Wake more people.

T+60m    Mitigation either complete or actively in progress with a
         clear plan. Resolve timeline drafted. Comms cadence set.
```

The structure isn't optional. It works because it removes "what should we be doing now?" from the cognitive load — the IC just runs the script.

### 5.1 The "stop the panic" sentence

A SEV-1 starts with chaos. The IC's first move is to *cut through it*. Use a script:

> "I am IC for incident XYZ. We have <symptom> affecting <scope>. Severity is <SEV>. I need a scribe, an ops lead, and a comms lead to volunteer in the next 60 seconds. Other engineers, please stand by — do not run commands without coordinating with the ops lead. Updates every 15 minutes. Status page going yellow."

The script isn't bureaucratic. It's the verbal equivalent of putting on a seatbelt. Read it. Mean it.

---

## 6. Mitigation Before Understanding

The single hardest cultural rule to enforce. Senior engineers love debugging — and they want to debug *during* the incident. Resist.

### 6.1 The principle

Customer pain is a clock. Every minute the bleed continues is dollars, trust, and SLO budget burned. *Mitigation* is anything that ends the user impact, even if it doesn't fix the cause:

- Roll back to a known-good version.
- Enable a kill switch / feature flag.
- Drain traffic from a degraded zone / region.
- Fail over to a hot standby.
- Redirect to a static error page (better than a hung page).
- Restart processes on a schedule.

None of these *fix* the cause. They *stop* the impact. The cause can wait until after.

### 6.2 The exception

Only one: **data loss or corruption**. If mitigation might *make the data worse* (e.g., rolling back during an in-flight migration could violate consistency), pause and understand. Even then, the rule is *find the safest mitigation that doesn't worsen the integrity issue*.

### 6.3 The "have we tried rollback yet?" question

For incidents starting within 30 minutes of a deploy, the IC's first question after triage is: *have we tried rollback?* Studies (Allspaw, Beyer) and informal data show ~60% of deploys-related incidents resolve on rollback. Trying it costs 5 minutes. Not trying it costs 30+.

### 6.4 Rollback safety

Rollback isn't always safe — schema migrations, in-flight async events, mid-rolling deploys. *That's why* a Production Readiness Review (`doc 17`) gates services on whether they have a safe rollback path. Without it, the team won't have a fast mitigation lever during the incident.

---

## 7. Communications

Three audiences. Three cadences. One IC.

### 7.1 Customer comms (status page)

| Severity | First update | Subsequent | Resolution |
|---|---|---|---|
| SEV-1 | ≤ 15 min from declaration | every 15 min | within 1 hr of mitigation |
| SEV-2 | ≤ 30 min | every 30 min | within 2 hr |
| SEV-3 | optional | as needed | optional |

Status-page best practices:

- **Lead with what's broken.** Not "we are investigating an issue" — "checkouts are failing for some customers."
- **Be specific about scope.** "Limited to EU region" if true.
- **Avoid technical jargon.** "Database error" is fine; "Postgres replica lag" is not.
- **Always say the next update time.** Even if there's no progress, *promised silence is reassuring.*
- **Resolution post must say what happened.** "Issue with our payment vendor; mitigated by routing to backup vendor" — calibrated transparency builds trust.

### 7.2 Internal comms

A dedicated `#incident-XYZ` Slack channel. Pinned message has:
- Severity, IC, roles.
- Status page link.
- Dashboard link.
- Doc link (Google Doc / Notion for the running timeline).

In the channel, the IC posts every 15-30 minutes. *No engineer should need to ask "what's going on?"* Rotate the answer back into the channel as the timeline.

### 7.3 Executive comms

Single point of contact: the Exec Liaison. Sends a structured update every 15-30 minutes:

```
Status: ACTIVE / MITIGATING / RESOLVED
Severity: SEV-1
Customer impact: ~10% of EU checkouts failing for the last 35 min
Cause (so far): Vendor X timing out; auto-retry exhausted
Mitigation: Manual fail-over to Vendor Y in progress; ETA 5 min
Next update: 14:30 UTC

ETA to resolution: ~15 min
```

Five lines. Each one specific. Executives who get this format learn to read it; they stop calling the IC.

### 7.4 The "no estimates" trap

Engineers hate giving ETA. They will be wrong. Give one anyway, and revise. *Silence is worse than a wrong ETA*, because silence implies "we don't know what's happening" which is worse than "we estimated wrong."

---

## 8. The IC's Checklist

A literal checklist to print and tape to the wall.

```
[ ] Severity declared (SEV-?, written down)
[ ] Roles assigned (IC, Ops, Scribe, Comms, Exec liaison if SEV-1)
[ ] #incident-XYZ Slack channel created and pinned
[ ] Running timeline doc opened (Google Doc / Notion / runbook system)
[ ] Status page updated within SLA
[ ] First mitigation attempted
[ ] Customer impact estimated (number, %, geography)
[ ] Internal stakeholders notified (engineering leadership)
[ ] Exec liaison engaged (SEV-1 only)
[ ] Comms cadence agreed (every X minutes)
[ ] Engineers not actively helping told to stand by
[ ] Other teams paged if dependency (with context)

DURING:
[ ] Scribe is logging events
[ ] No commands run without ops lead coordination
[ ] Status page updated on cadence
[ ] Comms updated on cadence

WHEN MITIGATED:
[ ] Verified mitigation is holding (≥ 5 min)
[ ] Status page updated to MONITORING
[ ] Customer impact endpoint confirmed
[ ] Comms cadence relaxed

WHEN RESOLVED:
[ ] Status page RESOLVED
[ ] Final internal update
[ ] Postmortem ticket filed
[ ] Postmortem owner assigned
[ ] On-call handoff if shift changes
[ ] Thank the team
```

The checklist matters because in a SEV-1, working memory degrades. The IC who reads from a checklist outperforms the IC who improvises, every time.

---

## 9. Tooling

The 2026 landscape.

| Tool | Strength | Notes |
|---|---|---|
| **incident.io** | All-in-one: paging + incident creation + Slack-native commands + postmortem | The fast-rising default for new orgs |
| **FireHydrant** | Mature workflow engine, runbook integration | Good for orgs with custom incident processes |
| **PagerDuty Incident Response** | If already using PagerDuty paging | Solid; tightly integrated with PagerDuty |
| **Slack-native bash scripts / GitHub Actions** | Cheap; portable | Works for small teams; doesn't scale |
| **Custom (built in-house)** | Deeply customized | Only if the off-shelf tools genuinely don't fit |
| **Jeli (Acquired by PagerDuty)** | Postmortem-focused | Strong on the learning side |
| **Howie** | Postmortem-focused | Open-source-ish; growing |

### 9.1 What good tooling does

- **One-command incident creation:** `/incident new` in Slack creates a channel, drafts a status page entry, pages on-call, and starts a timeline doc. Five seconds.
- **Role assignment via Slack commands:** `/incident assign @alice as IC`. Logs to timeline.
- **Status page automation:** updates from Slack commands push to the status page directly.
- **Timeline-as-Slack:** the channel itself *is* the timeline. Tools auto-extract events into the postmortem.
- **Runbook execution:** `/runbook checkout-rollback` runs the rollback action and logs it.
- **Post-incident workflow:** auto-creates the postmortem ticket, assigns owners, schedules review.

### 9.2 The "Slack is the war room" pattern

In 2026, most incident response happens in Slack channels (or Microsoft Teams). The tooling layers above orchestrate the channel — adding state, structure, and permanence. Don't fight it; lean in.

The downside: Slack history is ephemeral and search is poor. Always export the timeline to a permanent doc post-incident. The doc is the system of record; Slack is the workspace.

---

## 10. Game Days and Tabletop Exercises

You don't know if your runbooks work until something has gone wrong with humans watching. **Practice on a Tuesday afternoon when the room is calm.**

### 10.1 Game day formats

| Format | Realism | Cost | When |
|---|---|---|---|
| **Tabletop** | Discussion-only; "what would you do if..." | Low ($) | New team; new on-call | 
| **Wargame** | Simulated incident with fake alerts; engineers react in real time | Medium | Quarterly |
| **Game day (chaos injection)** | Real fault injected in pre-prod or production; team responds for real | High | Bi-annually |
| **Surprise game day** | Real fault, no warning to the team | Very high | Mature orgs only |

### 10.2 What a game day reveals

- **Runbook gaps.** "Step 3 says check Grafana — the dashboard URL is broken."
- **Knowledge silos.** "Only Alice knows how to fail over the Kafka cluster."
- **Tool failures.** "PagerDuty escalation didn't fire; secondary never paged."
- **Communication gaps.** "Nobody knew how to update the status page."
- **Cognitive load issues.** "I couldn't remember how to roll back at 3 AM."

### 10.3 The post-game-day retro

Same structure as a postmortem. Action items. Closure tracking. The retro is more valuable than the game day itself; without it, the exercise is performative.

### 10.4 Frequency

- New hires: tabletop in week 4; game day participant in week 6-8.
- Established team: quarterly game day; monthly tabletop on a specific scenario.
- Org-wide: annual major game day across multiple teams.

`doc 38` (continuous verification) goes deeper on chaos engineering and the tooling.

---

## 11. The Blameless Postmortem in Depth

The single most under-appreciated cultural artifact in SRE. *Blameless* is a precise word, not a soft one.

### 11.1 What blameless means

- **Assume good intent.** Every action taken during the incident, every line of code in production — assume the engineer was trying to do the right thing with the information available *at that time*.
- **Focus on systemic factors.** "Why was this possible?" not "Who did this?"
- **Never name a person as a cause.** Roles, decisions, processes, systems — yes. People — no.
- **Create a forum where engineers say what really happened**, including their own mistakes, without fear of HR consequences.

### 11.2 What blameless does NOT mean

- It does not mean *consequence-free*. Repeated negligence is a different conversation, handled in 1:1s, not postmortems.
- It does not mean *no accountability*. Action items have owners; teams own their service's reliability.
- It does not mean *nobody was wrong*. A line of code was wrong; a process was inadequate. Naming *what* failed isn't blame; naming *who* is.

### 11.3 The cultural mechanics

Blameless culture is a *practice*, not a poster. Specific norms:

1. **Postmortems are pre-circulated; nobody is surprised.** The author shares with the contributing engineer first; gets their version of events; incorporates their context.
2. **The postmortem reading is structured.** First the timeline. Then the contributing factors. Then action items. Each person speaks; nobody is in the hot seat.
3. **The senior-most person in the room speaks first about their own mistakes.** This sets the tone. "I should have flagged this in the design review" — said by the manager — invites the rest of the room to be honest.
4. **Pronouns matter.** *We* did, not *Alice* did. Third person plural.
5. **Pausing on judgment language.** "Should have" → "could have." "Failed to" → "was not yet able to." Linguistic discipline.

### 11.4 The pathologies

Without psychological safety:
- Engineers under-report incidents (especially near-misses).
- Postmortems become political documents.
- Senior leadership reads them as performance reviews.
- Team learns to *avoid* incidents rather than *recover from* them.

The cost of the bad culture is not visible quarter-to-quarter, but it compounds. The org slowly becomes brittle; engineers become risk-averse; novel failures become common.

### 11.5 The Allspaw rule

John Allspaw (Etsy CTO 2010-2017, founder of Adaptive Capacity Labs) is the canonical voice on blameless postmortems. His 2012 paper *Blameless PostMortems and a Just Culture* is required reading. The single most cited sentence:

> "If we go with a 'blame' point of view, we lose the ability to learn from failures... humans who design and operate complex systems are uniquely qualified to give *forward-looking* accountability."

*Forward-looking* accountability — fixing the system so this can't recur — is what postmortems produce. *Backward-looking* accountability — punishment — produces silence.

---

## 12. Postmortem Structure

A standard template every postmortem should follow.

```markdown
# [INC-1234] Checkout outage — 2026-05-05

## Summary
2-3 sentence executive summary. What happened, what was the impact,
what was the duration. Plain English; no jargon.

## Severity
SEV-1

## Impact
- Duration: 47 minutes (14:32 UTC – 15:19 UTC)
- Customers affected: ~12,000 (estimated from telemetry)
- Revenue impact: ~$40,000 (estimated)
- SLO budget consumed: 4.2% of 28-day checkout-availability budget
- Data loss: None
- Detection: Automated burn-rate alert; first paged at 14:34 UTC

## Timeline
14:32 — deploy of checkout-svc v2026.5.5 begins
14:34 — checkout-availability fast-burn page fires
14:34 — primary on-call (Alice) acks
14:36 — Alice opens dashboard; identifies elevated 5xx from auth-svc
14:38 — Alice declares SEV-2; opens #incident-1234
14:40 — Bob assigned IC; Alice assigned Ops
14:42 — Status page yellow: "Investigating reports of checkout failures"
14:45 — Alice runs `kubectl rollout undo deploy/checkout-svc`
14:47 — Rollback complete; checking dashboards
14:50 — Burn rate not declining; deploy was not the cause
14:52 — Charlie joins as scribe; reviews logs from auth-svc
14:55 — Charlie spots deploy of auth-svc at 14:30 (not in our team's awareness)
14:58 — Bob escalates to identity-team on-call (Dave)
15:01 — Dave joins; identifies regression in auth-svc password validation
15:05 — Dave rolls back auth-svc
15:07 — Burn rate begins to decline
15:12 — Burn rate at steady state; SLO recovering
15:15 — Status page green: "Issue resolved; investigating cause"
15:19 — Bob declares incident resolved
15:30 — Postmortem ticket filed; review scheduled

## What went well
- Burn-rate alert fired within 2 minutes of impact (good detection).
- IC pattern engaged at 7 minutes (good role hygiene).
- Cross-team escalation succeeded in 6 minutes (good runbook).
- Status page was current throughout.

## What went wrong
- Auth-svc deploy happened at 14:30 with no notification to checkout team.
- Checkout team's runbook didn't include "check auth-svc deploy timing"
  as a triage step.
- Initial rollback (of checkout-svc) was wasted effort because the
  symptom was upstream.
- Auth-svc has no canary deploy; full-fleet rollout exposed everyone.

## What we got lucky on
- The auth-svc bug only affected ~30% of password-validation paths
  (lucky cardinality of bug).
- Dave was at his desk; if it had been off-hours, escalation would
  have added 10+ minutes.
- The fast-burn alert fired; if we had only the slow-burn rule, we'd
  have seen impact 30+ minutes later.

## Contributing factors
1. Auth-svc deploy went out without a canary, exposing 100% at once.
2. No cross-service deploy notification: auth-svc deploys are not
   announced to dependent teams.
3. Runbook for "checkout availability burn" did not include checking
   upstream service deploys.
4. The auth-svc password-validation regression was not caught in test
   because the test fixtures used stub credentials that didn't trigger
   the broken code path.
5. We rolled back our own service first, costing 5 minutes, before
   considering upstream causes.

## Action items
| ID | Action | Owner | Due | Priority |
|----|--------|-------|-----|----------|
| AI-1 | Add canary deploy to auth-svc | identity-team | 2026-05-19 | High |
| AI-2 | Cross-team deploy announcements via #deploys | platform | 2026-05-12 | High |
| AI-3 | Update checkout runbook to include upstream-deploy check | payments | 2026-05-12 | Medium |
| AI-4 | Add auth-svc password-validation test fixtures | identity-team | 2026-05-19 | Medium |
| AI-5 | Establish IC training for cross-team escalation | platform | 2026-06-01 | Low |

## Lessons
- Cross-service deploy timing is a frequent confounder; we should
  default to assuming it's upstream until proven otherwise.
- Service-level rollback is easy; cross-team escalation is what's
  expensive — invest in that latency.
```

The template is what gets you to consistent, readable, action-oriented postmortems. Use it; don't reinvent it per incident.

### 12.1 The "what went well" section is mandatory

Every postmortem covers what went *well* in addition to what went wrong. Two reasons:

1. The team did things right; learning from successes is as valuable as learning from failures.
2. Without it, the postmortem is psychologically punishing — only failures attributed to the team. The well-section creates balance.

### 12.2 The "what we got lucky on" section is the highest-value

The next-incident catalyst hides here. *Things that almost made the incident worse, but didn't.* These are the unfixed accidents waiting to happen.

If your postmortems don't have a "got lucky" section, you're missing the early-warning system on your reliability backlog.

---

## 13. Action Items

The output of the postmortem. The thing that makes the loop close.

### 13.1 Action item rules

1. **Every action item is a ticket.** Ticket ID, owner, due date, priority. Not a TODO in a doc.
2. **Owner is one human, not a team.** Teams don't ship; humans do.
3. **Due date within 30 days for high-priority.** Otherwise it never closes.
4. **Closure is verified.** "Done" is in the ticket, with proof (PR link, runbook update, etc.).
5. **Track closure rate.** A platform SLO; below 70% is a fire.

### 13.2 The 70% bar

Across the org, ≥ 70% of action items must close on time. Below that:

- The postmortem-to-action loop is broken.
- The same incident kinds keep recurring.
- Engineers stop writing meaningful action items because "they don't get done."

The 70% bar is empirical (Allspaw, Beyer). Below that, postmortems become theater.

### 13.3 Action item categories

| Type | Lead time | Examples |
|---|---|---|
| **Quick fix** | < 1 week | Update runbook; add an alert; tweak a threshold |
| **Backlog item** | 1-4 weeks | Add canary deploys; add feature flag; refactor a fragile call site |
| **Architectural** | 1+ quarters | Multi-region failover; cell architecture; rewrite a fragile component |

The fast wins are the high-frequency category. Quarter-long architectural changes also matter, but get tracked separately on the reliability roadmap so they don't expire on a 30-day clock.

### 13.4 The "incident kind" frequency tracker

Aggregate postmortems by *kind of incident* (e.g., "deploy regression," "vendor outage," "schema migration," "expired cert"). When one kind happens 3+ times in a quarter, *that's* the pattern to systematically address — beyond the per-incident action items, an architectural change is needed.

This pattern-spotting is the IC manager's job, the SRE manager's job, or — in mature orgs — a dedicated *incident analyst* whose job is reading every postmortem and finding the meta-patterns.

---

## 14. The "Five Whys" Trap

The "five whys" technique (Toyota, 1950s) asks "why?" five times to reach a root cause. It's appealing, simple, and **wrong for distributed systems.**

### 14.1 Why it's wrong

Distributed systems fail from *combinations* of factors, not single chains. A "five whys" walk produces a *single* line back to a root cause:

```
Q1: Why did checkout fail?           A: Auth-svc returned 5xx.
Q2: Why did auth-svc fail?           A: Deploy regression.
Q3: Why was the regression deployed? A: No canary.
Q4: Why no canary?                   A: Wasn't required.
Q5: Why not required?                A: PRR didn't enforce it.
ROOT CAUSE: PRR didn't enforce canary.
```

But also: no cross-team notification, runbook gap, fixture gap, full-fleet rollout. Five-whys missed all of these by walking *one* line.

### 14.2 The replacement: contributing factors

A *list* of contributing factors, not a chain. From the example postmortem (§12), five factors. None alone caused the incident; their combination did. Each can be addressed independently.

### 14.3 The "no single root cause" principle

Sidney Dekker's *The Field Guide to Understanding Human Error* (2014) is canonical: complex systems fail from *interactions* of factors, not single causes. The phrase "the root cause" should be retired from postmortems. *Contributing factors*, plural, is the correct framing.

### 14.4 Cause vs trigger

Sometimes a useful sub-distinction:
- **Triggers** are what *started* the incident: a deploy, a traffic spike, a hardware failure.
- **Contributing factors** are what *let* the trigger become an incident: missing canary, no isolation, weak runbook.

Triggers are often hard to prevent (you can't prevent vendors having outages). Contributing factors are addressable: that's where the action items go.

---

## 15. Postmortem Reviews

The structured social ritual that makes postmortems work.

### 15.1 The review meeting

60 minutes. Attendees: incident participants, related team leads, on-call champion, optionally other interested engineers (open-attendance rule).

Agenda:
1. Author walks through summary, impact, timeline (10 min).
2. Q&A on the timeline (5 min).
3. Contributing factors (5 min).
4. Action items: discuss owners, dates, priority (15 min).
5. What we got lucky on (5 min).
6. Open discussion (15 min).
7. Wrap-up: confirm action items, schedule follow-up review (5 min).

### 15.2 The "what we'd do differently" exercise

In the open discussion: the author asks each participant: *"Knowing what you know now, what would you do differently if this happened again?"* Each participant answers. The answers become candidate action items.

This is more productive than asking *"what was the cause?"* because it focuses on *future* behavior, which is in everyone's control.

### 15.3 Pre-circulation

The postmortem is shared 24-48 hours before the review. People come having read it. Don't waste the meeting on reading; spend it on synthesis.

### 15.4 The IC manager / SRE manager attends

Not to interrogate, but to: spot patterns across multiple incidents, ensure action items are funded, and hold the room to blameless norms when someone slips.

### 15.5 Open-attendance norm

Anyone in the org can attend. This sounds ceremonial; it's not. Engineers learn faster from reading other teams' postmortems than from any internal training. Open-attendance creates a shared learning culture.

---

## 16. The Institutional Memory Layer

Where postmortems live, how they're searched, how they accumulate into a knowledge graph.

### 16.1 Storage

Don't store postmortems in Google Docs. Use:

- A **versioned wiki** (Notion, Confluence) with a postmortems collection.
- An **internal microsite** (gitbook, mkdocs) generated from markdown in Git.
- A **dedicated tool** (Jeli, Howie, incident.io's postmortem features).

The chosen system needs:
- Full-text search.
- Tagging by service, contributing factor type, severity.
- Action item tracking integration.
- Stable URLs (so they can be linked from runbooks, docs, other postmortems).

### 16.2 The cross-reference web

Every postmortem links to:
- The incident channel.
- The status page entry.
- Related runbooks (which were used; which were updated).
- Prior similar postmortems.
- Action item tickets.

This builds a *web* of institutional memory. Future engineers searching "auth-svc password regression" find this postmortem and the context it embeds.

### 16.3 The annual incident review

Once a year: aggregate all postmortems. What were the top contributing factor categories? Where did action items not close? What incident kinds keep recurring?

The annual review feeds the next year's reliability roadmap. It's the org's macro-learning loop.

### 16.4 Tagging taxonomy

A consistent tagging vocabulary is essential for searchability:

- `cause:deploy-regression` / `cause:vendor-outage` / `cause:capacity` / `cause:config-drift` / `cause:certificate-expiry`
- `surface:checkout` / `surface:browse` / `surface:auth`
- `fix-type:rollback` / `fix-type:kill-switch` / `fix-type:scale-up` / `fix-type:vendor-failover`
- `severity:1` / `severity:2` / `severity:3`

The tags are how the IC manager finds patterns. Without them, every incident looks novel.

---

## 17. Anti-Patterns

A field guide.

### 17.1 The "we all know what happened" postmortem

Symptom: writeup is two paragraphs; nobody reads it.
Fix: structured template (§12); 60-minute review meeting.

### 17.2 The "Alice was at fault" postmortem

Symptom: a person is named as a cause.
Fix: editor pass before publication. Strip names; replace with role / system. The on-call champion or IC manager owns this edit.

### 17.3 The "30 action items, none closed" postmortem

Symptom: the doc is impressive; the ticket queue shows 0% closure.
Fix: cap action items at 7. Owner-and-date for each. Track closure rate.

### 17.4 The "we found the root cause" postmortem

Symptom: single chain, single owner, no systemic factors.
Fix: replace "root cause" with "contributing factors" (plural). Force ≥ 3 factors.

### 17.5 The "no review meeting" postmortem

Symptom: written in isolation, never read aloud, no synthesis.
Fix: meeting is mandatory for SEV-1 and SEV-2; recommended for all.

### 17.6 The "punitive postmortem"

Symptom: leadership uses the postmortem as a performance review.
Fix: postmortems are excluded from performance reviews by policy. State this explicitly.

### 17.7 The "one team's incident, no cross-team learning"

Symptom: each team's postmortems are private.
Fix: open-attendance reviews; central searchable archive.

### 17.8 The "we'll fix it next quarter" postmortem

Symptom: action items dated 6 months out; never close.
Fix: < 30 days for high-priority; otherwise it's a roadmap entry, not an action item.

---

## 18. Worked Example: A 47-Minute Checkout Incident

The full lifecycle (referenced in §12 above; here we walk the ops side).

### 18.1 The timeline

Already shown in §12. The technical mitigation took 35 minutes; the postmortem and action items took 3 weeks. The action items closed at 80% on time — the AI-2 (cross-team announcements) was delayed two weeks because of dependency on a Slack workflow.

### 18.2 The patterns

- Cross-team escalation cost 6 minutes. Could be 2 minutes with better tooling.
- Initial rollback (of own service) was wasted effort. The runbook should default to suspecting upstream when own-service-rollback doesn't help in 5 minutes.
- The auth-svc deploy was a *trigger*; the contributing factor was *no canary*.

### 18.3 The recurrence prevention

90 days later, a similar pattern fired again — but the action items had landed. Auth-svc had canary; the canary caught the regression before full deploy. No incident. The system had learned.

This is what postmortem ROI looks like: *measurable* in averted future incidents.

---

## 19. Pitfalls

1. **Skipping declaration; "let's just fix it."** No roles, no structure, blast-radius unknown.
2. **One person doing everything.** Cognitive overload; bad outcomes.
3. **No status page update.** Customers learn from Twitter.
4. **Diagnosing before mitigating.** Customers suffer while engineers debug.
5. **No scribe.** Timeline reconstructed by archaeology; full of errors.
6. **No exec liaison on SEV-1.** Engineers interrupted constantly.
7. **Severity inflation or deflation.** Either alert fatigue or undercoverage.
8. **Single root cause framing.** Misses systemic factors.
9. **Action items without owners or dates.** Never close.
10. **No closure tracking.** 70% bar invisible; postmortems become theater.
11. **Naming people in postmortems.** Erodes psychological safety; engineers hide future mistakes.
12. **No game days.** Runbooks fail at 3 AM, untested.
13. **Postmortems never archived.** Search fails; institutional memory leaks.
14. **No cross-team review attendance.** Each team relearns the same lessons.
15. **No annual review.** Patterns never escape individual incidents.

---

## 20. Mental Models

> **Mitigation precedes understanding.** Stop the bleed; learn after.

> **In doubt, declare higher.** Under-declaration costs customer impact; over-declaration costs an hour.

> **Roles within five minutes.** IC, Ops, Scribe, Comms, (Exec liaison). Not optional for SEV-1/2.

> **The IC commands; the Ops Lead acts.** Different humans for non-trivial incidents.

> **Customer comms every 30 min minimum.** Promised silence beats unannounced silence.

> **Blameless or worthless.** Without psychological safety, postmortems produce silence, not learning.

> **Contributing factors, not root cause.** Distributed systems fail from interactions, not single chains.

> **Action items are the product.** A brilliant postmortem with 0% closure rate is theater.

> **70% closure rate or it's broken.** Track it as a platform SLO.

> **Game days reveal what runbooks hide.** Practice on Tuesdays.

> **The annual review is the org's macro-learning loop.** Without it, patterns never become roadmap.

Now go to `doc 16` (capacity planning) — the discipline that prevents incidents that would otherwise show up in this chapter as "we ran out of headroom."
