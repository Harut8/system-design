# Appendix L — The control plane: guardrails, token budgets, rate limits, tool governance and the 2026 gateway layer

> **Why this appendix exists.** Every control in this folder is taught inside the chapter that owns
> it: guardrails in [`17`](17-safety-guardrails-and-prompt-injection.md), quotas and routing in
> [`23`](23-multi-llm-model-gateway.md), tool registries and authorization in
> [`24`](24-tool-calling-and-enterprise-integration.md), token budgets in
> [`06`](06-context-engineering.md) and [`30`](30-context-engineering-for-agents.md), agent loop caps
> in [`22`](22-agent-orchestration-patterns.md) §4.2. That is the right place to learn each one.
>
> It is the wrong shape for shipping. In production these are not six features, they are **one
> control plane**: a single envelope per request that every model call, tool call, sub-agent and MCP
> server has to respect, enforced at five chokepoints, and degraded in a known order when something
> runs out. A system that has all six controls but no shared envelope still dies the usual way — a
> runaway loop inside a request that already passed admission, a sub-agent that inherited no
> deadline, a guardrail outage that fails open because nobody chose.
>
> This page adds the four things no chapter covers:
>
> 1. The **envelope** and its propagation rules: deadline, token budget, spend cap, tool-call and
>    depth caps, policy version — who creates it, who decrements it, what a child inherits (§2).
> 2. **Admission control and the degradation ladder** for LLM traffic: priority classes mapped onto
>    provider service tiers, deadline-aware shedding, and what to give up first (§3, §4).
> 3. **The 2026 gateway layer** — AI gateway, MCP gateway and agentic gateway are three different
>    planes, and the token-accounting semantics differ between them in a way that decides whether a
>    single large request can blow your quota (§8).
> 4. **Two reference configurations**: the minimum viable control set for three engineers, and the
>    enterprise set, with the signals that tell you to move from one to the other (§9).
>
> **Read from source.** Provider behaviour was read from vendor documentation in October 2026 and is
> marked *(docs, 2026-10)*. Prices and limits change; the arithmetic in §5 is written so you can
> re-run it with today's numbers. Where a number is a modelling assumption rather than a documented
> fact it is marked *(assumption — measure yours)*. Nothing here is a benchmark result; where a
> number would need measuring, it is a lab in §12.
>
> **Companions:** [appendix J](appendix-j-production-ai-stack.md) maps the whole production stack by
> layer; [appendix I](appendix-i-langchain-v1-in-practice.md) and
> [appendix K](appendix-k-langgraph-runtime-and-platform-in-practice.md) cover the framework-level
> hooks (middleware, `RetryPolicy`, rate limiters) that implement parts of this page.

## Contents

