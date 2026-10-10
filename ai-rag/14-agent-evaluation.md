# 14 — agent evaluation

> **Prerequisites:** [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (the base layer:
> `pass@1` / `pass^k` in §10.8, LLM-as-judge validation in §11, intervals and tests in §13, gates in
> §14, the online flywheel in §15, and a short agentic section in §12 — this chapter assumes all of
> it and does **not** re-derive it),
> [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md) (§14 is the short
> summary of trajectory scoring and cost per resolved task that this chapter expands; §10 is the
> failure taxonomy the graders here must detect),
> [`24-tool-calling-and-enterprise-integration.md`](24-tool-calling-and-enterprise-integration.md)
> (§14 covers unit and replay tests of the tool boundary; §13 the tool-call tracing this chapter
> reuses online), and
> [`21-langgraph-deep-dive.md`](21-langgraph-deep-dive.md) (checkpointing is what makes an eval
> trial resumable and its state inspectable). Useful: the lab
> [`labs/tool-registry/`](labs/tool-registry/README.md) (the tool layer a harness wraps) and
> [`labs/golden-set/`](labs/golden-set/README.md) (golden-set versioning that §7 reuses for tasks).
>
> **Feeds into:** [`28-agentic-security-owasp-and-mcp-threats.md`](28-agentic-security-owasp-and-mcp-threats.md)
> (security regression suites are agent evals with an adversary in the user-simulator slot),
> [`29-agent-identity-and-delegated-authorization.md`](29-agent-identity-and-delegated-authorization.md)
> (authorization checks become graders), and
> [`30-context-engineering-for-agents.md`](30-context-engineering-for-agents.md) (every context
> change must be gated by the suite built here).
>
> **THESIS:** an agent eval measures a *system*, not a model: a model, a harness (the loop, tools,
> prompts, retries, budgets), an environment with state, and — for conversational agents — a
> simulated user. Four properties separate it from the retrieval and generation evals of chapter
> 08: variance compounds along a trajectory, actions have side effects that must be graded in the
> world rather than in the transcript, a multi-turn user is itself a stochastic system you must
> simulate, and the harness changes the score as much as the model does. The consequence is a
> different set of defaults: **grade the final environment state with code; record trajectories as
> diagnostics rather than gates; report reliability as `pass^k` rather than a single mean; report
> cost per *resolved* task rather than cost per run; and treat the harness as a versioned,
> pinned part of the thing under test.** A benchmark number is a measurement of one particular
> model-plus-harness-plus-grader combination on one particular task set at one particular date; the
> engineering skill in this chapter is knowing which of those four parts moved when a number
> changes.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [What makes agent eval different](#1-what-makes-agent-eval-different)
2. [Vocabulary: task, trial, grader, transcript, outcome, harness](#2-vocabulary-task-trial-grader-transcript-outcome-harness)
3. [Outcome, trajectory, and process metrics](#3-outcome-trajectory-and-process-metrics)
4. [State-based grading versus transcript grading](#4-state-based-grading-versus-transcript-grading)
5. [Tool-call correctness and argument accuracy](#5-tool-call-correctness-and-argument-accuracy)
6. [Reliability: pass@k, pass^k, and how pass^k decays](#6-reliability-passk-passk-and-how-passk-decays)
7. [Cost and latency per resolved task](#7-cost-and-latency-per-resolved-task)
8. [Harness versus model](#8-harness-versus-model)
9. [The public benchmark map](#9-the-public-benchmark-map)
10. [Building your own agent eval suite](#10-building-your-own-agent-eval-suite)
11. [Simulated users and their biases](#11-simulated-users-and-their-biases)
12. [Graders: code, rubric-LLM, human](#12-graders-code-rubric-llm-human)
13. [Variance and sample-size arithmetic for agent tasks](#13-variance-and-sample-size-arithmetic-for-agent-tasks)
14. [Online and production evaluation](#14-online-and-production-evaluation)
15. [Eval in CI: gates, flakiness, budgets](#15-eval-in-ci-gates-flakiness-budgets)
16. [Anti-patterns](#16-anti-patterns)
17. [Interview questions](#17-interview-questions)
18. [Lab exercises](#18-lab-exercises)
19. [Real-world cases](#19-real-world-cases)
20. [Sources](#sources)

---

## Start here — the whole chapter in plain words

**The problem.** To test a calculator you feed it inputs and compare outputs. To test an agent you
let it *act* — look up a booking, change a flight, run shell commands — over many steps, where each
step depends on the last and some steps change the world. The same agent given the same request can
succeed on Monday and fail on Tuesday. A single "accuracy" number hides all of this.

**A real-world example.** (All numbers here are an arithmetic illustration, not a measurement of a
real system.) An airline-support agent passes a given task 90% of the time per attempt. A customer
who contacts support once sees 90% reliability. But a team that needs the agent to handle the *same
kind of request* correctly ten times in a row for ten different customers is relying on
`0.9^10 ≈ 0.35`. The mean says "90%". The reliability says "worse than a coin flip that all ten
go right." Meanwhile the agent said "Done, your flight is changed" in every one of the ten
conversations — including the ones where the database was not changed. A grader that read the
transcript would have scored those as passes; a grader that inspected the database would not.

The chapter's defaults: grade the final environment state with code, not the transcript (§4); repeat
every task and report `pass^k` (§6); divide spend by *resolved* tasks (§7); name the harness in every
result (§8); build 20–50 tasks from real failures with resettable environments (§10); validate
simulators and judges like classifiers (§11–12); watch production and feed failures back (§14); gate
CI on regressions with budgets (§15).

### Symbols and parameters used in this chapter

| Symbol | Meaning | Typical value | Simple example |
|---|---|---|---|
| `T` | number of tasks in the suite | 30–300 | 40 tasks from production failures |
| `n` | trials run per task | 4–16 (CI: 3–5) | 8 trials per task |
| `c_i` | number of passing trials of task `i` | 0…`n` | task 3 passed 6 of 8 → `c_3 = 6` |
| `k` | repeat count used in the reliability metric | 1, 2, 4, 8 | all 4 trials must pass |
| `p` | per-trial success probability (one task, or the mean) | 0.5–0.99 | `p = 0.9` |
| `pass@k` | chance at least one of `k` trials passes | `1 − (1−p)^k` for flat `p` | `p=0.5, k=4` → 0.94 |
| `pass^k` | chance all `k` trials pass | `p^k` for flat `p` | `p=0.9, k=8` → 0.43 |
| `C(c,k)/C(n,k)` | unbiased estimate of `pass^k` for a task from `c` successes in `n` trials | — | `c=6, n=8, k=4` → 0.214 (naive `(6/8)^4 = 0.316`) |
| `q` | discordant rate: share of tasks where config A and B disagree | 0.1–0.3 | `q = 0.2` |
| `d` | smallest difference you want to detect | 0.05–0.10 | 5 points |
| `Z` | `Z_{α/2} + Z_{power}` | `1.96 + 0.84 = 2.8` | used in §13 |
| `CPR` | cost per resolved task = total cost / successes | $0.20–$5 | $1.00 for a coding agent |
| `L_p95` | 95th-percentile wall-clock time per trial | seconds–minutes | 140 s |
| `B` | budget (token / dollar / step / time cap per trial) | — | 25 steps or $0.50 |
| `OTEL` | OpenTelemetry; `invoke_agent`, `execute_tool` are GenAI span operations | — | §14.4 |

---

## 1. What makes agent eval different

> **In plain words.** A retrieval or QA eval is a function check: one input, one output, one grade.
> An agent eval is a small simulation: many steps, a changing world, sometimes a pretend user.
> Four things go wrong that never did before: errors pile up, actions leave marks, the user is a
> moving part, and the code around the model matters as much as the model.
>
> **Real-world example.** A coding agent edits the right file on step 3, then on step 9 "tidies up"
> by deleting a test directory, then reports success. The final answer is fluent and the diff looks
> plausible; only the filesystem shows the damage.

Chapter 08 §12 introduced the three grading layers. Here is what is structurally different from
every eval in chapters 01–11.

### 1.1 Non-determinism compounds over a trajectory

A single LLM call has output variance (08 §10.8: current Claude models reject
`temperature`/`top_p`/`top_k`, so variance is measured, not suppressed). In an agent loop the
variance is *path-dependent*: step 4's sampled tool call changes what step 5 sees. If each of `s`
steps is independently "good enough" with probability `a`, the chance of an error-free trajectory is
`a^s` — `0.98^20 ≈ 0.67`. This is a simplification (steps are neither independent nor all
equally fatal, and agents often recover), but it explains why trajectory length is the strongest
predictor of unreliability and why the right response is repeated trials (§6), not a single
"temperature 0" run.

### 1.2 Side effects: the world is part of the output

A QA answer is a string. An agent's product is often a *state change*: a row updated, a file
written, a ticket closed, an email sent. Three consequences:

- **Grade the state, not the words** (§4).
- **Environments must be reset per trial** (§10.3). Chapter 08 §12.4 already notes that shared
  state creates correlated failures and, per Anthropic, even lets a model read the git history of
  earlier trials for an unfair advantage; it is a correctness requirement, not hygiene.
- **Collateral damage is a failure.** A trial that fixes the target record and also modifies
  another is not a pass. Good graders assert what must *not* change (§4.2).

### 1.3 The multi-turn user is a stochastic component

Conversational agents face a dialogue where information is revealed over turns, so the "input" cannot be frozen; benchmarks such as τ-bench use an LLM user simulator (§9.1, §11), which adds its own variance and bias. The score is `agent × simulator × task`.

### 1.4 The harness is part of the system under test

"Model X scores Y" is shorthand for "model X, inside scaffold Z, with tool set W, prompts V,
timeouts U, scored by grader G". Anthropic's guide states it plainly: when you evaluate "an agent",
you are evaluating the harness and the model together. §8 shows how large the scaffold effect can be
on a public benchmark.

---

## 2. Vocabulary: task, trial, grader, transcript, outcome, harness

The terms below follow the usage in Anthropic's engineering guide *Demystifying evals for AI
agents* (see Sources); other groups use different words for the same objects, and the point of
fixing a vocabulary is that a report can say which object changed.

Terms follow the guide above: task = inputs plus success criteria; trial = one attempt; outcome = final environment state.

Two distinctions earn their keep:

- **Capability vs regression suites.** A capability suite is hard on purpose (starting pass rate
  low); a regression suite should sit near 100% and any drop is a signal. Tasks graduate from the
  first to the second as the agent improves — and when a capability suite saturates, it stops
  discriminating (08 §13.6).
- **Eval harness vs agent harness.** Both are called "the harness"; in reports always say which.
  The first should be boring and fixed; the second is a variable you will deliberately vary (§8).

---

## 3. Outcome, trajectory, and process metrics

> **In plain words.** Three questions at three zoom levels: *Did it work?* (outcome), *Did it go
> about it sensibly?* (trajectory), *Was each individual decision reasonable?* (process / per-turn).
> Gate on the first; use the other two to find out why the first moved.
>
> **Real-world example.** Two agents both resolve 90% of refund requests. Agent B takes 14 tool
> calls per request, Agent A takes 5. Outcome metrics tie; the trajectory metric (calls per
> resolved task) shows B costing ~2.8x as much and being more exposed to every tool's failure rate.

### 3.1 The three levels

| Level | Question | Examples | Use as gate? |
|---|---|---|---|
| **Outcome** | is the world in the right end state? | DB row has the new flight; tests pass; ticket closed with correct resolution code; no collateral change | yes |
| **Trajectory** | was the sequence acceptable? | right tools, right order where dependencies exist, no redundant calls, no policy-violating calls, step count ≤ budget | only for *hard constraints* (a forbidden call), else diagnostic |
| **Process / per-turn** | was this decision justified given what was known? | wrong tool chosen at step 3; asked a question the user had already answered; ignored a tool error | diagnostic, via judge |

Why trajectory metrics are mostly *not* gates: Anthropic's guidance, restated in 08 §12.2, is that
asserting an exact tool sequence is brittle because agents find valid paths the author did not
anticipate. Their example: Opus 4.5 on a τ²-bench flight-booking task found a loophole in the
policy that gave the customer a better outcome and was marked as failing by the eval as written.
Chapter 22 §14.2 gives a step-matching scorer (`score_trajectory`); treat its output as a *signal*.

The exceptions — trajectory constraints that **are** gates — are safety and policy rules that are
independent of the path: "never call `cancel` without a prior `get_reservation` for the same id",
"never write outside the working directory", "never send an email before the human-approval tool
returned". Those are assertions over the transcript (cheap, deterministic, code-graded) and belong in
the regression tier. They are different in kind from "the agent must call tools A, B, C in this
order".

### 3.2 A metric set to record on every trial

Record all of these on *every* trial, even when only one gates (the extension of 08 §12.3):

| Metric | Definition | Notes |
|---|---|---|
| `success` | state grader returns true | the gate |
| `partial` | weighted fraction of sub-goals satisfied | binary success discards signal; use partial credit for diagnosis and for noisy tasks |
| `collateral` | count of state changes outside the allowed set | any > 0 is a fail for write tasks |
| `silent_fail` | agent claimed success AND `success` false | the worst failure; only visible if you grade state |
| `n_calls`, `n_errors` | tool calls issued; calls that returned an error | thrash detector |
| `n_redundant` | repeated identical calls | loop detector |
| `tokens_in/out`, `usd` | per trial, split by agent / judge / simulator | CPR needs this (§7) |
| `wall_s` | wall-clock | p50 and p95 |
| `termination` | `done` / `budget` / `timeout` / `error` / `refused` | budget-terminations are not the same as wrong answers |

`silent_fail` deserves a headline slot. Define it as `claimed_success ∧ ¬state_success`, and compute
it in the same pass that computes `success`; it is the number that tells you whether you can trust
the agent's own "I'm done" (the lab in §18.1 shows a bare scaffold with a 12.5% silent-fail rate
because the fake agent always claims success).

---

## 4. State-based grading versus transcript grading

> **In plain words.** After the run, look at the database or the disk. Do not take the agent's word
> for what it did, and do not infer it from the chat log.
>
> **Real-world example.** The agent's last message reads "I've rebooked you on F300." The
> transcript grader (does it say "rebooked"?) passes. The state grader finds `R1.flight` is still
> `F100` because the tool call used the wrong reservation id and the error was ignored. One of the
> graders is wrong, and it is the cheap one.

### 4.1 Why the transcript is the weaker evidence

A transcript records what the agent *attempted* and *said*. It does not record what the world
*did*. The two diverge when: a tool call fails and the agent ignores it; the call succeeds against
the wrong object; a call succeeds twice; a side effect happens inside a tool the agent never
called directly (a trigger, a webhook); or the agent simply claims something it never did. Anthropic
defines the **outcome** as the final environment state — "whether a reservation actually exists in
a database, not just what the agent claims".

τ-bench was designed around this. The τ²-bench repository's `docs/evaluation.md` states it
precisely: the default `reward_basis` for airline, retail and telecom is `[DB, COMMUNICATE]`, the
final reward is the **product** of the components, and the `DB` check replays a reference action
list on a fresh "gold" environment and compares a **hash** of the resulting database with the
agent's end-state hash — any tool-call sequence reaching an equivalent end state passes;
`evaluation_criteria.actions` is one reference trajectory, not a required path (the `ACTION`
reward type that does require it is not used in airline/retail/telecom). `COMMUNICATE` is a
substring check that required strings appear in the agent's messages. OSWorld grades with a
per-task execution-based script over the resulting system state; SWE-bench grades with the test
suite run against the patched repository; Terminal-Bench ships a test script per task. The
community convergence on state-based grading is the main reason these benchmarks are usable at all.

### 4.2 What a state grader asserts

```python
from dataclasses import dataclass, field

@dataclass
class StateSpec:
    must_equal: dict                 # {("reservations", "R1", "flight"): "F300", ...}
    must_not_change: set = field(default_factory=set)   # keys whose rows must equal the seed
    invariants: list = field(default_factory=list)       # callables(db) -> bool

def grade_state(db, seed, spec: StateSpec) -> dict:
    out = {"must": all(db[t][k][f] == v for (t, k, f), v in spec.must_equal.items())}
    out["collateral"] = [rid for rid in spec.must_not_change
                         if db["reservations"][rid] != seed["reservations"][rid]]
    out["invariants"] = all(inv(db) for inv in spec.invariants)
    out["success"] = out["must"] and not out["collateral"] and out["invariants"]
    return out
```

Three assertion types, all deterministic: **must-equal** (the target state), **must-not-change**
(collateral damage), **invariants** (conservation laws: seats never negative, money in = money out,
the number of open tickets decreased by exactly one). Invariants catch whole classes of bug you did
not anticipate when writing the task.

### 4.4 The two-grader pattern

Run both a state grader and a cheap transcript grader and track their disagreement:

| State pass | Transcript says done | Meaning |
|---|---|---|
| yes | yes | true success |
| no | yes | **silent failure** — the dangerous cell |
| no | no | honest failure — recoverable by a human |
| yes | no | success but unannounced (UX bug, or a grader bug — read the transcript) |

A growing "no/yes" cell after a prompt change is the clearest regression signal an agent eval can
give, and it is invisible to any LLM-judge-on-the-transcript design.

### 4.5 Grader bugs are the main source of wrong benchmark numbers

Anthropic reports that Opus 4.5 scored 42% on CORE-Bench at first and about 95% after grading bugs
were fixed — most of the "capability gap" was in the grader (see §19, Case A). The consequence for
your own suite: a state grader needs its own tests (a known-good final state must pass, a
known-bad one must fail, a state with a collateral change must fail), run in CI like any other code.

---

## 5. Tool-call correctness and argument accuracy

> **In plain words.** For each tool call: was it the right tool, were the arguments right, was it
> allowed, and did the agent do something sensible with the result? Score the arguments field by
> field, because "right tool, wrong order id" is the failure that moves money.
>
> **Real-world example.** `change_flight(rid="R2", flight="F300")` when the customer owns `R1`:
> tool selection correct, schema valid, argument wrong. A tool-name accuracy of 100% hides it; an
> argument-level check against the task's expected call flags it.

Chapter 24 §3.4 and §14 introduced tool-call correctness at the tool boundary; chapter 22 §14.3
defined tool-selection precision/recall. This section adds the *eval-side* decomposition. Score each
call along independent axes so a regression localizes:

| Axis | Check | Cheap? | Typical failure it localizes |
|---|---|---|---|
| **Selection** | tool name ∈ acceptable set for this step | code | wrong tool for the intent; tool catalog too large (ch. 24 Case 2) |
| **Validity** | arguments parse and validate against the JSON Schema | code | missing/extra keys, wrong types, hallucinated tool names |
| **Argument accuracy** | each argument equals the expected value, under a per-field comparator | code (+ judge for free text) | wrong id, wrong amount, wrong date |
| **Authorization / policy** | call allowed for the principal and the policy | code | agent acting on another user's record |
| **Ordering / dependency** | prerequisite calls happened before the dependent call | code over transcript | writing before reading |
| **Result use** | the agent's next step reflects the tool result (error handled, value propagated) | judge | ignored errors, stale values |
| **Efficiency** | redundant / unnecessary calls | code | loops, re-fetching |

### 5.1 Matching a set of calls, not a sequence

When order does not matter (two independent lookups), compare as multisets of
`(tool, canonical_args)` and report precision and recall — the same shape as 22 §14.3 but at the
argument level. When order matters because of a dependency, encode the dependency as a rule
("`get_reservation(rid=X)` before `change_flight(rid=X)`"), not as a golden sequence. This is the
practical resolution of the "grade the product, not the path" principle: *constraints* over paths are
allowed; *whole-path equality* is not.

### 5.2 What BFCL does and does not tell you

The Berkeley Function-Calling Leaderboard (§9.9) scores function-call validity and correctness — selection, validity and arguments, single- and multi-turn — a *component* property. A high BFCL score and a low τ-bench score are compatible; BFCL says nothing about task completion under a policy or about your authorization layer.

---

## 6. Reliability: pass@k, pass^k, and how pass^k decays

> **In plain words.** `pass@k` asks "did at least one of k tries work?" — right for a tool where you
> pick the best of several outputs. `pass^k` asks "did *all* k tries work?" — right for a customer
> agent that must be right every time. One rises with k, the other collapses.
>
> **Real-world example.** An agent succeeds 75% of the time per attempt. Anthropic's guide gives the
> arithmetic: `0.75^3 ≈ 42%` of tasks pass three times in a row, and at k = 10 `pass@k` approaches
> 100% while `pass^k` approaches 0.

Definitions, the unbiased estimators, significance and the nondeterminism argument are in
[`08-evaluation-methodology.md`](08-evaluation-methodology.md) (symbols table; §10.8; §13). They are
not repeated; this section covers what agent evals add: the decay table, the estimator in code, and
what to do about it.

### 6.1 The decay table (flat per-trial success `p`)

For a task with a fixed per-trial success probability `p`, `pass^k = p^k` and `pass@k = 1−(1−p)^k`.

| `p` | `pass^1` | `pass^2` | `pass^4` | `pass^8` | `pass^10` | `pass^20` |
|---|---|---|---|---|---|---|
| 0.80 | 0.800 | 0.640 | 0.410 | 0.168 | 0.107 | 0.012 |
| 0.90 | 0.900 | 0.810 | 0.656 | 0.430 | 0.349 | 0.122 |
| 0.95 | 0.950 | 0.902 | 0.815 | 0.663 | 0.599 | 0.358 |
| 0.99 | 0.990 | 0.980 | 0.961 | 0.923 | 0.904 | 0.818 |

And `pass@k` for `p = 0.5`: k=1 → 0.500, k=2 → 0.750, k=4 → 0.938, k=8 → 0.996.

(Every value above is computed arithmetic, reproduced by the script in §18.2.) Reading the table:
to hold `pass^8 ≥ 0.9` you need per-trial reliability around `0.9^(1/8) ≈ 0.987`. Each additional
"nine" of reliability costs far more than the previous one, which is why customer-facing agents
cannot be signed off on a single pass-rate average.

A real, documented instance: the τ-bench repository's README lists, for the best tool-calling
entry at the time (claude-3-5-sonnet-20241022), airline `Pass^1 = 0.460` falling to `Pass^4 =
0.225`, and retail `0.692` falling to `0.462`. The τ-bench paper's abstract also states that
function-calling agents such as gpt-4o succeed on under 50% of tasks and are inconsistent (pass^8
under 25% in retail) — quoted here as reported; check the paper for the exact wording. These are
dated, superseded results chosen to show the *shape* of the decay, not current standings.

### 6.2 Tasks are heterogeneous, so the flat model understates suite reliability

Real suites mix tasks with `p_i` near 1 and near 0.5. Suite `pass^k` is the mean of per-task `p_i^k`, which by Jensen's inequality is ≥ `(mean p)^k`. **Compute `pass^k` per task and average**, and report the distribution (37 tasks at 8/8, 6 at 0/8, 7 in between): the in-between tasks are where engineering effort pays.

### 6.3 The unbiased estimator, in code

With `n ≥ k` trials of which `c` passed, the unbiased estimate of "all `k` passes" for that task is
`C(c,k)/C(n,k)` (drawing `k` of the `n` trials without replacement). Using `(c/n)^k` instead
overstates `pass^k` (for `c=6, n=8, k=4`: 0.316 versus the unbiased 0.214), because it treats
the `k` repeats as fresh draws with probability `c/n` each.

```python
from math import comb

def pass_hat_k(c: int, n: int, k: int) -> float:
    return comb(c, k) / comb(n, k) if n >= k else float("nan")


```

---

## 7. Cost and latency per resolved task

> **In plain words.** The price of an agent is not the bill divided by the number of runs; it is the
> bill divided by the number of runs that actually solved the problem, plus what each failure costs
> someone to clean up.
>
> **Real-world example.** (Arithmetic example.) Agent A costs $0.40 per run and resolves 78%; agent
> B costs $0.90 and resolves 91%. Cost per resolved task: A = 0.40/0.78 ≈ $0.51; B = 0.90/0.91 ≈
> $0.99. If a human clean-up of a failure costs $6, add `(1−s)·6/s` per resolved task: A adds
> 0.22×6/0.78 ≈ $1.69 (total ≈ $2.20), B adds 0.09×6/0.91 ≈ $0.59 (total ≈ $1.58). Ranking flips.

Chapter 22 §14.4 and chapter 08 §12.3 introduce cost per resolved task. The agent-specific
refinements:

### 7.1 Define CPR so it cannot be gamed

```
CPR = (Σ cost of ALL trials, including failures, retries, judge calls, simulator calls)
      / (number of trials graded success)
```

Rules: include failures (they cost tokens); include the *eval-only* components (judge,
simulator) in a separate column so production CPR is not polluted; include tool/API charges and
sandbox compute, not only tokens; count a budget-terminated trial as cost incurred, success zero.
With caching, report both nominal and billed cost (see ch. 23 for cache-aware pricing); a trial's
cost depends on cache state, which makes cost a noisy metric.

### 7.4 Budgets are part of the task

Give every task an explicit step/token/time/dollar budget and record `termination`; unbounded trials are how a suite becomes a cost incident (§15.4).

---

## 8. Harness versus model

> **In plain words.** The "agent" you benchmark is a model *plus* the code that prompts it, runs its
> tools, retries, and decides when to stop. Change the code, keep the model, and the score moves —
> sometimes by more than switching models would.
>
> **Real-world example.** On the public Terminal-Bench 2.0 leaderboard, the same underlying model
> appears paired with different agent scaffolds, and the accuracy differs by tens of points across
> rows (specific figures below).

### 8.2 The Terminal-Bench 2.0 example

Terminal-Bench is described in its ICLR 2026 paper, *Terminal-Bench: Benchmarking Agents on Hard,
Realistic Tasks in Command Line Interfaces*: 89 hard terminal tasks drawn from real workflows, each
with its own environment, a human-written reference solution and tests; the paper reports that
frontier models and agents score under 65% (ICLR 2026). The official repository names **Harbor** as the framework for running
Terminal-Bench 2.0 and shows a run as `harbor run --dataset terminal-bench@2.0 --agent <agent>
--model <model>`, with Claude Code, OpenHands and Codex CLI among supported agents. The agent is a
*parameter* of the run, separate from the model — exactly the separation §1.4 asks you to keep.

The leaderboard pairs agents with models, so the same model appears several times under different
scaffolds. Across those rows the spread for one model is routinely tens of percentage points. That
structure is the lesson, not any single number. Check
[tbench.ai/leaderboard/terminal-bench/2.0](https://www.tbench.ai/leaderboard/terminal-bench/2.0)
for current, dated numbers before quoting any.

One caveat: public leaderboard data is observational. Submissions differ in prompts, versions and
budgets, and many adjacent ranks are statistically indistinguishable (§13). It shows that harnesses
matter. It cannot tell you how much a given harness change will help your own tasks.

### 8.3 What to do about it in your own evals

1. **Name the harness in every result.** A result row is `(model id, harness version hash, tool set
   hash, prompt hash, effort/thinking, budgets, grader version, task-set version, date)`.
2. **Run a 2×2 before concluding.** To claim "model B beats model A", run both inside the same
   harness; to claim "harness 2 beats harness 1", run both with the same model. Only a factorial
   design separates the effects and shows interaction.
3. **Tune the baseline as hard as the challenger** (08 §8.5): a new harness compared to an untuned
   old prompt is a headline, not a finding.
4. **Hold out the harness choice.** If you pick the scaffold that maximizes a benchmark, the
   benchmark number is optimistically biased; evaluate on tasks the scaffold was not tuned against.
5. **Check budget fairness.** Equal step caps do not mean equal compute if one harness issues
   parallel calls; compare at equal *cost* as well as equal steps.

The lab (§18.3) runs the smallest possible version: one scripted "model", two harnesses, a
different `pass^k` curve.

---

## 9. The public benchmark map

> **In plain words.** Each public benchmark is a different instrument: some test conversation under
> a policy, some test a shell, some test a browser, some test only whether a function call is
> well-formed. Know what each grades, how, and where it is known to be unreliable before you let a
> number from it into a decision.
>
> **Real-world example.** A vendor claims "state-of-the-art on SWE-bench Verified". You check:
> which scaffold, which date, which of the 500 tasks are still unsolved, and whether the benchmark
> is saturated or contaminated. The headline number answers none of those.

Facts below come from each benchmark's official repository or the lab's announcement ; numbers only from search excerpts are labelled. No current leaderboard scores are given; any number is dated and attributed.

| Benchmark | Measures | Grading | Known weaknesses (documented) |
|---|---|---|---|
| τ-bench | tool-using customer-service agent with an LLM user, under a domain policy (airline, retail) | end DB state vs goal; `pass^k` | tasks not updated; simulator-dependent; task errors (see τ²/SABER) |
| τ²-bench | the above plus telecom, user with tools too ("dual-control"); later banking, voice | state + action checks; `reward_basis` gating | task fixes needed (SABER) |
| Terminal-Bench 2.0 | hard CLI tasks in a Docker sandbox | per-task test script | harness-dependence (§8) |
| SWE-bench Verified | resolve GitHub issues in Python repos | repo tests | saturation; spec/test flaws in the original, motivating Verified |
| SWE-bench Pro | harder, contamination-resistant issues | tests | reported task-quality disputes (third-party) |
| GAIA | general assistant questions needing tools/browsing | answer-string match | small, answers withheld; web drift |
| BrowseComp | persistent browsing for hard-to-find facts | short answer + LLM grader | narrow task shape; contamination via canary only |
| OSWorld(-Verified) | computer-use in real OS VMs | execution-based scripts per task | env/setup fragility; fixes in -Verified |
| WebArena | web tasks on self-hosted sites | programmatic checks | env reset, annotation bugs |
| BFCL | function-call validity | AST match / execution / multi-turn state | not task success |

### 9.1 τ-bench and τ-bench successors

**τ-bench** (Yao et al., Sierra; arXiv 2406.12045) emulates conversations between a simulated user
(an LM) and an agent that has domain-specific API tools and policy guidelines. The repository
README lists the **airline** and **retail** domains, notes that the default user simulator is
`gpt-4o` with an `llm` strategy (alternatives `react`, `verify`, `reflection`), reports
leaderboard columns Pass^1 to Pass^4, and warns that the tasks in that repo are no longer
updated, pointing to τ²/τ³-bench. It also ships an automatic error-identification tool that
assigns fault (user, agent, environment) with an LLM and warns it may be inaccurate. Grading is DB end-state hash plus required-communication substrings (§4.1). The paper's contribution is `pass^k` itself (defined in chapter 08).

**τ²-bench** (Barres et al., 2025; arXiv 2506.07982; title: *Evaluating Conversational Agents in a
Dual-Control Environment*). The repository README lists domains `mock`, `airline`, `retail`,
`telecom` and `banking_knowledge` (the last attributed to *τ-Knowledge*, arXiv 2603.04370) and
both a **text** (half-duplex) mode and a **voice** (full-duplex, *τ-Voice*, arXiv 2603.13686) mode.
The README describes each domain as a policy, a set of tools, a set of tasks and optionally a set
of **user tools** for the user simulator — that is the "dual-control" idea: the simulated user also
acts on the shared environment, so the agent must guide the user rather than merely act (the README
does not define the term; this reading comes from that structure and the paper title). The README also credits **SABER** (Cuadron et al., arXiv
2512.07850) as informing task fixes — evidence that even carefully built agent tasks contain errors
that surface only after wide use. Weaknesses to carry: simulator dependence (§11), policy loopholes
(the Opus 4.5 flight-booking case in §3.1), and the "tasks not updated" status of the original.

### 9.2 Terminal-Bench 2.0

89 terminal tasks, each with an environment, a human reference solution and tests (ICLR 2026 paper;
see §8.2). Harness: the original `tb` CLI needs `uv` and Docker; the repo says to use **Harbor**
for 2.0. Grading is the task's own test script. Weaknesses: scores are strongly agent-scaffold
dependent (§8), and the paper itself includes an error analysis and failure taxonomy that you
should read before interpreting a number. 
### 9.3 SWE-bench Verified and SWE-bench Pro

**Verified.** OpenAI announced SWE-bench Verified on 2024-08-13: a **500**-sample subset of the
SWE-bench test set that human annotators confirmed as non-problematic (the SWE-bench repository
describes it as problems "that real software engineers have confirmed are solvable"). OpenAI's
stated rationale for human validation: the original had **overly specific unit tests**,
**underspecified problem descriptions**, and **hard-to-set-up development environments**, which made
scores both under- and over-state ability. Grading runs the repo's tests against the model's patch
in a containerized harness (`swebench eval`); the tests are the FAIL_TO_PASS and PASS_TO_PASS sets
(the standard SWE-bench test sets). Weaknesses: saturation (above) and
possible training-data exposure, since the issues are from public, permissively licensed
repositories.

**Pro.** Scale AI's SWE-bench Pro (arXiv 2509.16941; via search excerpts): 1,865 problems from 41 repositories — public (731 tasks, 11 repos), held-out (858, 12 repos), commercial (276, 18 proprietary repos). Its contamination defense is licensing: public tasks come from strong-copyleft repos and commercial tasks from proprietary code, on the argument that permissively licensed repos are prime candidates for web-crawled pretraining. The paper reports frontier models under 25% with SWE-Agent at launch. Epoch AI's review rates the benchmark "Flawed" and reports audits finding many tasks with problems (one audit: 83 of 100 public-set problems, mainly "requirements inflation"); that is a secondary claim, so verify it before repeating.

### 9.4 GAIA

*GAIA: a benchmark for General AI Assistants* (Mialon et al., ICLR 2024, arXiv 2311.12983): 466
questions that are conceptually simple for people but need multi-step reasoning, browsing and tool
use; the abstract reports humans at 92% versus 15% for GPT-4 with plugins. About 300 answers are
withheld for a public leaderboard. Grading is comparison of a short final answer to a reference
(the answer is the product). Weaknesses: small size (wide intervals), web content that drifts so questions can become unanswerable, and public availability since 2023 (treat the released split as possibly seen in training).

### 9.5 BrowseComp

OpenAI, April 2025 (arXiv 2504.12516): **1,266** questions designed to be hard to find but easy to
verify — short answers with, in principle, a single correct response. Grading in the open
`simple-evals` code is an LLM grader with a template adapted from HLE; it extracts the final answer
and returns `correct: yes/no`, parsed by regex with unparseable output counted as incorrect; a
`confidence` field is requested but unused in scoring. The dataset is distributed encrypted with a
per-row canary string (the key is derived from the canary), and the paper asks readers not to
publish examples in plain text. Weaknesses: it measures a particular skill (persistent search for
obscure facts), not general browsing usefulness; the canary is a deterrent, not a guarantee against
training exposure; and an LLM grader inherits chapter 08 §11's biases, though short-answer
verification keeps that small.

### 9.6 OSWorld and OSWorld-Verified

OSWorld (Xie et al., NeurIPS 2024 Datasets & Benchmarks): computer-use tasks in real VMs
(VMware/VirtualBox/Docker with KVM/cloud providers). The paper reports humans completing over
72.36% of tasks versus 12.24% for the best model then (as relayed by search excerpts; those 2024
numbers are long superseded). Each task has a programmatic evaluation script over the resulting
system state. The repository's `OSWorld-Verified` update (2025-07-28) says it fixes issues reported
by the community, adds AWS-based parallel evaluation and a public evaluation process where
maintainers run submitted agents. Documented operational weaknesses in the README: leftover Docker
containers from interrupted runs degrade performance, macOS hosts lack KVM, and some tasks require
Google-account OAuth or proxy setup, otherwise they fail and **scores drop** — an
environment-configuration effect that looks like a capability gap.

### 9.7 WebArena

Zhou et al.: **812** examples over self-hosted websites (shopping, shopping admin, a Reddit-like
forum, GitLab, a map, an offline Wikipedia). Graded programmatically from the resulting page and
backend state. The README: the environment must be set up by you for correct evaluation (public demo
sites are browse-only) and **reset after the 812 examples**; annotation bugs were fixed in v0.2.0;
the authors recommend the separate AgentLab framework for improved web-navigation infrastructure
(update dated 2024-12-05). Weakness: reset discipline — an unreset environment silently corrupts the
next run (§10.3).

### 9.9 BFCL: a component benchmark

The Berkeley Function-Calling Leaderboard (Gorilla project). Per the official leaderboard page
(page dated 2025-12-16): v1 introduced AST-based evaluation, v2 enterprise and
open-source functions, v3 multi-turn interaction, v4 "holistic agentic evaluation" (web search,
memory, format sensitivity per third-party documentation); overall accuracy is the unweighted
average of sub-categories; results are tied to a specific commit and reproducible with a pinned
`bfcl-eval` package. It evaluates **function-call validity and correctness**, not whether a
user's task was completed in an environment with a policy. Use it as an input to model selection
and as a template for §5's decomposition; do not use it as evidence an agent will resolve
customer cases.

---

## 10. Building your own agent eval suite

> **In plain words.** Start from what actually went wrong in production, turn each failure into a
> task with a starting state and a checkable end state, make the environment resettable, and add a
> few dozen of them before you add a few thousand.
>
> **Real-world example.** Support sees 12 wrong-refund tickets in a week. Each becomes a task: seed
> database with that customer's order history, user persona with the original complaint, state
> assertion "refund = $38.00, order status `refunded`, no other order touched".

Anthropic's roadmap (see Sources) is a good skeleton: start early with 20–50 tasks drawn from real
failures; begin with manual checks and user-reported bugs; write unambiguous tasks with reference
solutions (a 0% pass rate across many trials usually means a *broken task*, not an incapable
agent); balance the set to test when a behavior should *and should not* occur; build a stable
harness that mirrors production; design graders carefully; read transcripts; watch for saturation;
maintain with clear ownership.

### 10.1 Task design from production traces

Pipeline: **sample → reconstruct initial state → state the goal → write the end-state predicate →
write the reference solution → validate**.

1. **Sample** stratified (see §14.2), biased to failures, escalations, rephrases, high-cost and
   long traces. Uniform sampling mostly adds easy tasks.
2. **Reconstruct initial state.** The hard part. A trace shows tool *results*, which is enough to
   build fixtures for read tools; for write tools you also need the pre-state of rows touched.
   Instrument production to log a state-hash or a compact pre-image of every record a tool reads or
   writes (with PII handling — pseudonymize, §14.1).
3. **State the goal** in the user's words, plus any hidden facts a simulator may reveal.
4. **Write the end-state predicate** (§4.2): must-equal, must-not-change, invariants.
5. **Write a reference solution** — a scripted sequence of tool calls that reaches the end state.
   Run it through the grader: it must pass. This catches the most common task bug (an unsatisfiable
   or wrongly-specified goal).
6. **Validate negatives:** a do-nothing agent must fail; a plausible-but-wrong agent (wrong id) must
   fail. If a do-nothing agent passes, the task is vacuous.

Assert that the reference agent grades pass and the null and wrong-id agents grade fail, for every task, in CI.

### 10.3 Environment fixtures and reset

Every trial starts from a bit-identical state. Options, cheapest to most faithful: in-memory fake (dict/SQLite `:memory:`, the §18 lab), SQLite file copied from a golden snapshot, containerized service with DB snapshot restore, full VM/browser (OSWorld/WebArena style).

Rules: (1) **fresh state per trial**, including between `n` repeats — never reuse; (2) **mock the
irreversible**: payment, email, external writes go to recording fakes (ch. 24 §14.5 replays recorded
tool responses); (3) **freeze time** and random seeds *in the environment* (not the model); (4)
**no leakage between trials** — no shared cache, no readable git history of previous trials (a
documented source of inflated scores); (5) **record the pre-state hash** so a failing trial can be
reproduced; (6) **parallelism safe**: separate sandbox per trial.

---

## 11. Simulated users and their biases

> **In plain words.** For chat agents, another LLM plays the customer. That is the only way to run
> many conversations, but the pretend customer is nicer, clearer and more cooperative than real
> ones, and it can make mistakes of its own.
>
> **Real-world example.** The simulator is told "you want to cancel; your reservation id is R7". A
> real user types "uh, my booking, the one for next week?" The agent that scores 85% against the
> simulator may do far worse against that user.

τ-bench, the canonical case, makes the simulator a first-class configuration (default `gpt-4o`,
strategies `llm`, `react`, `verify`, `reflection` in its README), and warns that its auto
error-identification tool, which also uses an LLM, can be wrong. Treat the simulator as a component
with its own spec:

1. **Persona + scenario + hidden facts + stopping rule.** Give the simulator a goal, what it knows,
   what it will only reveal when asked, how it behaves (terse, confused, impatient), and when to
   end the conversation (`###STOP###`-style token or a turn cap).
2. **Known simulator biases**: over-cooperation (supplies information the real user wouldn't);
   over-fluent and over-long messages; leaking the goal verbatim; sycophancy toward the agent's
   framing; role drift (starting to act as the agent); and *self-preference* when the simulator and
   agent share a model family. These are the user-side analogues of 08 §11.2's judge biases.
3. **Measure simulator quality like a judge** (08 §11.1): sample real conversations, run the
   simulator on matching personas, have humans (or a calibrated judge) answer "can you tell which
   is simulated?" and "did the simulator follow its script?". Track *simulator failure rate*
   (task ended because the user went off-script) separately from agent failure.
4. **Pin the simulator**: model id, prompt, seed policy. A silent simulator upgrade changes your
   agent's score (the 08 §11.6 pinning argument applies verbatim).
5. **Report simulator-conditional results** ("pass^4 = X against simulator S on suite v7") and cross-check with a few scripted and human-driven conversations.
6. **Cost and attackers**: the simulator can cost more than the agent (§15.4); for security evals (ch. 17, 28) it plays the attacker.

---

## 12. Graders: code, rubric-LLM, human

> **In plain words.** Prefer the grader that cannot be argued with: code. Use an LLM with a rubric
> where code cannot see the quality (tone, helpfulness, explanation), and validate it against
> humans. Use humans to calibrate, not to grade everything.
>
> **Real-world example.** "Refund = $38.00 and the order is marked refunded" → code. "The agent
> explained the refund policy politely and accurately" → rubric judge, calibrated on 100 human
> labels. "Is this edge case policy-compliant?" → human, once, then it becomes a code rule.

Anthropic's summary of tradeoffs: code-based graders are fast, cheap, objective and reproducible
but brittle and poor at nuance; model-based graders are flexible but non-deterministic, costlier and
need calibration against humans; human graders are the gold standard but expensive and slow. Order
of preference: **code > rubric-LLM > human-in-the-loop for calibration**.

### 12.2 Judge calibration

Everything in [`08-evaluation-methodology.md`](08-evaluation-methodology.md) §11 applies: treat the
judge as a classifier; validate against human labels with agreement and a confusion matrix; know the
biases; pin the judge; budget its cost (§11.7). Agent-specific additions: (a) the judge should see
the **state diff** as well as the transcript, otherwise it grades the story; (b) calibrate
separately on *failed* trials, where the agent's fluent self-report is most misleading; (c) run the
judge on a sample of *passed* trials too, to catch graders that reward unsafe-but-successful paths.

---

## 13. Variance and sample-size arithmetic for agent tasks

> **In plain words.** Two sources of noise: which tasks you chose, and which way the dice fell on
> each run. More trials per task fixes the second; only more tasks fixes the first.
>
> **Real-world example.** 100 tasks, observed pass rate 70%: the 95% interval is about 61–78%.
> You can't tell a 70% agent from a 66% one on that suite. To resolve 5 points you need roughly
> a thousand tasks (unpaired) or, if you compare on the same tasks and half your tasks are
> unaffected, several hundred.

Chapter 08 §3.3 gives the power calculation for retrieval and §13 the tests; here are the
agent-shaped versions. All numbers below are computed arithmetic (script in §18.2).

### 13.1 Interval for a pass rate

For `T` tasks with one trial each and pass rate `p`, use the Wilson interval, not `p ± 1.96·sqrt(p(1−p)/T)`
when `p` is near 0 or 1 or `T` is small.

```python
from math import sqrt
def wilson(c, n, z=1.96):
    p = c / n; d = 1 + z*z/n; m = (p + z*z/(2*n)) / d
    h = z * sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return m - h, m + h
# wilson(70, 100) -> (0.604, 0.781);   wilson(210, 300) -> (0.646, 0.749)
```

### 13.2 Two levels of variance

Variance of the grand mean is `Var(p_i)/T + E[p_i(1−p_i)]/(nT)`: extra trials shrink only the second term. When most tasks are deterministic (always pass or always fail) buy more tasks; when many sit in the middle, buy trials. Bootstrap over tasks, not trials.

### 13.3 Comparing two configurations

Unpaired, two proportions around `p = 0.7`: `n/arm ≈ 2·Z²·p(1−p)/d²` with `Z = 2.8`:
`d = 0.10 → 329` tasks; `d = 0.05 → 1,317`. **Pair on task** (same tasks and seed family for both
arms): only tasks where the configs disagree carry information. With discordant rate `q`
(share of tasks where one passes and the other fails), `n ≈ Z²·q/d²` (a normal approximation of
McNemar; good when `d` is small relative to `q`): `q = 0.2` → `d = 0.10: 157`, `d = 0.05: 627`.
This is why paired analysis (08 §13.1) is the default; the lever is `q` — keep both arms on the
same tasks and control the shared noise sources (simulator seed policy, environment, tool
fixtures).

---

## 14. Online and production evaluation

> **In plain words.** Offline suites tell you what you tested. Production tells you what you
> missed. Sample real traces, score them with the judges and checks you already validated, alert
> when the numbers drift, and turn the failures into new offline tasks.
>
> **Real-world example.** A weekly sample of 300 traces shows `silent_fail` rising from 2% to 6%
> after a tool-schema change. The 18 failing traces become 18 new tasks, and the next CI run
> fails until the fix lands.

Chapter 08 §15 covers shadow/canary/A-B, production signals and the flywheel; read it first. The
agent-specific additions:

### 14.1 What to log so a trace can become a task

Per run: `conversation_id`, model/harness/prompt/tool-set versions, the full tool-call list with
arguments and results (PII-minimized or pseudonymized, with retention rules), token and cost
totals, `termination`, and a **pre-image** (hash or compact copy) of each record read or written —
without it, §10.1 step 2 is impossible. Log the agent's final claim separately from the verified
state where a verifier exists (a post-action read-back is itself a cheap online `silent_fail`
detector).

### 14.2 Sampling

Stratify: escalations and thumbs-down, rephrases, top-1% cost and long traces, budget terminations, tool-error bursts, plus a uniform 1–5% baseline slice; weight strata back when reporting population rates. Cheap deterministic checks (forbidden call, schema failure, step budget, read-back mismatch) run on 100% of traffic; a validated rubric judge (08 §11) runs on the sample, with its own drift monitored by re-scoring a fixed anchor set weekly.

### 14.4 Tracing with OpenTelemetry GenAI spans

The OpenTelemetry GenAI semantic conventions define agent spans. **Status: Development** — the
specification page itself is marked `Status: Development`, so names and attributes may change; pin
your instrumentation version. The current text lives in the
`open-telemetry/semantic-conventions-genai` repository (the conventions moved there from the main
semantic-conventions repo; the old docs path now only contains a "moved" notice):

| Span | Name (SHOULD) | Kind (SHOULD) | Notable attributes |
|---|---|---|---|
| invoke agent (remote agent service) | `invoke_agent {gen_ai.agent.name}` if available | `CLIENT` | `gen_ai.operation.name=invoke_agent` (Required), `gen_ai.provider.name` (Required), `gen_ai.agent.id/name/version`, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens` / `output_tokens` (Recommended) |
| invoke agent (in-process) | same | `INTERNAL` | same set |
| execute tool | `execute_tool {gen_ai.tool.name}` | `INTERNAL` | `gen_ai.tool.name` (Required), `gen_ai.tool.call.id` (Recommended), `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` (**Opt-In** — they may carry PII) |

How this feeds evaluation: (1) an eval trial can emit the same spans as production, so the
metrics in §3.2 (`n_calls`, tokens, latency per span) are computed by one pipeline for both;
(2) `gen_ai.conversation.id` is the join key from a production trace to the task you derive from
it; (3) tool-span attributes give selection and argument evidence for §5 without a custom
logger — with arguments opt-in, enabling capture is a privacy decision (ch. 17, 24 §6.5). Also defined there: `create_agent`, `invoke_workflow`, `plan` and memory operations. See also [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md).

---

## 15. Eval in CI: gates, flakiness, budgets

> **In plain words.** Run a small, cheap slice of the suite on every change, the bigger suite
> nightly and before release, and fail the build on a *drop* in reliability or a *rise* in cost
> per solved task — with enough repeats that the gate isn't a coin flip.
>
> **Real-world example.** A prompt change keeps `pass@1` at 91% but moves `pass^4` from 0.78 to 0.66
> and CPR from $0.52 to $0.71. The gate fails on both; a mean-only gate would have passed.

Tiers (08 §14.1 applies): **PR smoke** (20–40 tasks, `n=3`, minutes, cheap model-mocked layers
first), **nightly** (full suite, `n=5–8`), **release** (full, `n=8–16`, paired against the
current production build, intervals reported).

### 15.1 Gates

Gate on regression against the baseline build on the same suite version, not on absolutes (08 §14.2):

```python
def gate(new, base, *, min_delta_passk=-0.03, max_cpr_ratio=1.15, max_silent=0.02, max_viol=0):
    fails = []
    if new["pass_hat_4"] - base["pass_hat_4"] < min_delta_passk: fails.append("pass^4 regressed")
    if new["cpr"] > base["cpr"] * max_cpr_ratio:                  fails.append("CPR up >15%")
    if new["silent_fail"] > max_silent:                           fails.append("silent failures")
    if new["policy_violations"] > max_viol:                       fails.append("hard policy violation")
    return fails            # tolerances come from your noise measurement (§13), not from taste
```

Hard-constraint tasks (forbidden calls, authorization) gate with zero tolerance and `n` large
enough that a 1-in-`n` slip is detected: an agent with a true 5% violation rate escapes `n=8`
trials with probability `0.95^8 ≈ 0.66`, so a security regression tier needs many more trials or
deterministic enforcement outside the model (ch. 24, 29).

### 15.2 Flaky tests

Classify before you retry: (a) **environment/grader flake** — the reference solution fails
sometimes; fix the fixture, never mask it; (b) **agent variance** — a task between 0 and 1; keep it,
because that *is* the signal, and judge with `pass^k` and an interval, not a rerun-until-green;
(c) **infra flake** — sandbox timeouts, rate limits; retry the *trial* (record the retry count; cap
at 1–2) but never retry a trial because it failed the grader. Quarantine tasks whose reference
fails; they stop gating and open a ticket with a deadline.

### 15.4 Budget control

Eval cost = agent + simulator + judge + sandbox, times `T × n`. Illustrative arithmetic: 150 tasks
× 8 trials × $0.40 per trial (agent + simulator + judge) = $480 per full run; run nightly and that
is ~$14,400 a month — more than many products' inference bills. Controls: per-trial caps (steps,
tokens, wall-clock, dollars) enforced by the eval harness and recorded as `termination=budget`;
a **suite-level** spend cap that aborts the run; a smaller simulator/judge once validated; judge
only trials the code grader passed or marked ambiguous; Batch/caching where the API allows (08
§11.7); early stopping once a gate's outcome is statistically decided; and a nightly job that
reports its own cost as a metric.

---

## 16. Anti-patterns

- **Grading the transcript, not the state.** Rewards fluent false claims. (§4)
- **One run per task.** A mean from one trial hides reliability; report `pass^k`. (§6)
- **Gating on exact tool sequences.** Brittle; penalizes valid paths. Gate on end state and on
  *path-independent* hard constraints. (§3.1)
- **Never validating tasks.** No reference-solution run, no null-agent run: vacuous or unsolvable
  tasks silently skew the metric. (§10.1)
- **Unpinned simulator or judge.** The time series breaks when a vendor alias moves. (§11)
- **Treating a public benchmark number as a property of the model.** It belongs to model + harness + grader + date. (§8, §9)
- **Cost per run instead of per resolved task; no budget cap on the eval itself.** (§7, §15.4)

---

## 17. Interview questions

1. **Why can't you evaluate an agent with a single accuracy number?** Variance compounds along the
   trajectory, so one run is a draw from a distribution; the cost and side-effect profile are
   invisible in accuracy; and silent failures need state inspection. Report `pass^k`, CPR,
   `silent_fail`, with intervals.
2. **Transcript versus state grading: when does each win?** State wins for any task whose product is
   a change in the world (DB, filesystem): the agent may claim success falsely or hit the wrong
   object. Transcript grading wins for path-independent policy constraints and message-as-product
   tasks. Run both and track disagreement.
3. **Same model, two scaffolds, 15 points apart?** Scaffold is a first-order variable: run a 2×2 (model × harness) with identical budgets and graders and check intervals; leaderboards are observational.
4. **How do you build the first 30 tasks?** From real failures: reconstruct initial state, write a
   goal and an end-state predicate, validate with reference/null/wrong agents, tag with source and
   date, hold some out.
5. **How do you control user-simulator bias?** Pin and validate it like a judge, give it a hidden-facts
   script and a stop rule, compare with scripted and human-driven conversations, report
   simulator-conditional results.

6. **An agent has 90% per-trial success; what does a customer who repeats 8 times see?** If flat,
   `0.9^8 = 0.43`; with heterogeneous tasks suite `pass^k` is the mean of `p_i^k`, so compute per task.
7. **What belongs in a CI gate?** Regression in `pass^k` vs the baseline build, CPR ratio, `silent_fail`
   and hard-policy violations, with tolerances from measured run-to-run spread and enforced budgets.

---

## 18. Lab exercises

Each lab runs on a laptop with stdlib Python 3 (`python3 -I` to avoid loading local modules). The
environment and agent are deliberately toy: the point is the measurement machinery, not realism.
The code of §18.1 is a single file; outputs shown were produced by running it.

### 18.1 Toy airline DB, scripted agent, state grader, `pass^k`, harness variation

Save as `airline_eval.py`.

```python
import copy, random
from dataclasses import dataclass, field
from math import comb

SEED_DB = {
    "reservations": {
        "R1": {"user": "u1", "flight": "F100", "status": "booked", "bags": 0},
        "R2": {"user": "u2", "flight": "F100", "status": "booked", "bags": 1},
        "R3": {"user": "u3", "flight": "F200", "status": "booked", "bags": 0}},
    "flights": {"F100": {"seats": 1}, "F200": {"seats": 5}, "F300": {"seats": 3}},
}

class Env:                                   # fresh state per trial = isolation
    def __init__(self):
        self.db, self.calls = copy.deepcopy(SEED_DB), []
    def call(self, tool, **a):
        self.calls.append((tool, dict(a)))
        r = self.db["reservations"].get(a.get("rid"))
        if tool == "get_reservation": return r or {"error": "no such reservation"}
        if r is None: return {"error": "no such reservation"}
        if tool == "change_flight":
            f = self.db["flights"].get(a["flight"])
            if not f or f["seats"] < 1: return {"error": "no seats"}
            self.db["flights"][r["flight"]]["seats"] += 1
            f["seats"] -= 1; r["flight"] = a["flight"]; return {"ok": True}
        if tool == "cancel":
            r["status"] = "cancelled"; self.db["flights"][r["flight"]]["seats"] += 1
            return {"ok": True}
        return {"error": "unknown tool"}

@dataclass
class Task:
    id: str; steps: list; expected: dict
    forbidden_untouched: set = field(default_factory=set)

TASKS = [
    Task("move_R1", [("change_flight", {"rid": "R1", "flight": "F300"})],
         {("reservations", "R1", "flight"): "F300"}, {"R2", "R3"}),
    Task("cancel_R3", [("cancel", {"rid": "R3"})],
         {("reservations", "R3", "status"): "cancelled"}, {"R1", "R2"}),
    Task("move_R3_cancel_R2",
         [("change_flight", {"rid": "R3", "flight": "F300"}), ("cancel", {"rid": "R2"})],
         {("reservations", "R3", "flight"): "F300",
          ("reservations", "R2", "status"): "cancelled"}, {"R1"}),
]

def state_grade(task, env):                  # outcome grader, with collateral check
    ok = all(env.db[t][k][f] == v for (t, k, f), v in task.expected.items())
    return ok and all(env.db["reservations"][r] == SEED_DB["reservations"][r]
                      for r in task.forbidden_untouched)

def transcript_grade(msg): return "done" in msg.lower()   # the weak grader

@dataclass
class AgentCfg:
    p_wrong_arg: float = 0.12; p_skip: float = 0.05; p_fix_after_error: float = 0.8

def run_agent(task, env, rng, cfg, harness):
    for tool, args in task.steps:
        if rng.random() < cfg.p_skip: continue
        a = dict(args)
        if rng.random() < cfg.p_wrong_arg: a["rid"] = rng.choice(["R9", "R2", "R1", "R3"])
        res = harness.dispatch(env, tool, a)
        if "error" in res and rng.random() < cfg.p_fix_after_error:
            harness.dispatch(env, tool, dict(args))
    return "Done."                           # the fake agent always claims success

class BareHarness:
    def dispatch(self, env, tool, args): return env.call(tool, **args)

class GuardedHarness:                        # same "model", different scaffold
    def __init__(self, owned): self.owned = owned
    def dispatch(self, env, tool, args):
        if tool != "get_reservation" and args.get("rid") not in self.owned:
            return {"error": f"policy: caller does not own {args.get('rid')}"}
        return env.call(tool, **args)

def trial(task, seed, cfg, mk):
    env, rng = Env(), random.Random(seed)
    msg = run_agent(task, env, rng, cfg, mk(task))
    return state_grade(task, env), transcript_grade(msg), len(env.calls)

def pass_hat_k(c, n, k): return comb(c, k) / comb(n, k) if n >= k else float("nan")

def evaluate(mk, cfg, n=16):
    counts, silent, calls = [], 0, 0
    for ti, t in enumerate(TASKS):
        res = [trial(t, ti * 1000 + i, cfg, mk) for i in range(n)]
        counts.append(sum(r[0] for r in res))
        silent += sum((not r[0]) and r[1] for r in res); calls += sum(r[2] for r in res)
    N = n * len(TASKS)
    out = {"pass@1": sum(counts) / N, "silent_fail": silent / N, "calls/trial": calls / N}
    for k in (1, 2, 4, 8):
        out[f"pass^{k}"] = sum(pass_hat_k(c, n, k) for c in counts) / len(counts)
    return out

if __name__ == "__main__":
    cfg = AgentCfg()
    for name, mk in (("bare", lambda t: BareHarness()),
                     ("guarded", lambda t: GuardedHarness({a["rid"] for _, a in t.steps}))):
        print(f"{name:8s}", {k: round(v, 3) for k, v in evaluate(mk, cfg).items()})
```

Output of the run used for this chapter (seeded, deterministic):

```
bare     {'pass@1': 0.875, 'silent_fail': 0.125, 'calls/trial': 1.375, 'pass^1': 0.875, 'pass^2': 0.761, 'pass^4': 0.564, 'pass^8': 0.278}
guarded  {'pass@1': 0.938, 'silent_fail': 0.062, 'calls/trial': 1.312, 'pass^1': 0.938, 'pass^2': 0.878, 'pass^4': 0.767, 'pass^8': 0.578}
```

Same scripted "model" (identical error rates), different harness: `pass^8` goes 0.278 → 0.578. Tasks
to try: (a) replace `state_grade` with `transcript_grade` and watch every trial "pass"; (b) add a
`validate_task` run (§10.1) with a null agent; (c) set `p_fix_after_error=0` and see what the guard
alone buys; (d) add a `must_not_change` violation (agent cancels an extra reservation) and confirm
the grader fails it; (e) compute the spread of `pass^4` over 5 different base seeds to set a
CI tolerance (§15.1).

### 18.2 Decay table and sample-size arithmetic

```python
from math import sqrt
for p in (0.8, 0.9, 0.95, 0.99):
    print(p, [round(p**k, 3) for k in (1, 2, 4, 8, 10, 20)])
n_unpaired = lambda p, d: 2 * 2.8**2 * p * (1 - p) / d**2     # Z = 1.96 + 0.84 = 2.8 (rounded)
n_paired   = lambda q, d: 2.8**2 * q / d**2
print([round(n_unpaired(.7, d)) for d in (.05, .10)], [round(n_paired(.2, d)) for d in (.05, .10)])
```

(Using `Z = 2.8` gives 1,317/329 and 627/157, the values in §13; the exact `Z=1.96+0.84` rounds
the same.)

### 18.3 Harness-variation experiment

Add a harness that verifies by read-back (re-read the record after the last call, re-issue if wrong) and one that caps steps at 1; tabulate `(harness, pass@1, pass^4, silent_fail, calls/trial)`; then vary `p_wrong_arg` (0.12 vs 0.06) in a 2×2 and decide whether model or harness moves `pass^8` more, reporting the interaction.

---

## 19. Real-world cases

Documented cases first (each with its source); then illustrative composites.

**Case A — Grader bugs, not capability (documented).** Anthropic's guide reports that Opus 4.5
scored 42% on CORE-Bench at first and about 95% after grading bugs were fixed. Lesson: audit
graders before explaining a score (§4.5, §12.2).
Source: anthropic.com/engineering/demystifying-evals-for-ai-agents.

**Case B — A "failing" run that was better (documented).** Opus 4.5 on a τ²-bench flight-booking
task found a loophole in the policy; the eval as written marked it failed while the customer got a
better outcome. Lesson: grade outcomes, review disputed failures (same source).

**Case D — Why SWE-bench Verified exists (documented).** OpenAI (2024-08-13) found overly specific
tests, underspecified issue text and hard-to-set-up environments in the original SWE-bench and
released a 500-sample human-validated subset. The share of samples filtered out is given in
OpenAI's post; cite it from there. Source:
openai.com/index/introducing-swe-bench-verified/.

**Case E — Reliability decay in a published leaderboard (documented, dated).** The τ-bench README
lists claude-3-5-sonnet-20241022 at airline Pass^1 0.460 → Pass^4 0.225 (retail 0.692 → 0.462).
Superseded results; the shape is the lesson (§6.1). Source: github.com/sierra-research/tau-bench.

### Illustrative scenario (composite, not a specific company): the green dashboard

Setup: a refund agent, 200 tasks, judged by an LLM reading transcripts; `pass@1` 94%. Symptom:
finance finds wrong refunds. Diagnosis: adding a state grader shows 9% of "passes" had the wrong
order modified (`silent_fail`). Arithmetic (illustrative): 94% × 200 = 188 passes; 9% of 188 ≈ 17
wrong-state passes → true success ≈ 171/200 = 85.5%. Fix: state grader plus collateral assertions
as the gate; transcript judge demoted to a diagnostic column.

### Illustrative scenario (composite, not a specific company): the cheap model that cost more

Setup: model A $0.30/run at 70% success, model B $0.80/run at 92%; a failure costs $5 to fix.
Arithmetic: A = (0.30 + 0.30×5)/0.70 ≈ $2.57 per resolved task; B = (0.80 + 0.08×5)/0.92 ≈ $1.30.
The team had shipped A on cost per run.

---

## Sources

Fetched and read in full or in part (primary):
- Anthropic Engineering, *Demystifying evals for AI agents* — https://anthropic.com/engineering/demystifying-evals-for-ai-agents
- τ²-bench repository README and `docs/evaluation.md` — https://github.com/sierra-research/tau2-bench (raw: https://raw.githubusercontent.com/sierra-research/tau2-bench/main/docs/evaluation.md)
- τ-bench repository README — https://github.com/sierra-research/tau-bench
- Terminal-Bench repository README — https://github.com/laude-institute/terminal-bench ; Harbor — https://github.com/laude-institute/harbor
- SWE-bench repository README — https://github.com/SWE-bench/SWE-bench
- OSWorld repository README — https://github.com/xlang-ai/OSWorld
- WebArena repository README — https://github.com/web-arena-x/webarena
- OpenAI simple-evals (BrowseComp grader code) — https://github.com/openai/simple-evals
- OpenTelemetry GenAI semantic conventions (agent spans; tool spans) —
  https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md ,
  https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md
  (rendered at https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans)

Seen only as search-result excerpts (primary pages not opened): Terminal-Bench paper
(https://arxiv.org/abs/2601.11868), τ-bench paper (https://arxiv.org/abs/2406.12045), τ²-bench paper
(https://arxiv.org/abs/2506.07982), GAIA (https://arxiv.org/abs/2311.12983), BrowseComp
(https://arxiv.org/abs/2504.12516), SWE-bench Pro (https://arxiv.org/abs/2509.16941), OpenAI SWE-bench
Verified (https://openai.com/index/introducing-swe-bench-verified/), BFCL
(https://gorilla.cs.berkeley.edu/leaderboard), Epoch AI review (https://epoch.ai/benchmarks/swe-bench-pro/review),
Terminal-Bench leaderboard (https://www.tbench.ai/leaderboard/terminal-bench/2.0; §8.2 numbers came from secondary trackers).
