# 22 — agent orchestration patterns

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the pipeline as dataflow — an
> agent is that same dataflow with a decision node that used to be code replaced by a model call,
> and every consequence in that chapter about correctness living in the data still applies),
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md) (§13's
> multi-hop-retrieval-multiplies-everything remark is this chapter's §4 written out in full),
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (the golden-set and
> regression-gate discipline this chapter's §14 reuses wholesale — an agent trajectory is a
> harder-to-score version of the same problem, not a different one),
> [`../python-mastery/29-async-patterns-and-pitfalls.md`](../python-mastery/29-async-patterns-and-pitfalls.md)
> (bounded concurrency with cancellation is exactly what §7's parallel tool execution needs, and
> getting it wrong there is how a stuck tool call becomes a stuck agent here),
> [`../python-mastery/30-concurrency-correctness.md`](../python-mastery/30-concurrency-correctness.md)
> (shared mutable state under concurrent writers is §6's and §8's problem restated),
> [`../databases/16-failure-detection-and-leader-election.md`](../databases/16-failure-detection-and-leader-election.md)
> (a supervisor deciding a worker is dead and re-routing its work is failure detection with an LLM
> doing the detecting instead of a phi-accrual detector — the coordination problem underneath §5 is
> older than agents and better specified there),
> [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
> (the span-per-call discipline this chapter's §12 assumes already exists),
> [`../distributed-systems/README.md`](../distributed-systems/README.md) (a multi-agent system
> that lets agents coordinate without a central authority is a distributed system, and it inherits
> every hard problem in that folder — §5.3's warning about peer-to-peer is this fact stated
> plainly).
>
> **Feeds into:** `13-agents-and-tool-calling.md` (planned — the tool-schema and multi-hop-retrieval
> mechanics this chapter's §7 assumes as background), `14-agent-evaluation.md` (planned — §14 here
> is that chapter's summary; the full trajectory-scoring machinery belongs there),
> `16-multi-tenancy-and-isolation.md` (planned — §13's tenant-isolation requirement is that
> chapter's subject applied to an agent runtime), `17-safety-guardrails-and-prompt-injection.md`
> (planned — §7.3's "every tool output is untrusted input" rule is that chapter's thesis, needed
> one level earlier than that chapter can assume), `18-failure-modes-and-incident-walkthrough.md`
> (planned — §10's failure taxonomy is the vocabulary that walkthrough will use), and the P3/P4
> projects in this folder's `README.md` (§5 project ladder) directly.
>
> **THESIS:** an agent is not a new kind of software artifact. It is a control-flow decision —
> "what happens next" — moved from code, where it is cheap to test, bound and audit, into a model
> call, where it is none of those things. Every pattern in this chapter exists to buy back some of
> what that move cost: a workflow buys back testability by keeping the decision in code wherever
> the decision is actually knowable in advance; a loop with a budget buys back boundedness; a
> tool registry with schema validation buys back auditability; a supervisor pattern buys back
> decomposability. **The orchestration problem is not "how do I make the model smarter." It is
> "how much control flow can I keep in code, and how do I contain the blast radius of the part I
> can't."** A platform engineer's job — the job this document is written for — is to build the
> substrate that makes the contained part cheap to build correctly and hard to build incorrectly,
> for every team that needs one, not to build one clever agent.

---

## Contents

1. [The spectrum from chains to agents](#1-the-spectrum-from-chains-to-agents)
2. [Deterministic workflows versus agentic workflows](#2-deterministic-workflows-versus-agentic-workflows)
3. [Agent architectures — ReAct, Plan-and-Execute, Reflexion, LATS](#3-agent-architectures--react-plan-and-execute-reflexion-lats)
4. [The agent loop](#4-the-agent-loop)
5. [Multi-agent patterns](#5-multi-agent-patterns)
6. [Agent communication and the blackboard pattern](#6-agent-communication-and-the-blackboard-pattern)
7. [Tool orchestration](#7-tool-orchestration)
8. [State management for agents](#8-state-management-for-agents)
9. [Human-in-the-loop patterns](#9-human-in-the-loop-patterns)
10. [Failure handling](#10-failure-handling)
11. [Orchestration frameworks compared](#11-orchestration-frameworks-compared)
12. [Production concerns — observability, cost, latency, security](#12-production-concerns--observability-cost-latency-security)
13. [Enterprise agent platform design](#13-enterprise-agent-platform-design)
14. [Evaluation of agent systems](#14-evaluation-of-agent-systems)
15. [Anti-patterns](#15-anti-patterns)
16. [Interview questions](#16-interview-questions)
17. [Lab exercises](#17-lab-exercises)

---

## 1. The spectrum from chains to agents

Treat this as one axis, not six categories: **how much of the control flow is decided at compile
time (by an engineer, in code) versus at run time (by a model, in tokens).** Everything else —
cost variance, testability, security surface, the shape of your on-call rotation — is downstream
of where a given system sits on that axis.

**A single LLM call** is a stateless function: input in, completion out. The control flow is
"call the function." There is exactly one thing that can go wrong at the orchestration level —
the call itself fails or times out — and standard RPC reliability patterns (retry, timeout,
circuit breaker) cover it completely. There is no orchestration problem here worth a chapter.

**A chain** composes fixed LLM calls with fixed data transformations: extract, then summarize,
then classify, in an order the engineer wrote down and the runtime never deviates from. The
control flow is a DAG known at deploy time. You can unit test each node with a fixed input and a
fixed expected shape of output. You can put a real p99 latency SLO on the whole thing because the
number of calls is constant. This is still, structurally, a stateless function — it just has more
internal steps.

**RAG**, as covered in `00`–`04`, is a chain with one node — retrieval — whose *output* is
data-dependent (different documents for different queries) even though the *step it occupies in
the sequence* is not. This is an important distinction that gets blurred in casual conversation:
RAG is not "agentic" by virtue of having a retrieval step. The control flow — retrieve, then
generate — is fixed. Only the retrieved content varies. That is why `00`–`04` could write hard
recall-ceiling and cascade-ordering guarantees: those guarantees depend on the step sequence being
fixed, and it is.

**A tool-using agent** is the first point on this spectrum where the *step sequence itself*
becomes data-dependent: the model decides which tool to call, or whether to call one at all, and
that decision is not knowable at deploy time. This is a qualitative jump, not a bigger version of
the previous rungs. A chain's failure modes are "step N produced bad output." A tool-using agent's
failure modes include "step N called the wrong tool," "step N called the right tool with malformed
arguments," and "step N decided not to call a tool when it should have" — none of which have a
code-level analogue to test against, because the thing under test is a probability distribution
over next actions, not a function.

**An autonomous agent** extends this by removing the fixed *step count* as well as the fixed step
sequence: the model decides not just what to do next but when it is done. This is where §4's loop,
budget and termination machinery becomes mandatory rather than optional — a tool-using agent
embedded in a chain still terminates when the chain does, but an autonomous agent's only stopping
condition, absent engineering intervention, is the model deciding to stop, and models are not
reliable stopping-condition detectors.

**A multi-agent system** composes multiple autonomous-agent-shaped control loops, each with its
own decision authority, coordinating through some shared substrate. This is not "a bigger agent."
It is a distributed system whose nodes are non-deterministic, and it inherits every hard problem
that phrase implies: partial failure, message loss, inconsistent shared state, and — the one that
is genuinely new relative to `../distributed-systems/` — nodes that can be *actively wrong* about
what they just did, not merely delayed or crashed.

```
    control flow known at compile time ───────────────────────────► decided at run time
    │                                                                                  │
    single call ── chain ── RAG ── tool-using agent ── autonomous agent ── multi-agent system
    │                                                                                  │
    testable with     ...          step sequence          step count       coordination
    fixed fixtures                 is data-dependent       is data-dependent  is data-dependent
```

### 1.1 What actually changes at each step, stated precisely

Four properties degrade monotonically as you move right, and each one is the actual reason the
orchestration problem gets harder — not vague unease about "less control":

1. **The test oracle degrades.** A chain has a known-correct output shape you can assert against.
   A tool-using agent has a known-correct *action* for a given state, which you can assert against
   only if you have a labeled trajectory (§14). An autonomous agent has a known-correct *policy*,
   which you can only approximate with a distribution of acceptable trajectories. This is why
   evaluation infrastructure investment should scale with position on this axis, not with how
   impressive the demo looks.
2. **Cost and latency variance grows unbounded.** A chain's cost is `Σ(fixed calls)`. An
   autonomous agent's cost is `Σ(calls until stop)`, and "until stop" is a random variable with a
   right tail that, absent a hard cap, is unbounded — a stuck loop (§10.5) does not politely fail,
   it burns tokens until something external kills it.
3. **The security surface grows with the action space, not the input space.** A chain that only
   ever calls `search()` and `summarize()` has an attack surface bounded by what those two
   functions can do. An agent with a general-purpose `execute_shell()` tool has an attack surface
   bounded by what a shell can do, and every prompt-injection payload in its retrieved context
   (`17`, planned) is now attempting to steer a general-purpose actuator, not a text generator.
4. **Debuggability requires a trace, not a stack trace.** A chain's failure is "node 3 threw."
   An autonomous agent's failure is usually "the trajectory of 14 decisions was individually
   locally reasonable and collectively wrong," which is a property of a sequence, not a point, and
   which is why §12's observability requirement is not optional tooling hygiene — without a full
   decision trace, a multi-step agent failure is simply not reconstructible after the fact.

None of this is an argument against agents. It is the reason §2 exists: every one of these four
costs is optional per feature, not mandatory per system, and the engineering skill this whole
chapter is teaching is deciding, feature by feature, whether you need to pay it.

---

## 2. Deterministic workflows versus agentic workflows

Use this vocabulary precisely, because the industry does not and the imprecision costs real
money: a **workflow** is a system whose control flow — which steps run, in which order, under
which conditions — is fixed by code at deploy time, even though individual steps may call an LLM.
An **agent** is a system in which the LLM itself decides the control flow at run time: which step
runs next, whether to loop, when to stop. A system can call an LLM twenty times and still be a
workflow if code decided all twenty call sites in advance. A system that calls an LLM once to
decide "call tool A or tool B" has crossed into agent territory for that one decision, even if
everything else about it is a fixed pipeline.

This reframes the industry's favorite question — "should we build an agent or a workflow" — as
malformed. The real question is **which decisions in this system have a control-flow branch that
is genuinely unknowable in advance, and which only look that way because no one has enumerated the
cases.** Every decision that is actually enumerable belongs in code. Every decision that is not
belongs to the model, and only that decision, not the surrounding scaffolding.

### 2.1 The reliability-flexibility tradeoff, made concrete

A workflow's reliability comes from a property an agent structurally cannot have: **the space of
things that can happen is the space you tested.** If the workflow has five branches, you can write
five tests, and a green test suite means the deployed behavior is bounded by what you verified. An
agent's space of things that can happen is the space of token sequences the model can emit, which
for any interesting model is not enumerable, so "we tested it" can only ever mean "we sampled it,"
and a green eval run means the sampled behavior was acceptable on the sampled inputs — a much
weaker and much more honest claim (this is the same rung-1-versus-rung-3 distinction as
`08` §readme and `../python-mastery/31-measurement-methodology.md`, applied to control flow instead
of to a metric).

An agent's flexibility comes from the mirror-image property: it can handle an input shape nobody
anticipated, by construction, because nobody had to anticipate it. A workflow handles exactly the
input shapes its branches cover and silently mishandles (or crashes on) everything else, unless an
engineer goes back and adds a branch.

The tradeoff is therefore not a preference, it is a measurement question: **what is the actual
distribution of inputs this system will see, and what fraction of that distribution is covered by
enumerable branches versus genuinely novel cases?** Support ticket routing over five known
categories is almost entirely enumerable — build a workflow, and reserve the model for the
residual "none of the above" bucket, ideally routed to a human, not a bigger agent. Open-ended
research-assistant queries over an unbounded corpus are not enumerable — an agentic retrieval loop
(`04` §13's forward reference) is closer to the right shape, but even there the *tool set* and
*termination policy* should be fixed by code; only "which tool, how many times" is the model's
decision.

### 2.2 Anthropic's five workflow patterns, and why they are the right default

The useful taxonomy to internalize — because interviewers will probe for it directly — treats an
LLM call as one **augmented building block** (a call plus retrieval, tools, and memory) and
composes building blocks with code:

- **Prompt chaining**: fixed sequence of calls, each step's output feeding the next, with optional
  programmatic checks ("gates") between steps that can short-circuit the chain. This is `00`–`04`'s
  pipeline exactly.
- **Routing**: one call classifies the input, and code dispatches to one of several downstream
  chains based on the classification. The branch *set* is fixed; only which branch fires is
  data-dependent. This is the right shape for the support-ticket example above.
- **Parallelization**: independent sub-tasks fan out to concurrent LLM calls and fan back in,
  either as *sectioning* (split a task into independent pieces) or *voting* (run the same task N
  times and aggregate, for a self-consistency effect). Reuses `../python-mastery/29-async-patterns-and-pitfalls.md`'s
  bounded-concurrency machinery directly.
- **Orchestrator-workers**: a central call decomposes a task into an a-priori-unknown *number* of
  subtasks (unlike routing, where the branch set is fixed) and dispatches each to a worker call.
  This is the first pattern on this list with a genuinely agentic decision in it — "how many
  subtasks, and what are they" — but the workers themselves can still be pure functions.
- **Evaluator-optimizer**: one call generates, another call critiques against explicit criteria,
  and the loop repeats until the critique passes or a retry budget is exhausted. This is Reflexion
  (§3.3) collapsed into two roles and a fixed exit condition, and it is a workflow, not an agent,
  because the *loop structure* is code even though the *content* of each pass is a model call.

The pattern connecting all five: **the LLM decides content, code decides structure.** That is the
operational definition of "stay on the deterministic end," and it is achievable for a much larger
fraction of real product requirements than the "agents can do anything" pitch suggests.

### 2.3 Why "let the LLM figure it out" is the most expensive sentence in this chapter

Every one of §1.1's four costs is purchased, in full, the moment a team defaults to "give it a
system prompt and a pile of tools and let it figure out the steps" for a task whose steps were
actually enumerable. The failure mode is not dramatic — it does not usually look like a rogue agent
doing something alarming. It looks like a routing task that now costs 6x more in tokens than a
classifier would have, that fails unpredictably on inputs a `case` statement would have handled
correctly every time, that has no test suite because "testing an agent" felt like a research
problem, and whose on-call engineer cannot answer "why did it do that" for last night's incident
without re-reading a wall of chain-of-thought that was never designed to be an audit log.

```python
# The workflow version: decision structure is code, tested with five fixtures.
def route_ticket(ticket: Ticket) -> Response:
    category = classify(ticket.text)          # one bounded LLM call
    if category == "billing":
        return handle_billing(ticket)          # deterministic downstream logic
    elif category == "outage":
        return escalate_to_oncall(ticket)       # deterministic downstream logic
    elif category == "how_to":
        return answer_from_docs(ticket)         # RAG chain, fixed shape
    else:
        return route_to_human(ticket)           # the one honest "novel case" branch

# The over-agentic version: decision structure is inside the model, tested with vibes.
def route_ticket_agentically(ticket: Ticket) -> Response:
    agent = Agent(tools=[classify, handle_billing, escalate_to_oncall,
                          answer_from_docs, route_to_human, search_kb, send_email, ...])
    return agent.run(f"Handle this support ticket appropriately: {ticket.text}")
    # Every branch above still exists as a tool call the model *might* make.
    # None of them is guaranteed, none is bounded in call count, and the failure
    # mode when the model picks wrong is silent, not a stack trace.
```

The second version is not wrong because agents are bad. It is wrong because the task's decision
structure was fully known and the code threw that knowledge away and paid to have the model
re-derive it, badly, every single request. The rule of thumb worth carrying into an interview:
**default to the workflow; earn the agent by identifying the specific decision that is not
enumerable, and scope the model's autonomy to exactly that decision.**

---

## 3. Agent architectures — ReAct, Plan-and-Execute, Reflexion, LATS

Once §2 has identified a decision that genuinely belongs to the model, the next question is *how*
the model should make it — as a single reasoning pass, an interleaved loop, a plan committed up
front, or a search over multiple candidate trajectories. These four architectures are not
competing products; they are points on a cost-versus-recoverability curve, and picking one is
picking where on that curve the task's error tolerance actually sits.

### 3.1 Chain-of-thought with tools — the baseline everyone starts from

The simplest tool-using pattern lets the model reason in free text and occasionally emit a tool
call, with no structural enforcement of an act-then-observe rhythm. The model might reason for
three paragraphs, call one tool, reason for three more, and never look at the tool's result before
concluding. This is cheap to implement — most vendor SDKs give you this for free — and it is the
weakest architecture on this list precisely because there is no code-enforced checkpoint forcing
the model to *incorporate* an observation before proceeding. It is adequate for single-tool-call
tasks ("look up this one fact, then answer") and unreliable the moment a task needs the result of
tool call N to decide the arguments of tool call N+1.

### 3.2 ReAct — Reason and Act, interleaved

ReAct (Yao et al., 2022) is the architecture that made "agent loop" a standard term: the model
emits a `Thought`, then an `Action` (a tool call), the runtime executes the action and returns an
`Observation`, and the model's *next* thought is conditioned on that observation before it acts
again. The structural discipline — one action per turn, mandatory observation before the next
thought — is the entire value proposition. It does not make the model smarter; it forces the
model's reasoning to be grounded in what actually happened rather than in what it assumed would
happen, which is the single largest source of compounding hallucination in ungoverned
chain-of-thought.

```python
def react_loop(task: str, tools: dict, max_steps: int = 8) -> str:
    scratchpad = []
    for step in range(max_steps):
        prompt = build_react_prompt(task, scratchpad)
        thought, action, action_input = llm_call(prompt)          # one action per turn
        if action == "finish":
            return action_input
        if action not in tools:
            observation = f"Error: unknown tool '{action}'. Available: {list(tools)}"
        else:
            observation = tools[action](action_input)              # act
        scratchpad.append((thought, action, action_input, observation))  # observe, then loop
    return "Stopped: exceeded max_steps without reaching a final answer."
```

ReAct's cost profile is one model call per step, which is cheap per step and can be expensive in
aggregate on long trajectories, and its recoverability is good — because every step is grounded in
the previous observation, a bad intermediate result is visible to the next thought and can often be
corrected without restarting. It fits open-ended, moderate-length tasks: multi-hop question
answering, most agentic retrieval, most single-agent tool use in production today. It is the
correct default for §4's loop implementation.

### 3.3 Plan-and-Execute — commit to a plan, then run it

Plan-and-execute architectures separate two roles that ReAct fuses into every step: a **planner**
call produces an ordered list of subtasks up front, and an **executor** — which may itself be a
ReAct loop, a tool call, or a sub-agent — works through the list, with the planner optionally
invoked again to re-plan if a step fails or new information invalidates the remaining plan.

```python
def plan_and_execute(task: str, tools: dict) -> str:
    plan = llm_plan(task)                       # one call: ["step 1", "step 2", "step 3"]
    results = []
    for i, step in enumerate(plan):
        outcome = execute_step(step, tools, context=results)
        if outcome.failed:
            plan = llm_replan(task, plan, done=results, failed_at=i)  # re-plan, don't restart
            continue
        results.append(outcome)
    return llm_synthesize(task, results)
```

The cost advantage over ReAct is real: one planning call amortizes over N execution steps instead
of paying a full reasoning pass at every step, and it is cheaper still if execution steps are
deterministic tool calls rather than sub-LLM-calls. The recoverability disadvantage is equally
real: the plan is a prediction made with the least information the system will ever have (before
any step has run), so a plan that is wrong about step 3 because of something only discoverable at
step 1 either needs an explicit re-plan trigger (as above) or will execute a flawed plan to
completion. Plan-and-execute fits tasks with a knowable-in-advance decomposition and expensive
individual steps — data pipeline orchestration, multi-document report generation — where the
planning overhead is amortized across enough execution volume to matter, and where steps are
independent enough that an early planning error is unlikely to be discovered only long after the
fact.

### 3.4 Reflexion — verbal self-critique as reinforcement

Reflexion (Shinn et al., 2023) adds a role neither of the above has: after a trajectory concludes —
successfully or not — a **reflection** step has the model critique its own attempt in natural
language ("I failed because I assumed the API returned dates in ISO format; it returns Unix
timestamps"), and that reflection is stored in an episodic memory buffer that is included in the
prompt on the *next* attempt at the same or a similar task. It is reinforcement learning without
gradient updates: the "policy improvement" happens entirely in the prompt, across attempts, not
inside the model's weights.

```python
def reflexion_attempt(task: str, tools: dict, memory: list[str]) -> tuple[str, bool]:
    prompt = build_prompt(task, prior_reflections=memory)
    result = react_loop_with_prompt(prompt, tools)
    success = evaluate(task, result)             # needs an external or LLM-judge success signal
    if not success:
        reflection = llm_reflect(task, result)   # "what went wrong, in one paragraph"
        memory.append(reflection)
    return result, success

def reflexion_run(task: str, tools: dict, max_attempts: int = 3) -> str:
    memory: list[str] = []
    for _ in range(max_attempts):
        result, success = reflexion_attempt(task, tools, memory)
        if success:
            return result
    return result  # best-effort final attempt, with accumulated self-critique
```

Reflexion buys meaningfully better performance on tasks where failures are *legible* — the model
can articulate why it failed in a way that changes behavior on retry — and it requires a success
signal to trigger the reflection step, which is the part teams underestimate: without an external
verifier (unit tests, a checker function, a human), "evaluate success" degenerates into the model
grading its own homework, and self-graded Reflexion tends to converge on confident, wrong answers
faster than plain retry does. It fits coding and tool-use tasks with a cheap, objective checker
(does the test suite pass, does the API call return 200) and fits poorly where success is itself
subjective and unverifiable within the loop.

### 3.5 LATS — Language Agent Tree Search

LATS (Zhou et al., 2023) is the most expensive and most thorough architecture on this list: it
runs Monte Carlo Tree Search over the space of possible action sequences, using the LLM in three
roles at once — as the policy proposing candidate next actions from a node, as the value function
estimating how promising a partial trajectory is, and as the reflection mechanism (borrowed
directly from §3.4) informing which branches to prune or revisit. Where ReAct commits to one
action per step and Plan-and-Execute commits to one plan up front, LATS explicitly maintains
*multiple* candidate trajectories, expands the most promising ones, backtracks from dead ends, and
only commits to a final answer after the search budget is exhausted.

```
                     root (task)
                    /     |      \
              action A  action B  action C     <- policy proposes, value fn scores
                 |          |
            (expand best) (pruned: low value)
              /    \
        action A1  action A2
           |
      (simulate to leaf, backpropagate reward, reflect if it fails)
```

The cost is an order of magnitude or more above ReAct for a comparable task, because every node
expansion is at least one additional LLM call for proposal and one for evaluation, multiplied
across a branching factor and search depth. The benefit is genuine backtracking: ReAct cannot undo
a bad action three steps ago except by reasoning its way out of the corner it already committed to
in the transcript, while LATS can simply never have committed, because the bad branch was one of
several explored and discarded. LATS fits narrow, high-stakes, small-action-space problems with a
cheap and reliable intermediate-value signal — competitive programming, formal proof search,
constrained planning — and is a poor fit for open-ended agentic tasks with large action spaces or
expensive/slow tool calls, where the multiplicative cost of tree search compounds directly with
the already-high per-action cost.

### 3.6 Choosing among them

| Architecture | LLM calls per unit of work | Recoverability from a bad step | Needs external verifier? | Fits |
|---|---|---|---|---|
| CoT + tools (no loop discipline) | ~1 per tool call, no enforced observation | Poor — no forced grounding | No | Single-tool-call lookups |
| ReAct | 1 per step | Good — next thought sees the observation | No | Most production single-agent tool use |
| Plan-and-Execute | 1 planning + 1 per step (cheaper) | Fair — needs explicit re-plan trigger | No | Knowable decomposition, expensive steps |
| Reflexion | ReAct cost × attempts | Good across attempts, poor within one | Yes | Coding / tool tasks with a cheap checker |
| LATS | Branching factor × depth × 2 (propose+value) | Excellent — true backtracking | Ideally yes | Narrow, high-stakes, small action space |

The interview-ready version of this table is one sentence: **pick the cheapest architecture whose
recoverability matches how expensive a wrong step actually is** — a wrong step that costs a
retried API call needs ReAct; a wrong step that costs an hour of downstream compute needs
Plan-and-Execute with re-planning; a wrong step that costs a shipped bug needs Reflexion or LATS,
and only then if you can afford either's multiplier.

---

## 4. The agent loop

Every architecture in §3 that involves more than one step reduces, at the implementation level, to
the same four-phase loop — **Observe, Think, Act, Observe** — and almost every production incident
involving an agent traces back to one of that loop's four control points being unbounded: no
budget on tokens, no cap on iterations, no detector for repeated action, no explicit termination
contract with the model. This section is the part of the chapter that turns §3's architecture
choice into something that runs safely in production.

### 4.1 The loop, made explicit

```python
from dataclasses import dataclass, field

@dataclass
class LoopState:
    task: str
    history: list[dict] = field(default_factory=list)   # thought/action/observation triples
    tokens_used: int = 0
    iterations: int = 0
    action_hashes: list[str] = field(default_factory=list)  # for stuck-loop detection, §10.5

@dataclass
class LoopBudget:
    max_iterations: int = 12
    max_tokens: int = 40_000
    max_wall_clock_s: float = 60.0

def run_agent_loop(state: LoopState, tools: dict, budget: LoopBudget) -> str:
    start = time.monotonic()
    while True:
        # --- termination checks come FIRST, before spending another call ---
        if state.iterations >= budget.max_iterations:
            return finalize_incomplete(state, reason="max_iterations")
        if state.tokens_used >= budget.max_tokens:
            return finalize_incomplete(state, reason="max_tokens")
        if time.monotonic() - start >= budget.max_wall_clock_s:
            return finalize_incomplete(state, reason="timeout")

        # --- THINK: one bounded call, conditioned on the trimmed history ---
        prompt = build_prompt(state.task, trim_history(state.history, budget))
        response = llm_call(prompt)
        state.tokens_used += response.usage.total_tokens
        state.iterations += 1

        if response.is_final_answer:
            return response.content

        # --- ACT: validate before executing (§7.3, §10.4) ---
        action, args = response.action, response.action_input
        if action not in tools:
            observation = f"Error: '{action}' is not a registered tool."
        else:
            action_hash = hash_action(action, args)
            if is_stuck(state.action_hashes, action_hash):           # §10.5
                return finalize_incomplete(state, reason="stuck_loop")
            state.action_hashes.append(action_hash)
            observation = execute_tool_safely(tools[action], args)   # §7.5, §10

        # --- OBSERVE: fold the result back into history before the next Think ---
        state.history.append({
            "thought": response.thought, "action": action,
            "action_input": args, "observation": observation,
        })
```

The comment placement is the point: **termination checks run before the next model call, not
after.** An agent that checks its budget after calling the model has already spent the tokens it
was trying to cap.

### 4.2 Token budget management

Two budgets exist and are frequently conflated: a **per-iteration** budget (how large may this
one prompt get) and a **cumulative** budget (how many tokens may this whole task spend). The
per-iteration budget is a context-window constraint — reuse `06-context-engineering.md`'s
window-budgeting discipline directly, because an agent's growing history is exactly the
compaction problem that chapter covers, just with tool observations standing in for retrieved
passages. The cumulative budget is a cost-control constraint, and it belongs in the same place
`11-token-accounting-and-cost.md` (planned) puts every other cost control: attributed per task, per
tenant, and per agent role, enforced with a hard stop, not a dashboard alert that fires after the
money is spent.

```python
def trim_history(history: list[dict], budget: LoopBudget) -> list[dict]:
    """Keep the most recent steps in full; summarize the rest.
    This is the same shape as 06's compaction — recency matters, and an
    LLM-generated summary of stale steps costs far fewer tokens than the
    raw observations while preserving the decisions that still matter."""
    RECENT_KEPT_IN_FULL = 4
    if len(history) <= RECENT_KEPT_IN_FULL:
        return history
    stale, recent = history[:-RECENT_KEPT_IN_FULL], history[-RECENT_KEPT_IN_FULL:]
    summary = summarize_steps(stale)   # one cheap call, or a rule-based digest
    return [{"summary_of_earlier_steps": summary}, *recent]
```

Cheap-model-for-intermediate-steps is the other lever worth naming explicitly: the "Think" step
inside a long ReAct loop rarely needs the largest available model — routing, tool selection, and
simple observation summarization are frequently well within a small model's competence, and
reserving the frontier model for the final synthesis call, or for steps flagged as high-uncertainty,
is a 2-5x cost reduction with negligible quality loss on well-scoped tool-use tasks. This is model
routing (`12-serving-latency-and-caching.md`, planned) applied inside a single agent's own loop.

### 4.3 Termination conditions, enumerated

An agent loop should have every one of the following wired, not just the ones that occurred to
whoever wrote it first:

1. **Explicit success** — the model emits a structured "final answer" signal (not free text that
   happens to look final; a distinguishable action type, so the runtime does not have to guess).
2. **Max iterations** — a hard cap, sized to the task's expected step count with headroom, not to
   "whatever number felt safe."
3. **Token budget exhausted** — cumulative, checked before every model call.
4. **Wall-clock timeout** — independent of iteration count, because a single slow tool call can
   blow a latency SLO without ever tripping the iteration cap.
5. **Stuck-loop detection** (§10.5) — the same action with the same arguments (or a near-duplicate
   thought) repeating beyond a small threshold is not progress, it is a loop, and it should
   terminate with an escalation, not run out the iteration budget doing nothing.
6. **Explicit human interrupt** — an external signal (approval-gate rejection, §9, or an operator
   kill switch) that the loop checks on every iteration, not only at start.
7. **Irrecoverable tool failure** — a circuit breaker (§10.3) tripping on a dependency the task
   cannot proceed without should terminate the loop with a clear failure reason, not retry into a
   dead dependency until the iteration cap absorbs the cost.

The single most common production bug in agent loops is implementing only condition 1 and
assuming the others are edge cases. They are not edge cases; for a sufficiently large volume of
tasks running against a model with any non-zero probability of never emitting a clean "final
answer," they are the majority of your cost tail.

---

## 5. Multi-agent patterns

Multi-agent systems exist to manage complexity by decomposition, the same reason microservices
exist, and they inherit the same central lesson: decomposition only pays for itself when the
pieces have genuinely separable concerns and a well-defined interface between them. A multi-agent
system built because "one agent felt too complicated" without that separation is a monolith with
extra network hops — or, since these hops are LLM calls, extra cost and extra opportunities to lose
information (§6.3) for no architectural benefit.

### 5.1 Supervisor — one agent delegates to specialists

The supervisor pattern has a single orchestrating agent that receives the task, decides which
specialist agent (or tool) should handle each sub-piece, dispatches, collects results, and
synthesizes a final answer. It is the multi-agent pattern with the best cost-control and
observability properties, because the *coordination logic* is centralized and can be code, or a
tightly-scoped model call, rather than distributed across every agent's own judgment.

```python
class Supervisor:
    def __init__(self, specialists: dict[str, "Specialist"]):
        self.specialists = specialists    # {"research": ResearchAgent(), "code": CodeAgent(), ...}

    def run(self, task: str) -> str:
        route = llm_route(task, options=list(self.specialists))   # one bounded decision
        specialist = self.specialists[route.chosen]
        result = specialist.run(route.subtask, context=route.context)
        if result.needs_followup:                                  # supervisor can re-route
            followup = llm_route(result.followup_query, options=list(self.specialists))
            result2 = self.specialists[followup.chosen].run(followup.subtask)
            return llm_synthesize(task, [result, result2])
        return result.content
```

The supervisor's own decision — which specialist, how many rounds — is the *only* genuinely
agentic part of this pattern; each specialist can itself be anything from a pure function to a
full ReAct loop, and the supervisor does not need to know which. That encapsulation is the pattern's
real value: specialists are swappable and independently testable, exactly like well-isolated
microservices, because the supervisor only depends on their interface (subtask in, result out),
never their internals.

### 5.2 Hierarchical — a tree of supervisors

Hierarchical multi-agent systems nest the supervisor pattern: a top-level supervisor delegates to
mid-level supervisors, each of which delegates to leaf specialists, mirroring an org chart. This
buys the same benefit microservice-of-microservices architectures buy at the infrastructure layer —
each level's complexity is bounded by its own fan-out, not by the total system size — at the same
cost: latency accumulates additively down the tree (each level's dispatch is a sequential
round-trip unless levels are pipelined), and a failure at a leaf has to propagate back up through
every intermediate supervisor's own judgment about what the failure means, which is an additional
place for information to be lost or misrepresented (§6.3).

Use hierarchy when a single supervisor's specialist list would itself become large enough to need
routing logic — the same threshold `04` §6's candidate-budget reasoning uses for adding retrieval
branches: add structure when the flat version's own selection step becomes the bottleneck, not
before.

### 5.3 Peer-to-peer — agents communicate directly

Peer-to-peer patterns let agents message each other without a central router: agent A can ask
agent B a question, B can ask C, and control can flow in any direction the agents themselves
negotiate. This is the pattern to be most skeptical of in an interview answer, and the reason is
exactly `../distributed-systems/README.md`'s subject matter: **coordination without a central
authority is a hard distributed-systems problem even when every node is deterministic code**, and
here every node is a non-deterministic model whose next message depends on a probability
distribution, not a protocol. There is no equivalent of a consensus algorithm for "did these two
agents actually agree, and on what" — the closest thing available is re-reading the transcript and
hoping the natural-language agreement was unambiguous. Peer-to-peer is defensible for small,
bounded exchanges (two agents negotiating a single handoff) and a liability at any scale where
"which agent is in charge of ending this conversation" is itself unclear, because in that failure
mode you get exactly what an un-terminated ReAct loop gives you (§4.3) multiplied by the number of
agents talking past each other.

### 5.4 Debate — agents argue toward consensus

Debate patterns run two or more agents on the same question, optionally taking adversarial
positions, and have a judge (a separate model call, a rubric, or a fixed number of rounds followed
by majority vote) decide the outcome. It is the multi-agent analogue of Reflexion's self-critique,
externalized to a second party instead of a second attempt by the same one, and it buys the same
kind of benefit self-consistency voting (§2.2's parallelization pattern) buys: independent
"opinions" are more likely to catch an error than one opinion re-read by the same source. The cost
is proportional to the number of debaters and rounds — 2-3x a single call's cost is typical for a
two-agent, one-rebuttal debate — and the benefit is concentrated in exactly the cases where a
single model's confident wrong answer would otherwise go unchecked: high-stakes classification,
adversarial content review, contested factual claims. Debate is not a general-purpose quality
multiplier; running it on a task with an unambiguous right answer buys nothing over asking once,
and running it on a task with no objectively-checkable answer risks two confidently wrong models
converging on an agreement that is wrong in the same way, which a naive "did they agree" judge will
score as success.

### 5.5 Assembly line — sequential specialization

The assembly-line pattern runs a fixed sequence of specialist agents, each transforming the
previous one's output, with no branching and no re-routing: draft agent produces a first pass,
critique agent flags issues, revision agent addresses them, formatting agent finalizes. Despite
being marketed as "multi-agent," this is structurally §2.2's prompt-chaining pattern with each link
implemented as an agent instead of a bare LLM call — the control flow is entirely fixed by code,
and the only thing distributed across "agents" rather than one long prompt is context isolation
(each stage's prompt only contains what it needs, not the whole history) and role specialization
(each stage's system prompt is tuned for its one job, not a generalist prompt trying to do
everything). This is, empirically, the shape of the large majority of "multi-agent systems" that
actually run reliably in production, and recognizing it as a workflow rather than an agent system
is directly useful in an interview: it lets you claim the reliability properties of §2 for a system
that superficially looks like the harder case in §5.3.

```python
def assembly_line(brief: str, agents: list["Agent"]) -> str:
    artifact = brief
    for agent in agents:                      # ["drafter", "critic", "reviser", "formatter"]
        artifact = agent.run(artifact)         # fixed sequence, fixed count, no re-routing
    return artifact
```

### 5.6 Choosing among the five

| Pattern | Coordination authority | Latency shape | Failure isolation | Fits |
|---|---|---|---|---|
| Supervisor | Centralized (one router) | One dispatch round-trip (+ retries) | Good — supervisor sees every result | Task with a known, moderate set of specialties |
| Hierarchical | Centralized per level | Additive down the tree | Good per level, weaker in aggregate | Specialist set too large for one flat router |
| Peer-to-peer | Distributed / negotiated | Unbounded without an explicit protocol | Poor — no one node has the full picture | Small, bounded two-party handoffs only |
| Debate | External judge | 2-3x a single call, in parallel | Good for catchable errors, poor for shared blind spots | High-stakes, checkable-by-disagreement claims |
| Assembly line | Fully centralized (it's a workflow) | Linear in stage count | Excellent — each stage is independently testable | Sequential specialization, the common case |

---

## 6. Agent communication and the blackboard pattern

Every multi-agent pattern in §5 has to answer the same underlying question regardless of its
topology: **how does information produced by one agent become available to another, without being
silently degraded on the way?** The answer splits into two coordination substrates — message
passing and shared state — and one classical pattern, the blackboard, that is really a specific
discipline for using shared state well.

### 6.1 Message passing

In a message-passing design, agents exchange discrete, addressed messages — sender, recipient,
type, payload — and hold no state in common beyond what has been explicitly sent. This maps
directly onto the supervisor and peer-to-peer patterns: a supervisor's dispatch to a specialist and
the specialist's result are both messages.

```python
@dataclass
class AgentMessage:
    sender: str
    recipient: str
    msg_type: str          # "task", "result", "clarification_request", "error"
    payload: dict
    trace_id: str           # ties this message into §12's observability trace
    in_reply_to: str | None = None
```

The advantage is isolation: an agent's internal state cannot leak into another agent's context
except through what was deliberately sent, which makes each agent's behavior a function of its
message history alone — easy to replay, easy to test with mocked incoming messages. The
disadvantage is that anything not explicitly included in a message is invisible to the recipient,
which is exactly the failure mode §6.3 exists to prevent.

### 6.2 Shared state

In a shared-state design, agents read and write fields of a common state object rather than
sending each other messages — the dominant model in graph-based frameworks (§11), where every node
in the graph receives and returns (a delta to) the same typed state. The advantage is that nothing
has to be re-transmitted: an agent added late to the pipeline simply reads whatever prior agents
already wrote, with no need to know who wrote it or to have received it directly. The disadvantage
is the same one `../python-mastery/30-concurrency-correctness.md` describes for any shared mutable
structure: without a clear ownership discipline (which agent may write which field, and when),
shared state accumulates the equivalent of a race condition — two agents writing conflicting
conclusions to the same field, or one agent reading a field before the agent responsible for
populating it has run.

### 6.3 The blackboard pattern

The blackboard pattern, inherited from classical AI systems like Hearsay-II, is shared state used
with a specific discipline that resolves §6.2's ownership problem: a shared workspace (the
"blackboard") holds the evolving partial solution, a set of specialist agents ("knowledge sources")
each watch the blackboard for a pattern they know how to advance, and a separate **controller**
decides, at each step, which specialist gets to act next based on the blackboard's current state —
no specialist decides for itself when to act, and no specialist writes to the blackboard except
through the controller's turn.

```python
class Blackboard:
    def __init__(self):
        self.state: dict = {}          # the evolving partial solution, one shared object
        self.log: list[dict] = []       # append-only audit trail, never overwritten

    def post(self, agent: str, updates: dict):
        self.state.update(updates)
        self.log.append({"agent": agent, "updates": updates, "ts": time.time()})

class Controller:
    def __init__(self, blackboard: Blackboard, specialists: dict[str, "Specialist"]):
        self.bb, self.specialists = blackboard, specialists

    def run(self, max_rounds: int = 10) -> dict:
        for _ in range(max_rounds):
            candidates = [s for s in self.specialists.values() if s.can_contribute(self.bb.state)]
            if not candidates:
                break                                    # nothing left to add: converged
            best = max(candidates, key=lambda s: s.confidence(self.bb.state))
            updates = best.contribute(self.bb.state)
            self.bb.post(best.name, updates)
        return self.bb.state
```

The blackboard pattern's real contribution, relative to an unstructured shared-state design, is
that it makes the "who may write what, when" question a first-class object (the controller's
selection logic) instead of an implicit convention every agent has to independently respect. It is
the right choice when the number of specialists is large or grows over time and a fixed message-
passing topology between all of them would be combinatorial, and when a full audit trail of who
contributed what is a requirement, not a nicety — which for anything customer-facing or regulated,
it usually is.

### 6.4 Preventing information loss between agents

The single most common multi-agent bug, across every topology in §5, is a handoff that
**summarizes away the detail the next agent actually needed.** It happens because summarization
feels like good hygiene — shorter prompts, lower cost — and because the agent doing the summarizing
has no way to know in advance which details the *next* agent will turn out to need. Three concrete
practices prevent it:

1. **Pass structured artifacts, not prose summaries, wherever the data has structure.** A research
   agent handing off "found three relevant papers" loses the papers; handing off a list of
   `{title, authors, doi, key_finding}` objects loses nothing a downstream agent needs and costs
   barely more tokens.
2. **Pass references, not restatements, for anything retrievable.** If agent A retrieved a document
   and agent B needs it, hand B the document ID and let B fetch it (or receive the same shared
   context) rather than having A paraphrase it — a paraphrase is a lossy compression with no way to
   recover the original, and it compounds: paraphrase of a paraphrase is a game of telephone with an
   LLM playing every position.
3. **Keep one authoritative trace, not N independent summaries of it.** In the blackboard pattern,
   this is the log; in a supervisor pattern, this is the full set of specialist results attached to
   the final synthesis call, not a supervisor's own mental digest of them. The synthesis step should
   see the original outputs, because it is the step with the most context to judge what matters, and
   every earlier compression removes information the synthesis step cannot get back.

---

## 7. Tool orchestration

Tools are how an agent's decisions become effects in the world, which makes tool orchestration the
layer where §1.1's security-surface and testability costs are actually incurred or actually
contained. Everything in this section exists to make the boundary between "the model decided" and
"the effect happened" a place code can inspect, validate, and refuse.

### 7.1 Tool registries

A tool registry is the single source of truth for what tools exist, their schemas, and their
metadata — never a hand-maintained list duplicated into every agent's system prompt. At minimum it
carries a versioned JSON-schema-validated input/output contract, an authorization scope (§7.3), and
an idempotency classification (§7.6).

```python
@dataclass
class ToolSpec:
    name: str
    version: str
    input_schema: dict          # JSON Schema — validated before every call
    output_schema: dict         # JSON Schema — validated after every call
    scopes_required: set[str]   # §7.3
    idempotent: bool            # §7.6
    timeout_s: float
    fn: Callable

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        self._tools[spec.name] = spec

    def get(self, name: str, caller_scopes: set[str]) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownToolError(name)
        if not spec.scopes_required <= caller_scopes:
            raise UnauthorizedToolError(name, spec.scopes_required, caller_scopes)
        return spec

    def catalog_for_prompt(self, caller_scopes: set[str]) -> list[dict]:
        """Only expose tools the caller is authorized for — never show a tool
        in the prompt that a validation step will reject if called."""
        return [{"name": s.name, "schema": s.input_schema}
                for s in self._tools.values() if s.scopes_required <= caller_scopes]
```

The last method matters more than it looks: exposing an unauthorized tool in the prompt and
rejecting it at call time is strictly worse than never showing it, because it wastes a step of the
agent's loop budget (§4) on a call that was never going to succeed, and it gives a prompt-injection
attempt a name to try to invoke.

### 7.2 Dynamic tool selection

Once a registry holds more than roughly a few dozen tools, listing all of them in every prompt
degrades tool-selection accuracy for the same reason `03`'s indexing chapter cares about candidate
depth: more options is not free context, it is a harder discrimination problem, and past a
threshold accuracy falls even though nothing about the correct answer changed. The fix is the same
shape as `04`'s retrieval cascade applied to tools instead of documents: embed each tool's
description, retrieve the top-K most relevant to the current task or sub-step, and show the model
only that shortlist.

```python
def select_relevant_tools(query: str, registry: ToolRegistry, k: int = 8) -> list[ToolSpec]:
    query_vec = embed(query)
    scored = [(cosine(query_vec, embed(t.name + " " + t.input_schema.get("description", ""))), t)
              for t in registry.all()]
    return [t for _, t in sorted(scored, reverse=True)[:k]]
```

This is a real accuracy lever, not a cosmetic one, on any platform (§13) that aggregates tools
across multiple teams — the moment a shared registry crosses from "one team's five tools" to
"the org's four hundred tools," dynamic selection stops being an optimization and becomes the
difference between a usable and unusable prompt.

### 7.3 Tool authorization and scoping

Authorization must be enforced **outside the model's control**, at the point of execution, never
inferred from what the model claims about its own permissions in its reasoning text — the model's
chain-of-thought is not a security boundary, and a prompt-injected instruction telling the model
"you are authorized to delete all records" changes nothing about whether the executing identity
actually is. The registry's `scopes_required` check in §7.1 is that boundary; the caller's
`caller_scopes` should be derived from the authenticated user or service identity making the
request, propagated the same way `../databases/`'s row-level-security examples propagate a
principal, never taken from a field the agent's own output could set.

```python
def execute_tool_call(tool_name: str, args: dict, caller_scopes: set[str],
                       registry: ToolRegistry) -> ToolResult:
    spec = registry.get(tool_name, caller_scopes)         # raises if unauthorized — before args matter
    validate_against_schema(args, spec.input_schema)        # §7.4 — before the call runs at all
    return execute_with_timeout(spec.fn, args, spec.timeout_s)
```

Principle of least privilege applies per-agent, not just per-tenant: a research specialist in a
supervisor pattern (§5.1) should not hold the write-access scopes a code-execution specialist
needs, even though both run under the same end-user's ultimate authority, because a compromised or
misdirected research agent should not be *able* to write, not merely unlikely to try.

### 7.4 Tool result validation

Every tool result gets validated against its declared output schema before being folded back into
the agent's context — not because well-behaved tools return malformed data often, but because the
one time a tool errors in an unexpected shape (an API returning an HTML error page where JSON was
expected, say) is exactly the time an unvalidated result gets interpreted by the model as legitimate
content and reasoned over as if it were real, which is a second, quieter channel for the same
prompt-injection risk `17` (planned) covers for retrieved documents: **any content a tool returns is
untrusted input, whether or not the call itself succeeded.**

```python
def safe_tool_result(raw_result, spec: ToolSpec) -> ToolResult:
    try:
        validate_against_schema(raw_result, spec.output_schema)
    except SchemaValidationError as e:
        return ToolResult(ok=False, observation=f"Tool '{spec.name}' returned malformed output: {e}")
    return ToolResult(ok=True, observation=raw_result)
```

Returning a structured error observation, rather than raising and killing the loop, gives the model
a chance to self-correct on its next Think step — retry with different arguments, try a different
tool, or ask for help (§9) — which is strictly better than either silently passing through bad data
or crashing the whole task over one malformed response.

### 7.5 Parallel tool execution

Independent tool calls — ones whose inputs do not depend on each other's outputs — should execute
concurrently, using exactly the bounded-concurrency-with-cancellation discipline in
`../python-mastery/29-async-patterns-and-pitfalls.md`: a semaphore-bounded gather, a timeout on
each individual call, and a policy for what happens to the whole batch when one call fails
(fail-fast vs. best-effort partial results, chosen per use case, not defaulted silently).

```python
async def execute_parallel(calls: list[tuple[ToolSpec, dict]], max_concurrency: int = 5,
                            timeout_s: float = 10.0) -> list[ToolResult]:
    sem = asyncio.Semaphore(max_concurrency)
    async def bounded_call(spec, args):
        async with sem:
            try:
                return await asyncio.wait_for(spec.fn(**args), timeout=timeout_s)
            except asyncio.TimeoutError:
                return ToolResult(ok=False, observation=f"'{spec.name}' timed out after {timeout_s}s")
    return await asyncio.gather(*(bounded_call(s, a) for s, a in calls))
```

The latency payoff is direct: an agent step that needs three independent lookups pays one timeout
budget's worth of wall-clock time instead of three, which for a loop with §4's per-iteration budget
is frequently the difference between finishing inside an SLO and not.

### 7.6 Idempotent tools for retry safety

Retries (§10.1) are only safe if the retried operation is idempotent — calling it twice with the
same arguments must produce the same end state as calling it once, not a duplicated effect. Read
operations are idempotent by default; mutating operations (charge a card, send an email, create a
record) are not, unless deliberately made so, and an agent loop that retries a non-idempotent tool
call after an ambiguous failure (timeout with unknown server-side outcome, for instance) can and
will duplicate the effect.

```python
def idempotent_charge(customer_id: str, amount: int, idempotency_key: str) -> ChargeResult:
    """The caller supplies a stable key derived from the *task*, not the *attempt* —
    same task retried twice sends the same key, and the downstream system (or a local
    dedup table) returns the original result instead of charging twice."""
    existing = lookup_by_idempotency_key(idempotency_key)
    if existing:
        return existing
    result = payment_api.charge(customer_id, amount, idempotency_key=idempotency_key)
    record_idempotency_key(idempotency_key, result)
    return result
```

The registry's `idempotent` flag (§7.1) should gate the retry policy directly: non-idempotent tools
either need a caller-supplied idempotency key threaded through by the orchestrator (not invented by
the model, which cannot be trusted to generate a stable key across retries) or must be excluded
from automatic retry entirely, surfacing the ambiguous failure to §9's human-in-the-loop path
instead of guessing.

---

## 8. State management for agents

An agent's state is not one thing, and treating it as one thing — a single growing transcript
passed to every call — is the most common source of the context bloat, information loss, and
un-recoverable crashes this section exists to prevent. Separate it into three kinds, because each
has a different lifetime, a different consistency requirement, and a different storage answer.

### 8.1 Three kinds of state

**Conversation state** is the dialogue history between the user (or calling system) and the agent —
what was asked, what was answered, across turns. It is append-only, naturally chronological, and is
the state kind most existing chat-application infrastructure already knows how to persist.

**Task state** is the agent's own working memory about the task it is executing: the plan (§3.3),
which steps are done, intermediate results, the loop's iteration and token counters (§4.1). It is
mutable, structured, and specific to one task execution — it should not survive past that task's
completion except as an audit record, and it should not be conflated with conversation state, which
can span many tasks.

**World state** is the agent's model of external systems it is acting on or observing — the current
contents of a database it is querying, the state of a deployment it is managing — and it is the
kind most prone to silent staleness: an agent's belief about world state is a *cached* snapshot,
current at the observation that produced it and potentially wrong by the time the agent acts on it,
which is why every mutating tool call should re-verify preconditions at execution time rather than
trusting the agent's last observation of them.

```python
@dataclass
class ConversationState:
    session_id: str
    turns: list[dict]                    # append-only

@dataclass
class TaskState:
    task_id: str
    plan: list[str]
    completed_steps: list[dict]
    iteration: int
    tokens_used: int
    status: Literal["running", "done", "failed", "awaiting_approval"]   # §9

@dataclass
class WorldStateSnapshot:
    observed_at: float                    # staleness is measurable, not assumed away
    source: str
    data: dict
```

### 8.2 Short-term versus long-term memory

Short-term memory is what fits in, or is deliberately compacted into, the current context window —
the live task state and recent conversation turns, exactly the material §4.2's trimming logic
manages. Long-term memory is everything persisted *outside* the context window and retrieved back
in when relevant — episodic memory of past task attempts (Reflexion's reflection buffer, §3.4, is a
narrow instance of this), semantic memory of learned facts, and user or organizational preferences
that should survive across sessions. Long-term memory retrieval is, mechanically, `01`–`04`'s RAG
pipeline pointed at a different corpus: embed the query, retrieve the top-K relevant memories,
inject them into the prompt — which means every lesson those chapters teach about recall ceilings,
staleness, and reranking applies unchanged to "agent remembers things" as a feature, and a team that
builds a bespoke memory system without reusing that machinery is re-deriving it, usually worse.

### 8.3 Persistence for crash recovery

A production agent loop must be able to resume after a crash without restarting the task from
scratch, which means task state has to be checkpointed at each step boundary, not held only in
process memory. The pattern — a durable log of state transitions plus periodic full snapshots — is
the same one `../databases/14-write-ahead-log-internals.md` describes for a storage engine, applied
to an agent's own execution instead of a database's pages: after a crash, replay the log from the
last snapshot to reconstruct exactly where the loop left off, then resume the loop rather than
re-run completed steps (which for non-idempotent steps, §7.6, would be actively wrong, not merely
wasteful).

```python
class TaskCheckpointer:
    def __init__(self, store: "DurableKVStore"):
        self.store = store

    def save(self, state: TaskState):
        self.store.put(f"task:{state.task_id}", asdict(state))

    def load(self, task_id: str) -> TaskState | None:
        raw = self.store.get(f"task:{task_id}")
        return TaskState(**raw) if raw else None

def resumable_loop(task_id: str, checkpointer: TaskCheckpointer, tools, budget) -> str:
    state = checkpointer.load(task_id) or TaskState(task_id=task_id, plan=[], completed_steps=[],
                                                      iteration=0, tokens_used=0, status="running")
    while state.status == "running":
        step_result = run_one_step(state, tools, budget)
        state.completed_steps.append(step_result)
        state.iteration += 1
        checkpointer.save(state)          # after every step, not only at the end
        if step_result.is_final:
            state.status = "done"
    return state.completed_steps[-1].content
```

### 8.4 State schema design

The design rule that prevents state from becoming an unstructured dumping ground: every field
should be typed, versioned (so a schema change does not silently break replay of an
in-flight or historical task), and minimal — task state should hold what the loop itself needs to
resume correctly, not a copy of everything any agent has ever touched. A `TaskState.plan` field
holding a list of typed subtask objects is recoverable and diffable; a `TaskState.notes: str` field
that different code paths append arbitrary text to becomes, within a few months, exactly the kind
of undifferentiated blob that makes debugging a stuck task (§10.5) require re-reading a wall of text
instead of inspecting a structure.

---

## 9. Human-in-the-loop patterns

The honest framing for this section: a human-in-the-loop gate is not a concession that the agent
isn't good enough yet. It is a permanent architectural feature for any action whose cost of being
wrong exceeds the cost of a delay to check, and that set of actions does not shrink to zero as
models improve — it shrinks in scope but the highest-consequence actions in almost any domain stay
gated indefinitely, the same way code review does not disappear as engineers get more senior.

### 9.1 Approval gates

An approval gate suspends the agent loop before executing a flagged action, persists the pending
action as durable state (§8.3 — the task must survive the wait, which can be arbitrarily long), and
resumes only on an explicit approve or reject signal.

```python
@dataclass
class PendingApproval:
    task_id: str
    proposed_action: str
    proposed_args: dict
    risk_reason: str
    requested_at: float

def maybe_gate(action: str, args: dict, state: TaskState, policy: "ApprovalPolicy",
                checkpointer: TaskCheckpointer) -> ToolResult | None:
    if not policy.requires_approval(action, args):
        return None                                    # not gated, proceed normally
    state.status = "awaiting_approval"
    pending = PendingApproval(state.task_id, action, args,
                               risk_reason=policy.reason(action, args), requested_at=time.time())
    save_pending_approval(pending)
    checkpointer.save(state)
    return ToolResult(ok=False, observation="Action requires human approval; task suspended.")

def resume_after_approval(task_id: str, approved: bool, checkpointer: TaskCheckpointer, tools):
    state = checkpointer.load(task_id)
    pending = load_pending_approval(task_id)
    if not approved:
        state.completed_steps.append({"action": pending.proposed_action, "result": "rejected by reviewer"})
        state.status = "running"                        # let the agent's next Think react to the rejection
    else:
        result = execute_tool_call(pending.proposed_action, pending.proposed_args, ...)
        state.completed_steps.append({"action": pending.proposed_action, "result": result})
        state.status = "running"
    checkpointer.save(state)
    return continue_loop(state, tools)
```

The policy that decides `requires_approval` should be declarative and centrally owned (§13), not
scattered per-agent — irreversible actions (deletion, financial transactions above a threshold,
external communication on the organization's behalf) are the canonical gated set, and the gate list
is exactly the kind of guardrail that belongs in policy configuration, reviewed like any other
production change, not buried in one team's system prompt where it is invisible to everyone else
and to any centralized audit.

### 9.2 Escalation and confidence-based routing

Escalation differs from an approval gate in trigger: a gate fires on an *action type* known in
advance to be risky; escalation fires on a *runtime signal* that this particular attempt is going
poorly — repeated tool failures, a stuck-loop detection (§10.5), or an explicit low-confidence
signal from the model itself. Confidence-based routing generalizes this into a three-way policy
rather than a binary gate: high-confidence, low-risk actions execute automatically; medium
confidence or moderate risk routes to an approval gate; low confidence or high risk routes directly
to full human takeover, bypassing the agent entirely for that step.

```python
def route_by_confidence(action: str, args: dict, confidence: float, risk: str) -> str:
    if risk == "high" or confidence < 0.4:
        return "human_takeover"
    if risk == "medium" or confidence < 0.75:
        return "approval_gate"
    return "auto_execute"
```

Getting a reliable confidence signal out of an LLM is itself an unsolved problem worth naming
honestly in an interview — token-level log-probabilities correlate with correctness only loosely,
and asking the model to self-report a confidence score is asking it to perform a second, equally
uncalibrated generation task. The practical answer most production systems use is a proxy for
confidence rather than the model's own estimate: has this exact situation been seen before with a
known-good outcome, did the plan require re-planning (§3.3), did a verifier check pass (§3.4's
Reflexion machinery) — structural signals that correlate with actual reliability better than a
self-reported number does.

### 9.3 Feedback loops

A rejection or correction at an approval gate is a training signal, and the mistake to avoid is
letting it evaporate after unblocking the one task it applied to. At minimum, log every gate
decision with enough context to build a dataset — the proposed action, the risk reason, the human's
decision, and why (if captured) — because that dataset is simultaneously next quarter's fine-tuning
or few-shot-example candidate pool and this quarter's evidence for whether the approval policy
itself (§9.1) is calibrated correctly: a gate that is rejected 95% of the time is not catching edge
cases, it is catching a systematically wrong default the agent should not be proposing at all, and
that is a §2 control-flow fix, not a permanent human tax.

---

## 10. Failure handling

Failure in an agentic system has more distinct shapes than failure in a plain RPC call, because an
agent can fail by doing the wrong thing while returning success, not only by erroring — and the
handling strategy has to cover both.

### 10.1 Retry strategies

Retry only operations that are (a) transient-failure-prone and (b) idempotent (§7.6) or made safe
via an idempotency key, with exponential backoff and jitter to avoid synchronized retry storms
against an already-struggling dependency, and a hard cap that hands off to §10.2's fallback or
§9.2's escalation rather than retrying indefinitely.

```python
async def retry_with_backoff(fn, max_attempts=3, base_delay=0.5):
    for attempt in range(max_attempts):
        try:
            return await fn()
        except TransientError:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
```

### 10.2 Fallback chains

A fallback chain provides a strictly degraded but available alternative when the primary path
fails: a smaller or cheaper model when the primary model errors or is rate-limited, a cached or
rule-based answer when a tool is unavailable, a template response when the agent's synthesis step
itself fails. The design discipline is to make every fallback's degraded nature visible downstream
(mark the response, log the fallback event) rather than silently returning a worse answer indexed
as if it were a normal one — a silent fallback corrupts both user trust and, more insidiously,
§14's evaluation data, because a success metric computed over a stream that includes unmarked
fallback answers overstates the primary system's real quality.

### 10.3 Circuit breakers for tool calls

A tool that is failing should stop being called, not be retried by every concurrent task until it
recovers on its own — the standard circuit-breaker state machine (closed → open on a failure-rate
threshold → half-open probe → closed again on a successful probe) applies to agent tool calls
exactly as it does to any downstream service dependency.

```python
class ToolCircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout_s=30.0):
        self.failures, self.state = 0, "closed"
        self.failure_threshold, self.reset_timeout_s = failure_threshold, reset_timeout_s
        self.opened_at = None

    def call(self, fn, *args, **kwargs):
        if self.state == "open":
            if time.monotonic() - self.opened_at < self.reset_timeout_s:
                raise CircuitOpenError("tool unavailable, breaker open")
            self.state = "half_open"                        # allow one probe through
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state, self.opened_at = "open", time.monotonic()
            raise
        else:
            self.failures, self.state = 0, "closed"          # success resets fully
            return result
```

A tripped breaker should be a §4.3 termination condition for any task whose plan depends on that
tool, surfaced as a clear "dependency unavailable" failure rather than absorbed silently into the
iteration budget while the agent keeps trying variations of a call that cannot succeed.

### 10.4 What to do when the model hallucinates a tool call

A model emitting a call to a tool that does not exist, or to a real tool with arguments that
violate its schema, is not a rare edge case at scale — it is a routine occurrence any registry-based
system (§7.1, §7.4) is already positioned to catch, and the correct response is to **return a
structured error as the observation and let the next Think step self-correct**, exactly as §7.4
does for malformed tool *results*.

```python
def handle_hallucinated_call(action: str, args: dict, registry: ToolRegistry) -> str:
    if action not in registry.names():
        close = difflib.get_close_matches(action, registry.names(), n=1)
        hint = f" Did you mean '{close[0]}'?" if close else ""
        return f"Error: '{action}' is not a registered tool.{hint} Available: {registry.names()}"
    try:
        validate_against_schema(args, registry.get_schema(action))
    except SchemaValidationError as e:
        return f"Error: arguments for '{action}' are invalid: {e}. Expected schema: {registry.get_schema(action)}"
```

The fuzzy-match hint is a small addition with an outsized effect on recovery rate: most hallucinated
tool names are near-misses of a real tool (wrong casing, a plausible-but-wrong synonym), and giving
the model that hint in the observation resolves the majority of these on the very next step without
burning an escalation or a wasted retry.

### 10.5 Stuck-loop detection

A loop that keeps calling the same tool with the same arguments, or cycling through a small set of
actions without making progress, will not terminate on its own — nothing about generating the next
token makes a model notice it is repeating itself unless the runtime tells it so.

```python
def is_stuck(action_hashes: list[str], new_hash: str, window: int = 4, repeat_threshold: int = 2) -> bool:
    recent = action_hashes[-window:]
    return recent.count(new_hash) >= repeat_threshold

def hash_action(action: str, args: dict) -> str:
    return hashlib.sha256(f"{action}:{json.dumps(args, sort_keys=True)}".encode()).hexdigest()
```

A near-duplicate detector on the *thought* text (not just the action) catches the subtler variant
where the model rephrases its reasoning each time but keeps arriving at the same dead-end action —
a simple embedding-similarity threshold between consecutive thoughts, reusing `01`'s embedding
machinery, is usually sufficient. On detection, the correct response is §4.3's termination with an
escalation reason attached, not a silent retry — a human reviewing "the agent looped between two
actions eleven times" learns something actionable about a missing tool capability or a
misunderstood task; a system that just raises the iteration cap on the next run learns nothing and
pays for the same loop again, longer.

---

## 11. Orchestration frameworks compared

Every framework in this section is solving the same core problem — give control flow that includes
model decisions a structure that is inspectable, resumable, and composable — and they differ mainly
in which point on §1's spectrum they were designed around and how much of §4 through §10's
machinery they give you for free versus leave for you to build.

**LangGraph** models an agent (or a multi-agent system) explicitly as a graph: typed state,
nodes that read and write it, edges that route between nodes conditionally, and a built-in
checkpointer that persists the graph's state after every node execution — which gives §8.3's crash
recovery and §9.1's suspend-and-resume approval gates essentially as first-class primitives rather
than something a team has to hand-build. Its cost is a steeper conceptual model — you are
explicitly designing a state machine, not writing "an agent" in a sentence — which is precisely the
tradeoff worth making once a system's control flow needs the auditability §2 argues for: the graph
*is* the workflow diagram, in code, and it is inspectable independent of any run.

**CrewAI** organizes around roles: define a "crew" of agents each with a role, a goal, and a
backstory, and a process (sequential or hierarchical) that governs how they hand off work — a
direct, higher-level packaging of §5.1's supervisor and §5.2's hierarchical patterns. It is the
fastest framework on this list to get a first multi-agent prototype running, because the role
abstraction maps cleanly onto how people already describe a task ("a researcher, a writer, an
editor"), and it trades away low-level control over the loop internals (§4) that a production system
eventually wants — retry policy, token budget enforcement, and tool authorization are less exposed
as first-class configuration than in a graph-based framework, which makes CrewAI a strong choice for
prototyping the *shape* of a multi-agent system and a weaker default for a team that already knows
it needs §7 and §10's guarantees in production.

**AutoGen** centers on conversable agents exchanging messages in a group chat, which maps most
directly onto §5.3's peer-to-peer pattern and §5.4's debate pattern — it is the framework most
naturally suited to the patterns this chapter is most cautious about, and that caution is not a
knock against the framework so much as a reason to be deliberate about which of its patterns you
actually reach for. It is strong for research and experimentation with agent-to-agent dynamics and
has historically put less emphasis than LangGraph on durable checkpointing and fine-grained
authorization, which matters if the production target needs §8.3 and §7.3's guarantees natively
rather than layered on separately.

**Semantic Kernel** is Microsoft's SDK, built around a planner that decomposes a goal into a
sequence of calls against registered "plugins" (its name for tools), with first-class integration
into the Azure ecosystem and strong support for combining native code functions and LLM-based
"semantic" functions in the same plan — architecturally closer to §3.3's Plan-and-Execute than to
ReAct's step-by-step interleaving, and the natural choice when an organization's existing platform
investment is already Microsoft-centric (Azure AI, .NET) and the plugin ecosystem it needs to
integrate with is already built to that interface.

**A custom implementation** — a typed state machine you own outright, built from the primitives in
§4 through §10 — is the right choice, not a fallback, once a system's requirements include
guarantees a general-purpose framework does not give for free: multi-tenant isolation with
per-tenant credential scoping (§13), an internal audit and compliance format the framework's
checkpoint schema does not match, or performance characteristics (a hand-tuned parallel tool
dispatcher, §7.5) that a framework's abstraction layer adds overhead to. The tradeoff is
straightforwardly more work: every piece of §4's loop, §8's persistence, and §10's failure handling
that a framework gives you has to be built and, more importantly, *maintained* by the team that
built it.

| Framework | Core abstraction | Best-fit pattern (§5/§3) | Durable state out of the box | Fine-grained auth (§7.3) | Where it's weakest |
|---|---|---|---|---|---|
| LangGraph | Typed graph, nodes + edges | Any — graph expresses workflows and agent loops alike | Yes — built-in checkpointer | Bring your own | Conceptual overhead for simple cases |
| CrewAI | Role + goal + process | Supervisor, hierarchical | Limited | Bring your own | Low-level loop control |
| AutoGen | Conversable agents, group chat | Peer-to-peer, debate | Limited (improving) | Bring your own | Production durability, authorization |
| Semantic Kernel | Planner + plugins | Plan-and-execute | Framework/platform-dependent | Azure-ecosystem-native | Outside the Microsoft stack |
| Custom | Whatever you design | Whatever you need | You build it | You build it | Everything is your maintenance burden |

### 11.1 When to use a framework versus build your own

This is the same build-vs-buy question `19-build-vs-buy.md` (planned) asks about observability
platforms, applied one layer up: **adopt a framework when its opinionated structure matches your
actual requirements closely enough that fighting its abstractions would cost more than the
abstractions save**, and build your own once the gap between what the framework assumes and what
you actually need — multi-tenant isolation, a specific compliance-grade audit trail, a performance
budget the framework's overhead violates — is wide enough that you would spend more effort working
around the framework's model than building the primitives directly. In practice, most teams should
start with a framework (LangGraph is the most defensible default for anything beyond a prototype,
given its state-machine model maps most directly onto §2's "code decides structure" discipline) and
migrate specific subsystems to custom code only when a concrete requirement demands it, not
speculatively.

---

## 12. Production concerns — observability, cost, latency, security

Everything in §4 through §10 is necessary but not sufficient; a system that implements all of it
correctly in a single test run still needs the operational layer that tells you, at 3 a.m., whether
it is doing so in production, for whom, and at what price.

### 12.1 Observability — tracing agent decisions

The unit of observability for an agent is not the request, it is the **decision**: every Think step
(§4.1) should emit a span capturing the prompt (or a reference to it), the model's raw output, the
parsed action and arguments, and the resulting observation — reusing
`../sre-observability/26-llm-and-ai-observability.md`'s span-per-call convention and OTEL GenAI
semantic conventions directly, with one addition specific to agents: spans need a **trajectory ID**
that ties every step of one task's loop together as a single logical unit, distinct from the
request-level trace ID a supervisor's dispatch to a specialist would otherwise use, because a single
user request can spawn many trajectories (one per specialist, §5.1) and being able to view any one
of them in isolation, or the whole tree together, is what makes an incident reconstructible after
the fact rather than merely logged.

### 12.2 Cost management

Token budgets (§4.2) enforced per task are necessary but not sufficient at platform scale; cost has
to be attributed and capped at every level `11-token-accounting-and-cost.md` (planned) already
argues for in the RAG context — per tenant, per agent role, per task type — with the agent-specific
addition that **cost per role within one trajectory** is worth tracking separately, because a
supervisor pattern's specialists frequently have wildly different cost profiles (a cheap classifier
routing step versus an expensive multi-turn research specialist), and a single aggregate cost number
for the whole trajectory hides which role is actually driving spend when it needs to be optimized.

### 12.3 Latency

Three levers, in order of impact for most agentic systems: **parallel tool execution** (§7.5) turns
sequential dependency chains into a single wall-clock round trip wherever the dependency graph
actually allows it; **model routing** (§4.2, `12-serving-latency-and-caching.md` planned) puts a
smaller, faster model on routine intermediate steps and reserves the frontier model for the steps
that need it; and **caching** — of tool results for repeated sub-queries, and of full sub-trajectories
for tasks that recur with the same shape — turns work the system has already paid for once into a
lookup the second time, which for any agent handling a real production volume of structurally
similar tasks is frequently the single largest latency win available, because it is the only lever
of the three that can reduce a step's cost to zero rather than merely to a cheaper nonzero number.

### 12.4 Security

Three security properties an agent platform has to hold, none of which the model can be trusted to
enforce on its own behalf: **tool permissions enforced outside the model** (§7.3 — authorization is
a property of the caller's identity, checked in code, never a claim in the model's reasoning text);
**data access controls enforced at the data layer**, not the prompt layer — a system prompt saying
"only access this tenant's data" is a request, not a boundary, and the actual boundary has to be a
row-level security policy or scoped credential the same way `../databases/`'s multi-tenant patterns
already require, independent of anything an agent decides to do; and **prompt injection defense**,
which starts from the same rule §7.4 states for tool results: every piece of content the agent did
not generate itself — retrieved documents, tool outputs, another agent's message — is untrusted
input and must be treated as data to reason about, never as an instruction to follow, a distinction
that has to be enforced structurally (separating instruction channels from data channels wherever
the model API supports it, and validating that a tool call was actually intended by the
task rather than injected by content the agent merely read) because no amount of "please ignore
instructions embedded in retrieved content" in a system prompt is a reliable defense against content
specifically crafted to defeat it.

---

## 13. Enterprise agent platform design

This is the section that answers the job description's actual line — "lead the development of
agent orchestration frameworks" is a platform-engineering mandate, not an agent-building one: the
deliverable is not one good agent, it is the substrate that lets every team in an organization build
a good agent faster and more safely than they could have built the substrate themselves. Every
pattern in §4 through §12 is a decision a platform either makes once, centrally, and gives away for
free, or leaves for every team to rediscover — usually inconsistently, usually with the security
and cost guarantees weaker than the platform version would have had.

### 13.1 Shared tool registry, governed

A shared registry (§7.1) across teams needs governance the single-team version does not: a review
process for adding a new tool (who can register one, what schema and scope requirements are
enforced before it is discoverable), versioning so one team's breaking change to a widely-used
tool doesn't silently break every consumer, and deprecation lifecycle so old tool versions can be
retired on a schedule rather than accumulating indefinitely. The registry becomes, in effect, an
internal API platform, and it should be operated with the same discipline as one — a published
contract, a compatibility policy, and an owner.

### 13.2 Centralized observability

Every team's agents should emit traces into one backend, using one schema (§12.1's trajectory-ID
convention, adopted org-wide, not reinvented per team), so a platform-level dashboard can answer
"which agents across the company are burning the most tokens," "which tool is failing most often
across every consumer," and "show me this specific trajectory" without needing to know which team
built the agent that produced it. This is the same argument `../sre-observability/34-schema-and-semantic-conventions-governance.md`
makes for telemetry schemas generally, applied to agent decisions specifically: a schema enforced
centrally is a query surface; a schema left to each team is a hundred incompatible logs that happen
to look similar.

### 13.3 Policy enforcement as infrastructure, not convention

The approval-gate policy (§9.1), the tool authorization scopes (§7.3), and the prompt-injection
input-sanitization rules (§12.4) should all be enforced by a **policy engine that intercepts every
tool call and every model response at the platform layer**, not by convention documented in a wiki
that each team's agent code may or may not actually implement correctly.

```python
class PolicyEngine:
    def __init__(self, rules: list["PolicyRule"]):
        self.rules = rules

    def check(self, context: "ExecutionContext") -> "PolicyDecision":
        for rule in self.rules:
            decision = rule.evaluate(context)
            if decision.verdict != "allow":
                return decision                     # first denying/gating rule wins
        return PolicyDecision(verdict="allow")

# Rules are declarative and centrally reviewable — the enforcement point every
# agent's tool call routes through, regardless of which team wrote the agent.
class RequireApprovalForDeletion(PolicyRule):
    def evaluate(self, ctx):
        if ctx.action.startswith("delete_") :
            return PolicyDecision(verdict="gate", reason="destructive action")
        return PolicyDecision(verdict="allow")
```

The platform engineering insight this makes concrete: **the golden path should be the path of least
resistance, and the dangerous path should require deliberate, visible effort to reach** — a team
building a new agent on the platform gets tool authorization, approval gates, and cost budgets by
default, because they route through the shared policy engine automatically, and *disabling* any of
those protections (not merely forgetting to add them) is the action that would require an explicit,
reviewable exception.

### 13.4 Tenant isolation

For any platform serving multiple customers or business units from shared infrastructure,
`16-multi-tenancy-and-isolation.md` (planned) applies to the agent runtime exactly as it applies to
the retrieval layer: per-tenant credential scoping so one tenant's agent cannot access another
tenant's data even under a prompt-injection attempt that tries to instruct it to, per-tenant cost
budgets and quotas so one tenant's runaway agent cannot exhaust a shared token budget or degrade
latency for every other tenant (the noisy-neighbor problem, restated for LLM calls instead of CPU
or I/O), and per-tenant observability partitioning so a support engineer investigating one
customer's incident is not incidentally exposed to another customer's trace data.

### 13.5 The platform engineer's actual job

Concretely, a platform team building this substrate owns: the tool registry and its governance
(§13.1); a runtime that implements §4's loop, §8's persistence, and §10's failure handling as a
shared library or service, not a pattern each product team re-implements; the policy engine
(§13.3); the observability backend and its schema (§13.2); tenant isolation guarantees (§13.4); and
an evaluation harness (§14) that product teams can plug their agent's tasks into without building
trajectory-scoring infrastructure from scratch. What the platform team explicitly does **not** own
is any individual agent's system prompt, tool selection, or business logic — that boundary is the
same one a good internal-platform team draws anywhere else in an organization: own the substrate,
not the application built on it, and measure success by how many teams can ship a reliable agent
without needing platform-team help to do it, not by how impressive the platform team's own flagship
agent is.

---

## 14. Evaluation of agent systems

This section is the summary; `14-agent-evaluation.md` (planned) is where the full machinery
belongs. The core move is the same one `08-evaluation-methodology.md` made for retrieval, applied
to a harder scoring target: a retrieval system's output is a ranked list, comparable against a
labeled relevant set with a closed-form metric; an agent's output is a *trajectory*, and scoring it
correctly requires deciding what "correct" means at more than one level.

### 14.1 Task success rate

The outcome-level metric — did the final answer satisfy the task — is necessary and radically
insufficient on its own, for the same reason a retrieval system's end-to-end answer quality alone
cannot diagnose *which stage* of `04`'s cascade to fix: an agent that reaches the right answer by
calling the wrong tool three times, retrying blindly until something worked, has a success rate of
1 and a cost and reliability profile that will not survive contact with a harder or larger task
distribution.

### 14.2 Trajectory correctness

Trajectory-level evaluation scores *the sequence of actions*, not just the final answer, against a
labeled or rubric-defined acceptable path — reusing `08`'s golden-set discipline with the golden
label now being a trajectory (or a set of acceptable trajectories, since more than one correct path
often exists) rather than a single relevant-document set.

```python
def score_trajectory(actual: list[dict], golden: list[dict]) -> dict:
    actual_actions = [(s["action"], _canon(s["action_input"])) for s in actual]
    golden_actions = [(s["action"], _canon(s["action_input"])) for s in golden]
    correct_steps = sum(1 for a, g in zip(actual_actions, golden_actions) if a == g)
    return {
        "step_precision": correct_steps / max(len(actual_actions), 1),
        "step_recall": correct_steps / max(len(golden_actions), 1),
        "extra_steps": max(0, len(actual_actions) - len(golden_actions)),
        "exact_match": actual_actions == golden_actions,
    }
```

Trajectory scores are what let a regression gate (§8.4 of `08`, reused directly here) catch a
change that keeps the success rate flat while doubling the average number of tool calls — a
regression that a pure outcome metric is structurally blind to.

### 14.3 Tool selection accuracy

Narrower than full trajectory scoring: precision and recall on *which tools* were called relative
to which tools the golden trajectory called, independent of argument correctness or ordering. This
metric isolates the specific failure mode §7.2's dynamic tool selection and §10.4's hallucinated-call
handling exist to prevent, and tracking it separately from full trajectory match makes it possible
to tell "the agent is choosing tools well but sequencing them wrong" apart from "the agent is
reaching for the wrong tools entirely" — two failures with very different fixes.

### 14.4 Cost per resolved task

The metric that prevents the other three from being gamed independently: `cost per resolved task =
total tokens (or dollars) spent across all attempts / number of tasks that ended in success`. An
agent with a 95% success rate at 10,000 tokens per task and one with a 99% success rate at 40,000
tokens per task are not simply "the second is better" — the comparison depends on the task's actual
value, and reporting cost per resolved task alongside the raw success rate is what makes that
tradeoff visible instead of buried in an aggregate token bill nobody connects back to quality.

### 14.5 Regression testing for agents

Live tool calls in a test suite are both flaky (real APIs fail transiently, rate-limit, and change)
and expensive (every CI run pays real token and API cost) — the fix, reusing
`../python-mastery/43-testing-strategy.md`'s test-double discipline, is to replay agent tests
against **recorded or simulated tool responses**, not live ones: capture real tool call/response
pairs from production or staging, replay them deterministically in CI, and assert both the outcome
and the trajectory shape against the recorded golden run. This gives an agent regression suite the
same speed and determinism a workflow's fixture-based test suite already had (§2.1), applied to a
system whose control flow is not fixed, by fixing the *environment* even though the *policy*
(the model) remains non-deterministic between runs — which is why trajectory scoring (§14.2) uses a
tolerance band or an LLM-judge comparison against the golden path rather than requiring byte-exact
replay of the model's own output.

---

## 15. Anti-patterns

**Over-autonomy.** Granting an agent a broad tool set and a loose termination condition for a task
whose actual decision surface (§2) was narrow and enumerable. The tell is a system prompt that
reads like a job description ("you are a helpful assistant that can do anything the user needs")
rather than a scoped contract ("you may call these three tools, for this class of request, and you
must stop after producing X"). The fix is not less capability, it is *less unscoped* capability —
narrow the tool set and the termination contract to what the task's actual decision surface
requires, and let §5's decomposition patterns handle genuine breadth by composing narrow agents,
not by widening one agent's scope.

**Under-specification.** A system prompt that states a goal without stating the constraints,
failure modes to avoid, or format the caller needs, on the theory that a capable enough model will
infer the rest. It will infer *something*, and the something is a random draw from the model's
prior about what a reasonable assistant would do in this situation — which is rarely wrong in any
single sample and reliably inconsistent across a production volume of them. Every constraint that
matters for a task's correctness belongs in the prompt or the tool schema explicitly; "the model is
smart, it'll figure out the format" is a bet against variance that production traffic always loses
eventually.

**God-agent.** One agent, one system prompt, one enormous tool list, responsible for an entire
product surface. It fails §7.2's tool-selection-accuracy argument directly (too many tools degrades
selection), fails §14.3's evaluation story (a single trajectory metric across wildly different task
types is not comparable to itself over time), and fails the organizational test §13.5 cares about
most: a god-agent has no clean ownership boundary, so every team with a stake in part of its
behavior is editing the same prompt, and prompt changes for one team's use case silently regress
another's — the LLM-orchestration equivalent of a monolith with no module boundaries, with the
added property that its "modules" are prose instructions competing for the same context window.

**Ignoring cost.** Treating token spend as an infrastructure line item to notice on the monthly
invoice rather than a first-class per-task, per-tenant metric enforced with the same budget
discipline as `11-token-accounting-and-cost.md` (planned) demands for the plain-RAG case — and worse
in the agentic case, because §4.3's unbounded-loop failure mode means the tail of the cost
distribution, not the mean, is where the real damage happens, and a system with no per-task budget
has no defense against that tail at all.

**No evaluation.** Shipping an agent whose only quality signal is developer intuition from
manually trying it a few dozen times — rung 3 (`08`'s ledger vocabulary) presented as rung 1. Every
claim this chapter makes about an architecture, a pattern, or a framework choice being "better" is
a claim that only becomes true, for your task and your model, once §14's trajectory and cost metrics
are actually measured against a golden set; treating architectural taste as a substitute for that
measurement is the single anti-pattern underneath all the others, because it is what lets the first
four go unnoticed until they are expensive production incidents instead of caught findings in an
eval run.

---

## 16. Interview questions

Organized by theme; each entry states what a weak answer sounds like and what separates it from a
strong one, because in a senior platform-engineering interview the differentiator is rarely
knowing the term — it is knowing the tradeoff underneath it.

**1. "When would you use an agent instead of a workflow?"**
Weak: "when the task is complex." Strong: names the specific decision that is not enumerable at
deploy time (§2), and scopes the agent's autonomy to exactly that decision while keeping everything
enumerable in code — demonstrating the "code decides structure, model decides content" discipline
from §2.2, not a vibe about complexity.

**2. "Walk me through ReAct versus Plan-and-Execute — when would you pick each?"**
Weak: describes both accurately but has no selection criterion. Strong: ties the choice to
recoverability versus cost (§3.6) — ReAct for cheap, frequent, correctable steps; Plan-and-Execute
for expensive steps where amortizing one planning call matters more than the risk of a stale plan,
with an explicit re-plan trigger named.

**3. "What's wrong with letting a single agent have every tool in the system?"**
Weak: "it might get confused." Strong: names the specific mechanism — tool-selection accuracy
degrading with option count (§7.2), the security surface growing with the union of every tool's
capability (§1.1), and the ownership/testability collapse of a god-agent (§15) — as three distinct,
independently fixable problems, not one vague complaint.

**4. "How do you prevent an agent from running forever?"**
Weak: "set a max iterations." Strong: enumerates all of §4.3's termination conditions — iteration
cap, cumulative token budget, wall-clock timeout, stuck-loop detection, circuit-breaker trip, and
explicit human interrupt — and explains why relying on only the first one is the most common
production bug in agent loops.

**5. "How would you design a multi-agent system for [some task]?"**
Weak: jumps straight to naming agents and roles. Strong: starts by asking whether the task
actually needs decomposition (separable concerns, a real interface between pieces) or whether it's
a single well-scoped agent wearing a multi-agent costume, then picks a topology from §5.6's table
by matching coordination-authority and failure-isolation needs to the task, defaulting to
supervisor or assembly-line and justifying any move toward peer-to-peer or debate specifically.

**6. "What's the blackboard pattern, and when would you use it over direct agent-to-agent
messaging?"**
Weak: recites the definition. Strong: explains the ownership problem it solves (§6.3) — a
controller deciding who writes when, instead of every specialist independently deciding to act —
and names the scale threshold (large, growing specialist set; audit-trail requirement) at which it
beats a fixed message topology.

**7. "How do you keep information from getting lost when one agent hands off to another?"**
Weak: "make the prompts clear." Strong: names structured artifacts over prose summaries, references
over restatements, and one authoritative trace over N independent summaries (§6.4) — with a
concrete example of a handoff that failed because a summary dropped a detail the next stage needed.

**8. "How do you handle a tool call that fails halfway through — did the side effect happen or
not?"**
Weak: "just retry it." Strong: distinguishes idempotent from non-idempotent operations (§7.6),
explains why blind retry on a non-idempotent op after an ambiguous failure is a correctness bug, not
a resilience feature, and describes an idempotency-key design where the key is derived from the
task, not generated fresh by the model on each attempt.

**9. "What happens when the model calls a tool that doesn't exist?"**
Weak: "we validate it and throw an error." Strong: explains why the error should come back as a
structured *observation* the model can react to on its next step (§10.4), not an exception that
kills the loop — and mentions the fuzzy-match hint as a cheap, high-yield addition.

**10. "How do you detect an agent that's stuck in a loop?"**
Weak: "if it hits max iterations." Strong: distinguishes exhausting the budget from *detecting
non-progress* — hashing recent actions and checking for repetition (§10.5), optionally embedding
consecutive thoughts to catch rephrased-but-equivalent reasoning — and terminating early with an
escalation reason, not silently running out the clock.

**11. "How would you design approval gates for a customer-facing agent?"**
Weak: "add a confirmation step for risky actions." Strong: describes the gate as a suspend point in
a durable state machine (§9.1) — the pending action persisted, the task resumable after an
arbitrarily long wait — and ties the gated-action list to a centrally owned policy (§13.3), not a
per-agent judgment call.

**12. "How do you get an agent to know when to ask for help instead of guessing?"**
Weak: "ask the model for a confidence score." Strong: is honest that self-reported LLM confidence is
poorly calibrated (§9.2), and proposes structural proxies instead — repeated failures, a re-plan
trigger firing, a failed verifier check — as the actual routing signal into human takeover.

**13. "Compare LangGraph, CrewAI, and AutoGen — when would you pick each?"**
Weak: lists features. Strong: maps each to the pattern it's naturally shaped for (§11) — LangGraph's
graph-and-checkpoint model for anything needing durable, auditable control flow; CrewAI's role
abstraction for fast supervisor/hierarchical prototypes; AutoGen's conversable-agent model for
peer-to-peer and debate experimentation — and states the concrete gap (durability, fine-grained
authorization) that would push a production system toward a custom implementation instead.

**14. "When would you build a custom agent runtime instead of using an existing framework?"**
Weak: "when we need more control." Strong: names the specific unmet requirement — multi-tenant
credential isolation a framework doesn't provide, a compliance-grade audit schema the framework's
checkpoint format doesn't match, a performance-critical parallel dispatcher (§7.5) — and
acknowledges the tradeoff is taking on every piece of §4–§10's maintenance burden in exchange.

**15. "How would you design a shared agent platform for multiple product teams?"**
Weak: "give everyone the same framework." Strong: describes the actual platform surface — governed
shared tool registry (§13.1), centralized observability with a common trace schema (§13.2), a policy
engine enforcing authorization and approval gates outside any individual agent's control (§13.3),
tenant isolation (§13.4) — and frames success as how little platform-team involvement a new agent
needs, not how capable the platform team's own agent is.

**16. "How do you evaluate an agent, beyond 'did it get the right answer'?"**
Weak: "we check the final output." Strong: distinguishes task success rate, trajectory correctness,
tool selection accuracy, and cost per resolved task (§14) as four separate signals that can diverge,
and gives an example where success rate stayed flat while trajectory length doubled — a regression
a pure outcome metric would have missed entirely.

**17. "How do you write regression tests for something as non-deterministic as an agent?"**
Weak: "we don't, we just monitor production." Strong: describes recorded/replayed tool responses to
fix the environment while accepting the policy (the model) is still non-deterministic (§14.5), and
scores replayed runs against a golden trajectory with a tolerance band rather than requiring exact
match.

**18. "What's the security risk of giving an agent a broad tool, like shell execution or arbitrary
SQL?"**
Weak: "the model might do something dangerous." Strong: separates two distinct risks — an
unintentional error compounded by the broad action space (§1.1), and a prompt-injection payload in
retrieved content or a tool result steering that same broad actuator deliberately (§12.4) — and
states that the defense for both is authorization and validation enforced outside the model, never
an instruction inside the prompt.

**19. "How would you defend against prompt injection in a tool-using agent?"**
Weak: "tell the model to ignore instructions in the data." Strong: states plainly that instruction
in a system prompt is not a security boundary against content specifically crafted to defeat it,
and describes structural mitigations instead — separating instruction and data channels where the
model API allows it, validating that a proposed tool call actually serves the original task rather
than trusting it at face value, and treating every tool result and retrieved document as untrusted
input by default (§12.4, §7.4).

**20. "What's the single biggest mistake you've seen in agent system design?"**
Weak: a specific bug story with no generalizable lesson. Strong: names one of §15's anti-patterns —
most often over-autonomy or no evaluation — as a structural pattern, explains the mechanism by which
it becomes expensive at scale rather than in the first demo, and states the concrete architectural
fix, not just "we should have been more careful."

**21. "How do you decide how much budget (tokens, time, iterations) to give an agent for a task?"**
Weak: "pick a number that feels safe." Strong: ties the budget to the task's measured distribution
of successful-trajectory lengths (from §14's evaluation data, not guesswork), sets the cap with
headroom above the p95 of that distribution, and treats a cap that is frequently hit as a signal to
investigate the task's decomposition (§2), not just a signal to raise the number.

**22. "How do multi-agent systems relate to distributed systems more generally?"**
Weak: "they're similar I guess." Strong: states plainly that a multi-agent system without central
coordination is a distributed system whose nodes are non-deterministic (§5.3), inherits partial
failure and inconsistent shared state from that field directly, and adds a genuinely new failure
mode distributed systems theory doesn't have a name for — a node that is confidently, actively wrong
about what it just did, not merely delayed or crashed.

---

## 17. Lab exercises

**Lab 1 — A ReAct loop with every §4 guardrail wired, on a task you can score.**
*Goal:* build the loop in §4.1 for real, against a small tool set (three to five tools) over a task
you can objectively check (a multi-hop QA task over a fixed corpus is a good choice, since `04`'s
retrieval work gives you the tools for free).
*Steps:* implement the loop with all five termination conditions from §4.3 wired, not just max
iterations. Deliberately induce each one: give it a task with no valid answer (should hit max
iterations cleanly), remove a tool it needs mid-run (should hit the circuit-breaker path), and feed
it a task that invites a repeat-action loop (should trip stuck-loop detection). Log every step as a
span per §12.1's convention.
*Artifact:* the loop implementation, plus a trace for each of the three induced failures showing
the correct termination reason.
*Success criterion:* all three induced failures terminate with the correct, distinct reason logged
— not all three collapsing into "max iterations."
*Time:* ~1 day.
*Unblocks:* Labs 2–4, and the P3 project in this folder's `README.md`.

**Lab 2 — Tool registry with authorization and hallucinated-call recovery.**
*Goal:* build §7.1's registry and §10.4's recovery path, and prove both hold under adversarial
input.
*Steps:* register five tools with JSON-schema input/output contracts and per-tool scopes. Write a
test that calls each tool with a caller lacking the required scope and asserts a clean
`UnauthorizedToolError`, not a tool execution. Then feed the agent loop from Lab 1 a prompt
engineered to make it call a nonexistent tool name and a real tool with malformed arguments;
confirm both produce a structured observation (with the fuzzy-match hint for the first) rather than
crashing the loop, and confirm the agent self-corrects within two further steps in at least 80% of
sampled runs.
*Artifact:* the registry, the authorization test suite, and the self-correction rate measured over
a batch of induced hallucinations.
*Success criterion:* zero unauthorized executions across the test suite; a measured, not assumed,
self-correction rate.
*Time:* ~half a day.
*Unblocks:* Lab 5, and `17-safety-guardrails-and-prompt-injection.md`.

**Lab 3 — Supervisor pattern over two specialists, with a trajectory eval.**
*Goal:* build §5.1's supervisor pattern for real, and score it with §14.2's trajectory metric, not
just outcome accuracy.
*Steps:* build two specialist agents with clearly separable concerns (for example, a retrieval
specialist and a calculation specialist) behind a supervisor that routes and synthesizes. Hand-label
ten to twenty tasks with a golden trajectory (which specialist should be invoked, in what order).
Run the supervisor over them and compute step precision/recall per §14.2, alongside raw task success
rate.
*Artifact:* the supervisor implementation, the golden trajectory set, and a table comparing task
success rate against trajectory step precision/recall.
*Success criterion:* at least one case in your set where the two metrics diverge (success rate high,
trajectory score lower, or vice versa) — that divergence is the point of the lab, not a
success/failure gate on either number alone.
*Time:* ~1 day.
*Unblocks:* Lab 6, and `14-agent-evaluation.md`.

**Lab 4 — Crash-recoverable task state.**
*Goal:* prove §8.3's checkpoint-and-resume design actually survives a crash, not just a clean
restart.
*Steps:* wrap the Lab 1 loop with the `TaskCheckpointer` pattern, persisting state after every step.
Kill the process (`kill -9`, not a graceful shutdown) mid-task at three different iteration counts,
and confirm the resumed run picks up from the last checkpointed step rather than re-executing
already-completed, non-idempotent steps.
*Artifact:* the checkpointing implementation and a log showing three induced crashes, each followed
by a correct resume with no duplicated tool effects.
*Success criterion:* zero duplicated non-idempotent effects across all three induced crashes.
*Time:* ~half a day.
*Unblocks:* Lab 7, and any production deployment of the P3 project.

**Lab 5 — An approval gate with a durable pending-action queue.**
*Goal:* implement §9.1 end to end, including the wait being arbitrarily long.
*Steps:* add one "destructive" tool to the Lab 2 registry, gated by a policy rule. Trigger it,
confirm the task suspends into `awaiting_approval` and the pending action is durably persisted (not
held only in an in-memory queue — kill the process while a task is pending and confirm it is still
resumable). Implement both the approve and reject resume paths, and confirm a rejected action
produces an observation the agent's next step actually reacts to, rather than silently ending the
task.
*Artifact:* the gate implementation, plus a demonstrated resume-after-crash for a pending approval.
*Success criterion:* a pending approval survives a process restart and resolves correctly on both
the approve and reject paths.
*Time:* ~half a day.
*Unblocks:* Lab 8, and `16-multi-tenancy-and-isolation.md`'s policy-enforcement labs.

**Lab 6 — Cost-per-resolved-task, measured across two architectures.**
*Goal:* turn §14.4 from a formula into a number you've actually produced, and use it to make a real
architecture decision.
*Steps:* run the same task set through two of §3's architectures against the same tool set and
model — for example, plain ReAct versus Plan-and-Execute with re-planning. Record total tokens and
success/failure per task for both. Compute cost per resolved task for each, not just raw success
rate or raw cost independently.
*Artifact:* a two-architecture comparison table: success rate, mean tokens per task, and cost per
resolved task.
*Success criterion:* a decision, stated with the number that drove it — including "the cheaper
architecture wins despite a lower success rate" as a legitimate, evidence-backed outcome.
*Time:* ~1 day plus API cost.
*Unblocks:* `12-serving-latency-and-caching.md`'s model-routing labs.

**Lab 7 — Replay-based regression suite for an agent.**
*Goal:* build §14.5's recorded-tool-response regression harness, so agent behavior can be tested in
CI without live API calls.
*Steps:* record real tool call/response pairs from ten runs of the Lab 3 supervisor. Build a mock
tool layer that replays the recorded response for a matching call signature and errors loudly on
any unexpected call. Wire this into a CI-runnable test that replays each recorded task and asserts
the trajectory-similarity score (§14.2) against the original recorded trajectory stays above a
threshold.
*Artifact:* the recorded fixture set, the mock replay layer, and a passing CI job.
*Success criterion:* the suite runs with zero live API calls, completes in seconds not minutes, and
catches a deliberately introduced regression (change one specialist's prompt to make it choose a
worse but still plausible action) by failing the trajectory-similarity assertion.
*Time:* ~1 day.
*Unblocks:* `09-eval-infrastructure-and-ci.md`, and CI gating for the P3/P4 projects.

**Lab 8 — A minimal policy engine enforcing authorization and gates centrally.**
*Goal:* prove §13.3's platform-layer enforcement model — one interception point every agent's tool
call routes through — rather than authorization checks scattered per-tool.
*Steps:* build the `PolicyEngine` skeleton from §13.3 with at least three rules (a scope-based
authorization rule, a destructive-action gate rule, and a per-task token-budget rule). Route both
the Lab 1 agent and the Lab 3 supervisor's tool calls through the same engine instance, and confirm
a policy change (tightening a scope, adding a new gated action pattern) takes effect for both
agents without touching either agent's own code.
*Artifact:* the policy engine, its rule set, and a demonstration that one policy change affects two
independently built agents identically.
*Success criterion:* the policy change requires editing exactly one file (the rule set), not either
agent's implementation.
*Time:* ~half a day.
*Unblocks:* P4's flagship platform project, and `16-multi-tenancy-and-isolation.md`.

**Lab 9 — Stuck-loop and circuit-breaker induction test.**
*Goal:* make §10.3 and §10.5 tested behaviors, not intentions.
*Steps:* wrap one tool in the Lab 1 registry with the `ToolCircuitBreaker` from §10.3, and configure
it to fail on demand. Drive enough failures to trip the breaker and confirm the agent loop
terminates with the correct reason rather than exhausting its iteration budget retrying a dead
dependency. Separately, engineer a task that induces the agent to repeat one action, and confirm
`is_stuck` (§10.5) fires before the max-iteration cap does.
*Artifact:* two induced-failure logs, each showing early, correctly-reasoned termination rather than
a budget-exhaustion timeout.
*Success criterion:* both failures terminate measurably earlier than the max-iteration cap, with the
specific failure reason logged.
*Time:* ~half a day.
*Unblocks:* `18-failure-modes-and-incident-walkthrough.md`.

---

## Rung ledger

This document is **rung 3 — studied** (README §6, this folder's convention): the architectural
claims — why a cascade's control-flow discipline degrades along §1's spectrum, why ReAct's
mandatory observation step bounds compounding hallucination better than undisciplined
chain-of-thought, why a supervisor pattern's centralization buys better failure isolation than
peer-to-peer coordination, why authorization and injection defense must sit outside the model's own
reasoning — are derivable from the mechanisms themselves and are architecture, not measurement.

**Verified against primary sources, read for their mechanism, not their benchmark numbers:** the
ReAct paper (Yao, Zhao, Yu, Du, Shafran, Narasimhan & Cao, 2210.03629) for the interleaved
thought-action-observation loop structure; the Reflexion paper (Shinn, Cassano, Gopinath, Narasimhan
& Yao, 2303.11366) for the verbal-reinforcement-via-episodic-memory mechanism; the LATS paper (Zhou,
Yao, Shafran, Zamora, Hausman, Hariharan, Ichter & Narasimhan, or the commonly cited 2310.04406
record) for the tree-search-plus-reflection combination; Anthropic's "Building Effective Agents"
engineering post for the workflows-versus-agents distinction and the five named workflow patterns
in §2.2, quoted structurally (which patterns exist and what each composes) rather than for any
benchmark claim; and the classical blackboard-architecture literature (Hearsay-II and the
Erman/Lesser line of work it originated) for the controller/knowledge-source/shared-workspace
decomposition in §6.3.

**This document's own architecture, not a cited claim:** the four degrading properties in §1.1, the
"code decides structure, model decides content" framing in §2.3, the three-kind state taxonomy in
§8.1, and the platform-ownership boundary in §13.5 are this chapter's own synthesis, offered as a
framework for reasoning about the design space, not as a result anyone measured. Treat them as a
vocabulary to argue with, not a citation to defer to.

**Deliberately not in this document:** version-specific API syntax for any named framework in §11
(LangGraph, CrewAI, AutoGen, and Semantic Kernel all ship breaking changes faster than a static
document can track); any specific benchmark number for agent task success rates on any named
framework or model, since those numbers are corpus- and task-specific in exactly the way `04` §17's
ledger already argues reranker leaderboards are; and any claim about which multi-agent framework
"wins," for the same reason.

The labs in §17 are what convert this to **rung 1 — measured**, on your own tools, your own tasks,
and your own model — and every cost, latency, and trajectory-score number they produce should
travel with the exact task set, tool set, and model version that produced it, per this repo's
standing rung-1 discipline.