1. [What the control plane is: five chokepoints](#1-what-the-control-plane-is-five-chokepoints)
2. [The request envelope](#2-the-request-envelope)
3. [Admission control, priority classes and the degradation ladder](#3-admission-control-priority-classes-and-the-degradation-ladder)
4. [The 429 contract and the two ceilings](#4-the-429-contract-and-the-two-ceilings)
5. [Token optimization, ordered by payoff](#5-token-optimization-ordered-by-payoff)
6. [Guardrails: placement, products, latency budget](#6-guardrails-placement-products-latency-budget)
7. [Tool governance: the registry as a control surface](#7-tool-governance-the-registry-as-a-control-surface)
8. [The gateway layer in 2026: AI, MCP, agentic](#8-the-gateway-layer-in-2026-ai-mcp-agentic)
9. [Two reference configurations](#9-two-reference-configurations)
10. [Failure modes of the control plane itself](#10-failure-modes-of-the-control-plane-itself)
11. [Anti-patterns](#11-anti-patterns)
12. [Lab exercises](#12-lab-exercises)
13. [Interview questions](#13-interview-questions)
14. [Sources](#14-sources)

---

## 1. What the control plane is: five chokepoints

> **In plain words.** There are only five places where you can stop or shrink work: when the request
> arrives, just before each model call, inside the model call, just before each tool call, and just
> before the answer leaves. Every control you will ever add lives at one of those five. If a control
> has no chokepoint, it is a wish.

```
                    ┌─────────────────────────────────────────────────────────┐
  client ──────────▶│ A. INGRESS / ADMISSION                                  │
                    │    authn, tenant resolution, envelope created,          │
                    │    quota reserve, concurrency + queue, deadline set,    │
                    │    priority class chosen, load shed                     │
                    └───────────────────────┬─────────────────────────────────┘
                                            │ envelope
                    ┌───────────────────────▼─────────────────────────────────┐
                    │ B. PRE-MODEL                                            │
                    │    input guardrails, PII redaction, prompt assembly,    │
                    │    cache-aware layout, context trim to budget           │
                    └───────────────────────┬─────────────────────────────────┘
                    ┌───────────────────────▼─────────────────────────────────┐
                    │ C. MODEL CALL                                           │
                    │    model + service_tier choice, max_tokens, timeout     │
                    │    (≤ remaining deadline), retry policy, usage settle   │
                    └──────┬────────────────────────────────┬─────────────────┘
                           │ tool_use                       │ final
                    ┌──────▼─────────────────────────────┐  │
                    │ D. TOOL CALL                        │  │
                    │    registry lookup, authz, arg      │  │
                    │    validation, idempotency key,     │  │
                    │    per-tool rate limit, result      │  │
                    │    truncation, injection screen     │  │
                    └──────┬─────────────────────────────┘  │
                           └── loop (caps: turns, tokens, ──┘
                               spend, wall clock, depth)
                    ┌───────────────────────▼─────────────────────────────────┐
                    │ E. EGRESS                                               │
                    │    output guardrails, citation check, PII scan,         │
                    │    schema validation, settle usage, emit span + cost    │
                    └─────────────────────────────────────────────────────────┘
```

The map of what is already written, and what this page adds:

| Control | Chokepoint | Theory lives in | What this appendix adds |
|---|---|---|---|
| Input guardrails, injection defence | B | `17` §4–§6, `28` | Placement and fail-open choice per rail (§6.3), managed products (§6.2) |
| Output filtering, PII | E | `17` §7–§8 | The latency budget, inline vs parallel vs shadow (§6.4) |
| Token budget per request | B, C | `06` §1–§3, `30` §11 | The envelope that survives sub-agents and tools (§2) |
| Agent loop caps | D | `22` §4.2 | Charging them against one shared ledger, not per-node (§2.3) |
| Per-tenant quotas, reserve/settle | A, C | `23` §6 | The second ceiling, 429 semantics, spend-cap 429 (§4) |
| Tool authz, idempotency | D | `24` §6–§8 | The registry as a context-budget control too (§7) |
| Cost attribution | C, E | `23` §7 | Which six fields to log so savings are attributable (§5.6) |
| Model routing, fallback | C | `23` §4–§5 | Routing as a degradation step, not a feature (§3.4) |
| Prompt caching | B, C | `06` §12, `30` §10 | The TTL break-even and the stacking rules (§5.2) |
| Tool-definition tax | B | `30` §2–§3 | The registry-side controls and MCP aggregation (§7.2) |
| Admission control, shedding | A | *nowhere* | §3 |
| Priority classes, service tiers | A, C | *nowhere* | §3.3 |
| Degradation ladder | A–E | *nowhere* | §3.4 |
| AI / MCP / agentic gateways | A, D | `23` §15, `26` §8, appendix J | §8 |

**The one rule.** A control that cannot be *enforced* at a chokepoint is documentation. "Teams should
keep prompts under 20k tokens" is documentation. A trim step at B that truncates to the envelope's
remaining input budget and emits a counter when it fires is a control.

---

## 2. The request envelope

> **In plain words.** One small object is created when the request arrives and is passed to
> everything that runs for that request. It carries the clock, the money, the limits and the policy
> version. Sub-agents get a *child* of it, not a fresh one. When it says stop, everything stops.
>
> **Real-world example.** A support agent gets 60 seconds, 80k input tokens, 8 tool calls and $0.40.
> It calls a summarizer sub-agent twice; each child gets whatever is left at the moment it is
> created, and every token the child spends is charged to the parent's ledger. The request that used
> to cost $4 when a retrieval loop went pathological now returns a partial answer at $0.40.

### 2.1 The fields, and why each exists

| Field | Type | Why it exists | Failure it prevents |
|---|---|---|---|
| `tenant`, `principal` | ids | quota scope, authz, attribution | one tenant starving the rest |
| `deadline` | **absolute** monotonic instant | every timeout derives from it | per-hop timeouts multiplying (§2.2) |
| `input_budget`, `output_budget` | tokens | trim target at B, `max_tokens` at C | context creep, 10× bills on long threads |
| `spend_cap` | currency | the only limit a finance person understands | runaway loops |
| `tool_calls_left`, `turns_left` | ints | agent loop termination | ReAct ping-pong |
| `depth`, `max_depth` | ints | sub-agent recursion | an agent spawning itself |
| `priority` | enum | admission and `service_tier` choice | batch work evicting interactive work |
| `policy_version` | string | guardrail/registry config version used | un-auditable decisions, replica skew |
| `trace_id` | string | joins cost, eval and audit rows | "which request was that?" |
| `degraded` | list | what was already given up | silent quality loss (§3.4) |

### 2.2 Deadlines are absolute, or they multiply

A relative per-hop timeout composes by multiplication. With a 30 s model timeout, 3 retries, up to 10
tool calls per turn and 2 levels of sub-agent, the worst case is `30 × 4 × 10 × 2 = 2,400 s` of work
for a request whose caller gave up after 30 s. Nobody configured 40 minutes; it is the product of
four reasonable-looking numbers.

An absolute deadline cannot multiply: each hop computes `min(its own timeout, deadline - now)`, and a
hop that would start with less than its minimum useful slice does not start at all.

```python
import time, uuid
from dataclasses import dataclass, field, replace


class Exhausted(Exception):
    """Raised at a chokepoint, never swallowed by a retry wrapper."""
    def __init__(self, what: str, envelope: "Envelope"):
        super().__init__(f"{what} exhausted (trace={envelope.trace_id})")
        self.what = what


@dataclass
class Ledger:
    """Shared by a request and all of its children — the whole point."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    spend: float = 0.0
    tool_calls: int = 0


@dataclass(frozen=True)
class Envelope:
    tenant: str
    priority: str = "interactive"          # interactive | background | batch
    deadline: float = 0.0                  # time.monotonic() instant
    input_budget: int = 80_000
    output_budget: int = 4_000
    spend_cap: float = 0.40
    tool_calls_left: int = 8
    turns_left: int = 12
    depth: int = 0
    max_depth: int = 2
    policy_version: str = "unset"
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ledger: Ledger = field(default_factory=Ledger)
    degraded: tuple[str, ...] = ()

    # --- clock ---
    @property
    def remaining_s(self) -> float:
        return self.deadline - time.monotonic()

    def slice_for(self, hop_timeout_s: float, min_useful_s: float = 1.0) -> float:
        """Timeout for one hop. Never exceeds what is left."""
        left = self.remaining_s
        if left < min_useful_s:
            raise Exhausted("deadline", self)
        return min(hop_timeout_s, left)

    # --- money and tokens: charged after the fact, checked before ---
    def check(self) -> None:
        if self.remaining_s <= 0:
            raise Exhausted("deadline", self)
        if self.ledger.spend >= self.spend_cap:
            raise Exhausted("spend_cap", self)
        if self.ledger.input_tokens >= self.input_budget:
            raise Exhausted("input_budget", self)

    def charge(self, *, input_tokens=0, output_tokens=0, cache_read=0,
               cache_write=0, usd=0.0, tool_calls=0) -> None:
        self.ledger.input_tokens += input_tokens
        self.ledger.output_tokens += output_tokens
        self.ledger.cache_read_tokens += cache_read
        self.ledger.cache_write_tokens += cache_write
        self.ledger.spend += usd
        self.ledger.tool_calls += tool_calls

    # --- children: narrower, never wider, and sharing the ledger ---
    def child(self, *, timeout_s: float | None = None,
              input_budget: int | None = None,
              tool_calls: int | None = None) -> "Envelope":
        if self.depth + 1 > self.max_depth:
            raise Exhausted("max_depth", self)
        deadline = self.deadline
        if timeout_s is not None:
            deadline = min(deadline, time.monotonic() + timeout_s)
        return replace(
            self,
            depth=self.depth + 1,
            deadline=deadline,
            input_budget=min(self.input_budget, input_budget or self.input_budget),
            tool_calls_left=min(self.tool_calls_left, tool_calls or self.tool_calls_left),
            # ledger is NOT replaced: parent and children share one ledger
        )

    def degrade(self, step: str) -> "Envelope":
        return replace(self, degraded=self.degraded + (step,))
```

Three details carry the design:

- **`child()` can only narrow.** A sub-agent cannot buy itself more time or money than its parent
  has left. `min` everywhere, never a fresh default.
- **The ledger is shared by reference** while the envelope itself is frozen. Limits are immutable
  per hop; consumption is global per request. Copying the ledger into children is the single most
  common bug in home-grown versions of this object: each child then gets its own fresh budget and the
  request costs `fanout × cap`.
- **`check()` before, `charge()` after.** Token and money costs are only known once the response
  lands, so the loop is check-with-what-you-know, then charge the truth. That is the same
  reserve-then-settle shape as `23` §6.4, scoped to one request instead of one tenant.

### 2.3 Where it plugs in

| Framework | Hook | Note |
|---|---|---|
| Plain SDK loop | your own `while` | pass `env` explicitly; `env.check()` at the top of each turn |
| LangChain 1.x | middleware (`before_model`, `after_model`, `wrap_tool_call`) | appendix I; `ToolRuntime` context carries the envelope |
| LangGraph | state field + `Runtime` context, `recursion_limit` as backstop | `21`; note `recursion_limit` defaults to a number that is not a budget (appendix I) |
| MCP client | per-call wrapper around `call_tool` | `26`; the envelope cannot cross the wire, so send a deadline header and re-derive on the server |
| A2A / remote agent | task metadata | `27`; a remote agent that ignores your deadline needs a local hard timeout too |

Across a process boundary you lose enforcement and keep only a hint. Send the remaining budget as a
header (`x-deadline-ms`, `x-token-budget`), have the far side build its own envelope from
`min(its policy, your hint)`, and keep a local hard timeout for when it lies.

---

## 3. Admission control, priority classes and the degradation ladder

> **In plain words.** When more work arrives than you can serve, you must decide *which* work to
> drop, and in what order you give up quality. If you do not decide, the queue decides: everything
> gets slow, every request times out, and you pay for every token of every answer nobody waited for.

### 3.1 Why LLM traffic sheds differently

Three properties make the usual web-service answer wrong:

1. **Unit cost spreads over four orders of magnitude.** One request is 200 tokens, another is 200k.
   Concurrency limits alone ("128 in flight") do not bound either cost or load.
2. **Work is paid for whether or not anyone is waiting.** A streamed answer to a disconnected client
   costs full price. Shedding early is a cost control, not just a latency control.
3. **Time-to-first-token is minutes-scale at the tail**, so a queue that would be harmless at 50 ms
   service time becomes a deadline machine. A request admitted into a 40-deep queue at 8 s per
   service slot has already missed a 30 s deadline.

### 3.2 Deadline-aware shedding

The rule that matters: **never start work that cannot finish.** At admission, estimate service time
from the request's own shape (prompt tokens, `max_tokens`, model, current queue depth) and reject
immediately if `estimated_service_s > remaining_s`. A 503 at 5 ms is cheaper for everyone than a
timeout at 30 s that also burned 40k tokens.

```python
import asyncio, time


class Admission:
    """Per (tenant, class) concurrency + deadline-aware queue.

    LIFO under overload is deliberate: when a queue is backed up, the newest
    request has the most deadline left, so serving it is more likely to produce
    an answer somebody still wants. FIFO under overload serves requests whose
    callers have already given up.
    """

    def __init__(self, limit: int, queue_cap: int, p95_service_s: float):
        self._limit = limit
        self._sem = asyncio.Semaphore(limit)
        self._stack: list[tuple[float, asyncio.Future]] = []
        self._queue_cap = queue_cap
        self._p95 = p95_service_s
        self.shed = {"queue_full": 0, "deadline": 0}

    def _eta_s(self) -> float:
        """Crude but sufficient: queueing delay scales with queue depth per worker."""
        return self._p95 * (1 + len(self._stack) / self._limit)

    async def acquire(self, env) -> None:
        if self._eta_s() > env.remaining_s:
            self.shed["deadline"] += 1
            raise Exhausted("admission_deadline", env)
        if len(self._stack) >= self._queue_cap:
            self.shed["queue_full"] += 1
            raise Exhausted("admission_queue", env)
        if self._sem.locked():
            fut = asyncio.get_running_loop().create_future()
            self._stack.append((time.monotonic(), fut))   # LIFO: newest served first
            await fut
        await self._sem.acquire()
        # Re-check: the deadline may have passed while queued.
        if env.remaining_s <= 0:
            self.release()
            self.shed["deadline"] += 1
            raise Exhausted("deadline", env)

    def release(self) -> None:
        self._sem.release()
        while self._stack:
            _, fut = self._stack.pop()            # newest
            if not fut.done():
                fut.set_result(None)
                return
```

Two counters are non-negotiable: `shed_total{reason}` and `admitted_over_eta_total`. Without the
first you cannot tell overload from an outage; without the second your ETA model drifts silently.

### 3.3 Priority classes, and the provider tiers they map onto

Three classes are enough, and they map cleanly onto what providers now sell *(docs, 2026-10)*:

| Class | Example work | Deadline | Provider lever |
|---|---|---|---|
| `interactive` | chat, an agent a human is watching | seconds | Anthropic `service_tier="auto"` (uses Priority Tier capacity when the org has a commitment, falls back to standard); dedicated/provisioned capacity |
| `background` | enrichment, nightly summaries, re-embedding | minutes–hours | OpenAI **flex processing** (`service_tier="flex"`): priced at Batch API rates, slower, and may return `429 Resource Unavailable` — **not charged** when it does |
| `batch` | evals, backfills, bulk extraction | up to a day | Anthropic Message Batches (50% off; ≤100k requests or 256 MB per batch; most finish within 1 h, expire at 24 h; results kept 29 days); OpenAI Batch |

Facts worth knowing before you design around them *(docs, 2026-10)*:

- Anthropic's `service_tier` takes `"auto"` or `"standard_only"`, and the response's `usage` object
  reports which tier served the request — so tier choice is *measurable*, not an article of faith.
  With a commitment, `anthropic-priority-input-tokens-*` / `-output-tokens-*` headers expose the
  priority bucket separately from the standard one. A Priority Tier commitment is ITPM + OTPM + a
  duration (1, 3, 6 or 12 months) + a specific model version, targets 99.5% uptime, and overflows to
  standard rather than failing.
- A Message Batch request needs `max_tokens ≥ 1`; `max_tokens: 0` cache pre-warming is not supported
  inside a batch. The batch lane can also slightly overshoot a workspace spend limit because of
  concurrent processing — a real surprise if your budget alarm is tight.
- Batch is where the long-output beta lives: the `output-300k-2026-03-24` header raises `max_tokens`
  to 300,000 on batch requests only, not on the synchronous API.
- Flex's `429 Resource Unavailable` is **not** a quota signal. Retrying it with the same backoff
  policy you use for rate limits is wrong twice over: you are not over quota, and the fallback that
  actually works is re-submitting the request at the standard tier.

The design rule: **class is assigned at admission from the envelope, not chosen by the caller.** A
caller-supplied priority is a request, validated against what that tenant is entitled to.

### 3.4 The degradation ladder

Routing to a cheaper model is not a feature, it is rung four of a ladder you should write down before
the incident. Each rung is a step the system takes *by itself*, records in `envelope.degraded`, and
reports in the response metadata.

| Rung | Step | What it saves | What it costs | Trigger |
|---|---|---|---|---|
| 0 | Serve normally | — | — | — |
| 1 | Serve from exact cache | everything | staleness | identical request in window |
| 2 | Serve from semantic cache (`23` §11) | everything | wrong-answer risk at low thresholds | high similarity hit |
| 3 | Trim context: fewer chunks, drop history, skip rerank | input tokens, 100–300 ms | recall, then faithfulness (`04`, `06`) | input budget or deadline pressure |
| 4 | Route to a smaller model | 5–20× unit cost | accuracy, instruction-following, schema compliance (`07`) | tenant over soft budget, or provider 429 |
| 5 | Drop optional rails to shadow mode | guardrail latency | detection coverage — **never** the injection and PII rails (§6.3) | deadline pressure only |
| 6 | Single-shot instead of agentic: no tools, retrieval only | the whole loop | multi-hop questions fail | loop caps near exhaustion |
| 7 | Enqueue as `background` and return a ticket | capacity now | synchronous UX | interactive capacity exhausted |
| 8 | Abstain with a reason | everything | the answer | all of the above exhausted |

Two disciplines make a ladder real rather than aspirational: it is **monotone** (you never climb back
up mid-request, so behaviour stays explainable), and rung ≥ 3 is **visible** — the response says it
was degraded, the span carries `degraded=[...]`, and your eval set has a slice for each rung. A
degradation nobody can see is a silent quality regression, which is the single hardest class of
failure to find later. Treat each rung as an eval slice (`08`).

---

## 4. The 429 contract and the two ceilings

> **In plain words.** A 429 is not one thing. "Too many tokens this minute" clears in seconds; "you
> hit your monthly spend cap" never clears on its own. Retrying both the same way turns a billing
> problem into an outage, and an outage into a retry storm.

### 4.1 Read the headers, don't guess

*(docs, 2026-10)*

| Provider | What it tells you | Shape |
|---|---|---|
| Anthropic | RPM, **ITPM and OTPM separately**, per model class | `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}`; `reset` is RFC 3339; remaining token counts are rounded to the nearest thousand |
| Anthropic | when to retry | `retry-after` in seconds — **not on every 429**; the spend-cap 429 never carries it |
| Anthropic | sudden-ramp rejections | "acceleration limits" fire on a sharp usage increase even under your stated limit: ramp gradually |
| OpenAI | requests and tokens, org- and project-scoped | `x-ratelimit-{limit,remaining,reset}-{requests,tokens}`, plus `x-ratelimit-*-project-tokens`; `Retry-After` when present |
| OpenAI | spend | a spend *alert* notifies and traffic continues; a **hard spend limit** returns 429 |

Consequences for your retry wrapper:

1. **Honour `retry-after` exactly** when present. Earlier retries are rejected, so they are pure
   waste that also counts against RPM.
2. **Classify the 429 before retrying.** Quota-reset 429 → backoff with jitter. Spend-cap or
   hard-limit 429 → open a circuit, page a human, and do not retry: nothing will change this month.
   Flex `Resource Unavailable` → retry at a different tier, not after a longer sleep.
3. **Separate input and output limits.** An OTPM-shaped failure is fixed by lowering `max_tokens` or
   splitting the generation; an ITPM-shaped one by caching the prefix and trimming context. Treating
   both as "slow down" hides the cheap fix.
4. **Budget retries from the envelope.** `env.slice_for()` before every attempt; a retry that cannot
   finish before the deadline must not be sent. Retries consume the same quota as primary traffic
   (`07` §7), which is exactly how a 50%-capacity blip becomes a 4× self-inflicted load
   spike at three retries with no jitter.

### 4.2 Two ceilings, always

Your per-tenant bucket and the provider's org/model allocation are independent constraints. `23` §6.5
has the full treatment; the operational summary is: a tenant can be well inside their quota and still
get a provider 429 because aggregate traffic to that model exceeded the org allocation. You need a
shared bucket per `(provider, model)` *in addition to* the per-tenant one, and the shared one is the
thing that must be reconciled against the live headers — your model of the upstream ceiling drifts,
theirs does not.

The fairness question follows immediately: when the shared ceiling binds, whose request is rejected?
Pick one and write it down — strict priority (interactive wins), weighted fair share by paid tier, or
work-conserving borrowing with a hard cap. "Whoever got there first" is the default and it is the one
that lets a batch backfill take out your chat product.

---

## 5. Token optimization, ordered by payoff

> **In plain words.** Most token "optimization" advice is about writing shorter prompts, which is the
> smallest lever there is. The big levers are reusing the same prefix, not sending tool results you
> do not need, and running deferrable work on a discounted lane.

Order matters here: do them top-down, because the cheap ones change the arithmetic of the expensive
ones. Everything below is a *mechanism*; the measured effect on your corpus is a lab (§12).

| # | Lever | Typical effect on the bill | Cost to adopt | Where |
|---|---|---|---|---|
| 1 | Cache-aware prompt layout + prompt caching | largest single lever on repeated prefixes; reads bill at 0.1× base input (0.05× or 0.025× on some models) | low — reorder the prompt | §5.2, `06` §12, `30` §10 |
| 2 | Move deferrable work to batch / flex | 50% off list on that traffic | medium — needs an async path | §3.3 |
| 3 | Stop sending tool results you do not read | proportional to result size, often the largest term in agent loops | medium | §5.3, `30` §5 |
| 4 | Defer tool definitions | the tool-definition tax, every request | low | §7.2, `30` §2–§3 |
| 5 | Clear or compact stale turns server-side | unbounded growth on long threads | low | §5.4 |
| 6 | Retrieval-side: fewer, better chunks | input tokens, and often *better* answers | medium — needs eval | `04`, `06` §3 |
| 7 | Cap and shape output | output tokens are 3–5× input price | low | §5.5 |
| 8 | Route by difficulty | 5–20× unit cost on easy traffic | high — needs routing eval | `23` §4 |
| 9 | Semantic cache | whole calls | high — correctness risk | `23` §11, `05` |

### 5.1 The two prices you actually pay

Only two quantities drive the bill: **how many tokens cross the boundary** and **what multiplier each
one carries**. Every lever above moves one of them. Keep this table (your provider's version of it)
next to the code that picks a model:

| Token class | Multiplier on base input price *(Anthropic, docs 2026-10)* |
|---|---|
| Fresh input | 1× |
| 5-minute cache **write** | 1.25× |
| 1-hour cache **write** | 2× |
| Cache **read** | 0.1× (0.05× on Opus 5.5 / Sonnet 5.5; 0.025× on Fable 5.1 / Mythos 5.1) |
| Batch | 0.5× — **stacks** with the above and with long-context pricing |

Stacking is the part people miss: a cache read inside a batch request is `0.1 × 0.5 = 0.05×` base
input. That is the difference between "we cannot afford to re-run the eval set" and running it nightly.

### 5.2 Cache TTL break-even, as arithmetic

Let `P` be the cached prefix size and `p` the base input price per token. A read costs `0.1 p P`, a
5-minute write `1.25 p P`, a 1-hour write `2 p P`. Reads refresh the TTL.

**Case A — traffic denser than the TTL.** Every call after the first is a read, so the 5-minute TTL
wins outright: one write at 1.25×, then 0.1× forever. An agent loop is almost always this case.

```
# 20k-token stable prefix, base input $3/M, 30 model calls in one session, all within 5 min:
#   no cache : 30 × 20,000 × $3/1M                = $1.80
#   5-min    : 1.25 × $0.06 + 29 × 0.1 × $0.06    = $0.249   (~7.2× cheaper on the prefix)
# On a model with 0.05× reads the same session is $0.162 (~11×).
```

**Case B — gaps longer than 5 minutes.** Now the 5-minute TTL pays a fresh write every time, and the
1-hour TTL amortizes one expensive write over `n` calls:

```
5-minute TTL : n × 1.25 p P
1-hour  TTL  : 2 p P + (n − 1) × 0.1 p P
equal when   : 1.25 n = 2 + 0.1 (n − 1)  →  1.15 n = 1.9  →  n ≈ 1.65
```

**So the 1-hour TTL already wins from the second reuse within the hour.** The instinct that it needs
"heavy traffic to be worth 2×" is wrong by an order of magnitude; what it needs is *any* second call.
The real question is whether the prefix survives unchanged that long — a single edited token at
position 0 invalidates everything after it.

Three rules follow, and they are cheap to enforce at chokepoint B:

- **Static first, volatile last.** System prompt, tool definitions, policy text, long documents, then
  history, then the user turn. One mutable timestamp near the top destroys the whole prefix.
- **Cache breakpoints go at stability boundaries**, not at pretty section breaks.
- **Measure hit rate, not cache usage.** `cache_read_tokens / (cache_read + cache_creation)` per
  route, per day. A route at 15% has a prefix-stability bug, not a caching problem.

Other providers differ in shape but not in principle: Gemini's implicit caching is on by default for
2.5-and-newer models with a per-model minimum prompt size (2,048 tokens on 2.5 Flash/Pro; 4,096 on
the Gemini 3.x/4 families), reports `usage.total_cached_tokens`, and rewards putting large common
content at the front — while *explicit* cache objects are no longer available in the newest API
surfaces *(docs, 2026-10)*. If your gateway normalizes across providers, normalize the *usage fields*
too, or your savings reporting silently diverges per provider.

### 5.3 The biggest agent-loop lever: results you never read

In a tool-calling loop, the whole transcript is resent every turn, so a 50 KB tool result is paid for
on every subsequent turn, not once (`24` §15 has the quadratic-ish arithmetic). Three mechanisms,
cheapest first:

1. **Truncate at the tool boundary** with a documented contract: `max_characters` on the tool, a
   stable "truncated, N rows omitted, refine your query" marker, and the full result kept in a store
   the model can re-fetch by id. This is a chokepoint-D control and costs nothing.
2. **Server-side context editing.** Anthropic's `clear_tool_uses_20250919` edit (beta header
   `context-management-2025-06-27`) clears old tool results once input tokens cross a trigger,
   keeping the last `keep` tool uses, with `clear_at_least` and `exclude_tools` for control. The
   token-counting endpoint accepts the same `context_management` block and returns both
   `input_tokens` and `context_management.original_input_tokens`, so **you can price the edit before
   you ship it** *(docs, 2026-10)*. Note the interaction with §5.2: clearing the middle of a prompt
   invalidates the cache from that point, so a too-eager trigger trades a 0.1× read for a 1.25× write
   on everything after the cut.
3. **Keep intermediate results out of context entirely.** With programmatic tool calling, the model
   writes code that calls your tools inside a sandbox; intermediate tool results do not count toward
   token usage at all — only the final code-execution result and the response do. It is a strong fit
   for fan-out over many items and for filtering large results, and a weak fit for strictly
   sequential reasoning or a couple of small calls, where container and script overhead exceed the
   savings. The docs' own advice is the right engineering instinct: **measure billed input tokens
   with and without it on your real traffic** *(docs, 2026-10)*. `30` §5 covers the same idea as
   "code mode", with the context-engineering reasoning.

### 5.4 Long threads

A conversation that grows without bound is a cost bug with a quality bug attached (`06` §8, `30` §8).
The control-plane view: pick *one* owner for history length — your compaction policy, or the
provider's context editing, not both. Two independent trimmers produce prompts that are neither
cache-stable nor reproducible, and you will not be able to attribute a regression to either.

### 5.5 Output tokens

Output is the expensive side (commonly 3–5× input per token, before cache discounts). Three controls
that do not cost quality: a per-route `max_tokens` derived from `envelope.output_budget` rather than a
global default; a response schema that does not restate the input (`07`); and no "think step by step
in your answer" when a reasoning mode or a separate cheap pass does it better. The anti-control is
shaving `max_tokens` below what the task needs: you pay for the truncated attempt *and* the retry,
and on some providers unused `max_tokens` still counts against your output-token-per-minute ceiling.

### 5.6 Six fields, or your savings are a story

Log these per model call, joined to `trace_id` and `tenant`:

`model`, `service_tier`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens` — plus computed `usd` and the `degraded` list.

With those you can answer the only three questions that matter: which route spends the money, what
fraction of input is cache reads, and whether a change moved the bill or moved it between routes.
Without `cache_read` vs `cache_creation` split out you cannot distinguish "caching works" from
"caching is thrashing", and both look like "we enabled caching". `23` §7 and §9 cover the attribution
plumbing; `../sre-observability/26-llm-and-ai-observability.md` covers the span shape.

---

## 6. Guardrails: placement, products, latency budget

> **In plain words.** A guardrail is a check at a chokepoint with a decision attached: allow, mask,
> block, or log. The hard parts are not the classifiers — they are *where* each check runs, what
> happens when the checker is down, and how much latency you are willing to pay.

### 6.1 Five stages, five different jobs

NVIDIA's NeMo Guardrails names the stages well — **input, retrieval, dialog, execution, output** rails
*(docs, 2026-10)* — and the names transfer even if you never run it. Map yours onto chokepoints:

| Stage | Chokepoint | Checks | Chapter |
|---|---|---|---|
| Input | B | injection patterns, jailbreak classifier, PII in the question, topic allow-list | `17` §4, §6 |
| Retrieval | B | source trust, injected instructions inside retrieved chunks, tenant leakage | `17` §5 |
| Dialog | B/C | what the assistant is allowed to discuss or promise | `17` §10 |
| Execution | D | tool authz, argument validation, blast-radius checks before the side effect | `17` §9, `24` §6 |
| Output | E | harmful content, PII, citation support, schema | `17` §7–§8 |

The one people skip is **retrieval**, and it is the one that matters most for RAG: the input rail sees
a benign question, and the injection arrives inside a document that passed ingestion months ago.

### 6.2 The managed products, 2026

You do not have to build classifiers. What you must decide is what runs where, and what each product
can and cannot see. *(docs, 2026-10)*

| Product | Shape | Notable mechanics |
|---|---|---|
| **Amazon Bedrock Guardrails** | managed policy object, versioned drafts + test window | content filters (Hate, Insults, Sexual, Violence, Misconduct, **Prompt Attack**) with per-category strength, plus denied topics, word filters, sensitive-information and contextual-grounding policies; usable **without a model** via `ApplyGuardrail`; input *tagging* lets you evaluate only the user's text and skip system instructions, retrieved results and few-shots — SDK only, not the console |
| **Azure AI Content Safety — Prompt Shields** | detector for attacks | separates **user prompt attacks** from **document attacks**; configurable intervention points (user input, and in preview, tool responses for Foundry Agent Service agents); **Spotlighting** (preview) marks third-party document content so embedded instructions are treated as data |
| **Google Model Armor** | templates + org-level floor settings | per-category confidence thresholds (High / Medium-and-above / Low-and-above) for RAI categories, prompt-injection and jailbreak detection, and sensitive-data protection; enforcement is **inspect-only** or **inspect-and-block**; floor settings impose a minimum on every template in the hierarchy |
| **Llama Guard 4 (12B)** | open-weight classifier you host | multimodal (text + multiple images), pruned from Llama 4 Scout; labels prompts *and* responses against the MLCommons hazard taxonomy S1–S13 plus **S14 Code Interpreter Abuse** (text only) — the category that exists because of agents |
| **NeMo Guardrails** | library + server | the five rail stages, Colang flows for dialog rails, custom Python actions |
| **Guardrails AI** | Python validator framework | validators over inputs and outputs, including structure |
| **Gateway-level hooks** (LiteLLM and peers) | config, not code | guardrails attach per deployment with a `mode`: `pre_call`, `post_call`, `during_call` (runs *in parallel* with the model call; the response is withheld until the check returns), or `logging_only` (scan without changing the client response) |

Two selection rules. **Prefer the rail that sees the real bytes**: a provider-side filter cannot
inspect what your gateway rewrote afterwards, and a gateway rail cannot inspect what a tool returned
inside a sandbox. And **prefer a product with a standalone API** (`ApplyGuardrail`-style, or a local
classifier) for the stages that are not a model call — retrieval screening and tool-result screening
have no model call to attach to.

### 6.3 Fail-open or fail-closed, decided per rail

This is the decision teams postpone until the classifier's first outage decides it for them.

| Rail | Default | Why |
|---|---|---|
| Injection / jailbreak detection | **fail-closed** for tool-enabled or write-capable paths; fail-open for read-only Q&A | with tools, a bypass is an action; without, it is a bad answer |
| PII redaction (output) | **fail-closed** | a leak is irreversible and reportable |
| Tool authorization | **fail-closed**, always | this is authz, not a heuristic |
| Content safety | fail-open **with** a shadow log and an alert | availability usually outranks a marginal toxicity call, and you keep the evidence |
| Citation / grounding check | fail-open, mark the answer unverified | the honest degradation (`06` §10) |

Write the table for your system, put it next to the code, and make the behaviour testable — a unit
test per rail that asserts the decision when the checker raises. The policy version in the envelope is
what makes a past decision explainable six months later.

### 6.4 The latency budget

Rails are not free, and `17` §13 has the cost model. The control-plane question is *serialization*:

| Arrangement | Added p95 | When |
|---|---|---|
| Inline before the model | full checker latency | input rails that can block (cheap classifiers, regex, allow-lists) |
| In parallel with the model call (`during_call`) | `max(0, checker − model)` ≈ 0 | input rails whose verdict is only needed before *returning* |
| Streaming output rail | buffered chunks only | output filtering without killing time-to-first-token |
| Shadow (`logging_only`) | 0 on the response path | tuning a new rail on real traffic |
| Post-hoc, off the request path | 0 | expensive judges, sampled audit |

The rollout order that avoids the usual disaster: **shadow → threshold tuning on a labelled slice →
block**, with the thresholds chosen on your own data. Vendor guidance is explicit that the most
sensitive setting is a false-positive machine in production — Model Armor's own docs recommend
starting at High or Medium-and-above for injection/jailbreak and safety categories rather than
Low-and-above *(docs, 2026-10)*. A rail that blocks 2% of legitimate traffic will be turned off
entirely by the first product manager who notices, which costs you the whole rail instead of some
precision.

Guardrails also do not solve prompt injection. They raise the cost of an attack. `17` §3–§4 and `28`
are the real treatment; design the blast radius (the Agents Rule of Two, capability-scoped tools) as
if the rails were not there.

---

## 7. Tool governance: the registry as a control surface

> **In plain words.** The list of tools an agent can see is both a security boundary and a token
> bill. The same registry that decides "may this user call `refund`" also decides how many tokens of
> definitions you pay for on every single request.

### 7.1 What the registry must hold

`24` §5 has the registry; the fields the control plane needs from it are: owner, version, required
scopes, danger class (read / write / irreversible), idempotency semantics, inbound rate limit,
outbound rate limit of the system behind it, average and p95 result size, and token cost of the
definition. The last two are the ones usually missing, and they are what make tool choice a budget
decision rather than a vibe.

### 7.2 Filtering is a budget control, not only an authz control

Every tool definition in the request is input tokens on every turn. Anthropic's own thresholds for
reaching for the tool search tool are a usable rule of thumb *(docs, 2026-10)*: **10+ tools, or
definitions over 10k tokens, or aggregating MCP servers (200+ tools), or tool-selection accuracy
falling as the toolset grows**; and standard tool calling stays better below 10 tools or under ~100
tokens of definitions total. Two mechanics worth knowing precisely, because they are easy to
misconfigure:

- `defer_loading: true` controls **what enters the context window, not what you send**. You still
  transmit every definition on every request; the API needs them server-side to run the search and
  expand references. So it is a context lever, not a bandwidth one.
- Never defer the search tool itself (that is a 400), and keep your **3–5 hottest tools
  non-deferred** so the common path does not pay a search round-trip.

`30` §2–§3 is the full treatment with the context-engineering reasoning. The control-plane addition is
simply: the filtered tool list must be derived from the same registry and principal that authz uses.
Two independent lists — one for permissions, one for prompt assembly — drift, and the drift shows up
as a model happily calling a tool it is not allowed to use, which is then denied at D, which the
model retries, which burns the loop budget.

### 7.3 Per-tool limits in both directions

`24` §12.2 makes the distinction that matters: **inbound** (how often may this principal call this
tool) and **outbound** (what does the system behind the tool tolerate). Both belong in the registry
and both must surface as a *model-visible* error — "rate limited, retry after backoff" as a tool
result, not a silent failure, or the agent reruns the same call until the loop cap kills the request.
And a tool-level limit is the only control that stops one agent from DoS-ing an internal service that
has no idea what an LLM is.

---

## 8. The gateway layer in 2026: AI, MCP, agentic

> **In plain words.** Three different kinds of traffic now get their own proxy: calls to model APIs,
> calls to MCP tool servers, and calls between agents. Some products do one, some do all three. What
> you are buying is a place to put policy that is not inside every application.

### 8.1 Three planes, three jobs

| Plane | Terminates | Policy it enforces | Chapters |
|---|---|---|---|
| **AI / LLM gateway** | provider HTTP APIs, normalized | model routing, fallback, token quotas, cost attribution, caching, key custody, guardrail hooks | `23` |
| **MCP gateway** | MCP sessions over Streamable HTTP / SSE / stdio | tool filtering, aggregation of N servers behind one endpoint, OAuth, upstream credential injection, per-tool authz and limits | `26` |
| **Agentic gateway** | agent-to-agent calls (A2A and friends) | agent identity, delegation, task lifecycle, egress control | `27`, `29` |

A thing to notice before shopping: the MCP plane is where the *context* budget and the *security*
boundary coincide (§7.2), and the agentic plane is where identity stops being an API key and becomes
delegation (`29`). They are not the same product problem, which is why the landscape is consolidating
into proxies that speak all three.

### 8.2 What is actually out there *(project docs and repositories, 2026-10)*

| Project | Shape | Planes | Notable |
|---|---|---|---|
| **Agent Router** (formerly **Envoy AI Gateway**; joined the Agentic AI Foundation on 10 September) | Envoy data plane + Kubernetes CRDs, docs at v1.2 | LLM + MCP | token-based limits on Envoy Gateway's Global Rate Limit API with cost classes `InputToken`, `CachedInputToken`, `OutputToken`, `TotalToken` and `CEL` for custom formulas; model name extracted from the request body into `x-ai-eg-model` for per-model limits; `MCPRoute` gives server aggregation, tool filtering, server multiplexing, header forwarding, OAuth and authorization policies |
| **agentgateway** | standalone Rust proxy, Apache-2.0, in the Agentic AI Foundation | LLM + MCP + A2A | "agentic proxy" positioning: one data plane for agent-to-tool and agent-to-agent traffic |
| **IBM ContextForge** (`mcp-context-forge`) | Python registry + proxy | MCP + A2A + REST/gRPC | federates MCP servers *and* virtualizes legacy APIs as MCP tools (including gRPC via reflection); virtual servers, plugins, admin UI, user-scoped OAuth, built-in retries and rate limiting, OTel export, Redis caching, multi-cluster federation |
| **Docker MCP Gateway** | local/containerized gateway over the Docker MCP catalog | MCP | each server runs as a container, so isolation and secret handling are the container's, not your laptop's; one endpoint for many clients |
| **LiteLLM proxy** | Python OpenAI-compatible proxy | LLM (+ guardrail hooks) | virtual keys, team budgets, RPM/TPM limits, guardrails with `pre_call`/`during_call`/`post_call`/`logging_only` modes |
| **Kong / Cloudflare / cloud-native API gateways** | existing API gateway + AI plugins | LLM (+ MCP in newer releases) | the right answer when an API gateway is already the org's policy point; semantic caching and token-aware limits as plugins |
| **Portkey and hosted peers** | SaaS gateway | LLM | routing, fallbacks, guardrails, budgets without running infrastructure |

Vendor benchmark claims in this space (X% faster than Y) are marketing until reproduced on your
traffic shape; treat them as `README` §6 unlabelled numbers and run §12's L.6 instead.

### 8.3 The semantic difference that decides your quota behaviour

Read this before adopting any token-limiting gateway, because two defensible designs produce very
different failure modes:

- **Charge-after-response** (Agent Router's documented behaviour): a request is admitted by checking
  usage *already charged*; the gateway then extracts usage from the completed response and charges
  it. Simple, exact, no estimation error — but **one in-flight request can overshoot the limit
  arbitrarily**, and a burst of concurrent requests is all admitted against a stale balance.
- **Reserve-then-settle** (`23` §6.4): estimate input tokens, reserve against `max_tokens` or a
  learned p95 output, then refund the difference. Bounds overshoot and makes concurrency safe — at
  the cost of estimation error, which makes effective quotas conservative when every request reserves
  a worst case that rarely happens.

Neither is wrong. The choice is: do you care more about *never* exceeding an upstream ceiling
(reserve) or about never under-serving a tenant whose requests are mostly small (charge-after)? If
your provider allocation is the binding constraint and the penalty is a 429 storm, reserve. If your
own soft budget is the binding constraint and the penalty is a Slack message, charge-after is less
machinery. What you must not do is assume your gateway does the one you had in mind — check the docs,
then verify with a burst test (§12, L.2).

### 8.4 Build, buy, or neither

| Signal | Action |
|---|---|
| One app, one provider, one team | **No gateway.** A client wrapper plus §9.1's six controls. A gateway here is a second system to operate for zero policy benefit |
| Two or more providers in production, or a fallback requirement | Library-level abstraction first (`23` §3); a gateway when routing policy changes faster than deploys |
| Three or more teams sharing one provider allocation | **Gateway**, for the two-ceiling problem (§4.2) alone |
| Per-tenant billing or chargeback | Gateway, or an ingest path that cannot be bypassed |
| More than ~3 MCP servers, or MCP servers reachable by more than one agent | **MCP gateway**: tool filtering, one credential custody point, one audit log |
| Auditors asking "who called what, under which policy version" | Gateway + registry + policy versioning (§2.1) |
| Agents calling agents across team or company boundaries | Agentic plane: identity and delegation first (`29`), proxy second |

What a gateway will never do for you: define idempotency semantics for your tools, authorize at the
level of business objects ("may this user refund *this* order"), decide what good looks like
(`08`), or stop a loop that is making legal-but-useless calls. Those stay in the application, which
is exactly why §2's envelope lives there too.

---

## 9. Two reference configurations

### 9.1 Three engineers, one product: the minimum viable control set

In order. Each step is hours, not weeks, and each one prevents a failure that has a bill attached.

1. **Provider-side hard spend limit and a spend alert.** The only control that works while everyone
   is asleep. Know which one 429s and which one only notifies (§4.1).
2. **The envelope (§2), even if only deadline + token budget + tool-call cap + depth.** ~100 lines.
   One shared ledger. This is the step that prevents the pathological-loop story.
3. **A retry wrapper that classifies 429s** and honours `retry-after`, with jitter and a deadline
   check before each attempt (§4.1).
4. **One concurrency semaphore per model**, sized from the published TPM and your median request
   size, not from your instance count.
5. **Cache-aware prompt layout** (static prefix first) and a 5-minute cache breakpoint. Then look at
   `cache_read / (cache_read + cache_creation)` once a day for a week (§5.2, §5.6).
6. **Two guardrails, fail-closed: tool authorization, and output PII.** Plus one injection
   classifier in shadow mode so you have data before you have an incident (§6.3).
7. **One usage table**, six fields plus cost and outcome (§5.6). In the same database as your evals,
   so cost and quality can be joined.
8. **A written degradation ladder** with at least rungs 3, 4 and 8 implemented (§3.4).

What is *not* on the list at this size: a gateway, a semantic cache, a policy engine, per-tenant
fairness, a registry service. Every one of them is real work with an operator; none of them prevents
a failure you have yet.

### 9.2 Enterprise: the same controls, with a place to put them

| Layer | What runs | Owner |
|---|---|---|
| Ingress | authn, tenant resolution, envelope mint, admission + priority class, audit start | platform |
| AI gateway | provider abstraction, routing, two-ceiling limits, cost attribution, key custody, gateway-level rails | platform |
| Guardrail service | input/retrieval/output rails, standalone API for non-model stages, shadow pipeline, threshold registry, org-level floor settings | security |
| Tool / MCP plane | registry, MCP gateway, per-tool authz and limits, sandboxes, credential injection | platform + owning teams |
| Application | envelope propagation, degradation ladder, idempotency, business-object authz, compaction policy | app team |
| Finance / FinOps | budgets per tenant and team, chargeback, commitment and batch-lane planning | finance + platform |
| Eval & observability | traces, cost joins, rung slices, guardrail precision/recall tracking | app team + platform |

Three enterprise-only mechanisms worth the money: **org-level floor settings** so a new project
cannot ship with rails off (Model Armor's floor settings are the clearest implementation of the idea);
**priority-tier commitments** for the interactive class with explicit fallback to standard, so a
capacity event degrades instead of failing; and a **batch lane with its own budget**, so evals and
backfills can never compete with production traffic.

### 9.3 The migration order

Envelope → usage table → retry/429 discipline → caching layout → guardrail shadow → ladder →
*then* gateway → registry → policy as code → per-tenant fairness. Teams that start at "gateway"
end up with a well-instrumented proxy in front of applications that still have no deadline
propagation, and the first runaway loop goes straight through it: the gateway sees 400 legal
requests, each inside quota, and nothing is wrong except the bill.

---

## 10. Failure modes of the control plane itself

| Symptom | Cause | Fix |
|---|---|---|
| Cost per request 10× the model, randomly | no deadline propagation; a sub-agent inherited a fresh budget (copied ledger) | share the ledger by reference; assert `child.ledger is parent.ledger` in a test |
| 429s spike to a plateau and stay | retries without jitter, three deep, amplifying load 4× at the exact moment capacity is short | classify the 429, honour `retry-after`, jitter, circuit-break |
| Spend-cap 429 retried forever | treating a billing 429 as a rate 429 — it carries no `retry-after` and will not clear | separate classifier branch; page, don't retry |
| Tenant "under quota" but failing | only one ceiling modelled | shared `(provider, model)` bucket reconciled from live headers (§4.2) |
| Quota drains with no traffic | reserve leaked: client disconnected, `settle` never ran | settle in `finally`; reaper over reservation ids with a TTL |
| Cache hit rate collapses after a deploy | a timestamp, a user name or a tool list moved into the prefix | prefix hash in the span; alert on hash churn per route |
| Cache hit rate collapses mid-session | context editing or compaction cutting above a breakpoint (§5.3) | raise the trigger, or move the breakpoint below the mutable region |
| Latency doubles after enabling a rail | inline checker that could have run in parallel or in shadow | §6.4's arrangement table |
| An outage of a classifier takes the product down | fail-closed chosen by accident on a content rail | the §6.3 table, with a test per rail |
| Guardrail decisions unexplainable later | no policy version in the trace | `policy_version` in the envelope and in every span |
| Replicas disagree on limits | per-process buckets | one shared store (Redis + Lua, or the gateway) for anything enforced across replicas |
| p99 fine, users complain | FIFO queue serving requests whose callers left | deadline-aware shedding, LIFO under overload (§3.2) |
| Quality dropped and nobody knows when | degradation invisible: rungs not recorded | `degraded` in the response and the span; an eval slice per rung |
| Agent retries a denied tool until the cap | authz list and prompt tool list derived separately | one registry, one principal, one derivation (§7.2) |
| Batch backfill took out chat | no priority classes; shared concurrency and shared quota | classes at admission, separate budgets (§3.3) |

---

## 11. Anti-patterns

- **Request-count rate limiting for LLM traffic.** One request is 200 or 200k tokens. Limit tokens;
  keep request counts only as an abuse guard (`23` §6.2).
- **Per-node budgets in an agent graph.** Ten nodes with a 10k budget each is a 100k request. One
  ledger per request.
- **Timeouts without a deadline.** See §2.2's 40-minute request.
- **Routing to a cheap model as the first cost lever.** Caching and not resending tool results are
  larger, safer and do not need a routing eval (§5).
- **Shrinking `max_tokens` to save money.** You pay for the truncated attempt and the retry.
- **One guardrail mode for everything.** Injection on a write-capable path and toxicity on a FAQ bot
  do not deserve the same fail mode (§6.3).
- **Turning on the most sensitive thresholds "to be safe".** The rail gets switched off within a
  month, and you lose all of it instead of some precision (§6.4).
- **Trusting the input rail to stop injection in retrieved documents.** Different stage, different
  rail (§6.1).
- **A gateway as the first control.** It cannot see inside your loop, which is where the money goes
  (§9.3).
- **Aggregating 200 MCP tools behind one endpoint and sending them all.** You just added a five-digit
  per-request token tax and made tool selection worse (§7.2).
- **Enabling caching and never measuring hit rate.** Thrashing and working look identical in the
  invoice summary (§5.6).
- **Letting callers choose their own priority.** Everything becomes interactive.
- **A degradation ladder in a design document and not in the code.** During the incident, the ladder
  that exists is the one with the code.

---

## 12. Lab exercises

Written for a laptop, local models and a fake provider where a real one would cost money. Predict
each number before you measure it, and write both down (`LABS.md` rules).

**L.1 — The envelope, and the copied-ledger bug (Core).**
Implement §2's `Envelope`. Build a two-level agent: parent, two sub-agents, three tool calls each,
with a fake model client that charges 1,000 input and 200 output tokens per call. Run it twice: once
with the ledger shared, once with `ledger=Ledger()` in `child()`. *Predict* total charged tokens and
the number of calls in each case. *Verify* both, then add the test that would have caught it.

**L.2 — Charge-after vs reserve-then-settle under a burst (Core).**
Implement both limiters against a 100k-token-per-minute bucket. Fire 20 concurrent requests of 10k
estimated input and `max_tokens=4000`, where 19 return 200 output tokens and one returns 4,000.
*Predict* peak overshoot above the ceiling for each design, and the number of rejections. *Verify*,
and write one sentence on which you would put in front of a provider allocation (§8.3).

**L.3 — Cache TTL break-even (Core).**
Reproduce §5.2's arithmetic in code for your own prefix size and your provider's current
multipliers, then plot cost against inter-arrival gap for 5-minute and 1-hour TTLs. *Predict* the
crossover `n`. *Verify* against the closed form. *(Optional, paid)* run 10 real calls at each TTL and
compare `cache_read_tokens` / `cache_creation_tokens` against the model.

**L.4 — Deadline-aware shedding vs FIFO (Core).**
Fake model server: 8 s p95 service time, 4 workers. Offer 2× capacity for 60 s with 30 s client
deadlines. Compare FIFO-with-unbounded-queue, FIFO-with-cap, and §3.2's deadline-aware LIFO.
*Predict* goodput (answers delivered before the deadline) and wasted tokens for each. *Verify* all
three, and report wasted spend as a percentage.

**L.5 — The tool-definition tax (Core).**
Take 50 real tool schemas (or generate them from an OpenAPI spec). Count tokens for the full set, for
5 hot tools, and for a deferred-loading configuration, using a token counter rather than character
counts. *Predict* tokens per request and the cost over a 30-turn session at your prices, with and
without cache reads. *Verify*, then check selection accuracy on 20 queries with the full set vs the
filtered set (`30` §3).

**L.6 — Guardrail arrangement (Core).**
One local classifier (any small model or `Llama-Guard`-style prompt, or a regex stand-in) as an input
rail. Measure end-to-end p50/p95 with the rail inline, in parallel with the model call, and in shadow
mode. *Predict* the added p95 for each. *Verify*, then force the classifier to raise and confirm your
fail-open/fail-closed behaviour matches your §6.3 table — that assertion is the actual deliverable.

**L.7 — 429 discipline (Core).**
Fake provider that returns 429 with `retry-after: 3` for 10 s, then 200; and a second mode that
returns a spend-cap 429 with no `retry-after`, forever. Run a naive exponential-backoff client and a
classifying client. *Predict* requests sent, total wall time, and whether each terminates. *Verify*
both modes.

**L.8 — MCP gateway tool filtering (Stretch).**
Run two local MCP servers plus a gateway (Docker MCP Gateway or Agent Router's `MCPRoute`, or
ContextForge). Point one client at the servers directly and one at the gateway with a tool filter.
*Predict* the tool count and definition tokens the client sees in each case. *Verify*, and confirm
that a filtered-out tool cannot be called even when named explicitly.

**L.9 — Price the degradation ladder (Stretch).**
Implement rungs 3, 4, 6 and 8 behind one flag. On 50 golden-set questions (`labs/golden-set/`),
measure cost, latency and answer quality at each rung. *Predict* the cost ratio rung-0 to rung-6.
*Verify*, and produce the table you would show a product owner deciding what to sacrifice.

**L.10 — Context editing against cache (Stretch).**
With a long tool-heavy transcript, use a token-counting endpoint (or a local equivalent) to price
`clear_tool_uses` at several triggers. *Predict* the trigger value at which cache invalidation costs
more than the clearing saves. *Verify*, and state the rule you would ship.

**Checkpoint (closed book):**
1. Why must a deadline be absolute rather than per hop?
2. From how many reuses within an hour does a 1-hour cache TTL beat a 5-minute one, and why is the
   answer so small?
3. Which two 429s must never share a retry policy?
4. Name the two independent ceilings a multi-tenant system has to model.
5. Which guardrail stage does a RAG system most often forget?

---

## 13. Interview questions

**Q1. An agent request occasionally costs 40× the median. Everything is "within limits". Where do you
look first?**
At whether limits are per call or per request. Per-call caps multiply by fan-out: ten nodes with a 10k
budget is a 100k request, and a sub-agent with a fresh ledger multiplies again. Then check deadline
propagation — relative per-hop timeouts compose multiplicatively (§2.2) — and look for retries, which
are invisible in a per-call view and consume the same quota as primary traffic.

**Q2. Your gateway limits tokens per minute per tenant. A tenant under quota is getting 429s from the
provider. Explain.**
Two ceilings. The provider allocates per org and per model; the gateway's aggregate traffic to that
model can exceed it while every tenant is individually inside quota. Add a shared `(provider, model)`
bucket reconciled against the live rate-limit headers, checked in addition to the per-tenant bucket
(§4.2, `23` §6.5).

**Q3. Pick between charge-after-response and reserve-then-settle token limiting.**
Reserve-then-settle when the binding constraint is an upstream allocation and the penalty is a 429
storm: it bounds overshoot and makes concurrency safe, at the cost of conservative effective quotas
from estimation error. Charge-after-response when the binding constraint is your own soft budget: it
is exact and needs no estimator, but a single large request or a concurrent burst can overshoot
arbitrarily because admission is decided against an already-charged balance (§8.3).

**Q4. What is the first thing you would do to cut an LLM bill in half, and what would you not do?**
Measure the split first: input vs output vs cache reads vs cache writes per route. Then reorder
prompts for cache stability and stop resending tool results that are never read — in agent loops those
two dominate, because the transcript is resent every turn. I would not start with a cheaper model:
it needs a routing eval, risks accuracy and retries, and often moves cost rather than removing it
(§5).

**Q5. A classifier used as an input guardrail goes down. What should happen?**
It depends on the path, and the choice must be pre-made and tested. Tool-enabled or write-capable
paths fail closed, because a bypass becomes an action. Read-only Q&A fails open with a shadow log and
an alert. Tool authorization always fails closed — it is authz, not a heuristic. PII on output fails
closed, because a leak is irreversible (§6.3).

**Q6. Design the order in which a RAG service degrades under a capacity event.**
A monotone, visible ladder: exact cache, semantic cache, trim context (fewer chunks, no rerank, no
history), smaller model, optional rails to shadow, non-agentic single shot, enqueue as background,
abstain with a reason. Each step recorded in `envelope.degraded`, surfaced in the response, and
covered by an eval slice so the quality cost of each rung is known before the incident, not after
(§3.4).

**Q7. When is an AI gateway the wrong first investment?**
One app, one provider, one team. It cannot see inside your agent loop, which is where runaway cost
lives; it adds an operational dependency; and the controls that would actually have saved you — the
envelope, 429 classification, cache layout, a usage table — all live in the application. A gateway
pays for itself at the second provider, the third team sharing an allocation, or the first audit
question (§8.4).

**Q8. You aggregate four MCP servers, 180 tools total, behind one endpoint. What breaks?**
Context first: definitions are input tokens on every turn, so you added a five-digit per-request tax,
and selection accuracy falls as the toolset grows. Use the gateway's tool filtering plus deferred
loading, keeping 3–5 hot tools resident, and note that deferring changes what enters context, not
what you transmit. Security second: one endpoint means one place that must enforce per-principal
authz, and the filtered list must derive from the same registry and principal as authz, or the two
drift (§7.2).

**Q9. How do you know a caching change worked?**
`cache_read_tokens / (cache_read_tokens + cache_creation_tokens)` per route and per day, plus cost per
request on the same slice. Rising reads with flat writes is working; both rising together is
thrashing — usually a volatile token early in the prefix. Without the two fields split, "we enabled
caching" is unfalsifiable (§5.6).

**Q10. What is the minimum control set you would ship on day one of a small product?**
A provider hard spend limit, an envelope with deadline/token/tool-call/depth caps on one shared
ledger, a 429-classifying retry wrapper, one concurrency semaphore per model, cache-aware prompt
layout, tool authz and output PII fail-closed with an injection rail in shadow, one usage table with
six fields, and a written three-rung degradation ladder. Roughly a week, and it prevents every
failure in §10's top half (§9.1).

---

## 14. Sources

Provider documentation, read October 2026:

- Anthropic — rate limits and response headers, service tiers, prompt caching (multipliers, TTLs),
  Message Batches, context editing (`clear_tool_uses_20250919`), tool search and `defer_loading`,
  programmatic tool calling — <https://docs.claude.com/>.
- OpenAI — rate limits and headers, usage tiers, spend alerts vs hard spend limits, flex processing
  (`429 Resource Unavailable`, Batch-rate pricing) — <https://platform.openai.com/docs/>.
- Google — Gemini context caching (implicit caching, per-model minimums, `total_cached_tokens`) —
  <https://ai.google.dev/gemini-api/docs/caching>.

Guardrail products:

- Amazon Bedrock Guardrails, including `ApplyGuardrail` and input tagging —
  <https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html>.
- Azure AI Content Safety, Prompt Shields and Spotlighting —
  <https://learn.microsoft.com/en-us/azure/ai-services/content-safety/concepts/jailbreak-detection>.
- Google Model Armor: templates, confidence levels, floor settings, enforcement types —
  <https://cloud.google.com/security-command-center/docs/model-armor-overview>.
- Meta, Llama Guard 4 model card (12B, multimodal, MLCommons taxonomy S1–S14) —
  <https://huggingface.co/meta-llama/Llama-Guard-4-12B>.
- NVIDIA NeMo Guardrails (rail types, Colang) — <https://docs.nvidia.com/nemo/guardrails/>.
- Guardrails AI — <https://www.guardrailsai.com/docs>.

Gateways:

- Agent Router (formerly Envoy AI Gateway), usage-based rate limiting and MCP gateway capabilities,
  and the Agentic AI Foundation announcement — <https://aigateway.envoyproxy.io/docs/>.
- agentgateway — <https://github.com/agentgateway/agentgateway>.
- IBM ContextForge (`mcp-context-forge`) — <https://github.com/IBM/mcp-context-forge>.
- Docker MCP Gateway — <https://docs.docker.com/ai/mcp-gateway/>.
- LiteLLM proxy guardrails and event hooks — <https://docs.litellm.ai/docs/proxy/guardrails/quick_start>.

In this repo: `17` (guardrails and injection), `22` §4.2 and §12–§13 (loop caps, platform concerns),
`23` §6–§7 and §11 (quotas, cost, caching), `24` §5–§12 (registry, authz, idempotency, per-tool
limits), `06` §12 and §14 (prompt caching, cost arithmetic), `30` §2–§5 and §10 (tool tax, code mode,
cache-aware loops), `26`–`29` (MCP, A2A, agentic security, agent identity),
`../sre-observability/26-llm-and-ai-observability.md` (spans and cost telemetry),
`../sre-observability/31-finops-for-observability.md` (the attribution pattern).
